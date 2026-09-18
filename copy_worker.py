import collections
import concurrent.futures
import errno
import os
import queue
import re
import shutil
import stat
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Callable

from PyQt6.QtCore import QThread, pyqtSignal

from drive_utils import is_smb, is_ssh, build_rsync_cmd, _SSH_HOST_RE
from pre_post_hooks import run_hooks as _run_hooks
from state import logger
from translations import tr

from copy_worker_core import (
    _CHUNK, _IO_BUF, _WORKERS, _FLUSH_THRESH, _FLUSH_INTERVAL, _SCAN_EMIT_SECS,
    _SCAN_PIPE_BATCH, _LOCAL_BATCH, _PIPE_MAXSIZE, _ENTRY_EMIT_SECS,
    _SMB_WORKERS, _PID, _EUID, _O_NOATIME, _seen_dirs_lock, _seen_dirs_global, _TIME_CHECK_EVERY,
    _smb_procs, _smb_procs_lock, _RSYNC_DELETE_RE, _SKIP_GLOBS,
    _scale_params, _scan_dir_entries,
    _ensure_dir, _parse_smb, _run_futures, _silent_unlink
)
from copy_worker_smb import (
    _get_smb_credentials,
    _SmbJob, _SmbClient, _SmbScanner, _ShareProcessor
)

if TYPE_CHECKING:
    from copy_worker_smb import _SecurePw


def _ssh_join(dst_spec: str, rel_path: str) -> str:
    base = dst_spec.rstrip("/")
    rel = rel_path.lstrip("/")
    return f"{base}/{rel}" if rel else base


_RSYNC_ERROR_TAIL = 5
_NF_MARK = "\x1f"


def _not_found_reason(title: str) -> str:
    base = tr("Path does not exist — skipping")
    return f"{base}{_NF_MARK}{title}" if title else base


_RSYNC_GLOB_CHARS = re.compile(r"([\\*?\[])")


def _rsync_escape_component(name: str) -> str:
    return _RSYNC_GLOB_CHARS.sub(r"\\\1", name)


_LIKELY_FILE_TAIL_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _rsync_src_arg(src: str) -> str:
    if src.endswith("/"):
        return src
    if is_ssh(src):
        tail = src.rstrip("/").rsplit("/", 1)[-1]
        if not _LIKELY_FILE_TAIL_RE.search(tail):
            return f"{src}/"
        return src
    return f"{src}/" if os.path.isdir(src) else src


_SSH_URL_RE = re.compile(r"^(ssh://(?:[^@/]+@)?[^/]+)(/.*)?$")


def _ssh_split(spec: str) -> tuple[str, str]:
    m = _SSH_URL_RE.match(spec)
    if m:
        return m.group(1), (m.group(2) or "")
    m = _SSH_HOST_RE.match(spec)
    if m:
        path = m.group(2)
        return spec[:len(spec) - len(path)], path
    return spec, ""


def _relativize_excludes(raw_excludes: "list | None", base_dir: str, *,
                         split_fn=None) -> list[str]:
    patterns: list[str] = []
    if not raw_excludes:
        return patterns
    for ex in raw_excludes:
        ex_path = split_fn(ex)[1] if split_fn else ex
        if not ex_path:
            continue
        try:
            rel = os.path.relpath(ex_path, base_dir)
        except ValueError:
            continue
        if rel == os.curdir or rel.startswith(os.pardir):
            continue
        escaped = "/".join(_rsync_escape_component(p) for p in rel.split(os.sep))
        patterns.append("/" + escaped)
    return patterns


def _rsync_excludes(src: str, raw_excludes: "list | None") -> "list | None":
    skip = list(_SKIP_GLOBS)

    if is_ssh(src):
        _prefix, remote_root = _ssh_split(src)
        base_dir = (remote_root.rstrip("/") if remote_root.endswith("/")
                   else os.path.dirname(remote_root.rstrip("/") or remote_root))
        patterns = _relativize_excludes(raw_excludes, base_dir, split_fn=_ssh_split)
        return patterns + skip

    base_dir = src.rstrip("/") if src.endswith("/") else os.path.dirname(src.rstrip("/") or src)
    patterns = _relativize_excludes(raw_excludes, base_dir)
    return patterns + skip


def _bump_count(tc: dict, title: str, idx: int) -> None:
    counts = tc.get(title)
    if counts is None:
        counts = tc[title] = [0, 0, 0, 0]
    counts[idx] += 1


def _do_copy(entry, cancel: threading.Event, ok_l: list, sk_l: list, er_l: list, tc: dict) -> None:

    src, dst, title, st = entry
    try:
        status, aux, sz = _copy_file(src, dst, cancel, st)
    except Exception as exc:
        logger.error("copy %s: %s", src, exc)
        status, aux, sz = "error", str(exc), 0
    if status == "ok":
        ok_l.append((src, dst, sz))
        if title: _bump_count(tc, title, 0)
    elif status == "skip":
        sk_l.append((src, aux or tr("Up to date"), sz))
        if title: _bump_count(tc, title, 1)
    else:
        er_l.append((src, aux, 0))
        if title: _bump_count(tc, title, 2)


def _is_up_to_date_local(dst: str, src_st: "os.stat_result",
                          dst_st: "os.stat_result | None" = None) -> bool:

    try:
        d = dst_st if dst_st is not None else os.lstat(dst)
        return (stat.S_ISREG(d.st_mode)
                and d.st_size == src_st.st_size
                and abs(d.st_mtime_ns - src_st.st_mtime_ns) <= 2_000_000_000)
    except OSError:
        return False


def _is_symlink_up_to_date(dst: str, target: str) -> bool:
    try:
        return os.path.islink(dst) and os.readlink(dst) == target
    except OSError:
        return False


def _lstat_or_none(path: str) -> "os.stat_result | None":
    try:
        return os.lstat(path)
    except OSError:
        return None


def _clear_conflicting_dir(dst: str) -> bool:
    try:
        shutil.rmtree(dst)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        if os.path.isdir(dst) and not os.path.islink(dst):
            logger.error("could not remove conflicting directory %s: %s", dst, exc)
            return False
        return True


def _copy_symlink(src: str, dst: str) -> tuple:
    try:
        target = os.readlink(src)
    except OSError as exc:
        return "error", tr("Symlink unreadable: {err}", err=exc), 0

    dst_st = _lstat_or_none(dst)

    if dst_st is not None and stat.S_ISLNK(dst_st.st_mode):
        try:
            if os.readlink(dst) == target:
                return "skip", tr("Up to date"), 0
        except OSError:
            pass

    if not _ensure_dir(os.path.dirname(dst)):
        return "error", tr("Directory could not be created"), 0

    if (dst_st is not None and stat.S_ISDIR(dst_st.st_mode)
            and not _clear_conflicting_dir(dst)):
        return "error", tr("Destination is a non-empty directory and could not be replaced"), 0

    tmp = f"{dst}.{_PID}.{threading.get_ident()}.lnk.part"
    try:
        _silent_unlink(tmp)
        os.symlink(target, tmp)
        os.replace(tmp, dst)
        _fsync_parent_dir(dst)
        return "ok", dst, 0
    except OSError as exc:
        _silent_unlink(tmp)
        logger.error("symlink %s → %s: %s", src, dst, exc)
        return "error", str(exc), 0


def _copy_loop(rfd: int, wfd: int, total: int, cancel: threading.Event) -> "tuple[int, OSError | None]":
    rem = total
    last_err: OSError | None = None
    try:
        while rem > 0:
            if cancel.is_set():
                raise InterruptedError
            n = os.copy_file_range(rfd, wfd, min(rem, _CHUNK))
            if n == 0:
                break
            rem -= n
    except InterruptedError:
        raise
    except OSError as exc:
        if exc.errno not in (errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.EINVAL):
            raise
        logger.debug("copy_file_range not supported, falling back: %s", exc)
        try:
            os.lseek(wfd, 0, os.SEEK_SET)
        except OSError:
            pass
        try:
            os.ftruncate(wfd, 0)
        except OSError as _e:
            logger.debug("ftruncate fallback failed for wfd: %s", _e)
        try:
            os.lseek(rfd, 0, os.SEEK_SET)
        except OSError:
            pass
        rem = total
    if rem > 0:
        offset = total - rem
        try:
            while rem > 0:
                if cancel.is_set():
                    raise InterruptedError
                n = os.sendfile(wfd, rfd, offset, min(rem, _CHUNK))
                if n == 0:
                    break
                rem -= n
                offset += n
        except InterruptedError:
            raise
        except OSError as exc:
            logger.debug("sendfile fallback failed or not supported: %s", exc)
            last_err = exc
            if exc.errno in _FATAL_IO_ERRNOS:
                return total - rem, last_err
    if rem > 0:
        try:
            seek_to = total - rem
            os.lseek(rfd, seek_to, os.SEEK_SET)
            os.lseek(wfd, seek_to, os.SEEK_SET)
            while rem > 0:
                if cancel.is_set():
                    raise InterruptedError
                buf = os.read(rfd, min(rem, _IO_BUF))
                if not buf:
                    break
                written = 0
                while written < len(buf):
                    n = os.write(wfd, buf[written:])
                    if n == 0:
                        raise OSError("os.write returned 0")
                    written += n
                rem -= len(buf)
        except InterruptedError:
            raise
        except OSError as exc:
            logger.warning("read/write fallback failed after %d/%d bytes: %s", total - rem, total, exc)
            last_err = exc
    return total - rem, last_err


_FATAL_IO_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS})


class _IncompleteCopyError(OSError):
    pass


_fsynced_dirs_lock = threading.Lock()
_fsynced_dirs: set[str] = set()


def _fsync_parent_dir(path: str) -> None:
    d = os.path.dirname(path) or "."
    with _fsynced_dirs_lock:
        if d in _fsynced_dirs:
            return
        _fsynced_dirs.add(d)
    try:
        dfd = os.open(d, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as e:
        logger.debug("fsync parent dir %s failed: %s", d, e)


def _copy_file(src, dst, cancel, cached_st=None):
    if cached_st is True:
        return _copy_symlink(src, dst)
    if cached_st is None and os.path.islink(src):
        return _copy_symlink(src, dst)

    for _attempt in range(2):
        tmp = f"{dst}.{_PID}.{threading.get_ident()}.part"
        rfd = wfd = None
        success = False
        try:
            if cancel.is_set():
                return "skip", tr("Cancelled"), 0
            if cached_st is not None and _attempt == 0:
                st = cached_st
            else:
                try:
                    st = os.stat(src)
                except OSError:
                    return "error", tr("Source unreadable"), 0

            dst_st = _lstat_or_none(dst)

            if _attempt == 0 and dst_st is not None and _is_up_to_date_local(dst, st, dst_st):
                return "skip", tr("Up to date"), st.st_size

            if not _ensure_dir(os.path.dirname(dst)):
                return "error", tr("Directory could not be created"), 0

            if (dst_st is not None and stat.S_ISDIR(dst_st.st_mode)
                    and not _clear_conflicting_dir(dst)):
                return "error", tr("Destination is a non-empty directory and could not be replaced"), 0

            _may_use_noatime = _EUID == 0 or st.st_uid == _EUID
            try:
                rfd = os.open(src, os.O_RDONLY | (_O_NOATIME if _may_use_noatime else 0))
            except OSError:
                rfd = os.open(src, os.O_RDONLY)

            wfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, st.st_mode & 0o777)

            try:
                os.fchmod(wfd, st.st_mode & 0o777)
            except OSError as e:
                logger.debug("Could not fchmod %s: %s", tmp, e)

            if st.st_size > 0:
                try:
                    os.ftruncate(wfd, st.st_size)
                except OSError:
                    pass
                try:
                    os.posix_fadvise(rfd, 0, st.st_size, os.POSIX_FADV_SEQUENTIAL)
                    os.posix_fadvise(wfd, 0, st.st_size, os.POSIX_FADV_SEQUENTIAL)
                except OSError:
                    pass

            copied, copy_err = _copy_loop(rfd, wfd, st.st_size, cancel)
            if copied < st.st_size:
                if copy_err is not None and copy_err.errno in _FATAL_IO_ERRNOS:
                    raise copy_err
                reason = f": {copy_err}" if copy_err is not None else ""
                raise _IncompleteCopyError(
                    f"Incomplete copy: {copied}/{st.st_size} bytes written{reason}"
                )

            try:
                os.fsync(wfd)
            except OSError as e:
                logger.debug("fsync failed for %s: %s", tmp, e)

            try:
                os.close(wfd)
            except OSError:
                pass
            wfd = None

            try:
                os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
            except OSError as e:
                logger.debug("Could not preserve timestamps for %s: %s", tmp, e)

            os.replace(tmp, dst)
            success = True
            _fsync_parent_dir(dst)
            return "ok", dst, copied

        except InterruptedError:
            return "skip", tr("Cancelled"), 0
        except _IncompleteCopyError as exc:
            if _attempt == 0:
                logger.debug("copy %s: %s — retrying", src, exc)
                continue
            logger.error("copy %s → %s: %s", src, dst, exc)
            return "error", str(exc), 0
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                logger.error("copy %s → %s: destination out of space", src, dst)
                return "error", tr("No space left on destination device"), 0
            if exc.errno == errno.EDQUOT:
                logger.error("copy %s → %s: disk quota exceeded", src, dst)
                return "error", tr("Disk quota exceeded on destination device"), 0
            if exc.errno == errno.EROFS:
                logger.error("copy %s → %s: destination is read-only", src, dst)
                return "error", tr("Destination filesystem is read-only"), 0
            if exc.errno == errno.EIO:
                logger.error("copy %s → %s: I/O error", src, dst)
                return "error", tr("I/O error — check the source or destination device"), 0
            logger.error("copy %s → %s: %s", src, dst, exc)
            return "error", str(exc), 0
        finally:
            if rfd is not None:
                try:
                    os.close(rfd)
                except OSError:
                    pass
            if wfd is not None:
                try:
                    os.close(wfd)
                except OSError:
                    pass
            if not success:
                _silent_unlink(tmp)
    return "error", tr("Copy failed after retries"), 0


class _EntryTracker:

    __slots__ = ("_counts", "_emitted", "_last_emit", "_lock", "_signal")

    def __init__(self, signal=None) -> None:
        self._lock   = threading.Lock()
        self._signal = signal
        self._counts:  dict[str, list[int]] = {}
        self._emitted: dict[str, list[int]] = {}
        self._last_emit = time.monotonic()

    def batch_update(self, counts: dict) -> None:
        if not counts:
            return
        with self._lock:
            for title, vals in counts.items():
                if not title:
                    continue
                c = self._counts.get(title)
                if c is None:
                    c = self._counts[title] = [0, 0, 0, 0]
                c[0] += vals[0]
                c[1] += vals[1]
                c[2] += vals[2]
                c[3] += vals[3] if len(vals) > 3 else 0
        self.emit_deltas()

    def emit_deltas(self, *, force: bool = False) -> None:
        signal = self._signal
        if signal is None:
            return
        now = time.monotonic()
        with self._lock:
            if not force and (now - self._last_emit) < _ENTRY_EMIT_SECS:
                return
            self._last_emit = now
            pending: list[tuple[str, list[int]]] = []
            for title, cur in self._counts.items():
                prev = self._emitted.get(title)
                if prev is None:
                    prev = self._emitted[title] = [0, 0, 0, 0]
                delta = [cur[i] - prev[i] for i in range(4)]
                if delta[0] or delta[1] or delta[2] or delta[3]:
                    prev[:] = cur
                    pending.append((title, delta))
        for title, d in pending:
            signal.emit(title, d[0], d[1], d[2], d[3])

    def emit_final(self) -> None:
        self.emit_deltas(force=True)


class _Flusher:
    __slots__ = ("_de", "_er", "_flush_thresh", "_last_flush_t", "_lock", "_ok", "_signal", "_sk", "_total", "copied", "deleted", "done", "errors", "skipped")

    def __init__(self, signal, total: int, flush_thresh: int = _FLUSH_THRESH) -> None:
        self._signal       = signal
        self._total        = total
        self._flush_thresh = flush_thresh
        self._lock         = threading.Lock()
        self._ok: list     = []
        self._sk: list     = []
        self._er: list     = []
        self._de: list     = []
        self.done = self.copied = self.skipped = self.errors = self.deleted = 0
        self._last_flush_t = time.monotonic()

    def set_total(self, total: int) -> None:
        with self._lock: self._total = total

    def set_flush_thresh(self, thresh: int) -> None:
        with self._lock: self._flush_thresh = thresh

    def push(self, ok=(), sk=(), er=(), de=(), *, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if ok: self._ok.extend(ok)
            if sk: self._sk.extend(sk)
            if er: self._er.extend(er)
            if de: self._de.extend(de)
            n = len(self._ok) + len(self._sk) + len(self._er) + len(self._de)
            if n == 0:
                return
            timed_out = (now - self._last_flush_t) >= _FLUSH_INTERVAL
            if not force and n < self._flush_thresh and not timed_out:
                return
            payload_ok, self._ok = self._ok, []
            payload_sk, self._sk = self._sk, []
            payload_er, self._er = self._er, []
            payload_de, self._de = self._de, []
            self.done    += len(payload_ok) + len(payload_sk) + len(payload_er)
            self.copied  += len(payload_ok)
            self.skipped += len(payload_sk)
            self.errors  += len(payload_er)
            self.deleted += len(payload_de)
            done_snap  = self.done
            total_snap = self._total
            self._last_flush_t = now
        self._signal.emit(payload_ok, payload_sk, payload_er, payload_de, done_snap, total_snap)

    def flush(self) -> None: self.push(force=True)


def _push_errors(flusher: "_Flusher", tracker: "_EntryTracker",
                 errors: "list[tuple[str, str, str]]") -> None:

    if not errors:
        return
    flusher.push(er=[(src, msg, 0) for src, msg, _t in errors], force=True)
    counts: dict = {}
    for _src, _msg, title in errors:
        if title:
            counts.setdefault(title, [0, 0, 0, 0])[2] += 1
    if counts:
        tracker.batch_update(counts)


class _BatchBuffer:
    __slots__ = ("_flusher", "_tracker", "er", "ok", "pending", "sk", "tc")

    def __init__(self, flusher: "_Flusher", tracker: "_EntryTracker") -> None:
        self._flusher = flusher
        self._tracker = tracker
        self.ok: list = []
        self.sk: list = []
        self.er: list = []
        self.tc: dict = {}
        self.pending = 0

    def record(self, entry, cancel: threading.Event) -> None:
        _do_copy(entry, cancel, self.ok, self.sk, self.er, self.tc)
        self.pending += 1

    def flush(self) -> None:
        if self.ok or self.sk or self.er:
            self._flusher.push(ok=self.ok, sk=self.sk, er=self.er)
            self.ok.clear()
            self.sk.clear()
            self.er.clear()
        if self.tc:
            self._tracker.batch_update(self.tc)
            self.tc.clear()
        self.pending = 0


class CopyWorker(QThread):
    batch_update = pyqtSignal(list, list, list, list, int, int)
    finished_work = pyqtSignal(int, int, int, int, bool)
    scan_progress = pyqtSignal(str, int)
    entry_status = pyqtSignal(str, int, int, int, int)
    scan_finished = pyqtSignal(int)
    _RSYNC_PROGRESS_RE = re.compile(
        r"^\s*([\d,]+)\s+(\d+)%\s+([\d.]+\w+/s)\s+([\d:]+)"
    )

    def __init__(self, tasks) -> None:
        super().__init__()
        self._hooks: dict[str, tuple[list, list]] = self._extract_hooks(tasks)
        self._mirror_titles: set[str] = self._extract_mirror_flags(tasks)
        self.tasks = self._normalize_tasks(tasks)
        self._cancel = threading.Event()
        self._pre_fired_titles: set[str] = set()
        self._post_fired_titles: set[str] = set()

    @staticmethod
    def _extract_mirror_flags(tasks) -> set[str]:
        result: set[str] = set()
        for t in tasks:
            if not isinstance(t, (list, tuple)) or len(t) < 7:
                continue
            title = str(t[2]) if len(t) > 2 else ""
            if title and t[6]:
                result.add(title)
        return result

    @staticmethod
    def _extract_hooks(tasks) -> dict[str, tuple[list, list]]:
        result: dict[str, tuple[list, list]] = {}
        for t in tasks:
            if not isinstance(t, (list, tuple)) or len(t) < 3:
                continue
            title = str(t[2]) if len(t) > 2 else ""
            if not title or title in result:
                continue
            pre = list(t[4]) if len(t) > 4 and isinstance(t[4], list) else []
            post = list(t[5]) if len(t) > 5 and isinstance(t[5], list) else []
            if pre or post:
                result[title] = (pre, post)
        return result

    @staticmethod
    def _normalize_tasks(tasks) -> list[tuple[str, str, str, frozenset]]:
        result = []
        for t in tasks:
            if not isinstance(t, (list, tuple)) or len(t) < 2:
                continue
            src_raw, dst_raw = t[0], t[1]
            title = str(t[2]) if len(t) > 2 else ""
            raw_excl = t[3] if len(t) > 3 else {}
            srcs = [src_raw] if isinstance(src_raw, str) else src_raw
            dsts = [dst_raw] if isinstance(dst_raw, str) else dst_raw
            if not srcs or not dsts or len(srcs) != len(dsts):
                continue
            for s, d in zip(srcs, dsts, strict=True):
                if s and d:
                    s_str, d_str = str(s), str(d)
                    s_norm = s_str if (is_smb(s_str) or is_ssh(s_str)) else os.path.abspath(os.path.expanduser(s_str))
                    d_norm = d_str if (is_smb(d_str) or is_ssh(d_str)) else os.path.abspath(os.path.expanduser(d_str))
                    if isinstance(raw_excl, (set, frozenset)):
                        exc_set: frozenset = frozenset(raw_excl)
                    elif isinstance(raw_excl, dict):
                        names: list = raw_excl.get(s_norm) or raw_excl.get(s_str) or []
                        exc_set = frozenset(os.path.join(s_norm, n) for n in names)
                    else:
                        exc_set = frozenset()
                    result.append((s_norm, d_norm, title, exc_set))
        return result

    def cancel(self) -> None:
        self._cancel.set()
        with _smb_procs_lock:
            for proc in list(_smb_procs.values()):
                try:
                    proc.kill()
                except OSError:
                    pass

    @staticmethod
    def _fire_hooks(entry_title: str, phase: str, hooks: list[str], abort: bool) -> bool:
        if not hooks:
            return True
        ok, errors = _run_hooks(hooks, abort_on_error=abort, label=f"{entry_title}/{phase}")
        if errors:
            for err in errors:
                logger.error("Hook error [%s]: %s", entry_title, err)
        return ok or not abort

    def _run_pre_hooks(self, tasks: list) -> set[str]:
        skip_titles: set[str] = set()
        seen: set[str] = set()
        for _s, _d, _t, _exc in tasks:
            if _t and _t not in seen:
                seen.add(_t)
                _pre, _ = self._hooks.get(_t, ([], []))
                if _pre:
                    self._pre_fired_titles.add(_t)
                    if not self._fire_hooks(_t, "pre", _pre, abort=True):
                        skip_titles.add(_t)
        return skip_titles

    def _run_post_hooks(self, tasks: list) -> None:
        seen: set[str] = set()
        for _s, _d, _t, _exc in tasks:
            if _t and _t not in seen:
                seen.add(_t)
                self._run_post_hooks_for_title(_t)

    def _run_post_hooks_for_title(self, title: str) -> None:
        if not title or title in self._post_fired_titles:
            return
        self._post_fired_titles.add(title)
        _, _post = self._hooks.get(title, ([], []))
        if _post:
            self._fire_hooks(title, "post", _post, abort=False)

    def _run_pending_post_hooks(self) -> None:
        for title in sorted(self._pre_fired_titles - self._post_fired_titles):
            self._run_post_hooks_for_title(title)

    def run(self) -> None:
        with _seen_dirs_lock:
            _seen_dirs_global.clear()
        with _fsynced_dirs_lock:
            _fsynced_dirs.clear()
        try:
            self._run_impl()
        finally:
            self._run_pending_post_hooks()

    def _run_impl(self) -> None:
        pw: "_SecurePw | None" = None
        flusher = _Flusher(self.batch_update, 0)
        tracker = _EntryTracker(self.entry_status)
        try:
            smb_tasks, ssh_tasks, local_tasks = [], [], []
            for s, d, t, exc in self.tasks:
                if is_smb(s) or is_smb(d):
                    smb_tasks.append((s, d, t, exc))
                elif is_ssh(s) or is_ssh(d):
                    ssh_tasks.append((s, d, t, exc))
                else:
                    local_tasks.append((s, d, t, exc))

            user = ""
            smb_tool_missing = False
            if smb_tasks:
                user, pw = _get_smb_credentials()
                smb_tool_missing = shutil.which("smbclient") is None
                if smb_tool_missing:
                    logger.error("smbclient binary not found — SMB task(s) will be reported as errors")

            self.scan_progress.emit(tr("Scanning"), 0)

            all_tasks = local_tasks + ssh_tasks + smb_tasks
            skip_titles = self._run_pre_hooks(all_tasks) if not self._cancel.is_set() else set()
            if skip_titles:
                local_tasks = [x for x in local_tasks if x[2] not in skip_titles]
                ssh_tasks   = [x for x in ssh_tasks   if x[2] not in skip_titles]
                smb_tasks   = [x for x in smb_tasks   if x[2] not in skip_titles]
                all_tasks   = local_tasks + ssh_tasks + smb_tasks

            if not all_tasks:
                self.scan_finished.emit(0)
                self.finished_work.emit(0, 0, 0, 0, self._cancel.is_set())
                return

            local_found  = [0]
            smb_found    = [0]
            ssh_units    = len(ssh_tasks)
            total_lock   = threading.Lock()
            scan_pending = [(1 if local_tasks else 0) + (1 if smb_tasks else 0) + (1 if ssh_tasks else 0)]
            scan_emitted = [False]

            def _apply_total(_total: int) -> None:
                flusher.set_total(_total)
                _, ft_new, _ = _scale_params(_total)
                flusher.set_flush_thresh(ft_new)

            def _update_total() -> None:
                with total_lock:
                    _total = local_found[0] + smb_found[0] + ssh_units
                _apply_total(_total)

            def _on_local_count(n: int) -> None:
                with total_lock:
                    local_found[0] = n
                _update_total()

            def _on_smb_progress(n: int) -> None:
                self.scan_progress.emit(tr("Scanning SMB"), n)
                with total_lock:
                    smb_found[0] = n
                _update_total()

            def _scan_phase_done() -> None:
                with total_lock:
                    scan_pending[0] -= 1
                    ready = scan_pending[0] <= 0 and not scan_emitted[0]
                    if ready:
                        scan_emitted[0] = True
                    _total = local_found[0] + smb_found[0] + ssh_units
                if ready:
                    _apply_total(_total)
                    if not self._cancel.is_set():
                        self.scan_finished.emit(_total)

            def _phase_marker() -> "Callable[[], None]":
                fired = [False]

                def _mark() -> None:
                    if fired[0]:
                        return
                    fired[0] = True
                    _scan_phase_done()

                return _mark

            _update_total()

            local_mark = _phase_marker()
            smb_mark = _phase_marker()

            def _local_pipeline() -> None:
                try:
                    if not self._cancel.is_set():
                        self._scan_copy_local_pipelined(
                            local_tasks, flusher, tracker,
                            on_count_change=_on_local_count,
                            on_scan_done=local_mark,
                        )
                finally:
                    local_mark()

            def _ssh_pipeline() -> None:
                _scan_phase_done()
                if not self._cancel.is_set():
                    self._copy_ssh_tasks(ssh_tasks, flusher, tracker)

            def _smb_pipeline() -> None:
                try:
                    self._run_smb_pipeline(
                        smb_tasks, user, pw, smb_tool_missing,
                        flusher, tracker, _on_smb_progress,
                        smb_found, total_lock, _update_total, smb_mark,
                    )
                finally:
                    smb_mark()

            pipelines = []
            if local_tasks:
                pipelines.append(_local_pipeline)
            if smb_tasks:
                pipelines.append(_smb_pipeline)
            if ssh_tasks:
                pipelines.append(_ssh_pipeline)

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(pipelines)) as pool:
                futs = [pool.submit(fn) for fn in pipelines]
                _run_futures(futs, self._cancel, "backend pipeline")

            if not scan_emitted[0] and not self._cancel.is_set():
                with total_lock:
                    total = local_found[0] + smb_found[0] + ssh_units
                _apply_total(total)
                self.scan_finished.emit(total)

            self._run_post_hooks(all_tasks)
            flusher.flush()
            tracker.emit_final()
            self.finished_work.emit(flusher.copied, flusher.skipped, flusher.errors,
                                    flusher.deleted, self._cancel.is_set())
        except Exception as exc:
            logger.error("CopyWorker critical: %s", exc, exc_info=True)
            try:
                flusher.flush()
                tracker.emit_final()
            except Exception as e:
                logger.debug("Error during fallback cleanup: %s", e)
            self.finished_work.emit(flusher.copied, flusher.skipped, flusher.errors,
                                    flusher.deleted, False)
        finally:
            if pw is not None:
                pw.clear()

    def _run_smb_pipeline(self, smb_tasks: list, user: str, pw: "_SecurePw | None",
                          smb_tool_missing: bool, flusher: "_Flusher", tracker: "_EntryTracker",
                          on_progress, smb_found: list, total_lock: threading.Lock,
                          update_total, scan_phase_done) -> None:
        if self._cancel.is_set():
            scan_phase_done()
            return

        if smb_tool_missing:
            msg = tr("'smbclient' not found — install the Samba client tools (e.g. package "
                     "'smbclient' / 'samba-client') to enable SMB backups")
            errors = [(s_ if is_smb(s_) else d_, msg, t_) for s_, d_, t_, *_ in smb_tasks]
            with total_lock:
                smb_found[0] = len(errors)
            update_total()
            scan_phase_done()
            _push_errors(flusher, tracker, errors)
            return

        unreachable, auth_failed, guest = self._probe_shares(smb_tasks, user, pw)
        dead = unreachable | auth_failed
        alive_tasks = smb_tasks
        smb_errors: list[tuple[str, str, str]] = []
        if dead:
            alive_tasks, pre_err = self._filter_dead_tasks(smb_tasks, dead, unreachable)
            smb_errors.extend(pre_err)

        expanded: list[_SmbJob] = []
        scan_errors: list = []
        if alive_tasks and not self._cancel.is_set():
            scanner = _SmbScanner(user, pw, guest, self._cancel, on_progress)
            expanded, scan_errors = scanner.resolve(alive_tasks)
        smb_errors.extend(scan_errors)

        with total_lock:
            smb_found[0] = len(expanded) + len(smb_errors)
        update_total()
        scan_phase_done()

        if self._cancel.is_set():
            return

        self._copy_smb_all(expanded, smb_errors, user, pw, guest, flusher, tracker)

    def _copy_one_ssh_task(
            self,
            src: str, dst: str, title: str, extra: tuple,
            flusher: "_Flusher",
            tracker: "_EntryTracker",
    ) -> None:
        def _track(ok: int, skip: int, err: int, deleted: int = 0) -> None:
            if title:
                tracker.batch_update({title: (ok, skip, err, deleted)})

        if self._cancel.is_set():
            return

        self.scan_progress.emit(f"rsync  {title or src}", 0)

        rsync_src = _rsync_src_arg(src)
        excludes_raw = list(extra[0]) if extra and extra[0] else None
        excludes = _rsync_excludes(rsync_src, excludes_raw)
        mirror = title in self._mirror_titles

        cmd = build_rsync_cmd(rsync_src, dst, exclude=excludes, delete=mirror)
        logger.debug("_copy_ssh_tasks: %s", " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            logger.error("rsync launch failed for '%s': %s", src, exc)
            flusher.push(er=[(src, str(exc), 0)])
            _track(0, 0, 1)
            return

        if proc.stdout is None:
            logger.error("rsync stdout is None for '%s'", src)
            proc.kill()
            proc.wait()
            flusher.push(er=[(src, tr("rsync stdout unavailable"), 0)])
            _track(0, 0, 1)
            return

        last_pct = 0
        deleted_this_task: list = []
        error_lines: "collections.deque[str]" = collections.deque(maxlen=_RSYNC_ERROR_TAIL)
        _tid = threading.get_ident()
        with _smb_procs_lock:
            _smb_procs[_tid] = proc
        if self._cancel.is_set():
            try:
                proc.kill()
            except OSError:
                pass
        try:
            try:
                for raw_line in proc.stdout:
                    if self._cancel.is_set():
                        proc.kill()
                        break
                    line = raw_line.strip()
                    if not line:
                        continue
                    m = self._RSYNC_PROGRESS_RE.match(line)
                    if m:
                        pct = int(m.group(2))
                        if pct != last_pct:
                            last_pct = pct
                            self.scan_progress.emit(
                                f"rsync  {title or src}  {pct}%  {m.group(3)}",
                                pct,
                            )
                        continue
                    if mirror and (dm := _RSYNC_DELETE_RE.match(line)):
                        rel = dm.group(1).strip()
                        deleted_this_task.append((_ssh_join(dst, rel), tr("Mirror delete (remote)"), 0))
                    else:
                        logger.debug("rsync: %s", line)
                        error_lines.append(line)

            except OSError as exc:
                logger.warning("rsync read error for '%s': %s", src, exc)

            proc.wait()
        finally:
            with _smb_procs_lock:
                _smb_procs.pop(_tid, None)

        if deleted_this_task:
            flusher.push(de=deleted_this_task, force=True)
        if self._cancel.is_set():
            if deleted_this_task:
                _track(0, 0, 0, len(deleted_this_task))
            return

        if proc.returncode == 0:
            flusher.push(ok=[(src, dst, 0)])
            _track(1, 0, 0, len(deleted_this_task))
            logger.info("rsync OK: %s → %s", src, dst)
        else:
            detail = "; ".join(error_lines) if error_lines else None
            msg = (tr("rsync exit {code}: {detail}", code=proc.returncode, detail=detail)
                   if detail else tr("rsync exit {code}", code=proc.returncode))
            flusher.push(er=[(src, msg, 0)])
            _track(0, 0, 1, len(deleted_this_task))
            logger.error("rsync exit %d: %s → %s (%s)", proc.returncode, src, dst, detail or "no output captured")

    def _copy_ssh_tasks(
            self,
            tasks: list[tuple[str, str, str, frozenset]],
            flusher: "_Flusher",
            tracker: "_EntryTracker",
    ) -> None:
        if not tasks:
            return
        if len(tasks) == 1:
            src, dst, title, *extra = tasks[0]
            self._copy_one_ssh_task(src, dst, title, tuple(extra), flusher, tracker)
            return

        workers = min(_SMB_WORKERS, len(tasks))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [
                pool.submit(self._copy_one_ssh_task, src, dst, title, tuple(extra), flusher, tracker)
                for src, dst, title, *extra in tasks
            ]
            _run_futures(futs, self._cancel, "rsync task")

    def _scan_copy_local_pipelined(self, tasks: list, flusher: "_Flusher", tracker: "_EntryTracker", *,
                                    emit_scan_finished: bool = False,
                                    on_count_change: "Callable[[int], None] | None" = None,
                                    on_scan_done: "Callable[[], None] | None" = None) -> int:

        cancel = self._cancel
        if not tasks:
            if on_scan_done is not None:
                on_scan_done()
            if emit_scan_finished:
                self.scan_finished.emit(0)
            return 0

        pipe_q: queue.Queue = queue.Queue(maxsize=_PIPE_MAXSIZE)
        sentinel = object()
        work_q = queue.SimpleQueue()
        pend_lock = threading.Lock()
        pending = [0]
        live_copiers = [0]
        dir_done = threading.Event()
        last_emit = [0.0]
        found = [0]
        missing = [0]
        copy_params = [_LOCAL_BATCH, _SCAN_PIPE_BATCH]
        copy_params_lock = threading.Lock()

        def _eq(item: tuple) -> None:
            with pend_lock:
                pending[0] += 1
            work_q.put(item)

        def _dq() -> None:
            with pend_lock:
                pending[0] -= 1
                if pending[0] == 0:
                    dir_done.set()

        missing_entries: list = []
        missing_counts: dict = {}
        for src, dst, title, *rest in tasks:
            excludes = rest[0] if rest else frozenset()
            if not os.path.exists(src):
                missing_entries.append((src, _not_found_reason(title), 0))
                if title:
                    missing_counts.setdefault(title, [0, 0, 0, 0])[1] += 1
                missing[0] += 1
            else:
                _eq((src, dst, title, excludes))

        if missing_entries:
            flusher.push(sk=missing_entries)
            tracker.batch_update(missing_counts)

        with pend_lock:
            if pending[0] == 0:
                dir_done.set()

        def _scan_worker() -> None:
            local_n = 0
            batch: list = []
            while not cancel.is_set():
                try:
                    _src, _dst, _title, _excl = work_q.get(timeout=0.1)
                except queue.Empty:
                    if dir_done.is_set():
                        break
                    continue

                with copy_params_lock:
                    spb = copy_params[1]
                try:
                    for is_dir, path, dst_path, entry_stat in _scan_dir_entries(_src, _dst, _excl, cancel):
                        if is_dir:
                            _eq((path, dst_path, _title, _excl))
                        else:
                            batch.append((path, dst_path, _title, entry_stat))
                            local_n += 1
                            if len(batch) >= spb:
                                while not cancel.is_set():
                                    try:
                                        pipe_q.put(batch, timeout=0.25)
                                        batch = []
                                        break
                                    except queue.Full:
                                        continue
                                if batch:
                                    local_n -= len(batch)
                                    batch = []
                except NotADirectoryError:
                    batch.append((_src, _dst, _title, None))
                    local_n += 1
                except (PermissionError, FileNotFoundError):
                    pass
                except OSError as exc:
                    logger.warning("scan %s: %s", _src, exc)
                finally:
                    emit_cur = -1
                    now = time.monotonic()
                    with pend_lock:
                        if now - last_emit[0] >= _SCAN_EMIT_SECS:
                            found[0] += local_n
                            local_n = 0
                            last_emit[0] = now
                            emit_cur = found[0]
                    if emit_cur >= 0:
                        cur_total = emit_cur + missing[0]
                        self.scan_progress.emit(tr("Scanning"), cur_total)
                        lb_new, ft_new, spb_new = _scale_params(cur_total)
                        with copy_params_lock:
                            copy_params[0] = lb_new
                            copy_params[1] = spb_new
                        flusher.set_flush_thresh(ft_new)
                        if on_count_change is not None:
                            on_count_change(cur_total)
                    _dq()

            if batch and not cancel.is_set():
                while not cancel.is_set():
                    try:
                        pipe_q.put(batch, timeout=0.25)
                        batch = []
                        break
                    except queue.Full:
                        continue

            if batch:
                local_n -= len(batch)

            if local_n > 0:
                with pend_lock:
                    found[0] += local_n

        def _copy_worker() -> None:
            with pend_lock:
                live_copiers[0] += 1
            buf = _BatchBuffer(flusher, tracker)
            last_fl_t = time.monotonic()
            _file_ctr = 0

            def _fl() -> None:
                nonlocal last_fl_t, _file_ctr
                buf.flush()
                last_fl_t = time.monotonic()
                _file_ctr = 0

            try:
                while True:
                    try:
                        item = pipe_q.get(timeout=0.1)
                    except queue.Empty:
                        if cancel.is_set():
                            break
                        if (time.monotonic() - last_fl_t) >= _FLUSH_INTERVAL:
                            _fl()
                        continue
                    if item is sentinel:
                        break
                    if cancel.is_set():
                        continue
                    with copy_params_lock:
                        lb = copy_params[0]
                    for entry in item:
                        if cancel.is_set():
                            break
                        buf.record(entry, cancel)
                        _file_ctr += 1
                        if buf.pending >= lb or (
                            _file_ctr >= _TIME_CHECK_EVERY
                            and (time.monotonic() - last_fl_t) >= _FLUSH_INTERVAL
                        ):
                            _fl()
            finally:
                try:
                    _fl()
                finally:
                    with pend_lock:
                        live_copiers[0] -= 1

        def _run_scan() -> None:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS) as sp:
                    futs = [sp.submit(_scan_worker) for _ in range(_WORKERS)]
                    _run_futures(futs, cancel, "scan worker")
                total = found[0] + missing[0]
                self.scan_progress.emit(tr("Scanning"), total)
                lb, ft, spb = _scale_params(total)
                with copy_params_lock:
                    copy_params[0] = lb
                    copy_params[1] = spb
                flusher.set_flush_thresh(ft)
                if on_count_change is not None:
                    on_count_change(total)
                if emit_scan_finished:
                    flusher.set_total(total)
                    self.scan_finished.emit(total)
            finally:
                if on_scan_done is not None:
                    on_scan_done()
                if cancel.is_set():
                    while True:
                        try:
                            pipe_q.get_nowait()
                        except queue.Empty:
                            break
                for _ in range(_WORKERS):
                    while True:
                        try:
                            pipe_q.put(sentinel, timeout=0.1)
                            break
                        except queue.Full:
                            with pend_lock:
                                if live_copiers[0] <= 0:
                                    break

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1 + _WORKERS)
        try:
            all_futs = [pool.submit(_run_scan)] + [pool.submit(_copy_worker) for _ in range(_WORKERS)]
            _run_futures(all_futs, cancel, "pipeline worker")
        finally:
            pool.shutdown(wait=True, cancel_futures=False)

        return found[0] + missing[0]

    def _probe_shares(self, smb_tasks, user, pw) -> tuple[set, set, bool]:
        unreachable: set[tuple[str, str]] = set()
        auth_failed: set[tuple[str, str]] = set()
        guest = False
        seen: set[tuple[str, str]] = set()
        shares: list = []

        for s, d, *_ in smb_tasks:
            if self._cancel.is_set():
                break
            h, sh, _ = _parse_smb(s if is_smb(s) else d)
            if (h, sh) not in seen:
                seen.add((h, sh))
                shares.append((h, sh))

        if not shares:
            return unreachable, auth_failed, guest

        lock = threading.Lock()

        def probe_one(_h: str, _sh: str) -> None:
            nonlocal guest
            if self._cancel.is_set():
                return
            result = _SmbClient(_h, _sh, user, pw).probe()
            with lock:
                if result == "timeout":
                    unreachable.add((_h, _sh))
                elif result == "auth":
                    auth_failed.add((_h, _sh))
                elif result == "guest":
                    guest = True

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(_SMB_WORKERS, len(shares))) as pool:
            futs = [pool.submit(probe_one, h, sh) for h, sh in shares]
            _run_futures(futs, self._cancel, "probe")

        return unreachable, auth_failed, guest

    @staticmethod
    def _filter_dead_tasks(smb_tasks, dead_shares, unreachable_shares) -> tuple[list, list]:
        alive: list = []
        errors: list = []
        for s, d, t, *rest in smb_tasks:
            excl = rest[0] if rest else frozenset()
            h, sh, _ = _parse_smb(s if is_smb(s) else d)
            if (h, sh) in dead_shares:
                reason = ("NT_STATUS_HOST_UNREACHABLE" if (h, sh) in unreachable_shares else tr("Authentication failed"))
                errors.append((s if is_smb(s) else d, reason, t))
            else:
                alive.append((s, d, t, excl))
        return alive, errors

    def _copy_smb_all(self, smb_expanded: list, smb_errors: list, user: str, pw: "_SecurePw | None", guest: bool,
                      flusher: _Flusher, tracker: _EntryTracker) -> None:
        cancel = self._cancel

        _push_errors(flusher, tracker, smb_errors)

        if not smb_expanded or cancel.is_set():
            return

        share_groups: dict = {}
        for job in smb_expanded:
            grp = share_groups.setdefault((job.host, job.share), {"get": [], "put": []})
            grp["get" if job.kind == "smb_get" else "put"].append(job)

        ri_cache: dict = {}
        ri_lock = threading.Lock()

        def run_share(host: str, share: str) -> None:
            group = share_groups[(host, share)]
            try:
                client = _SmbClient(host, share, user, pw, guest)
                processor = _ShareProcessor(client, cancel, flusher, tracker, ri_cache, ri_lock)
                processor.process(group["get"], group["put"])
            except Exception as exc:
                logger.error("SMB share error //%s/%s: %s", host, share, exc)
                reason = tr("Share processing crashed: {exc}", exc=exc)
                _push_errors(flusher, tracker, [
                    ((_job.src_url if _job.kind == "smb_put"
                      else f"smb://{host}/{share}/{_job.remote_path}"), reason, _job.title)
                    for _job in group["get"] + group["put"]
                ])

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(_SMB_WORKERS, len(share_groups))) as pool:
            futs = [pool.submit(run_share, h, sh) for h, sh in share_groups if not cancel.is_set()]
            _run_futures(futs, cancel, "SMB share thread")
