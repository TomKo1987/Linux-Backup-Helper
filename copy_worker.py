import concurrent.futures
import errno
import html
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from collections import deque as _deque
from dataclasses import dataclass
from functools import lru_cache
from itertools import groupby, islice
from pathlib import PurePosixPath
from typing import Optional, Protocol
from urllib.parse import urlparse

from PyQt6.QtCore import Qt, QElapsedTimer, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QProgressBar, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QApplication, QWidget,
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QSpinBox, QTextEdit,
)

from drive_utils import is_smb
from state import apply_replacements, logger
from themes import current_theme, font_sz

_CHUNK           =  16 * 1024 * 1024
_IO_BUF          =   4 * 1024 * 1024
_SCAN_WORKERS    = min(4, max(2, os.cpu_count() or 2))
_COPY_WORKERS    = min(4, max(2, os.cpu_count() or 2))
_SMB_WORKERS     = 10
_SMB_PROBE_TO    = 5
_SMB_BASE_TO     = 15
_SMB_FILE_SECS   = 3
_SMB_LS_TO       = 15
_SMB_CHUNK       = 200
_FLUSH_THRESH    = 1500
_SCAN_EMIT_SECS  = 0.5
_SCAN_PIPE_BATCH = 64
_LOCAL_BATCH     = 128
_CLAIM_SIZE      = 16
_PIPE_MAXSIZE    = 8_192


_SKIP_RE = re.compile(
    r"(^\.?lock$|\.lock$|^lockfile$|Singleton\w*$|cookies\.sqlite-wal$|\.lck$)", re.I
)

def _scale_params(total: int) -> tuple[int, int, int]:
    if total >= 100_000: return 256, 2048, 20_000
    if total >=  50_000: return 128, 1024, 10_000
    if total >=  10_000: return  64,  512,  5_000
    if total >=   2_000: return  32,  256,  2_500
    return _CLAIM_SIZE, _LOCAL_BATCH, _FLUSH_THRESH

_SMB_LINE_RE = re.compile(
    r"^(.+?)\s+([ADRHNSV]*)\s*(?:\(.*?\)\s*)?(\d+)"
    r"\s+\w{3}\s+\w{3}\s+[\s\d]\d\s+[\d:]+\s*\d*$"
)

_SMB_DOWN = frozenset((
    "HOST IS DOWN", "NT_STATUS_HOST_UNREACHABLE", "NT_STATUS_IO_TIMEOUT", "NT_STATUS_CONNECTION_REFUSED",
    "NT_STATUS_NETWORK_UNREACHABLE", "NT_STATUS_CONNECTION_RESET", "NT_STATUS_CONNECTION_DISCONNECTED",
    "CONNECTION REFUSED", "NO ROUTE TO HOST", "NETWORK IS UNREACHABLE", "CONNECTION TIMED OUT", "TIMEOUT"
))

_CACHE_MISS = object()

_O_NOATIME = os.O_NOATIME
_PID       = os.getpid()
_tls       = threading.local()
_SHM_DIR: "str | None" = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None

def _thread_ident() -> int:
    try:
        return _tls.ident
    except AttributeError:
        _tls.ident = threading.get_ident()
        return _tls.ident

_smb_procs:      dict[int, "subprocess.Popen"] = {}
_smb_procs_lock: threading.Lock                = threading.Lock()

def _ensure_dir(path: str) -> bool:
    if not path:
        return True
    tls_seen: set = _tls.__dict__.setdefault("seen_dirs", set())
    if path in tls_seen:
        return True
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        logger.error("mkdir %s: %s", path, exc)
        return False
    tls_seen.add(path)
    return True

@lru_cache(maxsize=256)
def _cached_mono_style(size: int, color: str, bold: bool = False, extra: str = "") -> str:
    s = f"font-family:monospace;font-size:{size}px;color:{color};"
    if bold:
        s += "font-weight:bold;"
    return s + extra

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")
def _format_unit(value: float, units: tuple = _SIZE_UNITS) -> str:
    if value <= 0:
        return f"0 {units[0]}"
    v = float(value)
    for i, unit in enumerate(units[:-1]):
        if v < 1024.0:
            return f"{int(v)} {unit}" if i == 0 else f"{v:.2f} {unit}"
        v /= 1024.0
    return f"{v:.2f} {units[-1]}"

def _is_unreachable(err: str) -> bool:
    up = err.upper()
    return any(s in up for s in _SMB_DOWN)

def _parse_smb(url: str) -> tuple[str, str, str]:
    p     = urlparse(url)
    host  = p.hostname or p.netloc
    parts = [x for x in p.path.split("/") if x]
    return host, (parts[0] if parts else ""), "/".join(parts[1:])

def _q(s: str) -> str:
    return s.replace("\n", "").replace("\r", "").replace("\\", "/").replace('"', '\\"')

class _SecurePw(Protocol):
    def get(self) -> str: ...
    def clear(self) -> None: ...

def _get_smb_credentials() -> tuple[str, "_SecurePw | None"]:
    from samba_credentials import SambaPasswordManager
    from sudo_password import SecureString
    u, p, _ = SambaPasswordManager().get_credentials()
    return (u or ""), (SecureString(p) if p else None)

def _smb_cred_file(user: str, pw: "_SecurePw") -> "tuple[str, str] | None":
    tmp_dir: "str | None" = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="smb_", dir=_SHM_DIR)
        os.chmod(tmp_dir, 0o700)
        path = os.path.join(tmp_dir, "cred")
        buf  = bytearray(f"username={user}\npassword={pw.get()}\n".encode("utf-8"))
        fd   = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, buf)
            os.fsync(fd)
        finally:
            os.close(fd)
            for i in range(len(buf)):
                buf[i] = 0
            del buf
        return tmp_dir, path
    except Exception as exc:
        logger.error("SMB cred file: %s", exc)
        if tmp_dir:
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass
        return None

def _wipe_smb_cred(tmp_dir: str, path: str) -> None:
    try:
        sz = os.stat(path).st_size
        fd = os.open(path, os.O_WRONLY)
        try:
            os.write(fd, os.urandom(sz))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.unlink(path)
    except OSError:
        pass
    try:
        os.rmdir(tmp_dir)
    except OSError:
        pass

def _is_up_to_date_local(dst: str, src_st: "os.stat_result") -> bool:
    try:
        d = os.stat(dst)
        return d.st_size == src_st.st_size and d.st_mtime_ns >= src_st.st_mtime_ns - 2_000_000_000
    except OSError:
        return False

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
        if exc.errno == errno.EXDEV:
            pass
        else:
            logger.warning("copy_file_range failed, falling back to read/write: %s", exc)
    if rem > 0:
        try:
            os.lseek(rfd, total - rem, os.SEEK_SET)
            while rem > 0:
                if cancel.is_set():
                    raise InterruptedError
                buf = os.read(rfd, min(rem, _IO_BUF))
                if not buf:
                    break
                written = os.write(wfd, buf)
                rem -= written
        except InterruptedError:
            raise
        except OSError as exc:
            logger.error("read/write fallback failed after %d/%d bytes: %s", total - rem, total, exc)
    return total - rem

def _copy_file(src: str, dst: str, cancel: threading.Event, src_st: "os.stat_result | None" = None) -> tuple[str, str, int]:
    tmp = f"{dst}.{_PID}.{_thread_ident()}.part"
    rfd = wfd = None
    success   = False
    try:
        if cancel.is_set():
            return "skip", "", 0
        try:
            st = src_st if src_st is not None else os.stat(src)
        except OSError:
            return "error", "Source unreadable", 0

        if _is_up_to_date_local(dst, st):
            return "skip", "Up to date", st.st_size

        if not _ensure_dir(os.path.dirname(dst)):
            return "error", "Directory could not be created", 0

        try:
            rfd = os.open(src, os.O_RDONLY | _O_NOATIME)
        except (PermissionError, OSError):
            rfd = os.open(src, os.O_RDONLY)

        wfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, st.st_mode & 0o777)

        if st.st_size > 0:
            try:
                os.posix_fallocate(wfd, 0, st.st_size)
                os.posix_fadvise(rfd, 0, st.st_size, os.POSIX_FADV_SEQUENTIAL)
            except OSError:
                pass

        copied = _copy_loop(rfd, wfd, st.st_size, cancel)
        if copied < st.st_size:
            raise OSError(f"Incomplete copy: {copied}/{st.st_size} bytes written")
        os.fdatasync(wfd)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, dst)
        success = True
        return "ok", dst, copied

    except InterruptedError:
        return "skip", "", 0
    except Exception as exc:
        logger.error("copy %s → %s: %s", src, dst, exc)
        return "error", str(exc), 0
    finally:
        if rfd is not None:
            os.close(rfd)
        if wfd is not None:
            os.close(wfd)
        if not success:
            try:
                os.unlink(tmp)
            except OSError:
                pass


@dataclass
class _SmbJob:
    src_url:     str
    dst_path:    str
    kind:        str
    host:        str
    share:       str
    remote_path: str
    remote_size: int = -1
    title:       str  = ""

    def size_matches_local(self) -> bool:
        if self.kind != "smb_get" or self.remote_size < 0:
            return False
        try:
            return self.remote_size == os.stat(self.dst_path).st_size
        except OSError:
            return False


def _build_smb_get_cmds(jobs: list[_SmbJob]) -> str:
    lines = []
    for rdir, group in groupby(
            sorted(jobs, key=lambda x: os.path.dirname(x.remote_path)),
            key=lambda x: os.path.dirname(x.remote_path)):
        lines.append(f'cd "/{_q(rdir)}"' if rdir else 'cd "/"')
        for j in group:
            lines.append(f'get "{_q(os.path.basename(j.remote_path))}" "{_q(j.dst_path)}"')
    lines.append("exit\n")
    return "\n".join(lines)


def _build_smb_put_cmds(jobs: list[_SmbJob]) -> str:
    rdirs       = sorted({str(PurePosixPath(j.remote_path).parent) for j in jobs})
    mkdir_lines = []
    seen_dirs:  set = set()
    for rdir in rdirs:
        if not rdir or rdir == ".":
            continue
        for p in [*reversed(PurePosixPath(rdir).parents), PurePosixPath(rdir)]:
            ps = str(p)
            if ps not in seen_dirs and ps != ".":
                seen_dirs.add(ps)
                mkdir_lines.append(f'mkdir "{_q(ps)}"')

    transfer_lines = []
    cur_local_dir = cur_rdir = None
    for j in sorted(jobs, key=lambda x: (os.path.dirname(x.remote_path), x.src_url)):
        local_dir = os.path.dirname(j.src_url)
        rdir      = os.path.dirname(j.remote_path).replace("\\", "/").strip("/")
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
        self.host   = host
        self.share  = share
        self._user  = user
        self._pw    = pw
        self._guest = guest
        self._argv  = (["smbclient", f"//{host}/{share}", "-N"] if guest else ["smbclient", f"//{host}/{share}"])

    def _run(self, argv: list[str], input_data: str, timeout: int) -> tuple[bool, str]:
        tid = _thread_ident()
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            with _smb_procs_lock:
                _smb_procs[tid] = proc
            try:
                _, err = proc.communicate(input=input_data, timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                logger.warning("SMB timeout after %ds (//%s/%s)", timeout, self.host, self.share)
                return False, "timeout"
            return proc.returncode == 0, (err.strip() or f"exit {proc.returncode}")
        except Exception as exc:
            logger.error("SMB run error: %s", exc)
            return False, str(exc)
        finally:
            with _smb_procs_lock:
                _smb_procs.pop(tid, None)

    def _argv_with_creds(self) -> "tuple[list[str], str | None, str | None]":
        if self._guest:
            return self._argv[:], None, None
        if not self._pw:
            return self._argv + ["-N"], None, None
        result = _smb_cred_file(self._user, self._pw)
        if result is None:
            logger.warning("SMB cred file unavailable for //%s/%s — falling back to guest", self.host, self.share)
            return self._argv + ["-N"], None, None
        tmp_dir, cred_path = result
        return self._argv + ["-A", cred_path], tmp_dir, cred_path

    def run(self, cmds: str, timeout: int) -> tuple[bool, str]:
        argv, tmp_dir, cred_path = self._argv_with_creds()
        try:
            return self._run(argv, cmds, timeout)
        finally:
            if tmp_dir and cred_path:
                _wipe_smb_cred(tmp_dir, cred_path)

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
        cmd  = (f'recurse on\nprompt off\ncd "{_q(base)}"\nls\n' if base else "recurse on\nprompt off\nls\n")
        argv, tmp_dir, cred_path = self._argv_with_creds()
        index: dict = {}
        tid = _thread_ident()
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            with _smb_procs_lock:
                _smb_procs[tid] = proc
            try:
                stdout, stderr = proc.communicate(input=cmd, timeout=_SMB_LS_TO)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return None
            finally:
                with _smb_procs_lock:
                    _smb_procs.pop(tid, None)
            if proc.returncode != 0:
                return None if _is_unreachable(stderr) else {}
            cur_dir = base
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("\\"):
                    cur_dir = line.replace("\\", "/").strip("/")
                    continue
                m = _SMB_LINE_RE.match(line)
                if m:
                    name, flags, size_s = m.groups()
                    name = name.strip()
                    if name not in (".", "..") and "D" not in flags:
                        index[f"{cur_dir}/{name}".lstrip("/")] = (int(size_s),)
        except Exception as exc:
            logger.error("SMB ls_index %s: %s", self.host, exc)
            return None
        finally:
            if tmp_dir and cred_path:
                _wipe_smb_cred(tmp_dir, cred_path)
        return index


class _SmbScanner:
    _CACHE_MAX = 1000

    def __init__(self, user: str, pw: "_SecurePw | None", guest: bool, cancel: threading.Event, progress_cb=None) -> None:
        self._user         = user
        self._pw           = pw
        self._guest        = guest
        self._cancel       = cancel
        self._progress_cb  = progress_cb
        self._ls_cache:    dict = {}
        self._cache_lock   = threading.Lock()
        self._result_lock  = threading.Lock()
        self._counter_lock = threading.Lock()
        self._counter      = 0

    def _client(self, host: str, share: str) -> _SmbClient: return _SmbClient(host, share, self._user, self._pw, self._guest)

    def _report(self, n: int) -> None:
        with self._counter_lock:
            self._counter += n
            cur = self._counter
        if self._progress_cb:
            self._progress_cb(cur)

    def resolve(self, jobs) -> tuple[list, list]:
        expanded: list = []
        errors:   list = []
        seen_get: set  = set()
        tasks:    list = []

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
                tasks.append(lambda h=host, sh=share, rp=rpath, d=dst, ti=title: self._do_get(h, sh, rp, d, ti, expanded, errors))
            elif os.path.isfile(src):
                tasks.append(lambda s=src, h=host, sh=share, rp=rpath, ti=title: self._do_put_file(s, h, sh, rp, ti, expanded))
            else:
                tasks.append(lambda s=src, h=host, sh=share, rp=rpath, ti=title: self._do_put_dir(s, h, sh, rp, ti, expanded))

        if self._cancel.is_set():
            return [], []

        with concurrent.futures.ThreadPoolExecutor(max_workers=_SMB_WORKERS) as pool:
            futs = [pool.submit(fn) for fn in tasks if not self._cancel.is_set()]
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

    def _cached_index(self, host: str, share: str, rpath: str) -> "dict | None":
        ck = f"{host}:{share}:{rpath}"
        with self._cache_lock:
            idx = self._ls_cache.get(ck, _CACHE_MISS)
            if idx is _CACHE_MISS and len(self._ls_cache) >= self._CACHE_MAX:
                del self._ls_cache[next(iter(self._ls_cache))]
        if idx is _CACHE_MISS:
            if self._cancel.is_set():
                return None
            idx = self._client(host, share).ls_index(rpath)
            with self._cache_lock:
                idx = self._ls_cache.setdefault(ck, idx)
        return idx

    def _do_get(self, host, share, rpath, dst, title, expanded, errors) -> None:
        idx     = self._cached_index(host, share, rpath)
        src_url = f"smb://{host}/{share}/{rpath}"
        lexp:   list = []
        lerr:   list = []
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

    def _do_put_file(self, src, host, share, rpath, title, expanded) -> None:
        if _SKIP_RE.search(os.path.basename(src)):
            return
        rp = f"{rpath}/{os.path.basename(src)}".lstrip("/")
        with self._result_lock:
            expanded.append(_SmbJob(src, "", "smb_put", host, share, rp, title=title))
        self._report(1)

    def _do_put_dir(self, src, host, share, rpath, title, expanded) -> None:
        lexp:  list = []
        stack: list = [src]
        while stack:
            if self._cancel.is_set():
                break
            try:
                with os.scandir(stack.pop()) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False) and not _SKIP_RE.search(e.name):
                            rel = os.path.relpath(e.path, src)
                            rp  = f"{rpath}/{rel}".replace(os.sep, "/").lstrip("/")
                            lexp.append(_SmbJob(e.path, "", "smb_put", host, share, rp, title=title))
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
    def host(self) -> str:  return self._client.host

    @property
    def share(self) -> str: return self._client.share

    def process(self, get_jobs: list, put_jobs: list) -> None:
        if self._cancel.is_set():
            return

        sk_immediate: list = []
        get_transfer: list = []

        for j in get_jobs:
            full_url = f"smb://{self.host}/{self.share}/{j.remote_path}"
            self._url_title[full_url] = (j.title, j.remote_size)
            if j.size_matches_local():
                sk_immediate.append((full_url, "Up to date"))
            else:
                get_transfer.append(j)

        put_transfer: list = []
        if put_jobs:
            ri = self._remote_index(put_jobs)
            for j in put_jobs:
                try:
                    local_sz = os.stat(j.src_url).st_size
                except OSError:
                    local_sz = 0
                self._url_title[j.src_url] = (j.title, local_sz)
                key  = j.remote_path.replace("\\", "/").lstrip("/")
                meta = ri.get(key)
                if meta and local_sz == meta[0]:
                    sk_immediate.append((j.src_url, "Up to date"))
                else:
                    put_transfer.append(j)

        for i in range(0, len(sk_immediate), _SMB_CHUNK):
            if self._cancel.is_set():
                break
            self._record([], sk_immediate[i: i + _SMB_CHUNK], [])

        if get_transfer and not self._cancel.is_set():
            for d in {os.path.dirname(j.dst_path) for j in get_transfer}:
                if d:
                    _ensure_dir(d)
            for i in range(0, len(get_transfer), _SMB_CHUNK):
                if self._cancel.is_set():
                    break
                if self._unreachable.is_set():
                    self._fail_batch(get_transfer[i:], is_get=True)
                    break
                ok_c, er_c = self._transfer(get_transfer[i: i + _SMB_CHUNK], _build_smb_get_cmds)
                self._record(ok_c, [], er_c)

        if put_transfer and not self._cancel.is_set():
            for i in range(0, len(put_transfer), _SMB_CHUNK):
                if self._cancel.is_set():
                    break
                if self._unreachable.is_set():
                    self._fail_batch(put_transfer[i:], is_get=False)
                    break
                ok_c, er_c = self._transfer(put_transfer[i: i + _SMB_CHUNK], _build_smb_put_cmds)
                self._record(ok_c, [], er_c)

    def _remote_index(self, put_jobs: list) -> dict:
        needed = {(os.path.dirname(j.remote_path).replace("\\", "/") or "").split("/")[0] for j in put_jobs}
        merged: dict = {}
        for top in needed:
            key = f"{self.host}:{self.share}:{top}"
            with self._ri_lock:
                cached = self._ri_cache.get(key, _CACHE_MISS)
            if cached is _CACHE_MISS:
                cached = self._client.ls_index(top)
                if cached is None:
                    with self._ri_lock:
                        self._ri_cache[key] = {}
                    logger.warning("SMB remote index unreachable //%s/%s", self.host, self.share)
                    break
                with self._ri_lock:
                    self._ri_cache[key] = cached
            merged.update(cached)
        return merged

    def _transfer(self, jobs: list, build_fn) -> tuple[list, list]:
        is_get  = build_fn is _build_smb_get_cmds
        ok_list: list = []
        er_list: list = []
        stack   = [list(jobs)]

        while stack:
            batch = stack.pop()
            if not batch or self._cancel.is_set():
                break
            if self._unreachable.is_set():
                er_list.extend((f"smb://{self.host}/{self.share}/{j.remote_path}"
                                if is_get else j.src_url, "NT_STATUS_HOST_UNREACHABLE") for j in batch)
                continue

            ok, err = self._client.run(build_fn(batch), max(_SMB_BASE_TO, len(batch) * _SMB_FILE_SECS))
            if ok:
                for j in batch:
                    src = (f"smb://{self.host}/{self.share}/{j.remote_path}" if is_get else j.src_url)
                    dst = (j.dst_path if is_get else f"smb://{self.host}/{self.share}/{j.remote_path}")
                    ok_list.append((src, dst))
            elif _is_unreachable(err):
                self._unreachable.set()
                er_list.extend((f"smb://{self.host}/{self.share}/{j.remote_path}" if is_get else j.src_url,
                                "NT_STATUS_HOST_UNREACHABLE") for j in batch)
            elif len(batch) == 1:
                src = (f"smb://{self.host}/{self.share}/{batch[0].remote_path}" if is_get else batch[0].src_url)
                er_list.append((src, err))
            else:
                mid = len(batch) // 2
                stack.append(batch[mid:])
                stack.append(batch[:mid])

        return ok_list, er_list

    def _record(self, ok_c: list, sk_c: list, er_c: list) -> None:
        def _meta(_url: str) -> tuple[str, int]:
            return self._url_title.get(_url, ("", 0))

        ok_w, batch_counts = [], {}
        for s, d in ok_c:
            title, sz = _meta(s)
            ok_w.append((s, d, sz))
            if title:
                batch_counts.setdefault(title, [0, 0, 0])[0] += 1

        sk_w = []
        for s, r in sk_c:
            title, sz = _meta(s)
            sk_w.append((s, r, sz))
            if title:
                batch_counts.setdefault(title, [0, 0, 0])[1] += 1

        er_w = [(s, e, 0) for s, e in er_c]
        for s, _e, _ in er_w:
            title = _meta(s)[0]
            if title:
                batch_counts.setdefault(title, [0, 0, 0])[2] += 1

        self._flusher.push(ok=ok_w, sk=sk_w, er=er_w)
        self._tracker.batch_update(batch_counts)

    def _fail_batch(self, remaining: list, *, is_get: bool) -> None:
        er_c = [(f"smb://{self.host}/{self.share}/{j.remote_path}" if is_get else j.src_url,
                 "NT_STATUS_HOST_UNREACHABLE") for j in remaining]
        if er_c:
            self._record([], [], er_c)


class _EntryTracker:
    __slots__ = ("_lock", "_counts")

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
                    c[1] += n_err
                    c[2] += n_skip

    def emit_all(self, signal) -> None:
        with self._lock:
            snap = {t: ec[:] for t, ec in self._counts.items()}
        for t, ec in snap.items():
            if t:
                signal.emit(t, ec[0], ec[1], ec[2])


class _Flusher:
    __slots__ = ("_signal", "_total", "_flush_thresh", "_lock", "_ok", "_sk", "_er", "done", "copied", "skipped", "errors", "_last_flush_t")

    def __init__(self, signal, total: int, flush_thresh: int = _FLUSH_THRESH) -> None:
        self._signal = signal
        self._total = total
        self._flush_thresh = flush_thresh
        self._lock = threading.Lock()
        self._ok: list = []
        self._sk: list = []
        self._er: list = []
        self.done = 0
        self.copied = 0
        self.skipped = 0
        self.errors = 0
        self._last_flush_t = time.monotonic()

    def set_total(self, total: int) -> None:
        with self._lock:
            self._total = total

    def set_flush_thresh(self, thresh: int) -> None:
        with self._lock:
            self._flush_thresh = thresh

    def push(self, ok=(), sk=(), er=(), *, force: bool = False) -> None:
        with self._lock:
            if ok: self._ok.extend(ok)
            if sk: self._sk.extend(sk)
            if er: self._er.extend(er)
            n = len(self._ok) + len(self._sk) + len(self._er)
            if n == 0:
                return
            now = time.monotonic()
            timed_out = (now - self._last_flush_t) >= 0.3
            if not force and n < self._flush_thresh and not timed_out:
                return
            payload_ok, self._ok = self._ok, []
            payload_sk, self._sk = self._sk, []
            payload_er, self._er = self._er, []
            self.done += n
            self.copied += len(payload_ok)
            self.skipped += len(payload_sk)
            self.errors += len(payload_er)
            done_snap = self.done
            total_snap = self._total
            self._last_flush_t = now
        self._signal.emit(payload_ok, payload_sk, payload_er, done_snap, total_snap)

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
        result = []
        for t in tasks:
            if not isinstance(t, (list, tuple)) or len(t) < 2:
                continue
            src_raw, dst_raw = t[0], t[1]
            title = str(t[2]) if len(t) > 2 else ""
            srcs  = [src_raw] if isinstance(src_raw, str) else src_raw
            dsts  = [dst_raw] if isinstance(dst_raw, str) else dst_raw
            if not srcs or not dsts or len(srcs) != len(dsts):
                continue
            for s, d in zip(srcs, dsts):
                if s and d:
                    result.append((os.path.expanduser(str(s)), os.path.expanduser(str(d)), title))
        return result

    def cancel(self) -> None:
        self._cancel.set()
        with _smb_procs_lock:
            for proc in list(_smb_procs.values()):
                try:
                    proc.kill()
                except OSError:
                    pass

    def run(self) -> None:
        pw: "_SecurePw | None" = None
        try:
            smb_tasks = [(s, d, t) for s, d, t in self.tasks if is_smb(s) or is_smb(d)]
            local_tasks = [(s, d, t) for s, d, t in self.tasks if not (is_smb(s) or is_smb(d))]

            user = ""
            if smb_tasks:
                user, pw = _get_smb_credentials()

            self.scan_progress.emit("Scanning", 0)

            if not smb_tasks:
                flusher = _Flusher(self.batch_update, 0)
                tracker = _EntryTracker()
                if local_tasks and not self._cancel.is_set():
                    self._scan_copy_local_pipelined(local_tasks, flusher, tracker)
                elif not self._cancel.is_set():
                    self.scan_finished.emit(0)
                if self._cancel.is_set():
                    self.finished_work.emit(0, 0, 0, True)
                    return
                flusher.flush()
                tracker.emit_all(self.entry_status)
                self.finished_work.emit(flusher.copied, flusher.skipped, flusher.errors, False)
                return

            local_items: list[tuple[str, str, str]] = []
            smb_expanded: list[_SmbJob] = []
            smb_errors: list[tuple[str, str]] = []
            _guest_box: list[bool] = [False]

            def _phase1_local() -> None:
                nonlocal local_items
                if local_tasks and not self._cancel.is_set():
                    local_items = self._scan_local_all(local_tasks)

            def _phase1_smb() -> None:
                nonlocal smb_expanded, smb_errors
                if not smb_tasks or self._cancel.is_set():
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
                p1_futs = [pool.submit(_phase1_local), pool.submit(_phase1_smb)]
                for fut in concurrent.futures.as_completed(p1_futs):
                    if self._cancel.is_set():
                        for f in p1_futs:
                            f.cancel()
                        break
                    try:
                        fut.result()
                    except Exception as exc:
                        logger.error("Phase-1 error: %s", exc, exc_info=True)

            if self._cancel.is_set():
                self.finished_work.emit(0, 0, 0, True)
                return

            guest = _guest_box[0]
            total = len(local_items) + len(smb_expanded) + len(smb_errors)
            cs, lb, ft = _scale_params(total)

            self.scan_finished.emit(total)

            flusher = _Flusher(self.batch_update, total, flush_thresh=ft)
            tracker = _EntryTracker()

            def _phase2_local() -> None:
                if local_items and not self._cancel.is_set():
                    self._copy_local_all(local_items, flusher, tracker, claim_size=cs, local_batch=lb)

            def _phase2_smb() -> None:
                if (smb_expanded or smb_errors) and not self._cancel.is_set():
                    self._copy_smb_all(smb_expanded, smb_errors, user, pw, guest, flusher, tracker)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                p2_futs = [pool.submit(_phase2_local), pool.submit(_phase2_smb)]
                for fut in concurrent.futures.as_completed(p2_futs):
                    if self._cancel.is_set():
                        for f in p2_futs:
                            f.cancel()
                        break
                    try:
                        fut.result()
                    except Exception as exc:
                        logger.error("Phase-2 error: %s", exc, exc_info=True)

            flusher.flush()
            tracker.emit_all(self.entry_status)
            self.finished_work.emit(flusher.copied, flusher.skipped, flusher.errors, self._cancel.is_set())

        except Exception as exc:
            logger.error("CopyWorker critical: %s", exc, exc_info=True)
            self.finished_work.emit(0, 0, 0, False)
        finally:
            if pw is not None:
                pw.clear()

    def _scan_local_all(self, tasks: list) -> list:
        cancel = self._cancel
        file_q = queue.SimpleQueue()

        work_q = queue.Queue()
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

        for src, dst, title in tasks:
            _enqueue((src, dst, title))

        def _worker() -> None:
            local_n = 0

            while not cancel.is_set():
                try:
                    _src, _dst, _title = work_q.get(timeout=0.1)
                except queue.Empty:
                    if all_done.is_set():
                        break
                    continue

                local_files: list = []

                try:
                    with os.scandir(_src) as it:
                        for e in it:
                            if cancel.is_set():
                                break
                            if e.is_dir(follow_symlinks=False):
                                _enqueue((e.path, os.path.join(_dst, e.name), _title))
                            elif (e.is_file(follow_symlinks=False)
                                  and not _SKIP_RE.search(e.name)):
                                try:
                                    est = e.stat(follow_symlinks=False)
                                except OSError:
                                    est = None
                                local_files.append((e.path, os.path.join(_dst, e.name), _title, est))
                except NotADirectoryError:
                    if not _SKIP_RE.search(os.path.basename(_src)):
                        local_files.append((_src, _dst, _title, None))
                except PermissionError:
                    pass
                except OSError as _exc:
                    logger.warning("scan %s: %s", _src, _exc)

                if local_files:
                    file_q.put(local_files)
                    local_n += len(local_files)

                    now = time.monotonic()
                    should_emit = False
                    with pend_lock:
                        if now - last_emit_t[0] >= _SCAN_EMIT_SECS:
                            total_found[0] += local_n
                            cur = total_found[0]
                            local_n = 0
                            last_emit_t[0] = now
                            should_emit = True
                    if should_emit:
                        self.scan_progress.emit("Scanning", cur)

                _finish_one()

            if local_n:
                with pend_lock:
                    total_found[0] += local_n

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=_SCAN_WORKERS)
        try:
            futs = [pool.submit(_worker) for _ in range(_SCAN_WORKERS)]
            for fut in concurrent.futures.as_completed(futs):
                if cancel.is_set():
                    for f in futs:
                        f.cancel()
                    break
                try:
                    fut.result()
                except Exception as exc:
                    logger.error("scan worker: %s", exc)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        self.scan_progress.emit("Scanning", total_found[0])

        result: list = []
        _get = file_q.get_nowait
        _Empty = queue.Empty
        while True:
            try:
                result.extend(_get())
            except _Empty:
                break
        return result

    def _scan_copy_local_pipelined(self, tasks: list, flusher: "_Flusher", tracker: "_EntryTracker") -> None:

        cancel = self._cancel
        if not tasks:
            self.scan_finished.emit(0)
            return

        sentinel = object()
        pipe_q: queue.Queue = queue.Queue(maxsize=_PIPE_MAXSIZE)

        work_q = queue.Queue()
        pend_lock = threading.Lock()
        pending = [0]
        dir_done = threading.Event()
        last_emit = [0.0]
        found = [0]

        copy_params = [_LOCAL_BATCH]

        def _eq(item: tuple) -> None:
            with pend_lock:
                pending[0] += 1
            work_q.put(item)

        def _dq() -> None:
            with pend_lock:
                pending[0] -= 1
                if pending[0] == 0:
                    dir_done.set()

        for src, dst, title in tasks:
            _eq((src, dst, title))

        def _scan_worker() -> None:
            local_n = 0
            batch: list = []
            while not cancel.is_set():
                try:
                    _src, _dst, _title = work_q.get(timeout=0.1)
                except queue.Empty:
                    if dir_done.is_set():
                        break
                    continue

                try:
                    with os.scandir(_src) as it:
                        for e in it:
                            if cancel.is_set():
                                break
                            if e.is_dir(follow_symlinks=False):
                                _eq((e.path, os.path.join(_dst, e.name), _title))
                            elif (e.is_file(follow_symlinks=False)
                                  and not _SKIP_RE.search(e.name)):
                                try:
                                    est = e.stat(follow_symlinks=False)
                                except OSError:
                                    est = None
                                batch.append((e.path, os.path.join(_dst, e.name), _title, est))
                                local_n += 1
                                if len(batch) >= _SCAN_PIPE_BATCH:
                                    pipe_q.put(batch)
                                    batch = []
                except NotADirectoryError:
                    if not _SKIP_RE.search(os.path.basename(_src)):
                        batch.append((_src, _dst, _title, None))
                        local_n += 1
                except PermissionError:
                    pass
                except OSError as _exc:
                    logger.warning("scan %s: %s", _src, _exc)

                now = time.monotonic()
                emit_cur = -1
                with pend_lock:
                    if now - last_emit[0] >= _SCAN_EMIT_SECS:
                        found[0] += local_n
                        local_n = 0
                        last_emit[0] = now
                        emit_cur = found[0]
                if emit_cur >= 0:
                    self.scan_progress.emit("Scanning", emit_cur)

                _dq()

            if batch:
                pipe_q.put(batch)
            if local_n:
                with pend_lock:
                    found[0] += local_n

        def _copy_worker() -> None:
            local_ok: list = []
            local_sk: list = []
            local_er: list = []
            tc: dict = {}

            def _fl() -> None:
                if local_ok or local_sk or local_er:
                    flusher.push(ok=local_ok, sk=local_sk, er=local_er)
                    local_ok.clear()
                    local_sk.clear()
                    local_er.clear()
                tracker.batch_update(tc)
                tc.clear()

            while not cancel.is_set():
                try:
                    item = pipe_q.get(timeout=0.1)
                except queue.Empty:
                    continue

                if item is sentinel:
                    pipe_q.put(sentinel)
                    break

                lb = copy_params[0]
                for entry in item:
                    if cancel.is_set():
                        break
                    _src = entry[0]
                    _dst = entry[1]
                    _title = entry[2]
                    src_st = entry[3]
                    try:
                        status, aux, sz = _copy_file(_src, _dst, cancel, src_st)
                    except Exception as _exc:
                        logger.error("copy %s: %s", _src, _exc)
                        status, aux, sz = "error", str(_exc), 0

                    c = tc.setdefault(_title, [0, 0, 0])
                    if status == "ok":
                        local_ok.append((_src, _dst, sz))
                        c[0] += 1
                    elif status == "skip":
                        local_sk.append((_src, aux or "Up to date", sz))
                        c[1] += 1
                    else:
                        local_er.append((_src, aux, 0))
                        c[2] += 1

                    if len(local_ok) + len(local_sk) + len(local_er) >= lb:
                        _fl()
            _fl()

        def _run_scan() -> None:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as sp:
                futs = [sp.submit(_scan_worker) for _ in range(_SCAN_WORKERS)]
                for _fut in concurrent.futures.as_completed(futs):
                    if cancel.is_set():
                        for _f in futs:
                            _f.cancel()
                        break
                    try:
                        _fut.result()
                    except Exception as _exc:
                        logger.error("scan worker: %s", _exc)

            total = found[0]
            self.scan_progress.emit("Scanning", total)

            cs, lb, ft = _scale_params(total)
            copy_params[0] = lb
            flusher.set_total(total)
            flusher.set_flush_thresh(ft)

            self.scan_finished.emit(total)

            pipe_q.put(sentinel)

        total_threads = 1 + _COPY_WORKERS
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=total_threads)
        try:
            scan_fut = pool.submit(_run_scan)
            copy_futs = [pool.submit(_copy_worker) for _ in range(_COPY_WORKERS)]
            all_futs = [scan_fut] + copy_futs
            for fut in concurrent.futures.as_completed(all_futs):
                if cancel.is_set():
                    for f in all_futs:
                        f.cancel()
                    break
                try:
                    fut.result()
                except Exception as exc:
                    logger.error("pipeline worker: %s", exc)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _probe_shares(self, smb_tasks, user, pw) -> tuple[set, set, bool]:
        unreachable: set[tuple[str, str]] = set()
        auth_failed: set[tuple[str, str]] = set()
        guest = False
        seen:  set[tuple[str, str]] = set()
        shares: list = []

        for s, d, _ in smb_tasks:
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
                if result == "timeout":  unreachable.add((_h, _sh))
                elif result == "auth":   auth_failed.add((_h, _sh))
                elif result == "guest":  guest = True

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(_SMB_WORKERS, len(shares) or 1)) as pool:
            futs = [pool.submit(probe_one, h, sh) for h, sh in shares]
            for fut in concurrent.futures.as_completed(futs):
                if self._cancel.is_set():
                    for f in futs:
                        f.cancel()
                    break
                try:
                    fut.result()
                except Exception as exc:
                    logger.error("probe error: %s", exc)

        return unreachable, auth_failed, guest

    @staticmethod
    def _filter_dead_tasks(smb_tasks, dead_shares, unreachable_shares) -> tuple[list, list]:
        alive:  list = []
        errors: list = []
        for s, d, t in smb_tasks:
            h, sh, _ = _parse_smb(s if is_smb(s) else d)
            if (h, sh) in dead_shares:
                reason = ("NT_STATUS_HOST_UNREACHABLE" if (h, sh) in unreachable_shares else "Authentication failed")
                errors.append((s if is_smb(s) else d, reason))
            else:
                alive.append((s, d, t))
        return alive, errors

    def _copy_local_all(self, items: list, flusher: _Flusher, tracker: _EntryTracker, claim_size: int = _CLAIM_SIZE,
                        local_batch: int = _LOCAL_BATCH) -> None:

        cancel = self._cancel
        n_items = len(items)
        if not n_items:
            return

        claim_lock = threading.Lock()
        claim_idx = [0]

        def _claim() -> "tuple[int,int] | None":
            with claim_lock:
                start = claim_idx[0]
                if start >= n_items:
                    return None
                end = min(start + claim_size, n_items)
                claim_idx[0] = end
                return start, end

        def _worker() -> None:
            local_ok: list = []
            local_sk: list = []
            local_er: list = []
            title_counts: dict = {}

            def _flush_local() -> None:
                if local_ok or local_sk or local_er:
                    flusher.push(ok=local_ok, sk=local_sk, er=local_er)
                    local_ok.clear()
                    local_sk.clear()
                    local_er.clear()
                tracker.batch_update(title_counts)
                title_counts.clear()

            while not cancel.is_set():
                claim = _claim()
                if claim is None:
                    break
                start, end = claim

                for i in range(start, end):
                    if cancel.is_set():
                        break
                    item = items[i]
                    src = item[0]
                    dst = item[1]
                    title = item[2]
                    src_st = item[3]
                    try:
                        status, aux, sz = _copy_file(src, dst, cancel, src_st)
                    except Exception as _exc:
                        logger.error("copy %s: %s", src, _exc)
                        status, aux, sz = "error", str(_exc), 0

                    tc = title_counts.setdefault(title, [0, 0, 0])
                    if status == "ok":
                        local_ok.append((src, dst, sz))
                        tc[0] += 1
                    elif status == "skip":
                        local_sk.append((src, aux or "Up to date", sz))
                        tc[1] += 1
                    else:
                        local_er.append((src, aux, 0))
                        tc[2] += 1

                    if len(local_ok) + len(local_sk) + len(local_er) >= local_batch:
                        _flush_local()

            _flush_local()

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=_COPY_WORKERS)
        try:
            futs = [pool.submit(_worker) for _ in range(_COPY_WORKERS)]
            for fut in concurrent.futures.as_completed(futs):
                if cancel.is_set():
                    for f in futs:
                        f.cancel()
                    break
                try:
                    fut.result()
                except Exception as exc:
                    logger.error("copy worker: %s", exc)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _copy_smb_all(self, smb_expanded: list, smb_errors: list, user: str, pw: "_SecurePw | None", guest: bool,
                      flusher: _Flusher, tracker: _EntryTracker) -> None:

        cancel = self._cancel

        fmt_errors = [(src, err, 0) for src, err in smb_errors]
        if fmt_errors:
            flusher.push(er=fmt_errors, force=True)

        if not smb_expanded or cancel.is_set():
            return

        share_groups: dict = {}
        for job in smb_expanded:
            grp = share_groups.setdefault((job.host, job.share), {"get": [], "put": []})
            grp["get" if job.kind == "smb_get" else "put"].append(job)

        ri_cache: dict = {}
        ri_lock        = threading.Lock()

        def run_share(host: str, share: str) -> None:
            try:
                client    = _SmbClient(host, share, user, pw, guest)
                processor = _ShareProcessor(client, cancel, flusher, tracker, ri_cache, ri_lock)
                processor.process(share_groups[(host, share)]["get"], share_groups[(host, share)]["put"])
            except Exception as _exc:
                logger.error("SMB share error //%s/%s: %s", host, share, _exc)
                er_w = []
                for _job in (share_groups[(host, share)]["get"] + share_groups[(host, share)]["put"]):
                    src = (_job.src_url if _job.kind == "smb_put" else f"smb://{host}/{share}/{_job.remote_path}")
                    er_w.append((src, f"Share processing crashed: {_exc}", 0))
                flusher.push(er=er_w)

        with concurrent.futures.ThreadPoolExecutor(max_workers=_SMB_WORKERS) as pool:
            futs = {pool.submit(run_share, h, sh): (h, sh) for h, sh in share_groups if not cancel.is_set()}
            for fut in concurrent.futures.as_completed(futs):
                if cancel.is_set():
                    for f in futs:
                        f.cancel()
                    break
                try:
                    fut.result()
                except Exception as exc:
                    logger.error("SMB share thread: %s", exc)


@dataclass
class _StatCard:
    frame:    QFrame
    val_lbl:  QLabel
    size_lbl: QLabel

    def set_val(self,  text: str) -> None: self.val_lbl.setText(text)
    def set_size(self, text: str) -> None: self.size_lbl.setText(text)


def _lbl(text: str, style: str) -> QLabel:
    w = QLabel(text)
    w.setStyleSheet(style)
    return w


def _make_stat_card(color: "str | None", title: str, val: str = "0",
                    size_title: int = 0, size_val: int = 0, bold_val: bool = True) -> _StatCard:

    t       = current_theme()
    s_title = size_title or font_sz(3)
    s_val   = size_val   or font_sz(16)

    frame = QFrame()
    frame.setMinimumWidth(240)
    border = f"border-left:4px solid {color};" if color else ""
    frame.setStyleSheet(f"QFrame {{background:{t['bg3']}; border-radius:8px; {border}}}")

    inner = QVBoxLayout(frame)
    inner.setContentsMargins(16, 14, 16, 14)

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

    return _StatCard(frame=frame, val_lbl=val_lbl, size_lbl=size_lbl)


class _SummaryWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._t = current_theme()
        t = self._t

        self._s_ok    = f"color:{t['success']};"
        self._s_skip  = f"color:{t['warning']};"
        self._s_err   = f"color:{t['error']};"
        self._s_dim   = f"color:{t['text_dim']};"
        self._s_title = f"color:{t['text']};"
        self._s_entry = _cached_mono_style(font_sz(-2), t["text"], extra="border:none; padding:2px 0px;")

        self._entry_results:    dict[str, list[int]] = {}
        self._entry_row_labels: dict[str, QLabel]    = {}
        self._entry_grid_cols = 1

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)

        self.header_card = QFrame()
        self.header_card.setObjectName("headerCard")
        self.header_card.setStyleSheet(f"#headerCard{{background:{t['bg3']}; border-radius:10px; border-left:4px solid {t['accent']};}}")

        hdr = QGridLayout(self.header_card)
        hdr.setContentsMargins(20, 15, 20, 15)
        for i in range(3):
            hdr.setColumnStretch(i, 1)

        self.op_lbl = QLabel("-")
        self.op_lbl.setStyleSheet(_cached_mono_style(font_sz(10), t["accent"], bold=True))

        self._status_center_lbl = QLabel()
        self._status_center_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._status_center_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_center_lbl.setStyleSheet("border:none; background:transparent;")

        self.total_lbl = QLabel("")
        self.total_lbl.setStyleSheet("font-size: 20px; border:none; background:transparent; padding:0px 0px")
        self.total_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        hdr.addWidget(self.op_lbl, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self._status_center_lbl, 0, 1, Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        hdr.addWidget(self.total_lbl, 0, 2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        stats_lay = QGridLayout()
        stats_lay.setSpacing(12)
        for i in range(3):
            stats_lay.setColumnStretch(i, 1)

        self.card_copied  = _make_stat_card(t["success"], "⤵ Copied",  "0")
        self.card_skipped = _make_stat_card(t["warning"],  "↷ Skipped", "0")
        self.card_errors  = _make_stat_card(t["error"],    "✗ Errors",  "0")

        for col, card in enumerate((self.card_copied, self.card_skipped, self.card_errors)):
            stats_lay.addWidget(card.frame, 0, col)

        self._progress_card = QFrame()
        self._progress_card.setObjectName("progressCard")
        self._progress_card.setStyleSheet(f"QFrame{{background:{t['bg3']}; border-radius:8px;}}")

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

        kw = dict(size_title=font_sz(2), size_val=font_sz(10))
        self._card_elapsed = _make_stat_card(None, "⏲️ Elapsed", "--:--", **kw)
        self._card_speed   = _make_stat_card(None, "🚤 Speed",   "---",   **kw)
        self._card_eta     = _make_stat_card(None, "🏁 ETA",     "--:--", **kw)

        for card in (self._card_elapsed, self._card_speed, self._card_eta):
            metrics_lay.addWidget(card.frame)

        self._rate_card = QFrame()
        self._rate_card.setObjectName("rateCard")
        self._rate_card.setStyleSheet(f"QFrame{{background:{t['bg3']}; border-radius:8px;}}")

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

        self._seg_copied  = QFrame()
        self._seg_skipped = QFrame()
        self._seg_errors  = QFrame()

        self._seg_copied.setStyleSheet(f"background:{t['success']};")
        self._seg_skipped.setStyleSheet(f"background:{t['warning']};")
        self._seg_errors.setStyleSheet(f"background:{t['error']};")

        for seg in (self._seg_copied, self._seg_skipped, self._seg_errors):
            seg.setFixedHeight(10)
            seg.setFixedWidth(0)
            seg_row.addWidget(seg)

        seg_row.addStretch(1)

        legend_row = QHBoxLayout()
        legend_row.setSpacing(20)

        legend_style = _cached_mono_style(font_sz(), t["text"], extra="border:none;")
        for key, text in (("success", "Copied"), ("warning", "Skipped"), ("error", "Errors")):
            dot = QLabel(f"<span style='color:{t[key]}'>■</span>  {text}")
            dot.setStyleSheet(legend_style)
            dot.setMinimumHeight(22)
            legend_row.addWidget(dot)

        legend_row.addStretch()
        rate_lay.addWidget(self._seg_track)
        rate_lay.addLayout(legend_row)

        self._entry_card = QFrame()
        self._entry_card.setObjectName("entryCard")
        self._entry_card.setStyleSheet(f"QFrame{{background:{t['bg3']}; border-radius:8px;}}")

        entry_lay = QVBoxLayout(self._entry_card)
        entry_lay.setContentsMargins(15, 10, 15, 10)
        entry_lay.setSpacing(5)

        entry_lay.addWidget(_lbl("Entries processed", _cached_mono_style(font_sz(2), t["text_dim"], extra="border:none;")))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")

        self._entry_list_widget = QWidget()
        self._entry_list_widget.setStyleSheet("background:transparent;")

        self._entry_grid = QGridLayout(self._entry_list_widget)
        self._entry_grid.setContentsMargins(1, 0, 1, 0)
        self._entry_grid.setHorizontalSpacing(5)
        self._entry_grid.setVerticalSpacing(8)

        scroll.setWidget(self._entry_list_widget)
        entry_lay.addWidget(scroll)

        lay.addWidget(self.header_card)
        lay.addLayout(stats_lay)
        lay.addLayout(metrics_lay)
        lay.addWidget(self._progress_card)
        lay.addWidget(self._rate_card)
        self._entry_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self._entry_card)

    def set_status_html(self, _html: str) -> None:
        self._status_center_lbl.setText(_html)

    def update_progress_bar(self, done: int, total: int) -> None:
        self._update_progress(done, total)

    def update_stats(self, operation: str, done: int, total: int, copied: int, skipped: int, errors: int, elapsed_s: int,
                     size_copied: int, size_skipped: int, finished: bool = False, cancelled: bool = False) -> None:

        self.op_lbl.setText(operation)
        self.card_copied.set_val(f"{copied:,}")
        self.card_copied.set_size(_format_unit(size_copied))
        self.card_skipped.set_val(f"{skipped:,}")
        self.card_skipped.set_size(_format_unit(size_skipped))
        self.card_errors.set_val(f"{errors:,}")
        size_str = _format_unit(size_copied + size_skipped)
        self.total_lbl.setText(f"{total:,} files / {size_str}" if total > 0 else "")
        self._update_progress(done, total, finished, cancelled)
        self._update_segments(copied, skipped, errors, finished=finished)
        self._update_timing(elapsed_s, done, total, finished, cancelled)

    def on_entry_status(self, title: str, ok: int, err: int, skip: int = 0) -> None:
        ec = self._entry_results.setdefault(title, [0, 0, 0])
        ec[0] += ok
        ec[1] += err
        ec[2] += skip
        self._refresh_entry_labels()

    def _update_progress(self, done: int, total: int, finished: bool = False, cancelled: bool = False) -> None:
        if finished and cancelled:
            self._progress_bar.setRange(0, 1)
            self._progress_bar.setValue(0)
            self._prog_pct.setText("—")
            self._progress_bar.setFormat("Cancelled")
            return
        if total > 0:
            pct = int(done * 100 / total)
            if self._progress_bar.maximum() != total:
                self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(done)
            self._prog_pct.setText(f"{pct}%")
            self._progress_bar.setFormat(f"{pct}%  —  {done:,} / {total:,} files")
        else:
            self._progress_bar.setRange(0, 0)
            self._progress_bar.setValue(0)
            self._prog_pct.setText("…")
            self._progress_bar.setFormat("Scanning…")

    def _update_segments(self, copied: int, skipped: int, errors: int, finished: bool = False) -> None:
        segs  = (self._seg_copied, self._seg_skipped, self._seg_errors)
        total = copied + skipped + errors
        if not finished or total == 0:
            for s in segs:
                s.setFixedWidth(0)
            return
        avail = self._rate_card.width() - 40
        for seg, count in zip(segs, (copied, skipped, errors)):
            seg.setFixedWidth(max(0, int(avail * count / total)))

    def _update_timing(self, elapsed_s: int, done: int, total: int, finished: bool, cancelled: bool) -> None:
        mins, secs = divmod(elapsed_s, 60)
        speed_str  = "---"
        eta_str    = "--:--"
        if elapsed_s > 0 and done > 0:
            rate      = done / elapsed_s
            speed_str = (f"{rate:,.1f} files/s" if rate >= 1 else f"1 file/{1 / rate:.1f}s")
            if not finished and total > done:
                eta_s   = int((total - done) / rate)
                eta_str = f"{eta_s // 60:02d}:{eta_s % 60:02d}"
            elif finished:
                eta_str = "Cancelled" if cancelled else "Done"
        self._card_elapsed.set_val(f"{mins:02d}:{secs:02d}")
        self._card_speed.set_val(speed_str)
        self._card_eta.set_val(eta_str)

    def _recalculate_grid(self) -> None:
        new_cols = max(1, (self._entry_card.width() - 40) // 280)
        if new_cols == self._entry_grid_cols:
            return
        self._entry_grid_cols = new_cols
        grid = self._entry_grid
        for i in range(max(grid.columnCount(), new_cols + 1)):
            grid.setColumnStretch(i, 0)
        for i in range(new_cols):
            grid.setColumnStretch(i, 1)

    @staticmethod
    def _html_title(title: str) -> str:
        return html.escape(title).replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")

    def _refresh_entry_labels(self) -> None:
        self._recalculate_grid()
        cols    = self._entry_grid_cols
        labels  = self._entry_row_labels
        results = self._entry_results
        rebuild = False

        for title, (ok, err, skip) in results.items():
            parts = []
            if ok:   parts.append(f"<span style='{self._s_ok}'>⤵ {ok:,}</span>")
            if skip: parts.append(f"<span style='{self._s_skip}'>↷ {skip:,}</span>")
            if err:  parts.append(f"<span style='{self._s_err}'>✗ {err:,}</span>")

            suffix     = "&nbsp; ".join(parts) if parts else f"<span style='{self._s_dim}'></span>"
            safe_title = self._html_title(title)
            _html       = f"<span style='{self._s_title}'>{safe_title}</span><br>{suffix}"

            lbl = labels.get(title)
            if lbl is None:
                lbl = QLabel()
                lbl.setTextFormat(Qt.TextFormat.RichText)
                lbl.setWordWrap(False)
                lbl.setStyleSheet(self._s_entry)
                labels[title] = lbl
                rebuild = True

            lbl.setText(_html)

        if not rebuild:
            return

        grid = self._entry_grid
        while grid.count():
            item = grid.takeAt(0)
            w    = item.widget()
            if w:
                w.hide()

        for idx, title in enumerate(sorted(labels)):
            row, col = divmod(idx, cols)
            labels[title].show()
            grid.addWidget(labels[title], row, col, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self._entry_list_widget.adjustSize()


class _LogWidget(QWidget):
    _PAGE    = 500
    _LOG_MAX = 150_000
    _sorted_ready = pyqtSignal(list, list)

    def __init__(self, color: str) -> None:
        super().__init__()
        t = current_theme()
        self._items:    list[str] = []
        self._items_lower: list[str] = []
        self._filtered: list[str] = []
        self._page          = 0
        self._finalized     = False
        self._truncated     = False
        self._last_rendered = ""
        self._search_cache: dict[str, list[str]] = {}
        self._sorted_ready.connect(self._apply_sorted)

        style_view   = (f"font-family:monospace; font-size:{font_sz(-1)}px; "
                        f"color:{color}; background:transparent; border:none;")
        style_search = (f"QLineEdit {{ background:{t['bg3']}; border:1px solid {t['header_sep']}; "
                        f"border-radius:6px; padding:0 10px; color:{t['text']}; }}")
        style_spin   = (f"QSpinBox{{border:1px solid {t['header_sep']}; border-radius:4px; "
                        f"padding:2px 5px; background:{t['bg3']}; color:{t['text']}; font-weight:bold}}"
                        f"QSpinBox:focus{{border:1px solid {t['accent']}; background:{t['bg2']}}}")
        style_muted  = f"color:{t['muted']}; font-size:{font_sz()}px; margin-left:10px;"
        style_btn    = (f"QPushButton {{ background:{t['bg3']}; border:1px solid {t['header_sep']}; "
                        f"border-radius:4px; padding:2px 8px; }} "
                        f"QPushButton:hover {{ background:{t['bg2']}; }}")

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
        self._prev  = QPushButton("‹ Prev")
        self._next  = QPushButton("Next ›")
        self._last  = QPushButton("»»")

        for btn, cb in ((self._first, lambda: self._go(0)), (self._prev,  lambda: self._go(self._page - 1)),
                        (self._next,  lambda: self._go(self._page + 1)), (self._last,  lambda: self._go(self._pages() - 1))):
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

        self._page_lbl  = QLabel("")
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

        self._html_prefix = (f"<style>body {{font-family: monospace; font-size: {font_sz(-1)}px; color: {color}}}"
                             f"hr {{background-color: {t['header_sep']}}} "
                             f".entry-odd  {{padding: 2px; background-color: rgba(0, 0, 0, 0.15)}} "
                             f".entry-even {{padding: 2px; background-color: rgba(255, 255, 255, 0.05)}}</style>")

    @property
    def is_truncated(self) -> bool:
        return self._truncated

    @property
    def log_max(self) -> int:
        return self._LOG_MAX

    @property
    def item_count(self) -> int:
        return len(self._items)

    def _pages(self) -> int: return max(1, (len(self._filtered) + self._PAGE - 1) // self._PAGE)

    def _go(self, page: int) -> None:
        self._page = max(0, min(page, self._pages() - 1))
        self._render()

    def _spin_changed(self) -> None:
        target = self._spin.value() - 1
        if target != self._page:
            self._go(target)

    def _render(self) -> None:
        pages = self._pages()
        start = self._page * self._PAGE
        chunk = self._filtered[start: start + self._PAGE]
        parts = [self._html_prefix]

        for i, item in enumerate(chunk):
            idx  = start + i + 1
            safe = html.escape(item).replace("\n", "<br>")
            row_class = "entry-even" if i % 2 == 0 else "entry-odd"
            parts.append(f'<div class="{row_class}"><b>{idx:,}:</b> {safe}</div>')
            if i < len(chunk) - 1:
                parts.append("<hr>")

        new_html = "".join(parts)
        if new_html != self._last_rendered:
            self._view.setHtml(new_html)
            self._last_rendered = new_html

        total = len(self._filtered)
        self._page_lbl.setText(f"<b>{pages}</b>")
        self._total_lbl.setText(f"({total:,} {'entry' if total == 1 else 'entries'})")

        self._spin.blockSignals(True)
        self._spin.setMaximum(pages)
        self._spin.setValue(self._page + 1)
        self._spin.blockSignals(False)

        can_back = self._page > 0
        can_fwd  = self._page < pages - 1
        self._first.setEnabled(can_back)
        self._prev.setEnabled(can_back)
        self._next.setEnabled(can_fwd)
        self._last.setEnabled(can_fwd)

    def bulk_add(self, entries: list[str]) -> None:
        if not entries or self._truncated:
            return
        remaining = self._LOG_MAX - len(self._items)
        if remaining <= 0:
            self._truncated = True
            return
        if len(entries) > remaining:
            entries = entries[:remaining]
            self._truncated = True
        needle = self._search.text().lower().strip()
        entries_lower = [e.lower() for e in entries]
        self._items.extend(entries)
        self._items_lower.extend(entries_lower)
        if not self._finalized:
            if needle:
                self._filtered.extend(e for e, el in zip(entries, entries_lower) if needle in el)
            else:
                self._filtered.extend(entries)
            self._search_cache.clear()
        if self._truncated:
            cap_msg = f"⚠ Log capped at {self._LOG_MAX:,} entries — use search to find specific files"
            self._items.append(cap_msg)
            self._items_lower.append(cap_msg.lower())
            if not self._finalized:
                self._filtered.append(cap_msg)

    def flush_final(self) -> None:
        self._finalized = True
        self._page = 0
        self._render()
        pairs = list(zip(self._items, self._items_lower))
        def _bg_sort() -> None:
            pairs.sort(key=lambda p: self._natural_sort_key(p[0].split('\n', 1)[0]))
            if pairs:
                sorted_items, sorted_lower = zip(*pairs)
            else:
                sorted_items, sorted_lower = [], []
            self._sorted_ready.emit(list(sorted_items), list(sorted_lower))
        threading.Thread(target=_bg_sort, daemon=True).start()

    def _apply_sorted(self, items: list, items_lower: list) -> None:
        needle            = self._search.text().lower().strip()
        self._items       = items
        self._items_lower = items_lower
        self._search_cache.clear()
        if needle:
            self._filtered = [i for i, il in zip(items, items_lower) if needle in il]
        else:
            self._filtered = items[:]
        self._render()

    def _on_search(self) -> None:
        needle = self._search.text().lower().strip()
        if needle in self._search_cache:
            self._filtered = self._search_cache[needle][:]
        else:
            if needle:
                self._filtered = [i for i, il in zip(self._items, self._items_lower) if needle in il]
            else:
                self._filtered = self._items[:]
            if len(self._search_cache) > 50:
                self._search_cache.pop(next(iter(self._search_cache)))
            self._search_cache[needle] = self._filtered[:]
        self._page = 0
        self._render()

    @staticmethod
    def _natural_sort_key(s: str) -> list: return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


class CopyDialog(QDialog):

    def __init__(self, parent, tasks, operation: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(operation)
        self._t = current_theme()
        t       = self._t

        self.c_ok = t["success"]
        self.c_sk = t["warning"]
        self.c_er = t["error"]

        self._status_fs = font_sz(8)

        screen = QApplication.primaryScreen()
        geo    = screen.availableGeometry() if screen else None
        if geo:
            self.setMinimumSize(min(1900, int(geo.width()  * 0.9)), min(925,  int(geo.height() * 0.9)))

        self._operation = operation
        self.worker     = CopyWorker(tasks)
        self.copied = self.skipped = self.errors = 0
        self._done  = self._total = 0
        self._final_elapsed: Optional[int] = None
        self._pending_ok = _deque()
        self._pending_sk = _deque()
        self._pending_er = _deque()
        self._size_copied = self._size_skipped = 0

        self._summary = _SummaryWidget()

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(2)
        sep.setStyleSheet(f"background: {t['header_sep']}; border: none;")

        self._w_copied  = _LogWidget(self.c_ok)
        self._w_skipped = _LogWidget(self.c_sk)
        self._w_errors  = _LogWidget(self.c_er)

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
        self.tabs.setStyleSheet(f"QTabWidget::pane {{border: none}} QTabBar::tab {{width: 200px; padding: 10px}}"
                                f"QTabBar::tab:selected {{background: {t['bg3']}; border-bottom: 2px solid {t['accent']}}}")

        self.cancel_btn = QPushButton("⏹ Cancel")
        self.cancel_btn.setMinimumHeight(50)
        self.cancel_btn.setStyleSheet(f"QPushButton {{background: {t['bg3']}; border: 1px solid {t['header_sep']}; "
                                      f"border-radius: 4px}} QPushButton:hover {{ background: {t['bg2']}; }}")
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
        self._summary.update_stats(self._operation, 0, 0, 0, 0, 0, 0, 0, 0, False)

    def _elapsed_s(self) -> int: return self._final_elapsed if self._final_elapsed is not None else self.timer.elapsed() // 1000

    def _status_badge(self, icon: str, label: str, color: str, border: "Optional[str]" = None) -> str:
        border = border or color
        return (f"<span style='display:inline-block; font-size:{self._status_fs}px; font-weight:bold; "
                f"font-family:monospace; color:{color};background:{self._t['bg2']}; border-left:5px solid {border}; "
                f"border-radius:7px;padding:6px 18px;'>{icon}&thinsp;{label}</span>")

    def _set_status_running(self) -> None:
        self._summary.set_status_html(self._status_badge("⏳", f"{self._operation} running…", self._t["cyan"], self._t["accent"]))

    def _set_status_scanning(self, phase: str, scanned: int) -> None:
        self._summary.set_status_html(self._status_badge("🔍", f"{phase}… ({scanned:,} found)", self._t["accent2"]))

    def _set_status_finished(self, icon: str, label: str, color: str) -> None:
        self._summary.set_status_html(self._status_badge(icon, label, color))

    def _on_scan_progress(self, phase: str, scanned: int) -> None: self._set_status_scanning(phase, scanned)

    def _on_scan_finished(self, total: int) -> None:
        self._total = total
        suffix = "file" if total == 1 else "files"
        self._summary.set_status_html(self._status_badge("📂", f"Scan complete — {total:,} {suffix} found", self._t["accent"]))
        if total > 0:
            self._summary.update_progress_bar(self._done, total)

    def _drain_pending(self) -> int:
        max_per = 750

        def process_batch(pending, widget, fmt):
            if not pending:
                return 0
            if widget.is_truncated:
                n = len(pending)
                pending.clear()
                return n
            n = min(max_per, len(pending))
            batch = [pending.popleft() for _ in range(n)]
            widget.bulk_add([fmt(*args) for args in batch])
            return n

        return (process_batch(self._pending_ok, self._w_copied, self._fmt_ok) +
                process_batch(self._pending_sk, self._w_skipped, self._fmt_sk) +
                process_batch(self._pending_er, self._w_errors, self._fmt_er))

    def _update_ui_tick(self) -> None:
        elapsed   = self._elapsed_s()
        processed = self._drain_pending()
        if processed:
            self._update_tab_labels()
        self._summary.update_stats(self._operation, self._done, self._total, self.copied, self.skipped, self.errors,
                                   elapsed, self._size_copied, self._size_skipped, finished=False)

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
            self._summary.update_progress_bar(done, total)

    def _on_done(self, c, s, e, cancelled) -> None:
        self._tick.stop()
        self._final_elapsed = self.timer.elapsed() // 1000

        for pending, widget, fmt in zip((self._pending_ok, self._pending_sk, self._pending_er),
                                        (self._w_copied, self._w_skipped, self._w_errors),
                                        (self._fmt_ok, self._fmt_sk, self._fmt_er)):
            if pending:
                cap = max(0, widget.log_max - widget.item_count)
                if cap > 0:
                    widget.bulk_add([fmt(*args) for args in islice(pending, cap)])
                pending.clear()

        if not cancelled:
            self.copied, self.skipped, self.errors = c, s, e
            self._done = self._total

        elapsed = self._final_elapsed
        tstr    = f"{elapsed // 60:02d}:{elapsed % 60:02d}"

        if cancelled:
            icon, label, col = "⏹", f"Cancelled after {tstr}", self.c_sk
        elif e > 0:
            icon, label, col = "⚠", f"Done with errors ✗ — {tstr}", self.c_er
        else:
            icon, label, col = "✓", f"Done — {tstr}", self.c_ok

        self._set_status_finished(icon, label, col)
        self._summary.update_stats(self._operation, self._done, self._total, self.copied, self.skipped, self.errors,
                                   elapsed, self._size_copied, self._size_skipped, finished=True, cancelled=cancelled)
        self._update_tab_labels()

        for w in (self._w_copied, self._w_skipped, self._w_errors):
            w.flush_final()

        if self._cancel_connected:
            try:
                self.cancel_btn.clicked.disconnect(self.worker.cancel)
            except RuntimeError:
                pass
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