import concurrent.futures
import os
import re
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from urllib.parse import urlparse

from drive_utils import is_smb, is_ssh, build_rsync_list_cmd, _SSH_USER_HOST_RE
from state import logger, frozen_safe_env
from themes import register_cache_invalidation_hook as _reg_cache_hook
from translations import tr
from ui_utils import format_size

_CHUNK           = 32 * 1024 * 1024
_IO_BUF          =  8 * 1024 * 1024
_WORKERS         = min(12, max(4, os.cpu_count() or 4))
_SMB_WORKERS     = 10
_SMB_TIMEOUT     = 10
_SMB_LS_INDEX_TIMEOUT = 120
_SMB_FILE_SECS   = 3
_SMB_MIN_BYTES_PER_SEC = 512 * 1024
_SMB_CHUNK       = 1_000
_FLUSH_THRESH    = 2_500
_FLUSH_INTERVAL  = 0.3
_ENTRY_EMIT_SECS = 0.5
_SCAN_EMIT_SECS  = 0.5
_SCAN_PIPE_BATCH = 128
_LOCAL_BATCH     = 256
_PIPE_MAXSIZE    = 1024
_MIN_FREE        = 500 * 1024 * 1024


def _scale_params(total: int) -> tuple[int, int, int]:
    if total >= 100_000: return 2048, 20_000, 1024
    if total >=  50_000: return 1024, 10_000,  512
    if total >=  10_000: return  512,  5_000,  256
    if total >=   2_000: return  256,  2_500,  128
    return _LOCAL_BATCH, _FLUSH_THRESH, _SCAN_PIPE_BATCH


_RSYNC_DELETE_RE = re.compile(r"^deleting\s+(.+)$")

_SKIP_GLOBS: tuple[str, ...] = (
    "lock", ".lock", "lockfile", ".lck", ".parentlock", "Singleton*",
    "cache", "Network Cache", "startupCache", "jumpListCache",
    "*.sqlite-wal", "*.sqlite-shm", "*.journal", "*-journal", "*_journal",
    "*.db-wal", "*.db-shm",
    "idb", "WebStorage", "Session Storage", "Local Storage", "leveldb", "*.ldb",
    "temp", "tmp", "*.tmp", "*.baklz4",
    "recovery.jsonlz4", "recovery.baklz4", "sessionstore-backups",
    "Thumbs.db", ".DS_Store", ".quota", ".user64", ".healthcheck", ".active-update",
    "GPUCache", "ShaderCache", "blob_storage",
)


def _glob_to_regex(pattern: str) -> str:
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return "".join(out)


_SKIP_RE = re.compile(
    "|".join(f"^{_glob_to_regex(p)}\\Z" for p in _SKIP_GLOBS),
    re.I | re.S,
)


def _build_skip_matcher(globs: tuple[str, ...]):
    exact: set[str] = set()
    prefixes: list[str] = []
    suffixes: list[str] = []
    complex_globs: list[str] = []
    for g in globs:
        low = g.lower()
        body = low.strip("*")
        if "?" in low or "*" in body or not body:
            complex_globs.append(g)
        elif low.startswith("*") and low.endswith("*"):
            complex_globs.append(g)
        elif low.startswith("*"):
            suffixes.append(body)
        elif low.endswith("*"):
            prefixes.append(body)
        else:
            exact.add(low)

    compiled_regex = (
        re.compile("|".join(f"^{_glob_to_regex(g)}\\Z" for g in complex_globs), re.I | re.S)
        if complex_globs else None
    )
    prefix_t = tuple(prefixes)
    suffix_t = tuple(suffixes)

    def matcher(name: str) -> bool:
        _low = name.lower()
        if _low in exact:
            return True
        if prefix_t and _low.startswith(prefix_t):
            return True
        if suffix_t and _low.endswith(suffix_t):
            return True
        if compiled_regex is None:
            return False
        return compiled_regex.match(name) is not None

    return matcher


_should_skip = _build_skip_matcher(_SKIP_GLOBS)

_SMB_LINE_RE = re.compile(
    r"^(.+?)\s+([ADRHNSV]*)\s*(?:\(.*?\)\s*)?(\d+)"
    r"\s+(\w{3}\s+\w{3}\s+[\s\d]\d\s+[\d:]+\s*\d*)$"
)

_SMB_MTIME_RE = re.compile(
    r"^\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})\s+(\d{4})$"
)
_SMB_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

_SMB_MTIME_TOLERANCE = 2


def _parse_smb_mtime(raw: str) -> int:
    m = _SMB_MTIME_RE.match(raw.strip())
    if not m:
        return -1
    mon_s, day_s, hh, mm, ss, year_s = m.groups()
    month = _SMB_MONTHS.get(mon_s)
    if month is None:
        return -1
    try:
        return int(time.mktime((
            int(year_s), month, int(day_s),
            int(hh), int(mm), int(ss), 0, 0, -1,
        )))
    except (ValueError, OverflowError):
        return -1


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
    expire_time = "0" if urgency == "critical" else "-1"
    try:
        proc = subprocess.Popen(
            [_NOTIFY_SEND, f"--urgency={urgency}", f"--expire-time={expire_time}", "--app-name=Backup Helper",
             "--icon=drive-harddisk", title, body],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True, env=frozen_safe_env())
    except OSError:
        return
    threading.Thread(target=proc.wait, name="notify-reaper", daemon=True).start()


def _check_destination_space(tasks: list[tuple]) -> list[str]:
    checked: set[str] = set()
    warnings: list[str] = []
    for _src, dst_raw, _title, *_ in tasks:
        dsts = dst_raw if isinstance(dst_raw, (list, tuple)) else (dst_raw,)
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
                        tr("• {dst!r}  —  only {free_mb:,} MB free", dst=dst, free_mb=free_mb)
                    )
            except OSError as exc:
                logger.debug("_check_destination_space: cannot check %r: %s", dst, exc)
    return warnings


_CACHE_MISS = object()
_O_NOATIME  = getattr(os, "O_NOATIME", 0)
_PID        = os.getpid()
_EUID       = os.geteuid()
_tls        = threading.local()
_TIME_CHECK_EVERY = 32
_seen_dirs_global: set[str] = set()
_seen_dirs_lock = threading.Lock()
def _find_ram_dir() -> str | None:
    for cand in ("/dev/shm", os.environ.get("XDG_RUNTIME_DIR", ""), f"/run/user/{os.getuid()}"):
        if cand and os.path.isdir(cand) and os.access(cand, os.W_OK | os.X_OK):
            return cand
    return None


_SHM_DIR: str | None = _find_ram_dir()

_smb_procs: dict[int, subprocess.Popen] = {}
_smb_procs_lock = threading.Lock()


def _scan_dir_entries(src: str, dst: str, excl: frozenset, cancel: threading.Event):
    join     = os.path.join
    skip     = _should_skip
    is_set   = cancel.is_set
    has_excl = bool(excl)
    with os.scandir(src) as it:
        for e in it:
            if is_set():
                break
            name = e.name
            if skip(name):
                continue
            path = e.path
            if has_excl and path in excl:
                continue
            try:
                if e.is_symlink():
                    yield False, path, join(dst, name), True
                    continue
                if e.is_dir(follow_symlinks=False):
                    try:
                        dir_st = e.stat(follow_symlinks=False)
                    except OSError:
                        dir_st = None
                    yield True, path, join(dst, name), dir_st
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    st = None
            except OSError:
                continue
            yield False, path, join(dst, name), st


_fsynced_dirs_lock = threading.Lock()
_fsynced_dirs: set[str] = set()


def _reset_dir_caches() -> None:
    with _seen_dirs_lock:
        _seen_dirs_global.clear()
    with _fsynced_dirs_lock:
        _fsynced_dirs.clear()


def _fsync_dir(path: str) -> None:
    try:
        dfd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError as exc:
        logger.debug("fsync dir %s failed: %s", path, exc)


def _fsync_dir_once(path: str) -> None:
    d = path or "."
    with _fsynced_dirs_lock:
        if d in _fsynced_dirs:
            return
        _fsynced_dirs.add(d)
    _fsync_dir(d)


def _fsync_parent_dir(path: str) -> None:
    _fsync_dir_once(os.path.dirname(path))


def _tmp_path(dst: str, suffix: str) -> str:
    parent, base = os.path.split(dst)
    tag = f".{_PID}.{threading.get_ident()}{suffix}"
    room = 255 - len(tag.encode())
    raw = os.fsencode(base)
    if len(raw) > room:
        base = os.fsdecode(raw[:max(room, 1)])
    return os.path.join(parent, base + tag) if parent else base + tag


def _ensure_dir(path: str) -> bool:
    if not path:
        return True
    try:
        seen: set = _tls.seen_dirs
    except AttributeError:
        seen = _tls.seen_dirs = set()
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


def _make_new_dir(path: str) -> "bool | None":
    for attempt in (0, 1):
        try:
            os.mkdir(path)
        except FileExistsError:
            return False if os.path.isdir(path) else None
        except FileNotFoundError:
            parent = os.path.dirname(path.rstrip("/"))
            if attempt or not parent or parent == path or not _ensure_dir(parent):
                return None
            continue
        except OSError as exc:
            logger.error("mkdir %s: %s", path, exc)
            return None
        _remember_dir(path)
        return True
    return None


def _remember_dir(path: str) -> None:
    with _seen_dirs_lock:
        _seen_dirs_global.add(path)


def _apply_dir_attrs(entries: "list[tuple[str, int, int, int]]") -> None:
    for path, mode, atime_ns, mtime_ns in sorted(entries, key=lambda e: e[0].count("/"), reverse=True):
        try:
            os.chmod(path, (mode & 0o7777) | 0o700)
        except OSError as exc:
            logger.debug("chmod %s failed: %s", path, exc)
        try:
            os.utime(path, ns=(atime_ns, mtime_ns))
        except OSError as exc:
            logger.debug("utime %s failed: %s", path, exc)


@lru_cache(maxsize=256)
def _cached_mono_style(size: int, color: str, bold: bool = False, extra: str = "") -> str:
    s = f"font-family:monospace;font-size:{size}px;color:{color};"
    if bold:
        s += "font-weight:bold;"
    return s + extra


def _invalidate_copy_worker_caches() -> None: _cached_mono_style.cache_clear()

_reg_cache_hook(_invalidate_copy_worker_caches)
del _reg_cache_hook


def _format_unit(value: float) -> str:
    return format_size(value, 2)


_RSYNC_ERROR_TAIL = 5
_RSYNC_LIST_TIMEOUT = 20
_RSYNC_GLOB_CHARS = re.compile(r"([\\*?\[])")
_LIKELY_FILE_TAIL_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_SSH_URL_RE = re.compile(r"^(ssh://(?:[^@/]+@)?[^/]+)(/.*)?$")
_RSYNC_LIST_LINE_RE = re.compile(
    r"^([dl\-cbps])[rwxsStT\-]{9}\s+[\d,.]+\s+\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\s(.*)$"
)


def _ssh_join(dst_spec: str, rel_path: str) -> str:
    base = dst_spec.rstrip("/")
    rel = rel_path.lstrip("/")
    return f"{base}/{rel}" if rel else base


def _rsync_escape_component(name: str) -> str:
    return _RSYNC_GLOB_CHARS.sub(r"\\\1", name)


def _ssh_split(spec: str) -> tuple[str, str]:
    m = _SSH_URL_RE.match(spec)
    if m:
        return m.group(1), (m.group(2) or "")
    m = _SSH_USER_HOST_RE.match(spec)
    if m:
        path = m.group("rpath")
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
        if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
            continue
        escaped = "/".join(_rsync_escape_component(p) for p in rel.split(os.sep))
        patterns.append("/" + escaped)
    return patterns


def _rsync_ci_pattern(pattern: str) -> str:
    return "".join(f"[{c.lower()}{c.upper()}]" if c.isascii() and c.isalpha() else c for c in pattern)


_RSYNC_SKIP_PATTERNS: tuple[str, ...] = tuple(_rsync_ci_pattern(g) for g in _SKIP_GLOBS)


def _rsync_excludes(src: str, raw_excludes: "list | None") -> "list | None":
    skip = list(_RSYNC_SKIP_PATTERNS)
    if is_ssh(src):
        _prefix, remote_root = _ssh_split(src)
        base_dir = (remote_root.rstrip("/") if remote_root.endswith("/")
                    else os.path.dirname(remote_root.rstrip("/") or remote_root))
        return _relativize_excludes(raw_excludes, base_dir, split_fn=_ssh_split) + skip
    base_dir = src.rstrip("/") if src.endswith("/") else os.path.dirname(src.rstrip("/") or src)
    return _relativize_excludes(raw_excludes, base_dir) + skip


def _run_capture(cmd: list[str], timeout: float,
                 cancel: "threading.Event | None" = None) -> "tuple[int, str, str] | None":
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=frozen_safe_env())
    except OSError as exc:
        logger.debug("_run_capture: cannot launch %s: %s", cmd[0], exc)
        return None
    deadline = time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=0.25)
            return proc.returncode, out or "", err or ""
        except subprocess.TimeoutExpired:
            if (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline:
                proc.kill()
                proc.communicate()
                return None


def _rsync_list(spec: str, cancel: "threading.Event | None" = None) -> "list[tuple[str, str]] | None":
    if shutil.which("rsync") is None:
        return None
    res = _run_capture(build_rsync_list_cmd(spec), _RSYNC_LIST_TIMEOUT, cancel)
    if res is None:
        return None
    rc, out, err = res
    if rc != 0:
        logger.debug("rsync --list-only %r failed (exit %d): %s", spec, rc, err.strip()[:200])
        return None
    entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        m = _RSYNC_LIST_LINE_RE.match(line)
        if m:
            name = m.group(2)
            if m.group(1) == "l":
                name = name.split(" -> ", 1)[0]
            entries.append((m.group(1), name))
    return entries


def _remote_kind(spec: str, cancel: "threading.Event | None" = None) -> "str | None":
    entries = _rsync_list(spec.rstrip("/"), cancel)
    if not entries:
        return None
    kind = entries[0][0]
    if kind == "d":
        return "dir"
    if kind == "l":
        probe = _rsync_list(spec.rstrip("/") + "/", cancel)
        return "dir" if probe and probe[0][0] == "d" else "file"
    return "file"


def _ssh_dir_is_empty(src_dir: str, cancel: "threading.Event | None" = None) -> "bool | None":
    entries = _rsync_list(src_dir.rstrip("/") + "/", cancel)
    if entries is None:
        return None
    return not any(name != "." for _kind, name in entries)


def _rsync_src_arg(src: str, cancel: "threading.Event | None" = None) -> str:
    if src.endswith("/"):
        return src
    if is_ssh(src):
        kind = _remote_kind(src, cancel)
        if kind is None:
            tail = src.rstrip("/").rsplit("/", 1)[-1]
            kind = "file" if _LIKELY_FILE_TAIL_RE.search(tail) else "dir"
        return f"{src}/" if kind == "dir" else src
    return f"{src}/" if os.path.isdir(src) else src



_SMB_STATUS_TOKEN_RE = re.compile(r"NT_STATUS_[A-Z0-9_]+")
_SMB_DOWN_STATUS = frozenset({
    "NT_STATUS_HOST_UNREACHABLE", "NT_STATUS_IO_TIMEOUT", "NT_STATUS_CONNECTION_REFUSED",
    "NT_STATUS_NETWORK_UNREACHABLE", "NT_STATUS_CONNECTION_RESET", "NT_STATUS_CONNECTION_DISCONNECTED",
})


def _is_unreachable(err: str) -> bool:
    codes = _SMB_STATUS_TOKEN_RE.findall(err.upper())
    if codes:
        return any(c in _SMB_DOWN_STATUS for c in codes)
    return bool(_SMB_DOWN_RE.search(err))


def _parse_smb(url: str) -> tuple[str, str, str]:
    u = url.replace("\\", "/")
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", u)
    if m:
        u = u[m.end():]
    netloc, _, path = u.partition("/")
    host = urlparse("//" + netloc).hostname or netloc
    parts = [x for x in path.split("/") if x]
    return host, (parts[0] if parts else ""), "/".join(parts[1:])


def _q(s: str) -> str:
    return s.replace("\n", "").replace("\r", "").replace("\\", "/").replace('"', '\\"')


def _run_futures(futs: list, cancel: threading.Event, tag: str = "worker") -> None:
    cancelled = False
    for fut in concurrent.futures.as_completed(futs):
        if not cancelled and cancel.is_set():
            cancelled = True
            for f in futs:
                f.cancel()
        try:
            fut.result()
        except concurrent.futures.CancelledError:
            pass
        except Exception as exc:
            logger.error("%s: %s", tag, exc)


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
