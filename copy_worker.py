import concurrent.futures
import errno
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from PyQt6.QtCore import QThread, pyqtSignal

from drive_utils import is_smb, is_ssh, build_rsync_cmd
from pre_post_hooks import run_hooks as _run_hooks
from state import logger

from copy_worker_core import (
    _CHUNK, _IO_BUF, _WORKERS, _FLUSH_THRESH, _FLUSH_INTERVAL, _SCAN_EMIT_SECS,
    _SCAN_PIPE_BATCH, _LOCAL_BATCH, _CLAIM_SIZE, _PIPE_MAXSIZE,
    _SMB_WORKERS, _PID, _O_NOATIME, _tls, _seen_dirs_lock, _seen_dirs_global, _TIME_CHECK_EVERY,
    _smb_procs, _smb_procs_lock,
    _scale_params, _scan_dir_entries,
    _ensure_dir, _parse_smb, _run_futures, _silent_unlink
)
from copy_worker_smb import (
    _SecurePw, _get_smb_credentials,
    _SmbJob, _SmbClient, _SmbScanner, _ShareProcessor
)


def _do_copy(entry, cancel: threading.Event, ok_l: list, sk_l: list, er_l: list, tc: dict) -> None:

    src, dst, title, st = entry
    try:
        status, aux, sz = _copy_file(src, dst, cancel, st)
    except Exception as exc:
        logger.error("copy %s: %s", src, exc)
        status, aux, sz = "error", str(exc), 0
    if status == "ok":
        ok_l.append((src, dst, sz))
        if title: tc.setdefault(title, [0, 0, 0])[0] += 1
    elif status == "skip":
        sk_l.append((src, aux or "Up to date", sz))
        if title: tc.setdefault(title, [0, 0, 0])[1] += 1
    else:
        er_l.append((src, aux, 0))
        if title: tc.setdefault(title, [0, 0, 0])[2] += 1

def _is_up_to_date_local(dst: str, src_st: "os.stat_result") -> bool:
    try:
        d = os.stat(dst)
        return d.st_size == src_st.st_size and abs(d.st_mtime_ns - src_st.st_mtime_ns) <= 2_000_000_000
    except OSError:
        return False


def _is_symlink_up_to_date(dst: str, target: str) -> bool:
    try:
        return os.path.islink(dst) and os.readlink(dst) == target
    except OSError:
        return False


def _copy_symlink(src: str, dst: str) -> tuple:
    try:
        target = os.readlink(src)
    except OSError as exc:
        return "error", f"Symlink unreadable: {exc}", 0

    if _is_symlink_up_to_date(dst, target):
        return "skip", "Up to date", 0

    if not _ensure_dir(os.path.dirname(dst)):
        return "error", "Directory could not be created", 0

    tmp = f"{dst}.{_PID}.{threading.get_ident()}.lnk.part"
    try:
        _silent_unlink(tmp)
        os.symlink(target, tmp)
        os.replace(tmp, dst)
        return "ok", dst, 0
    except OSError as exc:
        _silent_unlink(tmp)
        logger.error("symlink %s → %s: %s", src, dst, exc)
        return "error", str(exc), 0


def _copy_loop(rfd: int, wfd: int, total: int, cancel: threading.Event) -> int:
    rem = total
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
    return total - rem


def _copy_file(src, dst, cancel, cached_st=None):
    if os.path.islink(src):
        return _copy_symlink(src, dst)

    for _attempt in range(2):
        tmp = f"{dst}.{_PID}.{threading.get_ident()}.part"
        rfd = wfd = None
        success = False
        try:
            if cancel.is_set():
                return "skip", "Cancelled", 0
            if cached_st is not None and _attempt == 0:
                st = cached_st
            else:
                try:
                    st = os.stat(src)
                except OSError:
                    return "error", "Source unreadable", 0

            if _is_up_to_date_local(dst, st):
                return "skip", "Up to date", st.st_size

            if not _ensure_dir(os.path.dirname(dst)):
                return "error", "Directory could not be created", 0

            _euid = os.geteuid()
            _may_use_noatime = _euid == 0 or st.st_uid == _euid
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
                    os.posix_fadvise(rfd, 0, st.st_size, os.POSIX_FADV_SEQUENTIAL)
                except OSError:
                    pass

            copied = _copy_loop(rfd, wfd, st.st_size, cancel)
            if copied < st.st_size:
                raise OSError(f"Incomplete copy: {copied}/{st.st_size} bytes written")

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
            return "ok", dst, copied

        except InterruptedError:
            return "skip", "Cancelled", 0
        except OSError as exc:
            if "Incomplete copy" in str(exc) and _attempt == 0:
                logger.debug("copy %s: %s — retrying", src, exc)
                continue
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
    return "error", "Copy failed after retries", 0



class _EntryTracker:
    __slots__ = ("_counts", "_lock")

    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._counts: dict[str, list[int]] = {}

    def batch_update(self, counts: dict) -> None:
        if not counts:
            return
        with self._lock:
            for title, (n_ok, n_skip, n_err) in counts.items():
                if title:
                    c = self._counts.setdefault(title, [0, 0, 0])
                    c[0] += n_ok
                    c[1] += n_skip
                    c[2] += n_err

    def emit_all(self, signal) -> None:
        with self._lock:
            snap = {t: ec[:] for t, ec in self._counts.items()}
        for t, ec in snap.items():
            if t:
                signal.emit(t, ec[0], ec[1], ec[2])


class _Flusher:
    __slots__ = ("_er", "_flush_thresh", "_last_flush_t", "_lock", "_ok", "_signal", "_sk", "_total", "copied", "done", "errors", "skipped")

    def __init__(self, signal, total: int, flush_thresh: int = _FLUSH_THRESH) -> None:
        self._signal       = signal
        self._total        = total
        self._flush_thresh = flush_thresh
        self._lock         = threading.Lock()
        self._ok: list     = []
        self._sk: list     = []
        self._er: list     = []
        self.done = self.copied = self.skipped = self.errors = 0
        self._last_flush_t = time.monotonic()

    def set_total(self, total: int) -> None:
        with self._lock: self._total = total

    def set_flush_thresh(self, thresh: int) -> None:
        with self._lock: self._flush_thresh = thresh

    def push(self, ok=(), sk=(), er=(), *, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if ok: self._ok.extend(ok)
            if sk: self._sk.extend(sk)
            if er: self._er.extend(er)
            n = len(self._ok) + len(self._sk) + len(self._er)
            if n == 0:
                return
            timed_out = (now - self._last_flush_t) >= _FLUSH_INTERVAL
            if not force and n < self._flush_thresh and not timed_out:
                return
            payload_ok, self._ok = self._ok, []
            payload_sk, self._sk = self._sk, []
            payload_er, self._er = self._er, []
            self.done    += n
            self.copied  += len(payload_ok)
            self.skipped += len(payload_sk)
            self.errors  += len(payload_er)
            done_snap  = self.done
            total_snap = self._total
            self._last_flush_t = now
        self._signal.emit(payload_ok, payload_sk, payload_er, done_snap, total_snap)

    def flush(self) -> None: self.push(force=True)


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
        self._tracker.batch_update(self.tc)
        self.tc.clear()
        self.pending = 0


class CopyWorker(QThread):
    batch_update = pyqtSignal(list, list, list, int, int)
    finished_work = pyqtSignal(int, int, int, bool)
    scan_progress = pyqtSignal(str, int)
    entry_status = pyqtSignal(str, int, int, int)
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
                if _pre and not self._fire_hooks(_t, "pre", _pre, abort=True):
                    skip_titles.add(_t)
        return skip_titles

    def _run_post_hooks(self, tasks: list) -> None:
        seen: set[str] = set()
        for _s, _d, _t, _exc in tasks:
            if _t and _t not in seen:
                seen.add(_t)
                _, _post = self._hooks.get(_t, ([], []))
                if _post:
                    self._fire_hooks(_t, "post", _post, abort=False)

    def run(self) -> None:
        with _seen_dirs_lock:
            _seen_dirs_global.clear()
        if hasattr(_tls, "seen_dirs"):
            _tls.seen_dirs.clear()
        pw: "_SecurePw | None" = None
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

            self.scan_progress.emit("Scanning", 0)

            if not smb_tasks:
                flusher = _Flusher(self.batch_update, 0)
                tracker = _EntryTracker()
                skip_titles = self._run_pre_hooks(local_tasks + ssh_tasks) if not self._cancel.is_set() else set()
                active_local = [(s, d, t, e) for s, d, t, e in local_tasks if t not in skip_titles]
                active_ssh = [(s, d, t, e) for s, d, t, e in ssh_tasks if t not in skip_titles]
                if active_local and not self._cancel.is_set():
                    self._scan_copy_local_pipelined(active_local, flusher, tracker)
                else:
                    self.scan_finished.emit(0)
                if active_ssh and not self._cancel.is_set():
                    self._copy_ssh_tasks(active_ssh, flusher, tracker)
                self._run_post_hooks(local_tasks + ssh_tasks)
                flusher.flush()
                tracker.emit_all(self.entry_status)
                cancelled = self._cancel.is_set()
                self.finished_work.emit(flusher.copied, flusher.skipped, flusher.errors, cancelled)
                return

            local_items: list[tuple[str, str, str]] = []
            local_not_found: list = []
            smb_expanded: list[_SmbJob] = []
            smb_errors: list[tuple[str, str]] = []
            _guest_box: list[bool] = [False]

            skip_titles = self._run_pre_hooks(local_tasks + ssh_tasks + smb_tasks) if not self._cancel.is_set() else set()
            local_tasks = [(s, d, t, e) for s, d, t, e in local_tasks if t not in skip_titles]
            ssh_tasks = [(s, d, t, e) for s, d, t, e in ssh_tasks if t not in skip_titles]
            smb_tasks = [(s, d, t, e) for s, d, t, e in smb_tasks if t not in skip_titles]

            def _phase1_local() -> None:
                nonlocal local_items
                if local_tasks and not self._cancel.is_set():
                    local_items = self._scan_local_all(local_tasks, not_found=local_not_found)

            def _phase1_smb() -> None:
                if not smb_tasks or self._cancel.is_set():
                    return
                if smb_tool_missing:
                    smb_errors.extend(
                        (s_ if is_smb(s_) else d_,
                         "'smbclient' not found — install the Samba client tools (e.g. package "
                         "'smbclient' / 'samba-client') to enable SMB backups")
                        for s_, d_, *_ in smb_tasks
                    )
                    return
                ur, af, _guest = self._probe_shares(smb_tasks, user, pw)
                _guest_box[0] = _guest
                dead = ur | af
                alive_tasks = smb_tasks
                if dead:
                    alive_tasks, pre_err = self._filter_dead_tasks(smb_tasks, dead, ur)
                    smb_errors.extend(pre_err)
                if alive_tasks and not self._cancel.is_set():
                    scanner = _SmbScanner(user, pw, _guest, self._cancel, lambda n: self.scan_progress.emit("Scanning SMB", n))
                    exp, err = scanner.resolve(alive_tasks)
                    smb_expanded.extend(exp)
                    smb_errors.extend(err)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futs = [pool.submit(_phase1_local), pool.submit(_phase1_smb)]
                _run_futures(futs, self._cancel, "Phase-1")

            if self._cancel.is_set():
                self.finished_work.emit(0, 0, 0, True)
                return

            guest = _guest_box[0]
            total = len(local_items) + len(local_not_found) + len(smb_expanded) + len(smb_errors)
            cs, lb, ft, _spb, cw = _scale_params(total)
            self.scan_finished.emit(total)

            flusher = _Flusher(self.batch_update, total, flush_thresh=ft)
            tracker = _EntryTracker()

            def _phase2_local() -> None:
                if local_items and not self._cancel.is_set():
                    self._copy_local_all(local_items, flusher, tracker, claim_size=cs, local_batch=lb, workers=cw)
                if local_not_found and not self._cancel.is_set():
                    flusher.push(
                        sk=[(p, (f"Path does not exist — skipping ({t_})" if t_
                                 else "Path does not exist — skipping"), 0)
                            for p, t_ in local_not_found],
                        force=True,
                    )
                    nf_counts: dict = {}
                    for _, nf_title in local_not_found:
                        if nf_title:
                            nf_counts.setdefault(nf_title, [0, 0, 0])[1] += 1
                    if nf_counts:
                        tracker.batch_update(nf_counts)

            def _phase2_smb() -> None:
                if (smb_expanded or smb_errors) and not self._cancel.is_set():
                    self._copy_smb_all(smb_expanded, smb_errors, user, pw, guest, flusher, tracker)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futs = [pool.submit(_phase2_local), pool.submit(_phase2_smb)]
                _run_futures(futs, self._cancel, "Phase-2")

            if ssh_tasks and not self._cancel.is_set():
                self._copy_ssh_tasks(ssh_tasks, flusher, tracker)

            self._run_post_hooks(local_tasks + ssh_tasks + smb_tasks)
            flusher.flush()
            tracker.emit_all(self.entry_status)
            cancelled = self._cancel.is_set()
            self.finished_work.emit(flusher.copied, flusher.skipped, flusher.errors, cancelled)
        except Exception as exc:
            logger.error("CopyWorker critical: %s", exc, exc_info=True)
            self.finished_work.emit(0, 0, 0, False)
        finally:
            if pw is not None:
                pw.clear()

    def _copy_ssh_tasks(
            self,
            tasks: list[tuple[str, str, str, frozenset]],
            flusher: "_Flusher",
            tracker: "_EntryTracker",
    ) -> None:
        def _track(ok: int, skip: int, err: int) -> None:
            tracker.batch_update({title: (ok, skip, err)} if title else {})

        for src, dst, title, *extra in tasks:
            if self._cancel.is_set():
                break

            self.scan_progress.emit(f"rsync  {title or src}", 0)

            excludes = list(extra[0]) if extra and extra[0] else None

            cmd = build_rsync_cmd(src, dst, exclude=excludes, delete=title in self._mirror_titles)
            logger.debug("_copy_ssh_tasks: %s", " ".join(cmd))

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except (OSError, FileNotFoundError) as exc:
                logger.error("rsync launch failed for '%s': %s", src, exc)
                flusher.push(er=[(src, str(exc), 0)])
                _track(0, 0, 1)
                continue

            if proc.stdout is None:
                logger.error("rsync stdout is None for '%s'", src)
                flusher.push(er=[(src, "rsync stdout unavailable", 0)])
                _track(0, 0, 1)
                continue
            last_pct = 0
            try:
                for line in proc.stdout:
                    if self._cancel.is_set():
                        proc.kill()
                        break
                    line = line.rstrip()
                    m = self._RSYNC_PROGRESS_RE.match(line)
                    if m:
                        pct = int(m.group(2))
                        if pct != last_pct:
                            last_pct = pct
                            self.scan_progress.emit(
                                f"rsync  {title or src}  {pct}%  {m.group(3)}",
                                pct,
                            )
                    elif line:
                        logger.debug("rsync: %s", line)
            except OSError as exc:
                logger.warning("rsync read error for '%s': %s", src, exc)

            proc.wait()
            if self._cancel.is_set():
                break

            if proc.returncode == 0:
                flusher.push(ok=[(src, dst, 0)])
                _track(1, 0, 0)
                logger.info("rsync OK: %s → %s", src, dst)
            else:
                flusher.push(er=[(src, f"rsync exit {proc.returncode}", 0)])
                _track(0, 0, 1)
                logger.error("rsync exit %d: %s → %s", proc.returncode, src, dst)

    def _scan_local_all(self, tasks: list, not_found: "list | None" = None) -> list:
        cancel = self._cancel
        file_q = queue.SimpleQueue()
        work_q = queue.SimpleQueue()
        pend_lock = threading.Lock()
        pending = [0]
        all_done = threading.Event()
        total_found = [0]
        last_emit_t = [0.0]

        def _enqueue(item: tuple) -> None:
            with pend_lock:
                pending[0] += 1
            work_q.put(item)

        def _finish_one() -> None:
            with pend_lock:
                pending[0] -= 1
                if pending[0] == 0:
                    all_done.set()

        for src, dst, title, *rest in tasks:
            excludes = rest[0] if rest else frozenset()
            if not os.path.exists(src):
                if not_found is not None:
                    not_found.append((src, title))
            else:
                _enqueue((src, dst, title, excludes))

        with pend_lock:
            if pending[0] == 0:
                all_done.set()

        def _worker() -> None:
            local_n = 0
            while not cancel.is_set():
                try:
                    _src, _dst, _title, _excl = work_q.get(timeout=0.1)
                except queue.Empty:
                    if all_done.is_set():
                        break
                    continue

                local_files: list = []
                try:
                    for is_dir, path, dst_path, st in _scan_dir_entries(_src, _dst, _excl, cancel):
                        if is_dir:
                            _enqueue((path, dst_path, _title, _excl))
                        else:
                            local_files.append((path, dst_path, _title, st))
                except NotADirectoryError:
                    local_files.append((_src, _dst, _title, None))
                except (PermissionError, FileNotFoundError):
                    pass
                except OSError as exc:
                    logger.warning("scan %s: %s", _src, exc)
                finally:
                    if local_files:
                        file_q.put(local_files)
                        local_n += len(local_files)
                        now = time.monotonic()
                        with pend_lock:
                            if now - last_emit_t[0] >= _SCAN_EMIT_SECS:
                                total_found[0] += local_n
                                local_n = 0
                                last_emit_t[0] = now
                                cur = total_found[0]
                            else:
                                cur = -1
                        if cur >= 0:
                            self.scan_progress.emit("Scanning", cur)
                    _finish_one()

            if local_n:
                with pend_lock:
                    total_found[0] += local_n

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS)
        try:
            futs = [pool.submit(_worker) for _ in range(_WORKERS)]
            _run_futures(futs, cancel, "scan worker")
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        self.scan_progress.emit("Scanning", total_found[0])

        result: list = []
        try:
            while True:
                result.extend(file_q.get_nowait())
        except queue.Empty:
            pass
        return result

    def _scan_copy_local_pipelined(self, tasks: list, flusher: "_Flusher", tracker: "_EntryTracker") -> None:
        cancel = self._cancel
        if not tasks:
            self.scan_finished.emit(0)
            return

        pipe_q: queue.Queue = queue.Queue(maxsize=_PIPE_MAXSIZE)
        sentinel = object()
        work_q = queue.SimpleQueue()
        pend_lock = threading.Lock()
        pending = [0]
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

        for src, dst, title, *rest in tasks:
            excludes = rest[0] if rest else frozenset()
            if not os.path.exists(src):
                _nf_reason = (f"Path does not exist — skipping ({title})" if title
                              else "Path does not exist — skipping")
                flusher.push(sk=[(src, _nf_reason, 0)])
                if title:
                    tracker.batch_update({title: (0, 1, 0)})
                missing[0] += 1
            else:
                _eq((src, dst, title, excludes))

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
                                        if cancel.is_set():
                                            local_n -= len(batch)
                                            batch = []
                                            break
                                else:
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
                        self.scan_progress.emit("Scanning", emit_cur)
                    _dq()

            if batch and not cancel.is_set():
                while not cancel.is_set():
                    try:
                        pipe_q.put(batch, timeout=0.25)
                        batch = []
                        break
                    except queue.Full:
                        pass

            if cancel.is_set():
                local_n -= len(batch)

            if local_n > 0:
                with pend_lock:
                    found[0] += local_n

        def _copy_worker() -> None:
            buf = _BatchBuffer(flusher, tracker)
            last_fl_t = time.monotonic()
            _file_ctr = 0

            def _fl() -> None:
                nonlocal last_fl_t, _file_ctr
                buf.flush()
                last_fl_t = time.monotonic()
                _file_ctr = 0

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
            _fl()

        def _run_scan() -> None:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=_WORKERS) as sp:
                    futs = [sp.submit(_scan_worker) for _ in range(_WORKERS)]
                    _run_futures(futs, cancel, "scan worker")
                total = found[0] + missing[0]
                self.scan_progress.emit("Scanning", total)
                _, lb, ft, spb, _ = _scale_params(total)
                with copy_params_lock:
                    copy_params[0] = lb
                    copy_params[1] = spb
                flusher.set_total(total)
                flusher.set_flush_thresh(ft)
                self.scan_finished.emit(total)
            finally:
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
                            pass

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1 + _WORKERS)
        try:
            all_futs = [pool.submit(_run_scan)] + [pool.submit(_copy_worker) for _ in range(_WORKERS)]
            _run_futures(all_futs, cancel, "pipeline worker")
        finally:
            pool.shutdown(wait=True, cancel_futures=False)

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

        lock = threading.Lock()

        def probe_one(_h: str, _sh: str) -> None:
            if self._cancel.is_set():
                return
            result = _SmbClient(_h, _sh, user, pw).probe()
            with lock:
                nonlocal guest
                if result == "timeout":
                    unreachable.add((_h, _sh))
                elif result == "auth":
                    auth_failed.add((_h, _sh))
                elif result == "guest":
                    guest = True

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(_SMB_WORKERS, len(shares) or 1)) as pool:
            futs = [pool.submit(probe_one, h, sh) for h, sh in shares]
            _run_futures(futs, self._cancel, "probe")

        return unreachable, auth_failed, guest

    @staticmethod
    def _filter_dead_tasks(smb_tasks, dead_shares, unreachable_shares) -> tuple[list, list]:
        alive: list = []
        errors: list = []
        for s, d, t, *_ in smb_tasks:
            h, sh, _ = _parse_smb(s if is_smb(s) else d)
            if (h, sh) in dead_shares:
                reason = ("NT_STATUS_HOST_UNREACHABLE" if (h, sh) in unreachable_shares else "Authentication failed")
                errors.append((s if is_smb(s) else d, reason))
            else:
                alive.append((s, d, t))
        return alive, errors

    def _copy_local_all(self, items: list, flusher: _Flusher, tracker: _EntryTracker,
                        claim_size: int = _CLAIM_SIZE, local_batch: int = _LOCAL_BATCH,
                        workers: int = 0) -> None:
        cancel = self._cancel
        n_items = len(items)
        if not n_items:
            return

        claim_lock = threading.Lock()
        claim_idx = [0]

        def _claim() -> "tuple[int, int] | None":
            with claim_lock:
                start = claim_idx[0]
                if start >= n_items:
                    return None
                end = min(start + claim_size, n_items)
                claim_idx[0] = end
                return start, end

        def _worker() -> None:
            buf = _BatchBuffer(flusher, tracker)
            while not cancel.is_set():
                claim = _claim()
                if claim is None:
                    break
                start, end = claim
                for i in range(start, end):
                    if cancel.is_set():
                        break
                    buf.record(items[i], cancel)
                    if buf.pending >= local_batch:
                        buf.flush()
            buf.flush()

        _workers = workers or _WORKERS
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=_workers)
        try:
            futs = [pool.submit(_worker) for _ in range(_workers)]
            _run_futures(futs, cancel, "copy worker")
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def _copy_smb_all(self, smb_expanded: list, smb_errors: list, user: str, pw: "_SecurePw | None", guest: bool,
                      flusher: _Flusher, tracker: _EntryTracker) -> None:
        cancel = self._cancel

        if smb_errors:
            flusher.push(er=[(src, err, 0) for src, err in smb_errors], force=True)

        if not smb_expanded or cancel.is_set():
            return

        share_groups: dict = {}
        for job in smb_expanded:
            grp = share_groups.setdefault((job.host, job.share), {"get": [], "put": []})
            grp["get" if job.kind == "smb_get" else "put"].append(job)

        ri_cache: dict = {}
        ri_lock = threading.Lock()

        def run_share(host: str, share: str) -> None:
            try:
                client = _SmbClient(host, share, user, pw, guest)
                processor = _ShareProcessor(client, cancel, flusher, tracker, ri_cache, ri_lock)
                processor.process(share_groups[(host, share)]["get"], share_groups[(host, share)]["put"])
            except Exception as exc:
                logger.error("SMB share error //%s/%s: %s", host, share, exc)
                er_w = []
                for _job in share_groups[(host, share)]["get"] + share_groups[(host, share)]["put"]:
                    src = (_job.src_url if _job.kind == "smb_put" else f"smb://{host}/{share}/{_job.remote_path}")
                    er_w.append((src, f"Share processing crashed: {exc}", 0))
                flusher.push(er=er_w)

        with concurrent.futures.ThreadPoolExecutor(max_workers=_SMB_WORKERS) as pool:
            futs = [pool.submit(run_share, h, sh) for h, sh in share_groups if not cancel.is_set()]
            _run_futures(futs, cancel, "SMB share thread")
