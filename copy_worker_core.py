import concurrent.futures
import os
import re
import shutil
import subprocess
import threading
from functools import lru_cache
from urllib.parse import urlparse

from drive_utils import is_smb, is_ssh
from state import logger
from themes import register_cache_invalidation_hook as _reg_cache_hook

_CHUNK           = 32 * 1024 * 1024
_IO_BUF          =  8 * 1024 * 1024
_WORKERS         = min(12, max(4, os.cpu_count() or 4))
_SMB_WORKERS     = 10
_SMB_TIMEOUT     = 10
_SMB_FILE_SECS   = 3
_SMB_CHUNK       = 1_000
_FLUSH_THRESH    = 2_500
_FLUSH_INTERVAL  = 0.3
_SCAN_EMIT_SECS  = 0.5
_SCAN_PIPE_BATCH = 128
_LOCAL_BATCH     = 256
_CLAIM_SIZE      = 32
_PIPE_MAXSIZE    = 1024
_MIN_FREE        = 500 * 1024 * 1024


def _scale_params(total: int) -> tuple[int, int, int, int, int]:
    w = _WORKERS
    if total >= 100_000: return 256, 2048, 20_000, 1024, w
    if total >=  50_000: return 128, 1024, 10_000,  512, w
    if total >=  10_000: return  64,  512,  5_000,  256, min(w, 8)
    if total >=   2_000: return  32,  256,  2_500,  128, min(w, 6)
    return _CLAIM_SIZE, _LOCAL_BATCH, _FLUSH_THRESH, _SCAN_PIPE_BATCH, min(w, 4)


_SKIP_RE = re.compile(
    r"^(?:"
    r"\.?lock|lockfile|\.lck|\.parentlock|Singleton\w*|"
    r"cache|Network\sCache|startupCache|jumpListCache|"
    r".*\.sqlite-wal|.*\.sqlite-shm|.*\.journal|.*[-_]journal|.*\.db-wal|.*\.db-shm|"
    r"idb|WebStorage|Session\sStorage|Local\sStorage|leveldb|.*\.ldb|"
    r"temp|tmp|.*\.tmp|.*\.bak|.*\.baklz4|recovery\.jsonlz4|recovery\.baklz4|sessionstore-backups|"
    r"Thumbs\.db|\.DS_Store|\.quota|\.user64|\.healthcheck|\.active-update|"
    r"GPUCache|ShaderCache|blob_storage|prefs\.js"
    r")$",
    re.I
)

_SMB_LINE_RE = re.compile(
    r"^(.+?)\s+([ADRHNSV]*)\s*(?:\(.*?\)\s*)?(\d+)"
    r"\s+\w{3}\s+\w{3}\s+[\s\d]\d\s+[\d:]+\s*\d*$"
)

_SMB_DOWN_RE = re.compile(
    r"HOST IS DOWN|NT_STATUS_HOST_UNREACHABLE|NT_STATUS_IO_TIMEOUT|"
    r"NT_STATUS_CONNECTION_REFUSED|NT_STATUS_NETWORK_UNREACHABLE|"
    r"NT_STATUS_CONNECTION_RESET|NT_STATUS_CONNECTION_DISCONNECTED|"
    r"CONNECTION REFUSED|NO ROUTE TO HOST|NETWORK IS UNREACHABLE|"
    r"CONNECTION TIMED OUT|TIMEOUT",
    re.I,
)


_NOTIFY_SEND: str | None = shutil.which("notify-send")


def _notify(title: str, body: str, urgency: str = "normal") -> None:
    if not _NOTIFY_SEND:
        return
    try:
        subprocess.Popen([_NOTIFY_SEND, f"--urgency={urgency}", "--expire-time=0", "--app-name=Backup Helper",
                          "--icon=drive-harddisk", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def _check_destination_space(tasks: list[tuple]) -> list[str]:
    checked: set[str] = set()
    warnings: list[str] = []
    for _src, dst_raw, _title, *_ in tasks:
        dsts = dst_raw if isinstance(dst_raw, list) else (dst_raw,)
        for dst in dsts:
            dst = str(dst).strip()
            if not dst or is_smb(dst) or is_ssh(dst):
                continue
            dst = os.path.abspath(os.path.expanduser(dst))
            if dst in checked:
                continue
            checked.add(dst)
            probe = dst
            while probe and not os.path.exists(probe):
                parent = os.path.dirname(probe)
                probe = "" if parent == probe else parent
            if not probe:
                continue
            try:
                usage = shutil.disk_usage(probe)
                if usage.free < _MIN_FREE:
                    free_mb = usage.free // (1024 * 1024)
                    warnings.append(
                        f"• {dst!r}  —  only {free_mb:,} MB free"
                    )
            except OSError as exc:
                logger.debug("_check_destination_space: cannot check %r: %s", dst, exc)
    return warnings


_CACHE_MISS = object()
_O_NOATIME  = getattr(os, "O_NOATIME", 0)
_PID        = os.getpid()
_tls        = threading.local()
_TIME_CHECK_EVERY = 32
_seen_dirs_global: set[str] = set()
_seen_dirs_lock = threading.Lock()
_SHM_DIR: str | None = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None

_smb_procs: dict[int, subprocess.Popen] = {}
_smb_procs_lock = threading.Lock()


def _classify_entry(e: "os.DirEntry") -> "tuple[bool, bool] | None":
    try:
        is_symlink = e.is_symlink()
        is_dir_eff = (not is_symlink) and e.is_dir(follow_symlinks=False)
        is_file_eff = is_symlink or e.is_file(follow_symlinks=False)
        return is_dir_eff, is_file_eff
    except OSError:
        return None


def _scan_dir_entries(src: str, dst: str, excl: frozenset, cancel: threading.Event):
    with os.scandir(src) as it:
        for e in it:
            if cancel.is_set():
                break
            if e.path in excl or _SKIP_RE.search(e.name):
                continue
            cls = _classify_entry(e)
            if cls is None:
                continue
            is_dir_eff, is_file_eff = cls
            dst_path = os.path.join(dst, e.name)
            if is_dir_eff:
                yield True, e.path, dst_path, None
            elif is_file_eff:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    st = None
                yield False, e.path, dst_path, st


def _ensure_dir(path: str) -> bool:
    if not path:
        return True
    seen: set = _tls.__dict__.setdefault("seen_dirs", set())
    if path in seen:
        return True
    with _seen_dirs_lock:
        if path in _seen_dirs_global:
            seen.add(path)
            return True
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        logger.error("mkdir %s: %s", path, exc)
        return False
    with _seen_dirs_lock:
        _seen_dirs_global.add(path)
    seen.add(path)
    return True


@lru_cache(maxsize=256)
def _cached_mono_style(size: int, color: str, bold: bool = False, extra: str = "") -> str:
    s = f"font-family:monospace;font-size:{size}px;color:{color};"
    if bold:
        s += "font-weight:bold;"
    return s + extra


def _invalidate_copy_worker_caches() -> None: _cached_mono_style.cache_clear()

_reg_cache_hook(_invalidate_copy_worker_caches)
del _reg_cache_hook


_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")
def _format_unit(value: float) -> str:
    if value < 0:
        logger.debug("_format_unit: negative value %r", value)
        value = 0.0
    if value == 0:
        return f"0 {_SIZE_UNITS[0]}"
    for unit in _SIZE_UNITS[:-1]:
        if value < 1024.0:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} {_SIZE_UNITS[-1]}"


def _is_unreachable(err: str) -> bool:
    return bool(_SMB_DOWN_RE.search(err))


def _parse_smb(url: str) -> tuple[str, str, str]:
    p     = urlparse(url.replace("\\", "/"))
    host  = p.hostname or p.netloc
    parts = [x for x in p.path.split("/") if x]
    return host, (parts[0] if parts else ""), "/".join(parts[1:])


def _q(s: str) -> str:
    return s.replace("\n", "").replace("\r", "").replace("\\", "/").replace('"', '\\"')


def _run_futures(futs: list, cancel: threading.Event, tag: str = "worker") -> None:
    for fut in concurrent.futures.as_completed(futs):
        if cancel.is_set():
            for f in futs:
                f.cancel()
            return
        try:
            fut.result()
        except Exception as exc:
            logger.error("%s: %s", tag, exc)


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
