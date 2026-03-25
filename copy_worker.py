from typing import Protocol
from itertools import groupby
from functools import lru_cache
from dataclasses import dataclass
from urllib.parse import urlparse
from pathlib import PurePosixPath
from collections import deque as _deque
import os, subprocess, re, threading, concurrent.futures, stat

from PyQt6.QtCore import Qt, QElapsedTimer, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QProgressBar, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QApplication, QWidget,
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QSpinBox, QTextEdit
)

from drive_utils import is_smb
from themes import current_theme, font_sz
from state import apply_replacements, logger

_CHUNK             = 16 * 1024 * 1024
_IO_BUF_SZ         = 1 * 1024 * 1024
_SCAN_WORKERS      = max(8, min(32, (os.cpu_count() or 4)))
_COPY_WORKERS      = max(1, min(8, (os.cpu_count() or 2) // 2))
_SMB_SHARE_WORKERS = 10
_SMB_SCAN_WORKERS  = 10
_SMB_PROBE_TO      = 5
_SMB_BASE_TIMEOUT  = 15
_SMB_SECS_PER_FILE = 3
_SMB_LS_TIMEOUT    = 15
_FLUSH_THRESHOLD   = 500
_SMB_CHUNK_SIZE    = 500

_SKIP_RE = re.compile(
    r"(^\.?lock$|\.lock$|lockfile$|Singleton\w*$|cookies\.sqlite-wal$|\.lck$)", re.I
)
_SMB_LINE_RE = re.compile(
    r"^(.+?)\s+([ADRHNSV]*)\s*(?:\(.*?\)\s*)?(\d+)"
    r"\s+\w{3}\s+\w{3}\s+[\s\d]\d\s+[\d:]+\s*\d*$"
)

_SMB_UNREACHABLE = frozenset((
    "HOST IS DOWN", "NT_STATUS_HOST_UNREACHABLE", "NT_STATUS_IO_TIMEOUT", "NT_STATUS_CONNECTION_REFUSED",
    "NT_STATUS_NETWORK_UNREACHABLE", "NT_STATUS_CONNECTION_RESET", "NT_STATUS_CONNECTION_DISCONNECTED",
    "CONNECTION REFUSED", "NO ROUTE TO HOST", "NETWORK IS UNREACHABLE", "CONNECTION TIMED OUT", "TIMEOUT",
))


_DIRS_LOCK = threading.Lock()
_CREATED_DIRS: set[str] = set()

def _clear_dir_cache() -> None:
    with _DIRS_LOCK: _CREATED_DIRS.clear()

def _ensure_dir(path: str) -> bool:
    if not path: return True
    with _DIRS_LOCK:
        if path in _CREATED_DIRS: return True
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.error("Failed to create directory %s: %s", path, e)
        return False
    with _DIRS_LOCK: _CREATED_DIRS.add(path)
    return True


_O_NOATIME  = os.O_NOATIME
_PID        = os.getpid()
_CACHE_MISS = object()


@lru_cache(maxsize=256)
def _cached_mono_style(size: int, color: str, bold: bool = False, extra: str = "") -> str:
    base = f"font-family:monospace;font-size:{size}px;color:{color};"
    if bold:
        base += "font-weight:bold;"
    return base + extra


def _format_unit(value: float, units=None) -> str:
    if units is None:
        units = ["B", "KB", "MB", "GB", "TB"]
    if value <= 0:
        return f"0 {units[0]}"
    v = float(value)
    for i, unit in enumerate(units[:-1]):
        if v < 1024.0:
            return f"{int(v)} {unit}" if i == 0 else f"{v:.2f} {unit}"
        v /= 1024.0
    return f"{v:.2f} {units[-1]}"


def _file_size(path: str) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def _is_unreachable(err: str) -> bool:
    up = err.upper()
    return any(s in up for s in _SMB_UNREACHABLE)


def _parse_smb(url: str) -> tuple[str, str, str]:
    p     = urlparse(url)
    host  = p.hostname or p.netloc
    parts = [x for x in p.path.split("/") if x]
    return host, (parts[0] if parts else ""), "/".join(parts[1:])


def _q(s: str) -> str:
    return s.replace("\n", "").replace("\r", "").replace("\\", "/").replace('"', '\\"')


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _session_timeout(n: int) -> int:
    return max(_SMB_BASE_TIMEOUT, n * _SMB_SECS_PER_FILE)


class _SecurePw(Protocol):
    def get(self) -> str: ...
    def clear(self) -> None: ...


def _get_smb_credentials() -> tuple[str, "_SecurePw | None"]:
    try:
        from samba_credentials import SambaPasswordManager
        from sudo_password import SecureString
        u, p, _ = SambaPasswordManager().get_credentials()
        pw = SecureString(p) if p else None
        return (u or ""), pw
    except Exception as exc:
        logger.warning("SMB credentials unavailable: %s", exc)
        return "", None


def _open_src(path: str) -> int:
    try:
        return os.open(path, os.O_RDONLY | _O_NOATIME)
    except (PermissionError, OSError):
        return os.open(path, os.O_RDONLY)


def _copy_file(src: str, dst: str, cancel: threading.Event) -> tuple[str, str, int]:
    tmp = f"{dst}.{os.getpid()}.{threading.get_ident()}.part"
    rfd = wfd = None
    success = False
    try:
        if cancel.is_set():
            return "skip", "", 0
        st = _stat_safe(src)
        if st is None:
            return "error", "Quelle nicht lesbar", 0
        if _is_up_to_date(dst, st):
            return "skip", "Bereits aktuell", st.st_size
        if not _ensure_dir(os.path.dirname(dst)):
            return "error", "Verzeichnis konnte nicht erstellt werden", 0
        rfd = _open_src(src)
        wfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, st.st_mode & 0o777)
        if st.st_size > 0:
            try:
                os.posix_fallocate(wfd, 0, st.st_size)
                os.posix_fadvise(rfd, 0, st.st_size, os.POSIX_FADV_SEQUENTIAL)
            except (OSError, AttributeError):
                pass
        bytes_copied = _copy_loop(rfd, wfd, st.st_size, cancel)
        os.fdatasync(wfd)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, dst)
        success = True
        return "ok", dst, bytes_copied
    except InterruptedError:
        return "skip", "", 0
    except Exception as exc:
        logger.error("Fehler beim Kopieren %s -> %s: %s", src, dst, exc)
        return "error", str(exc), 0
    finally:
        if rfd is not None: os.close(rfd)
        if wfd is not None: os.close(wfd)
        if not success: _unlink(tmp)


def _stat_safe(path: str) -> "os.stat_result | None":
    try:
        return os.stat(path)
    except OSError:
        return None


def _is_up_to_date(dst: str, src_st: "os.stat_result") -> bool:
    try:
        dst_st = os.stat(dst)
        if src_st.st_size != dst_st.st_size:
            return False
        return dst_st.st_mtime >= src_st.st_mtime
    except OSError:
        return False


def _copy_loop(rfd: int, wfd: int, total_size: int, cancel: threading.Event) -> int:
    remaining = total_size
    try:
        while remaining > 0:
            if cancel.is_set():
                raise InterruptedError()
            n = os.copy_file_range(rfd, wfd, min(remaining, _CHUNK))
            if n == 0:
                break
            remaining -= n
    except (OSError, AttributeError):
        pass
    if remaining > 0:
        try:
            os.lseek(rfd, total_size - remaining, os.SEEK_SET)
            while remaining > 0:
                if cancel.is_set():
                    raise InterruptedError()
                chunk_to_read = min(remaining, _IO_BUF_SZ)
                buf = os.read(rfd, chunk_to_read)
                if not buf:
                    break
                written = os.write(wfd, buf)
                remaining -= written
        except OSError:
            pass
    return total_size - remaining


def _scan_local(scan_jobs, cancel, progress_cb=None) -> tuple[list, list, list]:
    pairs, skipped, errors = [], [], []
    lock = threading.Lock()
    counter = 0
    def scan_one(src_root: str, dst_root: str, title: str = "") -> None:
        nonlocal counter
        local_pairs, local_skipped = [], []
        try:
            st = os.stat(src_root)
        except OSError:
            with lock:
                errors.append((src_root, "Pfad existiert nicht", title))
            return
        if stat.S_ISREG(st.st_mode):
            if _SKIP_RE.search(os.path.basename(src_root)):
                local_skipped.append((src_root, "Übersprungen", title))
            else:
                local_pairs.append((src_root, dst_root, title))
        elif stat.S_ISDIR(st.st_mode):
            for root, dirs, files in os.walk(src_root):
                if cancel.is_set(): break
                rel_dir = os.path.relpath(root, src_root)
                dest_dir = dst_root if rel_dir == "." else os.path.join(dst_root, rel_dir)
                for f in files:
                    if _SKIP_RE.search(f):
                        local_skipped.append((os.path.join(root, f), "Übersprungen", title))
                    else:
                        local_pairs.append((os.path.join(root, f), os.path.join(dest_dir, f), title))
        with lock:
            pairs.extend(local_pairs)
            skipped.extend(local_skipped)
            counter += len(local_pairs) + len(local_skipped)
            if progress_cb: progress_cb(counter)
    with concurrent.futures.ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as pool:
        futs = [pool.submit(scan_one, s, d, t) for s, d, t in scan_jobs]
        for fut in concurrent.futures.as_completed(futs):
            if cancel.is_set(): break
            fut.result()
    return pairs, skipped, errors


@dataclass
class _SmbJob:
    src_url:     str
    dst_path:    str
    kind:        str
    host:        str
    share:       str
    remote_path: str
    remote_size: int = -1
    title:       str = ""

    def up_to_date(self) -> bool:
        if self.kind != "smb_get" or self.remote_size < 0:
            return False
        try:
            return self.remote_size == os.stat(self.dst_path).st_size
        except OSError:
            return False


def _build_smb_get_cmds(jobs: list[_SmbJob]) -> str:
    lines = []
    jobs_sorted = sorted(jobs, key=lambda x: os.path.dirname(x.remote_path))
    for rdir, group in groupby(jobs_sorted, key=lambda x: os.path.dirname(x.remote_path)):
        lines.append(f'cd "/{_q(rdir)}"' if rdir else 'cd "/"')
        for j in group:
            fname = os.path.basename(j.remote_path)
            lines.append(f'get "{_q(fname)}" "{_q(j.dst_path)}"')
    lines.append("exit\n")
    return "\n".join(lines)


def _build_smb_put_cmds(jobs: list[_SmbJob]) -> str:
    rdirs = sorted({str(PurePosixPath(j.remote_path).parent) for j in jobs})
    mkdir_lines, seen_dirs = [], set()

    for rdir in rdirs:
        if not rdir or rdir == ".": continue
        path = PurePosixPath(rdir)
        parents = sorted([path, *path.parents], key=lambda x: len(x.parts))
        for p in parents:
            p_str = str(p)
            if p_str not in seen_dirs and p_str != ".":
                seen_dirs.add(p_str)
                mkdir_lines.append(f'mkdir "{_q(p_str)}"')

    transfer_lines, cur_local_dir, cur_rdir = [], None, None
    for j in sorted(jobs, key=lambda x: (os.path.dirname(x.remote_path), x.src_url)):
        local_dir = os.path.dirname(j.src_url)
        rdir = os.path.dirname(j.remote_path).replace("\\", "/").strip("/")

        if local_dir != cur_local_dir:
            transfer_lines.append(f'lcd "{_q(local_dir)}"')
            cur_local_dir = local_dir
        if rdir != cur_rdir:
            transfer_lines.append(f'cd "/{_q(rdir)}"' if rdir else 'cd "/"')
            cur_rdir = rdir

        transfer_lines.append(f'put "{_q(os.path.basename(j.src_url))}" "{_q(os.path.basename(j.remote_path))}"')

    return "\n".join(mkdir_lines + transfer_lines + ["exit\n"])


class _SmbClient:

    def __init__(self, host: str, share: str, user: str, pw: "_SecurePw | None", guest: bool = False) -> None:
        self.host = host
        self.share = share
        self._user = user
        self._pw = pw
        self._guest = guest
        self._argv_cache = (["smbclient", f"//{host}/{share}", "-N"]
                            if guest else ["smbclient", f"//{host}/{share}", "-U", user])
        env = os.environ.copy()
        if pw is not None and not guest:
            env["PASSWD"] = pw.get()
        else:
            env.pop("PASSWD", None)
        self._env_cache = env

    def run(self, cmds: str, timeout: int) -> tuple[bool, str]:
        env = self._env_cache
        try:
            r = subprocess.run(
                self._argv_cache, input=cmds, text=True, capture_output=True, timeout=timeout, env=env, encoding="utf-8"
            )
            return r.returncode == 0, (r.stderr.strip() or f"exit {r.returncode}")
        except subprocess.TimeoutExpired as e:
            logger.warning("SMB command timeout after %ds", timeout)
            if e.stdout:
                logger.debug("SMB stdout before timeout: %.200s", e.stdout)
            if e.stderr:
                logger.debug("SMB stderr before timeout: %.200s", e.stderr)
            return False, "timeout"
        except Exception as exc:
            logger.error("SMB command failed: %s", exc)
            return False, str(exc)

    def probe(self) -> str:
        if self._user and self._pw is not None:
            ok, err = self.run("exit\n", _SMB_PROBE_TO)
            if ok:
                return "ok"
            if _is_unreachable(err):
                logger.warning("SMB unreachable //%s/%s: %s", self.host, self.share, err)
                return "timeout"
        ok, err = _SmbClient(self.host, self.share, "", None, guest=True).run("exit\n", _SMB_PROBE_TO)
        if ok:
            return "guest"
        if _is_unreachable(err):
            logger.warning("SMB unreachable //%s/%s: %s", self.host, self.share, err)
            return "timeout"
        logger.warning("SMB auth failed //%s/%s: %s", self.host, self.share, err)
        return "auth"

    def ls_index(self, base: str) -> "dict | None":
        base = base.replace("\\", "/").rstrip("/")
        cmd = f'recurse on\nprompt off\ncd "{_q(base)}"\nls\n' if base else "recurse on\nprompt off\nls\n"
        index = {}
        try:
            result = subprocess.run(self._argv_cache, input=cmd, text=True, capture_output=True, timeout=_SMB_LS_TIMEOUT,
                                    env=self._env_cache, encoding="utf-8")
            if result.returncode != 0:
                return None if _is_unreachable(result.stderr) else {}
            cur_dir = base
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line: continue
                if line.startswith("\\"):
                    cur_dir = line.replace("\\", "/").strip("/")
                    continue
                m = _SMB_LINE_RE.match(line)
                if m:
                    name, flags, size_s = m.groups()
                    name = name.strip()
                    if name not in (".", "..") and "D" not in flags:
                        full_p = f"{cur_dir}/{name}".lstrip("/")
                        index[full_p] = (int(size_s),)
        except (subprocess.TimeoutExpired, Exception) as exc:
            logger.error("SMB Index-Fehler für %s: %s", self.host, exc)
            return None
        return index


class _SmbScanner:

    _CACHE_MAX_SIZE = 1000

    def __init__(self, user: str, pw: "_SecurePw | None", guest: bool, cancel: threading.Event, progress_cb=None) -> None:
        self._ls_cache: dict = {}
        self._user         = user
        self._pw           = pw
        self._guest        = guest
        self._cancel       = cancel
        self._progress_cb  = progress_cb
        self._cache_lock   = threading.Lock()
        self._result_lock  = threading.Lock()
        self._counter      = 0

    def _client(self, host: str, share: str) -> _SmbClient: return _SmbClient(host, share, self._user, self._pw, self._guest)

    def _report(self, n: int) -> None:
        with self._result_lock:
            self._counter += n
            current = self._counter
        if self._progress_cb:
            self._progress_cb(current)

    def resolve(self, jobs) -> tuple[list, list]:
        expanded: list = []
        errors:   list = []
        scan_tasks: list = []
        seen_get:   set  = set()

        for src, dst, *rest in jobs:
            if self._cancel.is_set():
                break
            title      = rest[0] if rest else ""
            src_is_smb = is_smb(src)
            host, share, rpath = _parse_smb(src if src_is_smb else dst)
            if src_is_smb:
                key = (host, share, rpath, dst)
                if key in seen_get:
                    continue
                seen_get.add(key)
                scan_tasks.append(
                    lambda h=host, sh=share, rp=rpath, d=dst, ti=title: self._do_get(h, sh, rp, d, ti, expanded, errors))
            elif os.path.isfile(src):
                scan_tasks.append(
                    lambda s=src, d=dst, h=host, sh=share, rp=rpath, ti=title: self._do_put_file(s, d, h, sh, rp, ti, expanded))
            else:
                scan_tasks.append(
                    lambda s=src, d=dst, h=host, sh=share, rp=rpath, ti=title: self._do_put_dir(s, d, h, sh, rp, ti, expanded))

        if self._cancel.is_set():
            return [], []

        with concurrent.futures.ThreadPoolExecutor(max_workers=_SMB_SCAN_WORKERS) as pool:
            futs = [pool.submit(fn) for fn in scan_tasks if not self._cancel.is_set()]
            for fut in concurrent.futures.as_completed(futs):
                if self._cancel.is_set():
                    for f in futs:
                        f.cancel()
                    break
                try:
                    fut.result()
                except Exception as exc:
                    logger.error("SMB scan error: %s", exc)
                    with self._result_lock:
                        errors.append(("smb scan error", str(exc)))

        return ([], []) if self._cancel.is_set() else (expanded, errors)

    def _do_get(self, host, share, rpath, dst, title, expanded, errors) -> None:
        ck = f"{host}:{share}:{rpath}"
        with self._cache_lock:
            if len(self._ls_cache) > self._CACHE_MAX_SIZE:
                oldest = next(iter(self._ls_cache))
                del self._ls_cache[oldest]
                idx = self._ls_cache.get(ck, _CACHE_MISS)
        if idx is _CACHE_MISS:
            if self._cancel.is_set():
                return
            idx = self._client(host, share).ls_index(rpath)
            with self._cache_lock:
                existing = self._ls_cache.get(ck, _CACHE_MISS)
                if existing is _CACHE_MISS:
                    self._ls_cache[ck] = idx
                else:
                    idx = existing
        if self._cancel.is_set():
            return
        src_url = f"smb://{host}/{share}/{rpath}"
        lexp: list = []
        lerr: list = []
        if idx is None:
            lerr.append((src_url, "NT_STATUS_HOST_UNREACHABLE"))
        elif not idx:
            lerr.append((src_url, "SMB path empty or not found"))
        else:
            prefix = rpath.rstrip("/") + "/"
            for path, (sz,) in idx.items():
                if self._cancel.is_set():
                    break
                rel = (os.path.relpath(path, rpath) if path.startswith(prefix) else os.path.basename(path))
                lexp.append(_SmbJob(src_url, str(os.path.join(dst, rel)), "smb_get", host, share, path, sz, title))
        with self._result_lock:
            expanded.extend(lexp)
            errors.extend(lerr)
        self._report(len(lexp) + len(lerr))

    def _do_put_file(self, src, dst, host, share, rpath, title, expanded) -> None:
        if _SKIP_RE.search(os.path.basename(src)):
            return
        rp = f"{rpath}/{os.path.basename(src)}".lstrip("/")
        with self._result_lock:
            expanded.append(_SmbJob(src, dst, "smb_put", host, share, rp, title=title))
        self._report(1)

    def _do_put_dir(self, src, dst, host, share, rpath, title, expanded) -> None:
        lexp: list = []
        stack: list = [src]
        while stack:
            if self._cancel.is_set():
                break
            try:
                with os.scandir(stack.pop()) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif not _SKIP_RE.search(e.name):
                            rel = os.path.relpath(e.path, src)
                            rp  = f"{rpath}/{rel}".replace(os.sep, "/").lstrip("/")
                            lexp.append(_SmbJob(e.path, dst, "smb_put", host, share, rp, title=title))
            except PermissionError:
                pass
        with self._result_lock:
            expanded.extend(lexp)
        self._report(len(lexp))


class _ShareProcessor:

    def __init__(self, client: _SmbClient, cancel: threading.Event, flusher: "_Flusher", tracker: "_EntryTracker",
                 ri_cache: dict, ri_lock: threading.Lock) -> None:

        self._client      = client
        self._cancel      = cancel
        self._flusher     = flusher
        self._tracker     = tracker
        self._ri_cache    = ri_cache
        self._ri_lock     = ri_lock
        self._unreachable = threading.Event()
        self._url_title:  dict[str, tuple[str, int]] = {}

    @property
    def host(self) -> str:
        return self._client.host

    @property
    def share(self) -> str:
        return self._client.share

    def process(self, get_jobs: list, put_jobs: list) -> None:
        if self._cancel.is_set():
            return

        for j in get_jobs:
            self._url_title[f"smb://{self.host}/{self.share}/{j.remote_path}"] = (j.title, j.remote_size)
        for j in put_jobs:
            self._url_title[j.src_url] = (j.title, _file_size(j.src_url))

        sk_immediate:    list = []
        get_to_transfer: list = []
        for j in get_jobs:
            if j.up_to_date():
                sk_immediate.append((f"smb://{self.host}/{self.share}/{j.remote_path}", "Up to date"))
            else:
                get_to_transfer.append(j)

        put_to_transfer: list = []
        if put_jobs:
            ri = self._remote_index(put_jobs)
            for j in put_jobs:
                key  = j.remote_path.replace("\\", "/").lstrip("/")
                meta = ri.get(key)
                if meta and _file_size(j.src_url) == meta[0]:
                    sk_immediate.append((j.src_url, "Up to date"))
                else:
                    put_to_transfer.append(j)

        for i in range(0, len(sk_immediate), _SMB_CHUNK_SIZE):
            if self._cancel.is_set():
                break
            self._record([], sk_immediate[i: i + _SMB_CHUNK_SIZE], [])

        if get_to_transfer and not self._cancel.is_set():
            for d in {os.path.dirname(j.dst_path) for j in get_to_transfer}:
                if d:
                    _ensure_dir(d)
            for i in range(0, len(get_to_transfer), _SMB_CHUNK_SIZE):
                if self._cancel.is_set():
                    break
                if self._unreachable.is_set():
                    self._fail_jobs(get_to_transfer[i:], is_get=True, reason="NT_STATUS_HOST_UNREACHABLE")
                    break
                ok_c, er_c = self._transfer(get_to_transfer[i: i + _SMB_CHUNK_SIZE], _build_smb_get_cmds)
                self._record(ok_c, [], er_c)

        if put_to_transfer and not self._cancel.is_set():
            for i in range(0, len(put_to_transfer), _SMB_CHUNK_SIZE):
                if self._cancel.is_set():
                    break
                if self._unreachable.is_set():
                    self._fail_jobs(put_to_transfer[i:], is_get=False, reason="NT_STATUS_HOST_UNREACHABLE")
                    break
                ok_c, er_c = self._transfer(put_to_transfer[i: i + _SMB_CHUNK_SIZE], _build_smb_put_cmds)
                self._record(ok_c, [], er_c)

    def _remote_index(self, put_jobs: list) -> dict:
        needed = {(os.path.dirname(j.remote_path).replace("\\", "/") or "").split("/")[0] for j in put_jobs}
        merged: dict = {}
        for top in needed:
            key = f"{self.host}:{self.share}:{top}"
            with self._ri_lock:
                cached = self._ri_cache.get(key)
            if cached is None:
                cached = self._client.ls_index(top)
                if cached is None:
                    with self._ri_lock:
                        self._ri_cache[key] = {}
                    logger.warning("SMB remote index unreachable //%s/%s — skipping remaining tops", self.host, self.share)
                    break
                with self._ri_lock:
                    self._ri_cache[key] = cached
            merged.update(cached)
        return merged

    def _transfer(self, jobs: list, build_fn) -> tuple[list, list]:
        is_get = build_fn is _build_smb_get_cmds
        ok_list, er_list = [], []
        stack = [list(jobs)]
        while stack:
            batch = stack.pop()
            if not batch: continue
            if self._cancel.is_set() or self._unreachable.is_set():
                break
            current_timeout = _session_timeout(len(batch))
            ok, err = self._client.run(build_fn(batch), current_timeout)
            if ok:
                for j in batch:
                    src = f"smb://{self.host}/{self.share}/{j.remote_path}" if is_get else j.src_url
                    dst = j.dst_path if is_get else f"smb://{self.host}/{self.share}/{j.remote_path}"
                    ok_list.append((src, dst))
            elif _is_unreachable(err):
                self._unreachable.set()
                er_list.extend(
                    [((f"smb://{self.host}/{self.share}/{j.remote_path}" if is_get else j.src_url), err) for j in
                     batch])
            elif len(batch) == 1:
                src = f"smb://{self.host}/{self.share}/{batch[0].remote_path}" if is_get else batch[0].src_url
                er_list.append((src, err))
            else:
                mid = len(batch) // 2
                stack.append(batch[mid:])
                stack.append(batch[:mid])
        return ok_list, er_list

    def _record(self, ok_c: list, sk_c: list, er_c: list) -> None:
        def _meta(_url: str) -> tuple[str, int]:
            return self._url_title.get(_url, ("", 0))
        ok_w, ok_titles = [], []
        for s, d in ok_c:
            title, sz = _meta(s)
            ok_w.append((s, d, sz))
            ok_titles.append(title)
        sk_w, sk_titles = [], []
        for s, r in sk_c:
            title, sz = _meta(s)
            sk_w.append((s, r, sz))
            sk_titles.append(title)
        er_w = [(s, e, 0) for s, e in er_c]
        self._flusher.push(ok=ok_w, sk=sk_w, er=er_w)
        for title in ok_titles:           self._tracker.ok(title)
        for title in sk_titles:           self._tracker.skip(title)
        for url, _, _ in er_w:            self._tracker.err(_meta(url)[0])

    def _fail_jobs(self, remaining: list, is_get: bool, reason: str) -> None:
        er_c = [
            (f"smb://{self.host}/{self.share}/{j.remote_path}" if is_get else j.src_url, reason) for j in remaining]
        if er_c:
            self._record([], [], er_c)


class _EntryTracker:
    __slots__ = ("_lock", "_counts")

    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._counts: dict[str, list[int]] = {}

    def _bump(self, title: str, idx: int, n: int = 1) -> None:
        if not title:
            return
        with self._lock:
            self._counts.setdefault(title, [0, 0, 0])[idx] += n

    def ok(self,   title: str, n: int = 1) -> None: self._bump(title, 0, n)
    def err(self,  title: str, n: int = 1) -> None: self._bump(title, 1, n)
    def skip(self, title: str, n: int = 1) -> None: self._bump(title, 2, n)

    def merge_pre(self, pre: dict[str, list[int]]) -> None:
        with self._lock:
            for t, pc in pre.items():
                ec = self._counts.setdefault(t, [0, 0, 0])
                for i in range(3):
                    ec[i] += pc[i]

    def emit_all(self, _signal) -> None:
        with self._lock:
            counts_snapshot = {t: ec[:] for t, ec in self._counts.items()}
        for t, ec in counts_snapshot.items():
            if t:
                _signal.emit(t, ec[0], ec[1], ec[2])


class _Flusher:

    __slots__ = ("_signal", "_total", "_lock", "_ok", "_sk", "_er", "done", "copied", "skipped", "errors")

    def __init__(self, _signal, total: int, done: int = 0, copied: int = 0, skipped: int = 0, errors: int = 0) -> None:
        self._signal  = _signal
        self._total   = total
        self._lock    = threading.Lock()
        self._ok: list = []
        self._sk: list = []
        self._er: list = []
        self.done    = done
        self.copied  = copied
        self.skipped = skipped
        self.errors  = errors

    def push(self, ok=(), sk=(), er=(), *, force: bool = False) -> None:
        with self._lock:
            if ok: self._ok.extend(ok)
            if sk: self._sk.extend(sk)
            if er: self._er.extend(er)
            n = len(self._ok) + len(self._sk) + len(self._er)
            if n == 0 or (not force and n < _FLUSH_THRESHOLD):
                return
            payload_ok, self._ok = self._ok, []
            payload_sk, self._sk = self._sk, []
            payload_er, self._er = self._er, []
            self.done += n
            self.copied += len(payload_ok)
            self.skipped += len(payload_sk)
            self.errors += len(payload_er)
            done_snap = self.done
        self._signal.emit(payload_ok, payload_sk, payload_er, done_snap, self._total)

    def flush(self) -> None:
        self.push(force=True)


class CopyWorker(QThread):
    batch_update  = pyqtSignal(list, list, list, int, int)
    finished_work = pyqtSignal(int, int, int, bool)
    scan_progress = pyqtSignal(str, int)
    entry_status  = pyqtSignal(str, int, int, int)
    scan_finished = pyqtSignal(int)

    def __init__(self, tasks) -> None:
        super().__init__()
        self.tasks   = self._normalize_tasks(tasks)
        self._cancel = threading.Event()

    @staticmethod
    def _normalize_tasks(tasks) -> list[tuple[str, str, str]]:
        normalized = []
        for t in tasks:
            if not isinstance(t, (list, tuple)) or len(t) < 2: continue
            src_raw, dst_raw = t[0], t[1]
            title = str(t[2]) if len(t) > 2 else ""

            srcs = [src_raw] if isinstance(src_raw, str) else src_raw
            dsts = [dst_raw] if isinstance(dst_raw, str) else dst_raw

            if not srcs or not dsts or len(srcs) != len(dsts): continue

            for s, d in zip(srcs, dsts):
                if s and d: normalized.append((os.path.expanduser(str(s)), os.path.expanduser(str(d)), title))

        return normalized

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        pw: "_SecurePw | None" = None
        try:
            user: str = ""
            guest: bool = False

            smb_tasks = [(s, d, t) for s, d, t in self.tasks if is_smb(s) or is_smb(d)]
            local_tasks = [(s, d, t) for s, d, t in self.tasks if not (is_smb(s) or is_smb(d))]

            if smb_tasks:
                user, pw = _get_smb_credentials()

            self.scan_progress.emit("Scanning", 0)

            smb_pre_errors: list[tuple[str, str]] = []
            if smb_tasks and not self._cancel.is_set():
                unreachable, auth_failed, guest = self._probe_shares(smb_tasks, user, pw)
                dead = unreachable | auth_failed
                if dead:
                    smb_tasks, smb_pre_errors = self._filter_dead_tasks(smb_tasks, dead, unreachable)

            smb_expanded: list = []
            smb_errors: list = list(smb_pre_errors)
            if smb_tasks and not self._cancel.is_set():
                scanner = _SmbScanner(user, pw, guest, self._cancel, lambda n: self.scan_progress.emit("Scanning SMB", n))
                exp, err = scanner.resolve(smb_tasks)
                smb_expanded.extend(exp)
                smb_errors.extend(err)

            local_pairs: list = []
            local_skipped: list = []
            local_errors: list = []
            if local_tasks and not self._cancel.is_set():
                local_pairs, local_skipped, local_errors = _scan_local(
                    local_tasks, self._cancel,
                    lambda n: self.scan_progress.emit("Scanning Local", n),
                )

            total_files = (len(smb_expanded) + len(smb_errors) + len(local_pairs) + len(local_skipped) + len(local_errors))
            self.scan_finished.emit(total_files)

            if self._cancel.is_set():
                self.finished_work.emit(0, 0, 0, True)
                return

            done = copied = skipped = errors = 0

            if smb_expanded or smb_errors:
                done, copied, skipped, errors = self._run_smb(
                    smb_expanded, smb_errors, user, pw, guest, done, total_files, self._cancel,
                )

            pre_sk = [(path, reason, 0) for path, reason, _ in local_skipped]
            pre_er = [(path, msg, 0) for path, msg, _ in local_errors]
            if pre_sk or pre_er:
                skipped += len(pre_sk)
                errors += len(pre_er)
                done += len(pre_sk) + len(pre_er)
                self.batch_update.emit([], pre_sk, pre_er, done, total_files)

            pre_counts: dict[str, list[int]] = {}
            for _, _, title in local_skipped:
                if title:
                    pre_counts.setdefault(title, [0, 0, 0])[2] += 1
            for _, _, title in local_errors:
                if title:
                    pre_counts.setdefault(title, [0, 0, 0])[1] += 1

            if local_pairs and not self._cancel.is_set():
                done, copied, skipped, errors = self._run_local(
                    local_pairs, done, total_files, copied, skipped, errors, self._cancel, pre_counts,
                )

            self.finished_work.emit(copied, skipped, errors, self._cancel.is_set())

        except Exception as exc:
            logger.error("CopyWorker critical error: %s", exc, exc_info=True)
            self.finished_work.emit(0, 0, 0, False)
        finally:
            if pw is not None:
                pw.clear()
            _clear_dir_cache()

    def _probe_shares(self, smb_tasks, user, pw) -> tuple[set, set, bool]:
        unreachable: set[tuple[str, str]] = set()
        auth_failed: set[tuple[str, str]] = set()
        guest = False
        seen: set[tuple[str, str]] = set()
        shares_to_probe = []
        for s, d, _ in smb_tasks:
            if self._cancel.is_set():
                break
            h, sh, _ = _parse_smb(s if is_smb(s) else d)
            if (h, sh) not in seen:
                seen.add((h, sh))
                shares_to_probe.append((h, sh))

        lock = threading.Lock()

        def probe_one(_h: str, _sh: str):
            if self._cancel.is_set(): return
            result = _SmbClient(_h, _sh, user, pw).probe()
            with lock:
                nonlocal guest
                if result == "timeout":
                    unreachable.add((_h, _sh))
                elif result == "auth":
                    auth_failed.add((_h, _sh))
                elif result == "guest" and not guest:
                    guest = True

        if shares_to_probe:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(shares_to_probe))) as pool:
                futs = [pool.submit(probe_one, h, sh) for h, sh in shares_to_probe]
                for fut in concurrent.futures.as_completed(futs):
                    if self._cancel.is_set():
                        for f in futs:
                            f.cancel()
                        break
                    fut.result()

        return unreachable, auth_failed, guest

    @staticmethod
    def _filter_dead_tasks(smb_tasks, dead_shares, unreachable_shares) -> tuple[list, list]:
        alive:      list[tuple[str, str, str]] = []
        pre_errors: list[tuple[str, str]]       = []
        for s, d, t in smb_tasks:
            h, sh, _ = _parse_smb(s if is_smb(s) else d)
            if (h, sh) in dead_shares:
                src_url = s if is_smb(s) else d
                reason  = ("NT_STATUS_HOST_UNREACHABLE"
                           if (h, sh) in unreachable_shares else "Authentication failed")
                pre_errors.append((src_url, reason))
            else:
                alive.append((s, d, t))
        return alive, pre_errors

    def _run_smb(self, smb_expanded, smb_exp_errors, user, pw, guest, done_in: int, total: int, cancel) -> tuple[int, int, int, int]:

        formatted_errors = [(src, err, 0) for src, err in smb_exp_errors]
        done = done_in
        if formatted_errors:
            done += len(formatted_errors)
            self.batch_update.emit([], [], formatted_errors, done, total)

        if not smb_expanded or cancel.is_set():
            return done, 0, 0, len(formatted_errors)

        share_groups: dict = {}
        for job in smb_expanded:
            grp = share_groups.setdefault((job.host, job.share), {"get": [], "put": []})
            grp["get" if job.kind == "smb_get" else "put"].append(job)

        ri_cache, ri_lock = {}, threading.Lock()
        flusher = _Flusher(self.batch_update, total,
                           done=done, copied=0, skipped=0, errors=len(formatted_errors))
        tracker = _EntryTracker()

        def run_share(host, share) -> None:
            client    = _SmbClient(host, share, user, pw, guest)
            processor = _ShareProcessor(client, cancel, flusher, tracker, ri_cache, ri_lock)
            processor.process(share_groups[(host, share)]["get"], share_groups[(host, share)]["put"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=_SMB_SHARE_WORKERS) as pool:

            futs = {pool.submit(run_share, h, sh): (h, sh) for (h, sh) in share_groups if not cancel.is_set()}
            for fut in concurrent.futures.as_completed(futs):
                if cancel.is_set():
                    for f in futs:
                        f.cancel()
                    break
                try:
                    fut.result()
                except Exception as exc:
                    logger.error("SMB share error: %s", exc)

        flusher.flush()
        tracker.emit_all(self.entry_status)
        return flusher.done, flusher.copied, flusher.skipped, flusher.errors

    def _run_local(self, pairs, done_in: int, total: int, copied_in: int, skipped_in: int, errors_in: int,
                   cancel, pre_counts: dict | None = None) -> tuple[int, int, int, int]:

        flusher = _Flusher(self.batch_update, total,
                           done=done_in, copied=copied_in, skipped=skipped_in, errors=errors_in)
        tracker = _EntryTracker()
        if pre_counts:
            tracker.merge_pre(pre_counts)

        with concurrent.futures.ThreadPoolExecutor(max_workers=_COPY_WORKERS) as pool:
            futures = {pool.submit(_copy_file, s, d, cancel): (s, d, t) for s, d, t in pairs}

            for fut in concurrent.futures.as_completed(futures):
                if cancel.is_set():
                    for f in futures:
                        f.cancel()
                    break
                src, dst, title = futures[fut]
                try:
                    status, aux, sz = fut.result()
                    if status == "ok":
                        flusher.push(ok=[(src, dst, sz)])
                        tracker.ok(title)
                    elif status == "skip":
                        flusher.push(sk=[(src, aux or "Up to date", sz)])
                        tracker.skip(title)
                    elif status == "error":
                        flusher.push(er=[(src, aux, 0)])
                        tracker.err(title)
                except Exception as exc:
                    flusher.push(er=[("Error", str(exc), 0)])
                    tracker.err(title)

        flusher.flush()
        tracker.emit_all(self.entry_status)
        return flusher.done, flusher.copied, flusher.skipped, flusher.errors


def _lbl(text: str, style: str) -> QLabel:
    w = QLabel(text)
    w.setStyleSheet(style)
    return w


def _make_card(color, title, val, size_title=0, size_val=0, bold_val=True,
               contents_margins=(16, 14, 16, 14), spacing=None):

    t = current_theme()
    s_title = size_title if size_title != 0 else font_sz(3)
    s_val = size_val if size_val != 0 else font_sz(16)

    frame = QFrame()
    frame.setMinimumWidth(240)

    border_style = f"border-left:4px solid {color};" if color else ""
    frame.setStyleSheet(f"QFrame {{background:{t['bg3']}; border-radius:8px; {border_style}}}")

    inner = QVBoxLayout(frame)
    inner.setContentsMargins(*contents_margins)
    if spacing is not None:
        inner.setSpacing(spacing)

    title_lbl = QLabel(title)
    title_lbl.setStyleSheet(_cached_mono_style(s_title, t["text_dim"], extra="border:none;"))

    val_lbl = QLabel(val)
    val_lbl.setStyleSheet(_cached_mono_style(s_val, color or t["text"], bold=bold_val, extra="border:none;"))
    val_lbl.setMinimumWidth(225 if color else 250)
    val_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)

    inner.addWidget(title_lbl)

    val_row = QHBoxLayout()
    val_row.setSpacing(5)
    val_row.setContentsMargins(5, 2, 5, 2)
    val_row.addWidget(val_lbl, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)

    size_lbl = QLabel("")
    if color:
        size_lbl.setText("0 B")
        size_lbl.setStyleSheet(_cached_mono_style(font_sz(14), color, extra="border:none;"))
        size_lbl.setMinimumWidth(200)
        size_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
        val_row.addWidget(size_lbl, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
    val_row.addStretch()
    inner.addLayout(val_row)

    return {"frame": frame, "val": val_lbl, "size_lbl": size_lbl}


class CopyDialog(QDialog):
    def __init__(self, parent, tasks, operation: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(operation)
        self._t = current_theme()
        t = self._t
        self.c_ok = t["success"]
        self.c_sk = t["warning"]
        self.c_er = t["error"]
        self._status_fs = font_sz(8)
        self._status_template = ("<span style='display:inline-block; font-size:{fs}px; font-weight:bold; "
                                 "font-family:monospace; color:{color}; background:{bg}; "
                                 "border-left:5px solid {border}; border-radius:7px; "
                                 "padding:6px 18px;'>{icon}&thinsp;{label}</span>")
        screen = QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else None
        if geo:
            self.setMinimumSize(min(1900, int(geo.width() * 0.9)), min(925, int(geo.height() * 0.9)))
        self._operation = operation
        self.worker = CopyWorker(tasks)
        self.copied = self.skipped = self.errors = 0
        self._done = self._total = 0
        self._final_elapsed = None
        self._pending_ok = _deque()
        self._pending_sk = _deque()
        self._pending_er = _deque()
        self._size_copied = self._size_skipped = 0
        self._summary = _SummaryWidget()
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(2)
        sep.setStyleSheet(f"background: {t['header_sep']}; border: none;")
        self._w_copied = _LogWidget(self.c_ok)
        self._w_skipped = _LogWidget(self.c_sk)
        self._w_errors = _LogWidget(self.c_er)
        summary_page = QWidget()
        sl = QVBoxLayout(summary_page)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(self._summary)
        self.tabs = QTabWidget()
        self.tabs.addTab(summary_page, "📋 Summary")
        self.tabs.addTab(self._w_copied, "⤵ Copied (0)")
        self.tabs.addTab(self._w_skipped, "↷ Skipped (0)")
        self.tabs.addTab(self._w_errors, "✗ Errors (0)")
        self.tabs.setStyleSheet(f"""QTabWidget::pane {{ border: none; }} QTabBar::tab {{ width: 200px; padding: 10px; }}
        QTabBar::tab:selected {{ background: {t['bg3']}; border-bottom: 2px solid {t['accent']}; }}""")
        self.cancel_btn = QPushButton("⏹ Cancel")
        self.cancel_btn.setMinimumHeight(50)
        self.cancel_btn.setStyleSheet(f"""QPushButton {{ background: {t['bg3']}; border: 1px solid {t['header_sep']}; 
        border-radius: 4px; }} QPushButton:hover {{ background: {t['bg2']}; }}""")
        self._cancel_connected = True
        self.cancel_btn.clicked.connect(self.worker.cancel)
        layout = QVBoxLayout(self)
        layout.setSpacing(3)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.addWidget(sep)
        layout.addWidget(self.tabs)
        layout.addWidget(self.cancel_btn)
        self._set_status_running()
        self.timer = QElapsedTimer()
        self.timer.start()
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._update_ui_tick)
        self._tick.start(500)
        self.worker.scan_finished.connect(self._on_scan_finished)
        self.worker.batch_update.connect(self._on_batch)
        self.worker.finished_work.connect(self._on_done)
        self.worker.scan_progress.connect(self._on_scan_progress)
        self.worker.entry_status.connect(self._summary.on_entry_status)
        self.worker.start()
        self._summary.refresh(self._operation, 0, 0, 0, 0, 0, 0, False)

    def _elapsed_s(self) -> int: return self._final_elapsed if self._final_elapsed is not None else self.timer.elapsed() // 1000

    def _status_html(self, icon: str, label: str, color: str, border: str) -> str:
        return self._status_template.format(
            fs=self._status_fs, icon=icon, label=label, color=color, bg=self._t["bg2"], border=border)

    def _set_status_running(self) -> None:
        html = self._status_html("⏳", f"{self._operation} running…", self._t["cyan"], self._t["accent"])
        self._summary.set_status_html(html)

    def _set_status_scanning(self, phase: str, scanned: int) -> None:
        html = self._status_html("🔍", f"{phase}… ({scanned:,} found)", self._t["accent2"], self._t["accent2"])
        self._summary.set_status_html(html)

    def _set_status_finished(self, icon: str, label: str, color: str) -> None:
        html = self._status_html(icon, label, color, color)
        self._summary.set_status_html(html)

    def _on_scan_finished(self, total: int) -> None:
        suffix = "file" if total == 1 else "files"
        html = self._status_html("📂", f"Scan complete — {total:,} {suffix} found", self._t["accent"], self._t["accent"])
        self._summary.set_status_html(html)

    def _update_tab_labels(self) -> None:
        self.tabs.setTabText(1, f"⤵ Copied ({self.copied:,})")
        self.tabs.setTabText(2, f"↷ Skipped ({self.skipped:,})")
        self.tabs.setTabText(3, f"✗ Errors ({self.errors:,})")

    @staticmethod
    def _fmt_ok(s, d) -> str: return f"{apply_replacements(s)}\n Copied to ⤵\n{apply_replacements(d)}"

    @staticmethod
    def _fmt_sk(p, r) -> str: return f"{apply_replacements(p)} ↷ {r}"

    @staticmethod
    def _fmt_er(p, m) -> str: return f"{apply_replacements(p)} ❌ {m}"

    def _update_ui_tick(self) -> None:
        self._summary.update_elapsed(self._elapsed_s(), self._done, self._total)
        if not (self._pending_ok or self._pending_sk or self._pending_er):
            return
        max_per = 250
        def process_batch(pending: _deque, widget, fmt):
            if not pending:
                return 0
            n     = min(max_per, len(pending))
            batch = [pending.popleft() for _ in range(n)]
            widget.bulk_add([fmt(*args) for args in batch])
            return n
        processed = (process_batch(self._pending_ok, self._w_copied, self._fmt_ok) +
                     process_batch(self._pending_sk, self._w_skipped, self._fmt_sk) +
                     process_batch(self._pending_er, self._w_errors, self._fmt_er))
        if processed == 0:
            return
        self._update_tab_labels()
        self._summary.card_copied["val"].setText(f"{self.copied:,}")
        self._summary.card_copied["size_lbl"].setText(_format_unit(self._size_copied))
        self._summary.card_skipped["val"].setText(f"{self.skipped:,}")
        self._summary.card_skipped["size_lbl"].setText(_format_unit(self._size_skipped))
        self._summary.card_errors["val"].setText(f"{self.errors:,}")
        total_bytes = self._size_copied + self._size_skipped
        self._summary.update_total_size(total_bytes)

    def _on_scan_progress(self, phase: str, scanned: int) -> None: self._set_status_scanning(phase, scanned)

    def _on_batch(self, ok, sk, er, done, total) -> None:
        self._done, self._total = done, total
        self.copied  += len(ok)
        self.skipped += len(sk)
        self.errors  += len(er)
        for s, d, sz in ok:
            self._size_copied += sz
            self._pending_ok.append((s, d))
        for s, r, sz in sk:
            self._size_skipped += sz
            self._pending_sk.append((s, r))
        self._pending_er.extend((s, m) for s, m, _sz in er)
        if total > 0:
            self._summary.update_progress(done, total)
            self._summary.update_total_size(self._size_copied + self._size_skipped)

    def _on_done(self, c, s, e, cancelled) -> None:
        self._tick.stop()
        self._final_elapsed = self.timer.elapsed() // 1000
        for pending, widget, fmt in zip((self._pending_ok, self._pending_sk, self._pending_er),
                                        (self._w_copied, self._w_skipped, self._w_errors),
                                        (self._fmt_ok, self._fmt_sk, self._fmt_er)):
            if pending:
                widget.bulk_add([fmt(*args) for args in pending])
                pending.clear()
        if not cancelled:
            self.copied, self.skipped, self.errors = c, s, e
        elapsed = self._final_elapsed
        tstr = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        if cancelled: icon, label, col = "⏹", f"Cancelled after {tstr}", self.c_sk
        elif e > 0: icon, label, col = "⚠", f"Done with errors ✗ — {tstr}", self.c_er
        else: icon, label, col = "✓", f"Done — {tstr}", self.c_ok
        self._set_status_finished(icon, label, col)
        self._summary.refresh(
            self._operation, self._done, self._total, self.copied, self.skipped, self.errors, elapsed, True, cancelled)
        self._summary.card_copied["size_lbl"].setText(_format_unit(self._size_copied))
        self._summary.card_skipped["size_lbl"].setText(_format_unit(self._size_skipped))
        self._update_tab_labels()
        for w in (self._w_copied, self._w_skipped, self._w_errors): w.flush_final()
        if self._cancel_connected:
            try:
                self.cancel_btn.clicked.disconnect(self.worker.cancel)
            except RuntimeError: pass
            self._cancel_connected = False
        self.cancel_btn.setText("✓ Close")
        self.cancel_btn.clicked.connect(self.accept)

    def keyPressEvent(self, event) -> None:
        k = event.key()
        if k in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            focused = self.focusWidget()
            if isinstance(focused, QPushButton):
                focused.click()
        elif k == Qt.Key.Key_Escape:
            self.worker.cancel() if self.worker.isRunning() else self.accept()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if self.worker.isRunning():
            self.worker.cancel()
            event.ignore()
        else:
            super().closeEvent(event)


class _SummaryWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._t = current_theme()
        t = self._t
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)
        self._op_name = "-"
        self._status_html_content = ""
        self._total_size_bytes = 0
        self._style_mono_bold = _cached_mono_style(font_sz(10), t["accent"], bold=True)
        self._style_transparent = "border:none; background:transparent;"
        self._style_total_lbl = "font-size: 20px; border:none; background:transparent;"
        self._style_card_base = f"QFrame{{background:{t['bg3']}; border-radius:8px;}}"
        self._style_entry_label = _cached_mono_style(font_sz(-2), t["text"], extra="border:none; padding:2px 0px;")
        self._style_scroll_area = "QScrollArea { background:transparent; border:none; }"
        self.header_card = QFrame()
        self.header_card.setObjectName("headerCard")
        self.header_card.setStyleSheet(
            f"#headerCard{{background:{t['bg3']}; border-radius:10px; border-left:4px solid {t['accent']};}}")
        hdr = QGridLayout(self.header_card)
        hdr.setContentsMargins(20, 15, 20, 15)
        hdr.setColumnStretch(0, 1)
        hdr.setColumnStretch(1, 1)
        hdr.setColumnStretch(2, 1)
        self.op_lbl = QLabel("-")
        self.op_lbl.setStyleSheet(self._style_mono_bold)
        self._status_center_lbl = QLabel()
        self._status_center_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._status_center_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_center_lbl.setStyleSheet(self._style_transparent)
        self._state_lbl = QLabel()
        self._state_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._state_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.total_lbl = QLabel("")
        self.total_lbl.setStyleSheet(self._style_total_lbl)
        self.total_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self.op_lbl, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self._status_center_lbl, 0, 1, Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        right_col = QVBoxLayout()
        right_col.setSpacing(4)
        right_col.addWidget(self._state_lbl)
        right_col.addWidget(self.total_lbl)
        hdr.addLayout(right_col, 0, 2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        stats_lay = QGridLayout()
        stats_lay.setSpacing(12)
        stats_lay.setColumnStretch(0, 1)
        stats_lay.setColumnStretch(1, 1)
        stats_lay.setColumnStretch(2, 1)
        self.card_copied = _make_card(t["success"], "⤵ Copied", "0")
        self.card_skipped = _make_card(t["warning"], "↷ Skipped", "0")
        self.card_errors = _make_card(t["error"], "✗ Errors", "0")
        for col, card in enumerate((self.card_copied, self.card_skipped, self.card_errors)):
            stats_lay.addWidget(card["frame"], 0, col)
        self._progress_card = QFrame()
        self._progress_card.setObjectName("progressCard")
        self._progress_card.setStyleSheet(self._style_card_base)
        prog_lay = QVBoxLayout(self._progress_card)
        prog_lay.setContentsMargins(20, 14, 20, 14)
        prog_lay.setSpacing(8)
        prog_hdr = QHBoxLayout()
        prog_hdr.addWidget(_lbl("Progress", _cached_mono_style(font_sz(2), t["text_dim"], extra="border:none;")))
        prog_hdr.addStretch()
        self._prog_pct = QLabel("0%")
        self._prog_pct.setFixedWidth(60)
        self._prog_pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._prog_pct.setStyleSheet(_cached_mono_style(font_sz(2), t["text"], bold=True, extra="border:none;"))
        prog_hdr.addWidget(self._prog_pct)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("0%  —  0 / 0 files")
        self._progress_bar.setMinimumHeight(32)
        prog_lay.addLayout(prog_hdr)
        prog_lay.addWidget(self._progress_bar)
        metrics_lay = QHBoxLayout()
        metrics_lay.setSpacing(12)
        kw = dict(size_title=font_sz(2), size_val=font_sz(10), contents_margins=(16, 12, 16, 12), spacing=4)
        self._card_elapsed = _make_card(None, "⏲️ Elapsed", "--:--", **kw)
        self._card_speed = _make_card(None, "🚤 Speed", "---", **kw)
        self._card_eta = _make_card(None, "🏁 ETA", "--:--", **kw)
        for card in (self._card_elapsed, self._card_speed, self._card_eta): metrics_lay.addWidget(card["frame"])
        self._rate_card = QFrame()
        self._rate_card.setObjectName("rateCard")
        self._rate_card.setStyleSheet(self._style_card_base)
        rate_lay = QVBoxLayout(self._rate_card)
        rate_lay.setContentsMargins(20, 14, 20, 14)
        rate_lay.setSpacing(8)
        bd_lbl = _lbl("File breakdown", _cached_mono_style(font_sz(2), t["text_dim"], extra="border:none;"))
        bd_lbl.setMinimumHeight(22)
        rate_lay.addWidget(bd_lbl)
        self._seg_track = QFrame()
        self._seg_track.setFixedHeight(10)
        self._seg_track.setStyleSheet(f"background:{t['header_sep']}; border-radius:5px;")
        seg_row = QHBoxLayout(self._seg_track)
        seg_row.setSpacing(0)
        seg_row.setContentsMargins(0, 0, 0, 0)
        self._seg_copied = QFrame()
        self._seg_skipped = QFrame()
        self._seg_errors = QFrame()
        self._seg_copied.setStyleSheet(f"background:{t['success']}; border-radius:5px 0 0 5px;")
        self._seg_skipped.setStyleSheet(f"background:{t['warning']};")
        self._seg_errors.setStyleSheet(f"background:{t['error']}; border-radius:0 5px 5px 0;")
        for seg in (self._seg_copied, self._seg_skipped, self._seg_errors):
            seg.setFixedHeight(10)
            seg.setFixedWidth(0)
            seg_row.addWidget(seg, 0)
        seg_row.addStretch(1)
        legend_row = QHBoxLayout()
        legend_row.setSpacing(20)
        legend_style = _cached_mono_style(font_sz(), t["text"], extra="border:none;")
        for key, text in (("success", "Copied"), ("warning", "Skipped"), ("error", "Errors")):
            dot = QLabel(f"<span style='color:{t[key]}'>■</span>  {text}")
            dot.setStyleSheet(legend_style)
            dot.setMinimumHeight(20)
            legend_row.addWidget(dot)
        legend_row.addStretch()
        rate_lay.addWidget(self._seg_track)
        rate_lay.addLayout(legend_row)
        self._entry_card = QFrame()
        self._entry_card.setObjectName("entryCard")
        self._entry_card.setStyleSheet(self._style_card_base)
        entry_lay = QVBoxLayout(self._entry_card)
        entry_lay.setContentsMargins(15, 10, 15, 10)
        entry_lay.setSpacing(5)
        entry_lay.addWidget(
            _lbl("Entries processed", _cached_mono_style(font_sz(2), t["text_dim"], extra="border:none;")))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(self._style_scroll_area)
        self._entry_scroll = scroll
        self._entry_list_widget = QWidget()
        self._entry_list_widget.setStyleSheet("background:transparent;")
        self._entry_grid = QGridLayout(self._entry_list_widget)
        self._entry_grid.setContentsMargins(1, 0, 1, 0)
        self._entry_grid.setHorizontalSpacing(5)
        self._entry_grid.setVerticalSpacing(8)
        scroll.setWidget(self._entry_list_widget)
        entry_lay.addWidget(scroll)
        self._entry_row_labels: dict = {}
        self._entry_results: dict = {}
        self._entry_grid_cols = 1
        self._last_total = 0
        lay.addWidget(self.header_card)
        lay.addLayout(stats_lay)
        lay.addLayout(metrics_lay)
        lay.addWidget(self._progress_card)
        lay.addWidget(self._rate_card)
        self._entry_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self._entry_card)

    def set_status_html(self, html: str) -> None:
        self._status_html_content = html
        self._refresh_op_lbl()

    def update_total_size(self, total_bytes: int) -> None:
        self._total_size_bytes = total_bytes
        self._refresh_op_lbl()

    def _refresh_op_lbl(self) -> None: self._status_center_lbl.setText(self._status_html_content)

    def _recalculate_grid(self) -> None:
        new_cols = max(1, (self._entry_card.width() - 40) // 280)
        if new_cols == self._entry_grid_cols:
            return
        self._entry_grid_cols = new_cols
        old_count = self._entry_grid.columnCount()
        for i in range(max(old_count, new_cols + 1)): self._entry_grid.setColumnStretch(i, 0)
        for i in range(new_cols): self._entry_grid.setColumnStretch(i, 1)

    def on_entry_status(self, title: str, ok: int, err: int, skip: int = 0) -> None:
        ec = self._entry_results.setdefault(title, [0, 0, 0])
        ec[0] += ok; ec[1] += err; ec[2] += skip
        self._refresh_entry_labels()

    @staticmethod
    def _html_title(title: str) -> str:
        return (title.replace("&", "&amp;").replace("<br>",  "\x00").replace("<BR>",  "\x00")
        .replace("<br/>", "\x00").replace("<BR/>", "\x00").replace("<", "&lt;").replace(">", "&gt;")
        .replace("\r\n", "<br>").replace("\x00", "<br>").replace("\r",   "<br>").replace("\n",   "<br>"))

    @staticmethod
    def _count_html_lines(html: str) -> int:
        return html.count("<br>") + 1

    def _refresh_entry_labels(self) -> None:
        self._recalculate_grid()
        t = self._t
        cols = self._entry_grid_cols
        needs_rebuild = False
        results_snapshot = {k: v[:] for k, v in self._entry_results.items()}
        for title, ec in results_snapshot.items():
            ok, err, skip = ec
            parts = []
            if ok: parts.append(f"<span style='color:{t['success']}'>⤵ {ok:,}</span>")
            if skip: parts.append(f"<span style='color:{t['warning']}'>↷ {skip:,}</span>")
            if err: parts.append(f"<span style='color:{t['error']}'>✗ {err:,}</span>")
            suffix = "&nbsp; ".join(parts) if parts else f"<span style='color:{t['text_dim']}'>–</span>"
            safe_title = self._html_title(title)
            html = f"<span style='color:{t['text']}'>{safe_title}</span><br>{suffix}"
            if title not in self._entry_row_labels:
                lbl = QLabel()
                lbl.setTextFormat(Qt.TextFormat.RichText)
                lbl.setWordWrap(False)
                lbl.setStyleSheet(self._style_entry_label)
                self._entry_row_labels[title] = lbl
                needs_rebuild = True
            lbl = self._entry_row_labels[title]
            lbl.setText(html)
        if needs_rebuild:
            while self._entry_grid.count():
                item = self._entry_grid.takeAt(0)
                if item.widget(): item.widget().hide()
            for idx, title in enumerate(sorted(self._entry_row_labels)):
                row, col = divmod(idx, cols)
                lbl = self._entry_row_labels[title]
                lbl.show()
                self._entry_grid.addWidget(lbl, row, col, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self._entry_list_widget.adjustSize()

    def _update_segments(self, copied: int, skipped: int, errors: int, finished: bool = False) -> None:
        if not finished:
            for seg in (self._seg_copied, self._seg_skipped, self._seg_errors): seg.setFixedWidth(0)
            return
        total = copied + skipped + errors
        if total == 0:
            for seg in (self._seg_copied, self._seg_skipped, self._seg_errors): seg.setFixedWidth(0)
            return
        avail = self._rate_card.width() - 40
        for seg, count in ((self._seg_copied, copied), (self._seg_skipped, skipped), (self._seg_errors, errors)):
            seg.setFixedWidth(max(0, int(avail * count / total)))

    def refresh(self, operation, done, total, copied, skipped, errors, elapsed_s, finished, cancelled=False) -> None:
        self._op_name = operation
        self.op_lbl.setText(operation)
        self._refresh_op_lbl()
        size_str = _format_unit(self._total_size_bytes)
        self.total_lbl.setText(f"{total:,} files / {size_str}" if total >= 0 else "")
        self.card_copied["val"].setText(f"{copied:,}")
        self.card_skipped["val"].setText(f"{skipped:,}")
        self.card_errors["val"].setText(f"{errors:,}")
        self._last_total = total
        self._update_progress_card(done, total, finished, cancelled)
        self._update_segments(copied, skipped, errors, finished=finished)
        self._update_time_lbl(elapsed_s, done, total, finished, cancelled)

    def _update_progress_card(self, done, total, finished=False, cancelled=False) -> None:
        if finished and cancelled:
            self._progress_bar.setRange(0, 1)
            self._progress_bar.setValue(0)
            self._prog_pct.setText("—")
            self._progress_bar.setFormat("Cancelled")
        elif total > 0:
            pct = int(done * 100 / total)
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(done)
            self._prog_pct.setText(f"{pct}%")
            self._progress_bar.setFormat(f"{pct}%  —  {done:,} / {total:,} files")
        else:
            self._progress_bar.setRange(0, 0)
            self._progress_bar.setValue(0)
            self._prog_pct.setText("…")
            self._progress_bar.setFormat("Scanning…")

    def update_elapsed(self, elapsed_s, done, total, finished=False) -> None:
        self._update_progress_card(done, total, finished)
        self._update_time_lbl(elapsed_s, done, total, finished=finished)

    def update_progress(self, done, total) -> None: self._update_progress_card(done, total)

    def _update_time_lbl(self, elapsed_s, done, total, finished, cancelled=False) -> None:
        mins, secs = divmod(elapsed_s, 60)
        speed_str  = "---"
        eta_str    = "--:--"
        if elapsed_s > 0 and done > 0:
            rate      = done / elapsed_s
            speed_str = f"{rate:,.1f} files/s" if rate >= 1 else f"1 file/{1 / rate:.1f}s"
            if not finished and total > done:
                eta_s   = int((total - done) / rate)
                eta_str = f"{eta_s // 60:02d}:{eta_s % 60:02d}"
            elif finished: eta_str = "Cancelled" if cancelled else "Done"
        self._card_elapsed["val"].setText(f"{mins:02d}:{secs:02d}")
        self._card_speed["val"].setText(speed_str)
        self._card_eta["val"].setText(eta_str)


class _LogWidget(QWidget):
    _PAGE = 500

    def __init__(self, color: str) -> None:
        super().__init__()
        self._t = current_theme()
        t = self._t
        self._items: list[str] = []
        self._filtered: list[str] = []
        self._page = 0
        self._finalized = False
        self._last_rendered_content = ""
        self._search_cache: dict[str, list[str]] = {}
        style_view = f"font-family:monospace; font-size:{font_sz(-1)}px; color:{color}; background:transparent; border:none;"
        style_search = (f"QLineEdit {{ background:{t['bg3']}; border:1px solid {t['header_sep']}; border-radius:6px; "
                        f"padding:0 10px; color:{t['text']}; }}")
        style_spin = (
            f"QSpinBox{{border:1px solid {t['header_sep']}; border-radius:4px; padding:2px 5px; background:{t['bg3']}; "
            f"color:{t['text']}; font-weight:bold}} QSpinBox:focus{{border:1px solid {t['accent']}; background:{t['bg2']}}}")
        style_muted = f"color:{t['muted']}; font-size:{font_sz()}px; margin-left:10px;"
        style_btn = (f"QPushButton {{ background:{t['bg3']}; border:1px solid {t['header_sep']}; border-radius:4px; "
                     f"padding:2px 8px; }} QPushButton:hover {{ background:{t['bg2']}; }}")
        self._search = QLineEdit()
        self._search.setPlaceholderText(" 🔍  Search…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        self._search.setMinimumHeight(44)
        self._search.setStyleSheet(style_search)
        self._view = QTextEdit()
        self._view.setReadOnly(True)
        self._view.setStyleSheet(style_view)
        self._first = QPushButton("««")
        self._prev = QPushButton("‹ Prev")
        self._next = QPushButton("Next ›")
        self._last = QPushButton("»»")
        for btn, cb in ((self._first, lambda: self._go(0)), (self._prev, lambda: self._go(self._page - 1)),
                        (self._next, lambda: self._go(self._page + 1)), (self._last, lambda: self._go(self._pages() - 1))):
            btn.clicked.connect(cb)
            btn.setMinimumHeight(28)
            btn.setStyleSheet(style_btn)
        self._spin = QSpinBox()
        self._spin.setMinimum(1)
        self._spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._spin.setStyleSheet(style_spin)
        self._spin.setMinimumHeight(28)
        self._spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spin.editingFinished.connect(self._spin_changed)
        self._page_lbl = QLabel("")
        self._page_lbl.setMinimumHeight(28)
        self._total_lbl = QLabel("")
        self._total_lbl.setStyleSheet(style_muted)
        self._total_lbl.setMinimumHeight(28)
        nav = QHBoxLayout()
        nav.setContentsMargins(5, 5, 5, 5)
        nav.setSpacing(8)
        nav.addWidget(self._first)
        nav.addWidget(self._prev)
        nav.addStretch(1)
        pg = QHBoxLayout()
        pg.setSpacing(5)
        for w in (QLabel("Page"), self._spin, QLabel("of"), self._page_lbl):
            w.setMinimumHeight(28)
            pg.addWidget(w)
        nav.addLayout(pg)
        nav.addStretch(1)
        nav.addWidget(self._total_lbl)
        nav.addWidget(self._next)
        nav.addWidget(self._last)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(3)
        lay.addWidget(self._search)
        lay.addWidget(self._view)
        lay.addLayout(nav)

    def _pages(self) -> int: return max(1, (len(self._filtered) + self._PAGE - 1) // self._PAGE)

    def _go(self, page: int) -> None:
        self._page = max(0, min(page, self._pages() - 1))
        self._render()

    def _spin_changed(self) -> None:
        target = self._spin.value() - 1
        if target != self._page: self._go(target)

    def _render(self) -> None:
        pages = self._pages()
        start = self._page * self._PAGE
        chunk = self._filtered[start:start + self._PAGE]
        lines = []
        for i, item in enumerate(chunk):
            idx = start + i + 1
            parts = item.split("\n")
            lines.append(f"{idx:,}: {parts[0]}")
            lines.extend(parts[1:])
            lines.append("")
        new_text = "\n".join(lines)
        if new_text != self._last_rendered_content:
            self._view.setPlainText(new_text)
            self._last_rendered_content = new_text
        total = len(self._filtered)
        self._page_lbl.setText(f"<b>{pages}</b>")
        self._total_lbl.setText(f"({total:,} {'entry' if total == 1 else 'entries'})")
        self._spin.blockSignals(True)
        self._spin.setMaximum(pages)
        self._spin.setValue(self._page + 1)
        self._spin.blockSignals(False)
        can_back = self._page > 0
        can_fwd = self._page < pages - 1
        self._first.setEnabled(can_back)
        self._prev.setEnabled(can_back)
        self._next.setEnabled(can_fwd)
        self._last.setEnabled(can_fwd)

    def bulk_add(self, entries: list[str]) -> None:
        if not entries: return
        needle = self._search.text().lower().strip()
        self._items.extend(entries)
        if not self._finalized:
            if needle: self._filtered.extend(e for e in entries if needle in e.lower())
            else: self._filtered.extend(entries)
            self._search_cache.clear()

    def flush_final(self) -> None:
        self._finalized = True
        self._items.sort(key=self._natural_sort_key)
        needle = self._search.text().lower().strip()
        self._filtered = [i for i in self._items if not needle or needle in i.lower()]
        self._page = 0
        self._render()

    def _on_search(self) -> None:
        needle = self._search.text().lower().strip()
        if needle in self._search_cache: self._filtered = self._search_cache[needle][:]
        else:
            self._filtered = [i for i in self._items if not needle or needle in i.lower()]
            if len(self._search_cache) > 50: self._search_cache.pop(next(iter(self._search_cache)))
            self._search_cache[needle] = self._filtered[:]
        self._page = 0
        self._render()

    @staticmethod
    def _natural_sort_key(s: str) -> list:
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]
