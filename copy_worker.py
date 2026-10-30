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
from collections.abc import Callable
from typing import TYPE_CHECKING

from PyQt6.QtCore import QThread, pyqtSignal

from advanced_copy import (
    AdvancedOptionsResult, DeleteError, VersionPlan, apply_advanced_options, finalize_version,
)
from drive_utils import is_smb, is_ssh, build_rsync_cmd, get_mounts, unmounted_drive_for_path
from copy_progress import (
    COST_FILE, COST_REMOTE_TASK, COST_SKIP, STEP_CONFIRM, STEP_HOOKS, STEP_PREPARE, STEP_SCAN, STEP_SYNC,
    ProgressMeter, file_cost, set_stream_sink, stream_sink,
)
from pre_post_hooks import run_hooks as _run_hooks
from state import logger, frozen_safe_env
from translations import tr

from copy_worker_core import (
    _CHUNK, _IO_BUF, _FLUSH_THRESH, _FLUSH_INTERVAL, _SCAN_EMIT_SECS,
    _SCAN_PIPE_BATCH, _LOCAL_BATCH, _PIPE_MAXSIZE, _ENTRY_EMIT_SECS,
    _SMB_WORKERS, _EUID, _O_NOATIME, _TIME_CHECK_EVERY, _SMALL_FILE_MAX, _UMASK,
    _MODE_MASK, _CAP_MODE, _CAP_OWNER, _perm_caps_for, _drop_perm_caps,
    _mtime_tolerance_ns, _source_tolerance_ns, sync_destinations,
    _smb_procs, _smb_procs_lock, _RSYNC_DELETE_RE, _RSYNC_ERROR_TAIL,
    _scale_params, _scan_dir_entries, _check_destination_space, _copy_workers_for, _scan_workers_for,
    _ensure_dir, _make_new_dir, _apply_dir_attrs, _fsync_parent_dir, _tmp_path, _reset_dir_caches,
    _parse_smb, _run_futures, _silent_unlink, _hardlink_lookup, _hardlink_register,
    _ssh_join, _rsync_excludes, _rsync_src_arg, _ssh_dir_is_empty,
)
from copy_worker_smb import (
    _get_smb_credentials,
    _SmbJob, _SmbClient, _SmbScanner, _ShareProcessor
)

if TYPE_CHECKING:
    from copy_worker_smb import _SecurePw


_NF_MARK = "\x1f"
_FLUSH_AFTER_BYTES = 8 * 1024 * 1024
_RSYNC_ERR_HINT_RE = re.compile(
    r"^(?:rsync|ssh)\b|error|failed|denied|refused|timed out|no such|cannot|unable|unreachable|not permitted|connection",
    re.I)


def _not_found_reason(title: str) -> str:
    base = tr("Path does not exist — skipping")
    return f"{base}{_NF_MARK}{title}" if title else base


def _entry_size(st) -> int:
    return st.st_size if isinstance(st, os.stat_result) else 0


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


_META_SAME = 0
_META_FIXED = 1
_META_FAILED = 2


def _content_matches(src: "str | None", dst: str, src_st: "os.stat_result", d: "os.stat_result", *,
                      probe: bool = True) -> bool:
    if not stat.S_ISREG(d.st_mode) or d.st_size != src_st.st_size:
        return False
    diff = abs(d.st_mtime_ns - src_st.st_mtime_ns)
    if diff == 0:
        return True
    tolerance = _mtime_tolerance_ns(dst, d, probe=probe)
    if src is not None:
        tolerance = max(tolerance, _source_tolerance_ns(src, src_st.st_dev))
    return diff <= tolerance


def _meta_diff(src_st: "os.stat_result", d: "os.stat_result") -> int:
    flags = _CAP_MODE if (src_st.st_mode ^ d.st_mode) & _MODE_MASK else 0
    if _EUID == 0 and (src_st.st_uid != d.st_uid or src_st.st_gid != d.st_gid):
        flags |= _CAP_OWNER
    return flags


def _meta_pending(dst: str, src_st: "os.stat_result", d: "os.stat_result", *, probe: bool = True) -> int:
    flags = _meta_diff(src_st, d)
    if not flags:
        return 0
    return flags & _perm_caps_for(d.st_dev, dst, probe=probe)


def _sync_meta(dst: str, src_st: "os.stat_result", d: "os.stat_result", flags: int) -> int:
    try:
        if flags & _CAP_OWNER:
            os.lchown(dst, src_st.st_uid, src_st.st_gid)
        if flags & _CAP_MODE:
            os.chmod(dst, src_st.st_mode & _MODE_MASK)
        after = os.lstat(dst)
    except OSError as exc:
        logger.debug("metadata sync %s failed: %s", dst, exc)
        return _META_FAILED
    ignored = _meta_diff(src_st, after) & flags
    if ignored:
        _drop_perm_caps(d.st_dev, ignored)
        return _META_SAME
    return _META_FIXED


def _is_up_to_date_local(dst: str, src_st: "os.stat_result",
                          dst_st: "os.stat_result | None" = None, *, probe: bool = True,
                          src: "str | None" = None) -> bool:
    try:
        d = dst_st if dst_st is not None else os.lstat(dst)
    except OSError:
        return False
    return _content_matches(src, dst, src_st, d, probe=probe) and not _meta_pending(dst, src_st, d, probe=probe)


def _unchanged(src: str, dst: str, src_st: "os.stat_result") -> bool:
    try:
        d = os.lstat(dst)
    except OSError:
        return False
    return _content_matches(src, dst, src_st, d) and not _meta_pending(dst, src_st, d)


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
        os.rmdir(dst)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        if os.path.isdir(dst) and not os.path.islink(dst):
            logger.error("destination %s is a non-empty directory and will not be replaced: %s", dst, exc)
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

    tmp = _tmp_path(dst, ".lnk.part")
    try:
        _silent_unlink(tmp)
        os.symlink(target, tmp)
        if _EUID == 0:
            try:
                link_st = os.lstat(src)
                os.lchown(tmp, link_st.st_uid, link_st.st_gid)
            except OSError as exc:
                logger.debug("lchown %s failed: %s", tmp, exc)
        os.replace(tmp, dst)
        _fsync_parent_dir(dst)
        return "ok", dst, 0
    except OSError as exc:
        _silent_unlink(tmp)
        logger.error("symlink %s → %s: %s", src, dst, exc)
        return "error", str(exc), 0


_HAS_COPY_FILE_RANGE = hasattr(os, "copy_file_range")
_HAS_SENDFILE = hasattr(os, "sendfile")
_HAS_XATTR = hasattr(os, "listxattr") and hasattr(os, "setxattr") and hasattr(os, "getxattr")


_CFR_FALLBACK_ERRNOS = frozenset({
    errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EXDEV, errno.EINVAL,
    errno.EBADF, errno.EOVERFLOW, errno.ENODEV, errno.ENOTTY, errno.ESPIPE, errno.EPERM,
})


def _copy_loop(rfd: int, wfd: int, total: int, cancel: threading.Event) -> "tuple[int, OSError | None]":
    rem = total
    last_err: OSError | None = None
    sink = stream_sink()
    try:
        if not _HAS_COPY_FILE_RANGE:
            raise OSError(errno.ENOSYS, "copy_file_range unavailable")
        while rem > 0:
            if cancel.is_set():
                raise InterruptedError
            n = os.copy_file_range(rfd, wfd, min(rem, _CHUNK))
            if n == 0:
                break
            rem -= n
            if sink is not None:
                sink(n)
    except InterruptedError:
        raise
    except OSError as exc:
        if exc.errno not in _CFR_FALLBACK_ERRNOS:
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
    if rem > 0 and _HAS_SENDFILE:
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
                if sink is not None:
                    sink(n)
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
                if sink is not None:
                    sink(len(buf))
        except InterruptedError:
            raise
        except OSError as exc:
            logger.warning("read/write fallback failed after %d/%d bytes: %s", total - rem, total, exc)
            last_err = exc
    return total - rem, last_err


_SPARSE_SLACK = 64 * 1024
_HAS_SEEK_DATA = hasattr(os, "SEEK_DATA") and hasattr(os, "SEEK_HOLE")


def _is_sparse(st: "os.stat_result") -> bool:
    return (_HAS_SEEK_DATA and st.st_size > _SPARSE_SLACK
            and st.st_blocks * 512 + _SPARSE_SLACK < st.st_size)


def _copy_extents(rfd: int, wfd: int, size: int, cancel: threading.Event) -> None:
    pos = 0
    use_cfr = _HAS_COPY_FILE_RANGE
    sink = stream_sink()
    while pos < size:
        try:
            start = os.lseek(rfd, pos, os.SEEK_DATA)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                break
            raise
        end = min(os.lseek(rfd, start, os.SEEK_HOLE), size)
        while start < end:
            if cancel.is_set():
                raise InterruptedError
            if use_cfr:
                try:
                    n = os.copy_file_range(
                        rfd, wfd, min(end - start, _CHUNK), start, start)
                except OSError as exc:
                    if exc.errno not in _CFR_FALLBACK_ERRNOS:
                        raise
                    logger.debug("copy_file_range not usable for extents: %s", exc)
                    use_cfr = False
                    continue
            else:
                buf = os.pread(rfd, min(end - start, _IO_BUF), start)
                n = len(buf)
                done = 0
                while done < n:
                    w = os.pwrite(wfd, buf[done:], start + done)
                    if w == 0:
                        raise OSError(errno.EIO, "os.pwrite returned 0")
                    done += w
            if n == 0:
                raise OSError(errno.EINVAL, "source shrank during copy")
            start += n
            if sink is not None:
                sink(n)
        pos = end
    os.ftruncate(wfd, size)


def _copy_sparse_or_dense(rfd: int, wfd: int, size: int, cancel: threading.Event) -> "tuple[int, OSError | None]":
    try:
        _copy_extents(rfd, wfd, size, cancel)
        return size, None
    except InterruptedError:
        raise
    except OSError as exc:
        if exc.errno in _FATAL_IO_ERRNOS:
            raise
        logger.debug("sparse copy not possible, copying densely: %s", exc)
    for fd in (rfd, wfd):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError as exc:
            return 0, exc
    try:
        os.ftruncate(wfd, 0)
    except OSError as exc:
        logger.debug("ftruncate before dense copy failed: %s", exc)
    return _copy_loop(rfd, wfd, size, cancel)


_FATAL_IO_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS})
_FSYNC_UNSUPPORTED_ERRNOS = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})
_DROP_CACHE_MIN = 64 * 1024 * 1024


def _fsync_data(fd: int, path: str) -> None:
    try:
        os.fdatasync(fd)
    except OSError as exc:
        if exc.errno in _FSYNC_UNSUPPORTED_ERRNOS:
            logger.debug("fsync not supported for %s: %s", path, exc)
            return
        raise


def _copy_xattrs(rfd: int, wfd: int) -> None:
    if not _HAS_XATTR:
        return
    try:
        names = os.listxattr(rfd)
    except OSError:
        return
    for name in names:
        try:
            os.setxattr(wfd, name, os.getxattr(rfd, name))
        except OSError as exc:
            logger.debug("xattr %s not copied: %s", name, exc)


def _close_quiet(fd: "int | None") -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


class _IncompleteCopyError(OSError):
    pass


def _link_existing(first: str, dst: str, tmp: str, size: int) -> bool:
    try:
        first_st = os.lstat(first)
        if not stat.S_ISREG(first_st.st_mode) or first_st.st_size != size:
            return False
        _silent_unlink(tmp)
        os.link(first, tmp)
        os.replace(tmp, dst)
    except OSError:
        _silent_unlink(tmp)
        return False
    return True


def _copy_file(src, dst, cancel, cached_st=None):
    if cached_st is True:
        return _copy_symlink(src, dst)
    if cached_st is None and os.path.islink(src):
        return _copy_symlink(src, dst)

    for _attempt in range(2):
        tmp = ""
        rfd = wfd = None
        success = False
        dst_side = False
        link_key = None
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

            if not stat.S_ISREG(st.st_mode):
                return "skip", tr("Not a regular file (device, socket or pipe)"), 0

            dst_st = _lstat_or_none(dst)

            if _attempt == 0 and dst_st is not None and _content_matches(src, dst, st, dst_st):
                pending = _meta_pending(dst, st, dst_st)
                if not pending:
                    return "skip", tr("Up to date"), st.st_size
                verdict = _sync_meta(dst, st, dst_st, pending)
                if verdict == _META_FIXED:
                    return "ok", dst, 0
                if verdict == _META_SAME:
                    return "skip", tr("Up to date"), st.st_size

            if not _ensure_dir(os.path.dirname(dst)):
                return "error", tr("Directory could not be created"), 0

            if (dst_st is not None and stat.S_ISDIR(dst_st.st_mode)
                    and not _clear_conflicting_dir(dst)):
                return "error", tr("Destination is a non-empty directory and could not be replaced"), 0

            tmp = _tmp_path(dst, ".part")

            if st.st_nlink > 1:
                link_key = (st.st_dev, st.st_ino)
                first = _hardlink_lookup(link_key)
                if first and first != dst and _link_existing(first, dst, tmp, st.st_size):
                    success = True
                    _fsync_parent_dir(dst)
                    return "ok", dst, st.st_size

            _may_use_noatime = _EUID == 0 or st.st_uid == _EUID
            try:
                rfd = os.open(src, os.O_RDONLY | (_O_NOATIME if _may_use_noatime else 0))
            except OSError:
                rfd = os.open(src, os.O_RDONLY)

            try:
                opened_st = os.fstat(rfd)
            except OSError:
                opened_st = None
            if (opened_st is not None and stat.S_ISREG(opened_st.st_mode)
                    and (opened_st.st_size != st.st_size or opened_st.st_mtime_ns != st.st_mtime_ns)):
                st = opened_st

            dst_side = True
            tmp_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            try:
                wfd = os.open(tmp, tmp_flags, st.st_mode & 0o777)
            except FileExistsError:
                _silent_unlink(tmp)
                wfd = os.open(tmp, tmp_flags, st.st_mode & 0o777)

            if _EUID == 0:
                try:
                    os.fchown(wfd, st.st_uid, st.st_gid)
                except OSError as e:
                    logger.debug("Could not fchown %s: %s", tmp, e)
                perm = st.st_mode & 0o7777
                need_chmod = True
            else:
                perm = st.st_mode & 0o777
                need_chmod = bool(perm & _UMASK)
            if need_chmod:
                try:
                    os.fchmod(wfd, perm)
                except OSError as e:
                    logger.debug("Could not fchmod %s: %s", tmp, e)

            if st.st_size >= _SMALL_FILE_MAX:
                try:
                    os.ftruncate(wfd, st.st_size)
                except OSError:
                    pass
                try:
                    os.posix_fadvise(rfd, 0, st.st_size, os.POSIX_FADV_SEQUENTIAL)
                    os.posix_fadvise(wfd, 0, st.st_size, os.POSIX_FADV_SEQUENTIAL)
                except OSError:
                    pass

            if _is_sparse(st):
                copied, copy_err = _copy_sparse_or_dense(rfd, wfd, st.st_size, cancel)
            else:
                copied, copy_err = _copy_loop(rfd, wfd, st.st_size, cancel)
            if copied < st.st_size:
                if copy_err is not None and copy_err.errno in _FATAL_IO_ERRNOS:
                    raise copy_err
                reason = f": {copy_err}" if copy_err is not None else ""
                raise _IncompleteCopyError(
                    f"Incomplete copy: {copied}/{st.st_size} bytes written{reason}"
                )

            _copy_xattrs(rfd, wfd)

            try:
                os.utime(wfd, ns=(st.st_atime_ns, st.st_mtime_ns))
            except OSError as e:
                logger.debug("Could not preserve timestamps for %s: %s", tmp, e)

            if copied >= _SMALL_FILE_MAX:
                _fsync_data(wfd, tmp)
            if copied >= _DROP_CACHE_MIN:
                try:
                    os.posix_fadvise(rfd, 0, 0, os.POSIX_FADV_DONTNEED)
                    os.posix_fadvise(wfd, 0, 0, os.POSIX_FADV_DONTNEED)
                except OSError:
                    pass

            closing_fd, wfd = wfd, None
            os.close(closing_fd)

            os.replace(tmp, dst)
            success = True
            if link_key is not None:
                _hardlink_register(link_key, dst)
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
            if exc.errno == errno.EACCES:
                logger.error("copy %s → %s: permission denied (%s)", src, dst, "destination" if dst_side else "source")
                if dst_side:
                    return "error", tr("Permission denied — destination not writable"), 0
                return "error", tr("Permission denied — file not readable"), 0
            if exc.errno == errno.ENOENT and not os.path.lexists(src):
                logger.warning("copy %s: source vanished during backup", src)
                return "skip", tr("Source file vanished during backup"), 0
            if exc.errno == errno.EIO:
                logger.error("copy %s → %s: I/O error", src, dst)
                return "error", tr("I/O error — check the source or destination device"), 0
            logger.error("copy %s → %s: %s", src, dst, exc)
            return "error", str(exc), 0
        finally:
            _close_quiet(rfd)
            _close_quiet(wfd)
            if tmp and not success:
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

    def error_count(self, title: str) -> int:
        with self._lock:
            c = self._counts.get(title)
            return c[2] if c else 0


class _Flusher:
    __slots__ = ("_de", "_er", "_flush_thresh", "_last_flush_t", "_lock", "_meter", "_ok", "_signal", "_sk")

    def __init__(self, signal, meter: ProgressMeter, flush_thresh: int = _FLUSH_THRESH) -> None:
        self._signal       = signal
        self._meter        = meter
        self._flush_thresh = flush_thresh
        self._lock         = threading.Lock()
        self._ok: list     = []
        self._sk: list     = []
        self._er: list     = []
        self._de: list     = []
        self._last_flush_t = time.monotonic()

    def counts(self) -> tuple[int, int, int, int]:
        return self._meter.counts()

    def set_flush_thresh(self, thresh: int) -> None:
        with self._lock: self._flush_thresh = thresh

    def push(self, ok=(), sk=(), er=(), de=(), *, work: "int | None" = None,
             release_work: int = 0, release_size: int = 0, force: bool = False) -> None:
        if ok or sk or er or de or release_work or release_size:
            if work is None:
                work = COST_FILE * (len(ok) + len(sk) + len(er))
            self._meter.record(ok, sk, er, de, work=work, release_work=release_work, release_size=release_size)
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
            self._last_flush_t = now
        self._signal.emit(payload_ok, payload_sk, payload_er, payload_de)

    def flush(self) -> None:
        self.push(force=True)


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
    __slots__ = ("_cap", "_cur", "_flusher", "_meter", "_tracker", "er", "ok", "partial", "pending", "sk", "tc", "work")

    def __init__(self, flusher: "_Flusher", tracker: "_EntryTracker", meter: ProgressMeter) -> None:
        self._flusher = flusher
        self._tracker = tracker
        self._meter = meter
        self.ok: list = []
        self.sk: list = []
        self.er: list = []
        self.tc: dict = {}
        self.pending = 0
        self.work = 0
        self.partial = 0
        self._cap = 0
        self._cur = 0

    def stream(self, n: int) -> None:
        room = self._cap - self._cur
        if room <= 0:
            return
        if n > room:
            n = room
        self._cur += n
        self.partial += n
        self._meter.add_inflight(n, n)

    def record(self, entry, cancel: threading.Event) -> None:
        size = _entry_size(entry[3])
        self._cap = size
        self._cur = 0
        _do_copy(entry, cancel, self.ok, self.sk, self.er, self.tc)
        self.work += file_cost(size)
        self.pending += 1

    def record_skip(self, src: str, size: int, title: str, reason: str) -> None:
        self.sk.append((src, reason, size))
        if title:
            _bump_count(self.tc, title, 1)
        self.work += COST_SKIP
        self.pending += 1

    def flush(self) -> None:
        if self.ok or self.sk or self.er:
            self._flusher.push(ok=self.ok, sk=self.sk, er=self.er, work=self.work,
                               release_work=self.partial, release_size=self.partial)
            self.ok.clear()
            self.sk.clear()
            self.er.clear()
            self.work = 0
            self.partial = 0
        if self.tc:
            self._tracker.batch_update(self.tc)
            self.tc.clear()
        self.pending = 0


class _ConfirmRequest:
    __slots__ = ("answer", "event")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.answer = False


class CopyWorker(QThread):
    batch_update = pyqtSignal(object, object, object, object)
    finished_work = pyqtSignal(int, int, int, int, bool)
    entry_status = pyqtSignal(str, int, int, int, int)
    prepared = pyqtSignal(object, object)
    confirm_delete = pyqtSignal(str, list, object)
    space_warnings = pyqtSignal(list)
    _RSYNC_PROGRESS_RE = re.compile(
        r"^\s*([\d,.\u202f\u00a0 ]+?)\s+(\d+)%\s+([\d.,]+\w+/s)\s+([\d:]+)"
    )

    def __init__(self, tasks, *, prepare: bool = False, is_restore: bool = False,
                 interactive: bool = True) -> None:
        super().__init__()
        self.meter = ProgressMeter()
        self._cancel = threading.Event()
        self._prepare_raw: list | None = list(tasks) if prepare else None
        self._is_restore = is_restore
        self._interactive = interactive
        self._hooks: dict[str, tuple[list, list]] = {}
        self._mirror_pairs: set[tuple[str, str]] = set()
        self.tasks: list[tuple[str, str, str, frozenset]] = []
        self._pre_fired_titles: set[str] = set()
        self._post_fired_titles: set[str] = set()
        self._started_titles: set[str] = set()
        self._pre_hooks_done = False
        self._version_plans: list[VersionPlan] = []
        self._hook_failures: list[str] = []
        self._finish_args: tuple[int, int, int, int, bool] | None = None
        self._sync_failed = False
        if not prepare:
            self._configure(tasks)

    def _emit_finished(self, copied: int, skipped: int, errors: int, deleted: int, cancelled: bool) -> None:
        if self._finish_args is None:
            self._finish_args = (copied, skipped, errors, deleted, cancelled)

    def _configure(self, tasks) -> None:
        self._hooks = self._extract_hooks(tasks)
        self._mirror_pairs = self._extract_mirror_pairs(tasks)
        self.tasks = self._normalize_tasks(tasks)

    @staticmethod
    def _norm_path(path: str) -> str:
        return path if (is_smb(path) or is_ssh(path)) else os.path.abspath(os.path.expanduser(path))

    @classmethod
    def _iter_pairs(cls, task):
        src_raw, dst_raw = task[0], task[1]
        srcs = [src_raw] if isinstance(src_raw, str) else src_raw
        dsts = [dst_raw] if isinstance(dst_raw, str) else dst_raw
        if not srcs or not dsts or len(srcs) != len(dsts):
            return
        for s, d in zip(srcs, dsts, strict=True):
            if s and d:
                s_str, d_str = str(s), str(d)
                yield s_str, d_str, cls._norm_path(s_str), cls._norm_path(d_str)

    @classmethod
    def _extract_mirror_pairs(cls, tasks) -> set[tuple[str, str]]:
        result: set[tuple[str, str]] = set()
        for t in tasks:
            if not isinstance(t, (list, tuple)) or len(t) < 7 or t[6] is not True:
                continue
            for _s, _d, s_norm, d_norm in cls._iter_pairs(t):
                result.add((s_norm, d_norm))
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
    def _dst_inside_src_guard(src: str, dst: str) -> frozenset:
        if is_smb(src) or is_ssh(src) or is_smb(dst) or is_ssh(dst):
            return frozenset()
        try:
            if not os.path.isdir(src):
                return frozenset()
            real_src = os.path.realpath(src)
            real_dst = os.path.realpath(dst)
            if real_dst == real_src or os.path.commonpath([real_src, real_dst]) != real_src:
                return frozenset()
        except (OSError, ValueError):
            return frozenset()
        logger.warning("destination %r lies inside source %r — it is excluded from the scan", dst, src)
        rel = os.path.relpath(real_dst, real_src)
        return frozenset({dst, os.path.join(src, rel)})

    @classmethod
    def _normalize_tasks(cls, tasks) -> list[tuple[str, str, str, frozenset]]:
        result = []
        for t in tasks:
            if not isinstance(t, (list, tuple)) or len(t) < 2:
                continue
            title = str(t[2]) if len(t) > 2 else ""
            raw_excl = t[3] if len(t) > 3 else {}
            for _s_str, _d_str, s_norm, d_norm in cls._iter_pairs(t):
                if isinstance(raw_excl, (set, frozenset)):
                    exc_set: frozenset = frozenset(raw_excl)
                elif isinstance(raw_excl, dict):
                    names: list = raw_excl.get(s_norm) or raw_excl.get(_s_str) or []
                    exc_set = frozenset(os.path.join(s_norm, n) for n in names)
                else:
                    exc_set = frozenset()
                guard = cls._dst_inside_src_guard(s_norm, d_norm)
                if guard:
                    exc_set = frozenset(exc_set | guard)
                result.append((s_norm, d_norm, title, exc_set))
        return result

    @staticmethod
    def _guard_unmounted(tasks: list) -> "tuple[list, list[tuple[str, str, str]]]":
        kept: list = []
        errors: list[tuple[str, str, str]] = []
        mounts = None
        for t in tasks:
            if not isinstance(t, (list, tuple)) or len(t) < 2:
                kept.append(t)
                continue
            srcs = [t[0]] if isinstance(t[0], str) else list(t[0])
            dsts = [t[1]] if isinstance(t[1], str) else list(t[1])
            title = str(t[2]) if len(t) > 2 else ""
            if len(srcs) != len(dsts):
                kept.append(t)
                continue
            if mounts is None:
                mounts = get_mounts(max_age=0.0)
            ok_s: list = []
            ok_d: list = []
            for s_, d_ in zip(srcs, dsts, strict=True):
                drive = unmounted_drive_for_path(str(d_), mounts) if d_ else None
                if drive:
                    errors.append((str(d_), tr("Destination drive '{drive}' is not mounted — entry skipped",
                                               drive=drive), title))
                    continue
                ok_s.append(s_)
                ok_d.append(d_)
            if not ok_s:
                continue
            if len(ok_s) == len(srcs):
                kept.append(t)
                continue
            kept.append((ok_s, ok_d, *t[2:]))
        return kept, errors

    def cancel(self) -> None:
        self._cancel.set()
        with _smb_procs_lock:
            for proc in list(_smb_procs.values()):
                try:
                    proc.kill()
                except OSError:
                    pass

    @staticmethod
    def _fire_hooks(entry_title: str, phase: str, hooks: list[str], abort: bool,
                    cancel: "threading.Event | None" = None) -> bool:
        if not hooks:
            return True
        ok, errors = _run_hooks(hooks, abort_on_error=abort, label=f"{entry_title}/{phase}", cancel=cancel)
        if errors:
            for err in errors:
                logger.error("Hook error [%s]: %s", entry_title, err)
        return ok or not abort

    def _run_pre_hooks(self, tasks: list) -> set[str]:
        skip_titles: set[str] = set()
        seen: set[str] = set()
        for task in tasks:
            if self._cancel.is_set():
                break
            _t = str(task[2]) if len(task) > 2 else ""
            if _t and _t not in seen:
                seen.add(_t)
                _pre, _ = self._hooks.get(_t, ([], []))
                if _pre:
                    self.meter.set_step(STEP_HOOKS)
                    self._pre_fired_titles.add(_t)
                    if not self._fire_hooks(_t, "pre", _pre, abort=True, cancel=self._cancel):
                        skip_titles.add(_t)
                        if not self._cancel.is_set():
                            self._hook_failures.append(_t)
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
            self.meter.set_step(STEP_HOOKS)
            self._fire_hooks(title, "post", _post, abort=False)

    def _run_pending_post_hooks(self) -> None:
        pending = (self._pre_fired_titles | self._started_titles) - self._post_fired_titles
        for title in sorted(pending):
            self._run_post_hooks_for_title(title)

    def run(self) -> None:
        _reset_dir_caches()
        failure: Exception | None = None
        try:
            if self._prepare_raw is not None and not self._prepare():
                return
            self._run_impl()
        except Exception as exc:
            failure = exc
            logger.error("CopyWorker aborted: %s", exc, exc_info=True)
        finally:
            try:
                self._finalize_versions(None, None)
            except Exception as exc:
                logger.error("Finalizing versioned archives failed: %s", exc, exc_info=True)
            try:
                self._run_pending_post_hooks()
            except Exception as exc:
                logger.error("Post-hooks failed: %s", exc, exc_info=True)
            finish_args = self._finish_args
            if finish_args is None:
                if failure is not None:
                    self.prepared.emit([], [DeleteError(path=tr("Preparing…"), title="", reason=str(failure))])
                finish_args = (0, 0, 0, 0, self._cancel.is_set())
            self.finished_work.emit(*finish_args)

    def _ask_confirm(self, title: str, paths: list[str]) -> bool:
        request = _ConfirmRequest()
        previous = self.meter.current_step()
        self.meter.set_step(STEP_CONFIRM)
        try:
            self.confirm_delete.emit(title, paths, request)
            while not request.event.wait(0.2):
                if self._cancel.is_set():
                    return False
            return request.answer
        finally:
            self.meter.set_step(previous)

    def _prepare(self) -> bool:
        raw, self._prepare_raw = self._prepare_raw or [], None
        self.meter.set_step(STEP_PREPARE)
        try:
            self._hooks = self._extract_hooks(raw)
            skip_titles = self._run_pre_hooks(raw) if not self._cancel.is_set() else set()
            self._pre_hooks_done = True
            self.meter.set_step(STEP_PREPARE)
            if skip_titles:
                raw = [t for t in raw if (str(t[2]) if len(t) > 2 else "") not in skip_titles]
            raw, guard_errors = self._guard_unmounted(raw)
        except Exception as exc:
            logger.error("Pre-hooks failed: %s", exc, exc_info=True)
            self.prepared.emit([], [DeleteError(path=tr("Preparing…"), title="", reason=str(exc))])
            self._emit_finished(0, 0, 0, 0, False)
            return False
        if self._cancel.is_set():
            self._emit_finished(0, 0, 0, 0, True)
            return False
        try:
            result: AdvancedOptionsResult = apply_advanced_options(
                raw,
                confirm=self._ask_confirm if self._interactive else None,
                is_restore=self._is_restore,
                cancel=self._cancel,
                progress=self.meter.step_progress,
            )
        except Exception as exc:
            logger.error("Advanced options failed: %s", exc, exc_info=True)
            self.prepared.emit([], [DeleteError(path=tr("Preparing…"), title="", reason=str(exc))])
            self._emit_finished(0, 0, 0, 0, False)
            return False
        self._version_plans = list(result.version_plans)
        if self._version_plans:
            self.meter.reserve_tail()
        hook_errors = [DeleteError(path=t, title=t, reason=tr("Pre-hook failed — entry skipped"))
                       for t in self._hook_failures]
        hook_errors += [DeleteError(path=p, title=t, reason=m) for p, m, t in guard_errors]
        self._hook_failures.clear()
        self.prepared.emit(result.deleted, hook_errors + list(result.errors))
        if self._cancel.is_set():
            self._finalize_versions(None, None)
            self._emit_finished(0, 0, 0, 0, True)
            return False
        hooks = self._hooks
        self._configure(result.tasks)
        self._hooks = hooks
        return True

    def _finalize_versions(self, flusher: "_Flusher | None", tracker: "_EntryTracker | None") -> None:
        plans, self._version_plans = self._version_plans, []
        if not plans:
            return
        aborted = self._cancel.is_set() or self._sync_failed
        removed: list = []
        for plan in plans:
            if aborted or tracker is None:
                ok = False
            elif plan.title:
                ok = tracker.error_count(plan.title) == 0
            else:
                ok = flusher is not None and flusher.counts()[2] == 0
            deleted, errors = finalize_version(plan, ok, self._cancel, self.meter.step_progress)
            removed.extend((d.path, d.reason, d.size) for d in deleted)
            for err in errors:
                logger.warning("Versioned archive [%s]: %s: %s", plan.title, err.path, err.reason)
        if removed and flusher is not None:
            flusher.push(de=removed, force=True)

    def _sync_local_destinations(self, flusher: "_Flusher") -> None:
        dests = sorted({d for _s, d, _t, _x in self.tasks if not is_smb(d) and not is_ssh(d)})
        if not dests:
            return
        self.meter.set_step(STEP_SYNC)
        for path, err in sync_destinations(dests):
            logger.error("syncfs %s: %s", path, err)
            self._sync_failed = True
            flusher.push(er=[(path, tr("Could not flush data to the destination device: {err}", err=err), 0)],
                         force=True)

    def _run_impl(self) -> None:
        pw: _SecurePw | None = None
        meter = self.meter
        flusher = _Flusher(self.batch_update, meter)
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

            space_warnings = _check_destination_space(self.tasks)
            if space_warnings:
                self.space_warnings.emit(space_warnings)

            all_tasks = local_tasks + ssh_tasks + smb_tasks
            skip_titles = (self._run_pre_hooks(all_tasks)
                           if not self._pre_hooks_done and not self._cancel.is_set() else set())
            if skip_titles:
                local_tasks = [x for x in local_tasks if x[2] not in skip_titles]
                ssh_tasks   = [x for x in ssh_tasks   if x[2] not in skip_titles]
                smb_tasks   = [x for x in smb_tasks   if x[2] not in skip_titles]
                all_tasks   = local_tasks + ssh_tasks + smb_tasks

            self._started_titles = {t for _s, _d, t, _x in all_tasks if t}

            meter.set_step(STEP_SCAN)

            if self._hook_failures:
                failed = list(self._hook_failures)
                meter.plan("hooks", len(failed), len(failed) * COST_FILE)
                for failed_title in failed:
                    flusher.push(er=[(failed_title, tr("Pre-hook failed — entry skipped"), 0)], force=True)
            self._hook_failures.clear()

            if not all_tasks:
                meter.finish_scan()
                self._emit_finished(0, 0, 0, 0, self._cancel.is_set())
                return

            ssh_units    = len(ssh_tasks)
            scan_lock    = threading.Lock()
            scan_pending = [(1 if local_tasks else 0) + (1 if smb_tasks else 0) + (1 if ssh_tasks else 0)]
            scan_emitted = [False]

            if ssh_units:
                meter.plan("ssh", ssh_units, ssh_units * COST_REMOTE_TASK)

            def _rescale() -> None:
                flusher.set_flush_thresh(_scale_params(meter.files_total())[1])

            def _on_local_plan(files: int, work: int, size: int) -> None:
                meter.plan("local", files, work, size)
                _rescale()

            def _on_smb_found(n: int) -> None:
                meter.plan("smb", n, n * COST_FILE)
                _rescale()

            def _scan_phase_done() -> None:
                with scan_lock:
                    scan_pending[0] -= 1
                    ready = scan_pending[0] <= 0 and not scan_emitted[0]
                    if ready:
                        scan_emitted[0] = True
                if ready:
                    _rescale()
                    if not self._cancel.is_set():
                        meter.finish_scan()

            def _phase_marker() -> "Callable[[], None]":
                fired = [False]

                def _mark() -> None:
                    if fired[0]:
                        return
                    fired[0] = True
                    _scan_phase_done()

                return _mark

            local_mark = _phase_marker()
            smb_mark = _phase_marker()

            def _local_pipeline() -> None:
                try:
                    if not self._cancel.is_set():
                        self._scan_copy_local_pipelined(
                            local_tasks, flusher, tracker,
                            on_plan_change=_on_local_plan,
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
                        flusher, tracker, _on_smb_found, smb_mark,
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
                _rescale()
                meter.finish_scan()

            flusher.flush()
            tracker.emit_final()
            if self._version_plans:
                self._sync_local_destinations(flusher)
            self._finalize_versions(flusher, tracker)
            self._sync_local_destinations(flusher)
            self._run_post_hooks(all_tasks)
            flusher.flush()
            tracker.emit_final()
            self._emit_finished(*flusher.counts(), self._cancel.is_set())
        except Exception as exc:
            logger.error("CopyWorker critical: %s", exc, exc_info=True)
            try:
                flusher.flush()
                tracker.emit_final()
                self._finalize_versions(flusher, tracker)
                self._sync_local_destinations(flusher)
                flusher.flush()
            except Exception as e:
                logger.debug("Error during fallback cleanup: %s", e)
            self._emit_finished(*flusher.counts(), self._cancel.is_set())
        finally:
            if pw is not None:
                pw.clear()

    def _run_smb_pipeline(self, smb_tasks: list, user: str, pw: "_SecurePw | None",
                          smb_tool_missing: bool, flusher: "_Flusher", tracker: "_EntryTracker",
                          on_found, scan_phase_done) -> None:
        if self._cancel.is_set():
            scan_phase_done()
            return

        if smb_tool_missing:
            msg = tr("'smbclient' not found — install the Samba client tools (e.g. package "
                     "'smbclient' / 'samba-client') to enable SMB backups")
            errors = [(s_ if is_smb(s_) else d_, msg, t_) for s_, d_, t_, *_ in smb_tasks]
            on_found(len(errors))
            scan_phase_done()
            _push_errors(flusher, tracker, errors)
            return

        unreachable, auth_failed, guest, no_share = self._probe_shares(smb_tasks, user, pw)
        dead = unreachable | auth_failed | no_share
        alive_tasks = smb_tasks
        smb_errors: list[tuple[str, str, str]] = []
        if dead:
            alive_tasks, pre_err = self._filter_dead_tasks(smb_tasks, dead, unreachable, no_share)
            smb_errors.extend(pre_err)

        expanded: list[_SmbJob] = []
        scan_errors: list = []
        if alive_tasks and not self._cancel.is_set():
            scanner = _SmbScanner(user, pw, guest, self._cancel, on_found)
            expanded, scan_errors = scanner.resolve(alive_tasks)
        smb_errors.extend(scan_errors)

        on_found(len(expanded) + len(smb_errors))
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

        rsync_src = _rsync_src_arg(src, self._cancel)
        excludes_raw = list(extra[0]) if extra and extra[0] else None
        excludes = _rsync_excludes(rsync_src, excludes_raw)
        mirror = (src, dst) in self._mirror_pairs

        if mirror:
            blocked = self._mirror_guard(src, rsync_src)
            if blocked is not None:
                logger.warning("mirror skipped for '%s': %s", src, blocked)
                flusher.push(er=[(src, blocked, 0)], work=COST_REMOTE_TASK)
                _track(0, 0, 1)
                return

        if shutil.which("rsync") is None:
            logger.error("rsync binary not found — remote task '%s' reported as error", title or src)
            flusher.push(er=[(src, tr("'rsync' not found — install it (package 'rsync') to enable SSH backups"), 0)],
                         work=COST_REMOTE_TASK)
            _track(0, 0, 1)
            return

        cmd = build_rsync_cmd(rsync_src, dst, exclude=excludes, delete=mirror)
        logger.debug("_copy_ssh_tasks: %s", " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=frozen_safe_env(),
            )
        except OSError as exc:
            logger.error("rsync launch failed for '%s': %s", src, exc)
            flusher.push(er=[(src, str(exc), 0)], work=COST_REMOTE_TASK)
            _track(0, 0, 1)
            return

        if proc.stdout is None:
            logger.error("rsync stdout is None for '%s'", src)
            proc.kill()
            proc.wait()
            flusher.push(er=[(src, tr("rsync stdout unavailable"), 0)], work=COST_REMOTE_TASK)
            _track(0, 0, 1)
            return

        last_pct = 0
        credit = 0
        deleted_this_task: list = []
        error_lines: collections.deque[str] = collections.deque(maxlen=_RSYNC_ERROR_TAIL)
        tail_lines: collections.deque[str] = collections.deque(maxlen=_RSYNC_ERROR_TAIL)
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
                            target = COST_REMOTE_TASK * min(pct, 99) // 100
                            if target > credit:
                                self.meter.add_inflight(target - credit)
                                credit = target
                        continue
                    if mirror and (dm := _RSYNC_DELETE_RE.match(line)):
                        rel = dm.group(1).strip()
                        deleted_this_task.append((_ssh_join(dst, rel), tr("Mirror delete (remote)"), 0))
                    else:
                        logger.debug("rsync: %s", line)
                        tail_lines.append(line)
                        if _RSYNC_ERR_HINT_RE.search(line):
                            error_lines.append(line)

            except OSError as exc:
                logger.warning("rsync read error for '%s': %s", src, exc)

            proc.wait()
        finally:
            with _smb_procs_lock:
                _smb_procs.pop(_tid, None)
            try:
                proc.stdout.close()
            except OSError:
                pass

        if deleted_this_task:
            flusher.push(de=deleted_this_task, force=True)
        if self._cancel.is_set():
            if deleted_this_task:
                _track(0, 0, 0, len(deleted_this_task))
            return

        if proc.returncode in (0, 24):
            if proc.returncode == 24:
                logger.warning("rsync: some source files vanished during transfer (exit 24): %s", src)
            flusher.push(ok=[(src, dst, 0)], work=COST_REMOTE_TASK, release_work=credit)
            _track(1, 0, 0, len(deleted_this_task))
            logger.info("rsync OK: %s → %s", src, dst)
        else:
            detail = "; ".join(error_lines or tail_lines) or None
            msg = (tr("rsync exit {code}: {detail}", code=proc.returncode, detail=detail)
                   if detail else tr("rsync exit {code}", code=proc.returncode))
            flusher.push(er=[(src, msg, 0)], work=COST_REMOTE_TASK, release_work=credit)
            _track(0, 0, 1, len(deleted_this_task))
            logger.error("rsync exit %d: %s → %s (%s)", proc.returncode, src, dst, detail or "no output captured")


    def _mirror_guard(self, src: str, rsync_src: str) -> "str | None":
        empty_msg = tr("Source directory is empty — mirror deletion skipped to protect the destination")
        if is_ssh(src):
            if rsync_src.endswith("/") and _ssh_dir_is_empty(rsync_src, self._cancel):
                return empty_msg
            return None
        try:
            if os.path.isdir(src):
                with os.scandir(src) as it:
                    if next(it, None) is None:
                        return empty_msg
        except OSError as exc:
            return str(exc)
        return None

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
                                    on_plan_change: "Callable[[int, int, int], None] | None" = None,
                                    on_scan_done: "Callable[[], None] | None" = None) -> int:

        cancel = self._cancel
        meter = self.meter
        if not tasks:
            if on_scan_done is not None:
                on_scan_done()
            return 0

        up_to_date = tr("Up to date")
        pipe_q: queue.Queue = queue.Queue(maxsize=_PIPE_MAXSIZE)
        scan_done = threading.Event()
        work_q: queue.SimpleQueue = queue.SimpleQueue()
        pend_lock = threading.Lock()
        pending = [0]
        dir_done = threading.Event()
        last_emit = [0.0]
        found = [0, 0, 0]
        missing = [0]
        copy_params = [_LOCAL_BATCH, _SCAN_PIPE_BATCH]
        copy_params_lock = threading.Lock()
        dir_attrs: list = []
        dir_attrs_lock = threading.Lock()

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
        guard_errors: list[tuple[str, str, str]] = []
        mounts = get_mounts(max_age=0.0)
        for src, dst, title, *rest in tasks:
            excludes = rest[0] if rest else frozenset()
            drive = unmounted_drive_for_path(dst, mounts)
            if drive:
                guard_errors.append((dst, tr("Destination drive '{drive}' is not mounted — entry skipped",
                                             drive=drive), title))
                missing[0] += 1
                continue
            if not os.path.exists(src):
                missing_entries.append((src, _not_found_reason(title), 0))
                if title:
                    missing_counts.setdefault(title, [0, 0, 0, 0])[1] += 1
                missing[0] += 1
            else:
                _eq((src, dst, title, excludes, None))

        if missing[0] and on_plan_change is not None:
            on_plan_change(missing[0], missing[0] * COST_FILE, 0)
        if missing_entries:
            flusher.push(sk=missing_entries)
            tracker.batch_update(missing_counts)
        if guard_errors:
            _push_errors(flusher, tracker, guard_errors)

        with pend_lock:
            if pending[0] == 0:
                dir_done.set()

        def _put_batch(batch: list) -> bool:
            while not cancel.is_set():
                try:
                    pipe_q.put(batch, timeout=0.25)
                    return True
                except queue.Full:
                    continue
            return False

        def _single_file_stat(path: str) -> "os.stat_result | None":
            try:
                if os.path.islink(path):
                    return None
                st = os.stat(path)
            except OSError:
                return None
            return st if stat.S_ISREG(st.st_mode) else None

        def _scan_loop(sbuf: "_BatchBuffer") -> None:
            local_n = 0
            local_work = 0
            local_size = 0
            batch: list = []
            batch_work = 0
            batch_size = 0
            last_sfl = time.monotonic()
            while not cancel.is_set():
                try:
                    _src, _dst, _title, _excl, _dir_st = work_q.get(timeout=0.1)
                except queue.Empty:
                    if dir_done.is_set():
                        break
                    continue

                with copy_params_lock:
                    lb_now, spb = copy_params
                try:
                    if self._prepare_dst_dir(_src, _dst, _dir_st, dir_attrs, dir_attrs_lock) is None:
                        _push_errors(flusher, tracker, [(_dst, tr("Directory could not be created"), _title)])
                        local_n += 1
                        local_work += COST_FILE
                        continue
                    for is_dir, path, dst_path, entry_stat in _scan_dir_entries(_src, _dst, _excl, cancel):
                        if is_dir:
                            _eq((path, dst_path, _title, _excl, entry_stat))
                        elif (entry_stat is not None and entry_stat is not True
                              and _unchanged(path, dst_path, entry_stat)):
                            size = entry_stat.st_size
                            sbuf.record_skip(path, size, _title, up_to_date)
                            local_n += 1
                            local_work += COST_SKIP
                            local_size += size
                            if sbuf.pending >= lb_now:
                                sbuf.flush()
                                last_sfl = time.monotonic()
                        else:
                            size = _entry_size(entry_stat)
                            cost = file_cost(size)
                            batch.append((path, dst_path, _title, entry_stat))
                            batch_work += cost
                            batch_size += size
                            local_n += 1
                            local_work += cost
                            local_size += size
                            if len(batch) >= spb:
                                if not _put_batch(batch):
                                    local_n -= len(batch)
                                    local_work -= batch_work
                                    local_size -= batch_size
                                batch = []
                                batch_work = 0
                                batch_size = 0
                except NotADirectoryError:
                    single_st = _single_file_stat(_src)
                    size = _entry_size(single_st)
                    cost = file_cost(size)
                    batch.append((_src, _dst, _title, single_st))
                    batch_work += cost
                    batch_size += size
                    local_n += 1
                    local_work += cost
                    local_size += size
                except FileNotFoundError:
                    pass
                except PermissionError:
                    logger.warning("scan %s: permission denied", _src)
                    _push_errors(flusher, tracker, [(_src, tr("Permission denied — directory not readable"), _title)])
                    local_n += 1
                    local_work += COST_FILE
                except OSError as exc:
                    logger.warning("scan %s: %s", _src, exc)
                    _push_errors(flusher, tracker, [(_src, str(exc), _title)])
                    local_n += 1
                    local_work += COST_FILE
                finally:
                    if sbuf.pending and time.monotonic() - last_sfl >= _FLUSH_INTERVAL:
                        sbuf.flush()
                        last_sfl = time.monotonic()
                    try:
                        emit = False
                        now = time.monotonic()
                        with pend_lock:
                            if now - last_emit[0] >= _SCAN_EMIT_SECS:
                                found[0] += local_n
                                found[1] += local_work
                                found[2] += local_size
                                local_n = 0
                                local_work = 0
                                local_size = 0
                                last_emit[0] = now
                                emit_files, emit_work, emit_size = found
                                emit = True
                        if emit:
                            cur_files = emit_files + missing[0]
                            lb_new, ft_new, spb_new = _scale_params(cur_files)
                            with copy_params_lock:
                                copy_params[0] = lb_new
                                copy_params[1] = spb_new
                            flusher.set_flush_thresh(ft_new)
                            if on_plan_change is not None:
                                on_plan_change(cur_files, emit_work + missing[0] * COST_FILE, emit_size)
                    finally:
                        _dq()

            if batch and not cancel.is_set():
                if _put_batch(batch):
                    batch = []

            if batch:
                local_n -= len(batch)
                local_work -= batch_work
                local_size -= batch_size

            if local_n > 0 or local_work > 0:
                with pend_lock:
                    found[0] += local_n
                    found[1] += local_work
                    found[2] += local_size

        def _scan_worker() -> None:
            sbuf = _BatchBuffer(flusher, tracker, meter)
            try:
                _scan_loop(sbuf)
            finally:
                sbuf.flush()

        def _copy_worker() -> None:
            buf = _BatchBuffer(flusher, tracker, meter)
            set_stream_sink(buf.stream)
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
                        if scan_done.is_set():
                            try:
                                item = pipe_q.get_nowait()
                            except queue.Empty:
                                break
                        else:
                            if (time.monotonic() - last_fl_t) >= _FLUSH_INTERVAL:
                                _fl()
                            continue
                    if cancel.is_set():
                        continue
                    with copy_params_lock:
                        lb = copy_params[0]
                    for entry in item:
                        if cancel.is_set():
                            break
                        big = _entry_size(entry[3]) >= _FLUSH_AFTER_BYTES
                        if big and buf.pending:
                            _fl()
                        buf.record(entry, cancel)
                        _file_ctr += 1
                        if big or buf.pending >= lb or (
                            _file_ctr >= _TIME_CHECK_EVERY
                            and (time.monotonic() - last_fl_t) >= _FLUSH_INTERVAL
                        ):
                            _fl()
            finally:
                _fl()
                set_stream_sink(None)

        def _run_scan() -> None:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=scan_n) as sp:
                    futs = [sp.submit(_scan_worker) for _ in range(scan_n)]
                    _run_futures(futs, cancel, "scan worker")
                total_files = found[0] + missing[0]
                lb, ft, spb = _scale_params(total_files)
                with copy_params_lock:
                    copy_params[0] = lb
                    copy_params[1] = spb
                flusher.set_flush_thresh(ft)
                if on_plan_change is not None:
                    on_plan_change(total_files, found[1] + missing[0] * COST_FILE, found[2])
            finally:
                scan_done.set()
                if on_scan_done is not None:
                    on_scan_done()

        io_paths = [p for t in tasks for p in (t[0], t[1])]
        n_copy = _copy_workers_for(io_paths)
        scan_n = _scan_workers_for(io_paths)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1 + n_copy)
        try:
            all_futs = [pool.submit(_run_scan)] + [pool.submit(_copy_worker) for _ in range(n_copy)]
            _run_futures(all_futs, cancel, "pipeline worker")
        finally:
            pool.shutdown(wait=True, cancel_futures=False)
            _apply_dir_attrs(dir_attrs)

        return found[0] + missing[0]

    @staticmethod
    def _prepare_dst_dir(src: str, dst: str, dir_st: "os.stat_result | None",
                         attrs: list, lock: threading.Lock) -> "bool | None":
        if dir_st is None:
            if os.path.isdir(src):
                return True if _ensure_dir(dst) else None
            return True
        created = _make_new_dir(dst)
        if created is not None and stat.S_ISDIR(dir_st.st_mode):
            with lock:
                attrs.append((dst, dir_st.st_mode, dir_st.st_atime_ns, dir_st.st_mtime_ns, dir_st.st_uid, dir_st.st_gid))
        return None if created is None else True

    def _probe_shares(self, smb_tasks, user, pw) -> tuple[set, set, bool, set]:
        unreachable: set[tuple[str, str]] = set()
        auth_failed: set[tuple[str, str]] = set()
        no_share: set[tuple[str, str]] = set()
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
            return unreachable, auth_failed, guest, no_share

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
                elif result == "share":
                    no_share.add((_h, _sh))
                elif result == "guest":
                    guest = True

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(_SMB_WORKERS, len(shares))) as pool:
            futs = [pool.submit(probe_one, h, sh) for h, sh in shares]
            _run_futures(futs, self._cancel, "probe")

        return unreachable, auth_failed, guest, no_share

    @staticmethod
    def _filter_dead_tasks(smb_tasks: list,
                           dead_shares: "set | frozenset",
                           unreachable_shares: "set | frozenset",
                           missing_shares: "set | frozenset" = frozenset()) -> tuple[list, list]:
        alive: list = []
        errors: list = []
        for s, d, t, *rest in smb_tasks:
            excl = rest[0] if rest else frozenset()
            h, sh, _ = _parse_smb(s if is_smb(s) else d)
            if (h, sh) in dead_shares:
                if (h, sh) in unreachable_shares:
                    reason = "NT_STATUS_HOST_UNREACHABLE"
                elif (h, sh) in missing_shares:
                    reason = tr("Share not found")
                else:
                    reason = tr("Authentication failed")
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
