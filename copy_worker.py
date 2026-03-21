from typing import Protocol
from dataclasses import dataclass
from urllib.parse import urlparse
import errno, os, subprocess, re, threading, concurrent.futures

from PyQt6.QtCore import Qt, QElapsedTimer, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QProgressBar, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QApplication, QWidget,
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QSpinBox, QTextEdit
)

from drive_utils import is_smb
from themes import current_theme
from state import apply_replacements, logger

_CHUNK             = 8 * 1024 * 1024
_SCAN_WORKERS      = 25
_COPY_WORKERS      = min(16, (os.cpu_count() or 4) * 2)
_SMB_SHARE_WORKERS = 25
_SMB_SCAN_WORKERS  = 35
_SMB_PROBE_TO      = 5
_SMB_BASE_TIMEOUT  = 15
_SMB_SECS_PER_FILE = 3
_SMB_LS_TIMEOUT    = 15
_FLUSH_THRESHOLD   = 75
_SMB_CHUNK_SIZE    = 50

_COLOR_COPIED  = "#00ff80"
_COLOR_SKIPPED = "#ddff03"
_COLOR_ERROR   = "#aa0000"

_SKIP_RE = re.compile(
    r"(^\.?lock$|\.lock$|lockfile$|Singleton\w*$|cookies\.sqlite-wal$|\.lck$)", re.I
)
_SMB_LINE_RE = re.compile(
    r"^(.+?)\s+([ADRHNSV]*)\s*(?:\(.*?\)\s*)?(\d+)"
    r"\s+\w{3}\s+\w{3}\s+[\s\d]\d\s+[\d:]+\s*\d*$"
)
_SMB_UNREACHABLE = frozenset((
    "Host is down", "NT_STATUS_HOST_UNREACHABLE", "NT_STATUS_IO_TIMEOUT",
    "NT_STATUS_CONNECTION_REFUSED", "NT_STATUS_NETWORK_UNREACHABLE",
    "NT_STATUS_CONNECTION_RESET", "NT_STATUS_CONNECTION_DISCONNECTED",
    "Connection refused", "No route to host", "Network is unreachable",
    "Connection timed out", "timeout",
))

_O_NOATIME:      int = getattr(os, "O_NOATIME", 0)
_copy_file_range     = getattr(os, "copy_file_range", None)


def _mono_style(size: int, color: str, bold: bool = False, extra: str = "") -> str:
    w = "font-weight:bold;" if bold else ""
    return f"font-size:{size}px;{w}color:{color};font-family:monospace;{extra}"


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


class _SecurePw(Protocol):
    def get(self) -> str: ...
    def clear(self) -> None: ...


def _is_unreachable(err: str) -> bool:
    up = err.upper()
    return any(s.upper() in up for s in _SMB_UNREACHABLE)


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


def _get_smb_credentials() -> tuple[str, "_SecurePw | None"]:
    try:
        from samba_credentials import SambaPasswordManager
        from sudo_password import SecureString
        u, p, _ = SambaPasswordManager().get_credentials()
        pw = SecureString(p) if p else None
        del p
        return (u or ""), pw
    except Exception as exc:
        logger.warning("SMB credentials unavailable: %s", exc)
        return "", None


def _smb_env(pw) -> dict:
    env = os.environ.copy()
    if pw is not None:
        env["PASSWD"] = pw.get()
    else:
        env.pop("PASSWD", None)
    return env


def _smb_argv(host: str, share: str, user: str, guest: bool = False) -> list[str]:
    base = ["smbclient", f"//{host}/{share}"]
    return base + (["-N"] if guest else ["-U", user])


def _smb_run(host, share, user, pw, cmds, timeout, guest=False) -> tuple[bool, str]:
    env = _smb_env(pw)
    try:
        r = subprocess.run(
            _smb_argv(host, share, user, guest),
            input=cmds, text=True, capture_output=True,
            timeout=timeout, env=env, encoding="utf-8",
        )
        return r.returncode == 0, (r.stderr.strip() or f"exit {r.returncode}")
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as exc:
        return False, str(exc)
    finally:
        env.pop("PASSWD", None)


def _probe_smb(host, share, user, pw) -> tuple | str:
    if user and pw is not None:
        ok, err = _smb_run(host, share, user, pw, "exit\n", _SMB_PROBE_TO)
        if ok:
            return user, pw, False
        if _is_unreachable(err):
            logger.warning("SMB unreachable //%s/%s: %s", host, share, err)
            return "timeout"
    ok, err = _smb_run(host, share, "", None, "exit\n", _SMB_PROBE_TO, guest=True)
    if ok:
        return "", None, True
    if _is_unreachable(err):
        logger.warning("SMB unreachable //%s/%s: %s", host, share, err)
        return "timeout"
    logger.warning("SMB auth failed //%s/%s: %s", host, share, err)
    return "auth"


def _smb_ls_index(host, share, base, user, pw, guest=False) -> dict:
    base = base.replace("\\", "/").rstrip("/")
    cmd  = "recurse on\nprompt off\n"
    if base:
        cmd += f'cd "{_q(base)}"\n'
    cmd += "ls\n"
    index: dict = {}
    env = _smb_env(pw)
    try:
        out = subprocess.check_output(
            _smb_argv(host, share, user, guest),
            input=cmd, text=True, stderr=subprocess.DEVNULL,
            timeout=_SMB_LS_TIMEOUT, env=env, encoding="utf-8",
        )
        cur_dir = base
        for raw in out.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("\\"):
                path    = line.replace("\\", "/").strip("/")
                cur_dir = (
                    path if (not base or path.startswith(base + "/") or path == base)
                    else f"{base}/{path}"
                )
                continue
            m = _SMB_LINE_RE.match(line)
            if not m:
                continue
            name, flags, size_s = m.groups()
            name = name.strip()
            if name not in (".", "..") and "D" not in flags and not _SKIP_RE.search(name):
                index[f"{cur_dir}/{name}".lstrip("/")] = (int(size_s),)
    except Exception as exc:
        logger.debug("SMB ls index failed: %s", exc)
    finally:
        env.pop("PASSWD", None)
    return index


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
    lines, cur_rdir = [], None
    for j in sorted(jobs, key=lambda x: os.path.dirname(x.remote_path)):
        rdir  = os.path.dirname(j.remote_path).replace("\\", "/").strip("/")
        fname = os.path.basename(j.remote_path)
        if rdir != cur_rdir:
            lines.append(f'cd "/{_q(rdir)}"' if rdir else 'cd "/"')
            cur_rdir = rdir
        lines.append(f'get "{_q(fname)}" "{_q(str(j.dst_path))}"')
    lines.append("exit\n")
    return "\n".join(lines)


def _build_smb_put_cmds(jobs: list[_SmbJob]) -> str:
    rdirs = sorted({os.path.dirname(j.remote_path).replace("\\", "/").strip("/") for j in jobs})
    mkdir_lines: list[str] = []
    for rdir in rdirs:
        if not rdir:
            continue
        parts = [p for p in rdir.split("/") if p]
        for i in range(len(parts)):
            mkdir_lines.append(f'mkdir "{_q("/".join(parts[:i + 1]))}"')
    transfer_lines: list[str] = []
    cur_local_dir = cur_rdir = None
    for j in sorted(
        jobs,
        key=lambda x: (
            os.path.dirname(x.remote_path),
            os.path.dirname(os.path.abspath(str(x.src_url))),
        ),
    ):
        src_abs   = os.path.abspath(str(j.src_url))
        local_dir = os.path.dirname(src_abs)
        rdir      = os.path.dirname(j.remote_path).replace("\\", "/").strip("/")
        if local_dir != cur_local_dir:
            transfer_lines.append(f'lcd "{_q(local_dir)}"')
            cur_local_dir = local_dir
        if rdir != cur_rdir:
            transfer_lines.append(f'cd "/{_q(rdir)}"' if rdir else 'cd "/"')
            cur_rdir = rdir
        transfer_lines.append(
            f'put "{_q(os.path.basename(src_abs))}" "{_q(os.path.basename(j.remote_path))}"'
        )
    return "\n".join(mkdir_lines + transfer_lines + ["exit\n"])


def _smb_transfer_chunk(host, share, jobs, user, pw, guest, cancel, ok_list, er_list, build_fn):
    is_get = build_fn is _build_smb_get_cmds
    stack  = [list(jobs)]
    while stack:
        batch = stack.pop()
        if not batch or cancel.is_set():
            return
        ok, err = _smb_run(
            host, share, user, pw,
            build_fn(batch), _session_timeout(len(batch)), guest,
        )
        if ok:
            for j in batch:
                ok_list.append(
                    (f"smb://{host}/{share}/{j.remote_path}", j.dst_path)
                    if is_get
                    else (j.src_url, f"smb://{host}/{share}/{j.remote_path}")
                )
        elif len(batch) == 1:
            j   = batch[0]
            src = f"smb://{host}/{share}/{j.remote_path}" if is_get else j.src_url
            logger.warning("SMB op failed %s/%s/%s: %s", host, share, j.remote_path, err)
            er_list.append((src, err))
        elif not cancel.is_set():
            mid = len(batch) // 2
            stack.extend([batch[mid:], batch[:mid]])


def _smb_remote_index(host, share, put_jobs, user, pw, guest, ri_cache, ri_lock) -> dict:
    needed = {
        (os.path.dirname(j.remote_path).replace("\\", "/") or "").split("/")[0]
        for j in put_jobs
    }
    merged: dict = {}
    for top in needed:
        key = f"{host}:{share}:{top}"
        with ri_lock:
            cached = ri_cache.get(key)
        if cached is None:
            cached = _smb_ls_index(host, share, top, user, pw, guest)
            with ri_lock:
                ri_cache[key] = cached
        merged.update(cached)
    return merged


def _resolve_smb_jobs(jobs, cancel, user, pw, guest=False, progress_cb=None) -> tuple[list, list]:
    cache, cache_lock = {}, threading.Lock()
    expanded, errors  = [], []
    result_lock       = threading.Lock()
    counter           = [0]

    def _report(n: int) -> None:
        with result_lock:
            counter[0] += n
        if progress_cb:
            progress_cb(counter[0])

    scan_tasks: list = []
    seen_get:   set  = set()
    for src, dst, *rest in jobs:
        if cancel.is_set():
            break
        title      = rest[0] if rest else ""
        src_is_smb = is_smb(src)
        host, share, rpath = _parse_smb(src if src_is_smb else dst)
        if src_is_smb:
            key = (host, share, rpath, dst)
            if key not in seen_get:
                seen_get.add(key)
                scan_tasks.append(("smb_get", host, share, rpath, dst, title))
        elif os.path.isfile(src):
            scan_tasks.append(("smb_put_file", src, dst, host, share, rpath, title))
        else:
            scan_tasks.append(("smb_put_dir", src, dst, host, share, rpath, title))

    if cancel.is_set():
        return [], []

    def do_get(_host, _share, _rpath, _dst, _title):
        ck = f"{_host}:{_share}:{_rpath}"
        with cache_lock:
            idx = cache.get(ck)
        if idx is None:
            if cancel.is_set():
                return
            idx = _smb_ls_index(_host, _share, _rpath, user, pw, guest)
            with cache_lock:
                cache[ck] = idx
        if cancel.is_set():
            return
        src_url     = f"smb://{_host}/{_share}/{_rpath}"
        lexp: list  = []
        lerr: list  = []
        if not idx:
            lerr.append((src_url, "SMB empty or not found"))
        else:
            prefix = _rpath.rstrip("/") + "/"
            for path, (sz,) in idx.items():
                if cancel.is_set():
                    break
                rel = (
                    os.path.relpath(path, _rpath)
                    if path.startswith(prefix)
                    else os.path.basename(path)
                )
                lexp.append(
                    _SmbJob(src_url, str(os.path.join(_dst, rel)),
                            "smb_get", _host, _share, path, sz, _title)
                )
        with result_lock:
            expanded.extend(lexp)
            errors.extend(lerr)
        _report(len(lexp) + len(lerr))

    def do_put_file(_src, _dst, _host, _share, _rpath, _title):
        if _SKIP_RE.search(os.path.basename(_src)):
            return
        rp = f"{_rpath}/{os.path.basename(_src)}".lstrip("/")
        with result_lock:
            expanded.append(_SmbJob(_src, _dst, "smb_put", _host, _share, rp, title=_title))
        _report(1)

    def do_put_dir(_src, _dst, _host, _share, _rpath, _title):
        lexp:  list = []
        stack: list = [_src]
        while stack:
            if cancel.is_set():
                break
            try:
                with os.scandir(stack.pop()) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif not _SKIP_RE.search(e.name):
                            rel = os.path.relpath(e.path, _src)
                            rp  = f"{_rpath}/{rel}".replace(os.sep, "/").lstrip("/")
                            lexp.append(
                                _SmbJob(e.path, _dst, "smb_put", _host, _share, rp, title=_title)
                            )
            except PermissionError:
                pass
        with result_lock:
            expanded.extend(lexp)
        _report(len(lexp))

    dispatch = {
        "smb_get":      lambda t: do_get(*t[1:]),
        "smb_put_file": lambda t: do_put_file(*t[1:]),
        "smb_put_dir":  lambda t: do_put_dir(*t[1:]),
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=_SMB_SCAN_WORKERS) as pool:
        futs = [pool.submit(dispatch[task[0]], task) for task in scan_tasks if not cancel.is_set()]
        try:
            for fut in concurrent.futures.as_completed(futs):
                if cancel.is_set():
                    for f in futs:
                        f.cancel()
                    break
                try:
                    fut.result()
                except Exception as exc:
                    logger.error("SMB scan error: %s", exc)
                    with result_lock:
                        errors.append(("smb scan error", str(exc)))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    return ([], []) if cancel.is_set() else (expanded, errors)


def _scan_local(scan_jobs, cancel, progress_cb=None) -> tuple[list, list, list]:
    pairs, skipped, errors = [], [], []
    lock    = threading.Lock()
    counter = [0]

    def scan_one(src_root: str, dst_root: str, title: str = "") -> None:
        lp: list = []
        ls: list = []
        if os.path.isfile(src_root):
            if _SKIP_RE.search(os.path.basename(src_root)):
                ls.append((src_root, "Lock/Temp file", title))
            else:
                lp.append((src_root, dst_root, title))
        elif not os.path.exists(src_root):
            with lock:
                errors.append((src_root, "Source path does not exist", title))
            return
        else:
            stack = [src_root]
            while stack:
                if cancel.is_set():
                    break
                try:
                    with os.scandir(stack.pop()) as it:
                        for e in it:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            elif not e.is_file():
                                pass
                            elif _SKIP_RE.search(e.name):
                                ls.append((e.path, "Lock/Temp file", title))
                            else:
                                rel = os.path.relpath(e.path, src_root)
                                lp.append((e.path, os.path.join(dst_root, rel), title))
                except PermissionError:
                    pass
        with lock:
            pairs.extend(lp)
            skipped.extend(ls)
            counter[0] += len(lp) + len(ls)
        if progress_cb:
            progress_cb(counter[0])

    if not scan_jobs:
        return pairs, skipped, errors

    with concurrent.futures.ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as pool:
        futs = [pool.submit(scan_one, s, d, t) for s, d, t in scan_jobs]
        try:
            for fut in concurrent.futures.as_completed(futs):
                if cancel.is_set():
                    for f in futs:
                        f.cancel()
                    break
                try:
                    fut.result()
                except Exception as exc:
                    with lock:
                        errors.append(("scan error", str(exc), ""))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    return pairs, skipped, errors


def _open_src(path: str) -> int:
    try:
        return os.open(path, os.O_RDONLY | _O_NOATIME)
    except PermissionError:
        return os.open(path, os.O_RDONLY)


def _kernel_copy(rfd: int, wfd: int, size: int, cancel: threading.Event) -> None:
    remaining = size
    offset    = 0

    if _copy_file_range and remaining > 0:
        try:
            while remaining > 0:
                if cancel.is_set():
                    raise InterruptedError
                n = _copy_file_range(
                    rfd, wfd, min(remaining, _CHUNK),
                    offset_src=offset, offset_dst=offset,
                )
                if n == 0:
                    break
                offset    += n
                remaining -= n
            return
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
                raise
            os.lseek(rfd, offset, os.SEEK_SET)
            os.lseek(wfd, offset, os.SEEK_SET)

    if remaining > 0:
        try:
            while remaining > 0:
                if cancel.is_set():
                    raise InterruptedError
                sent = os.sendfile(wfd, rfd, offset, min(remaining, _CHUNK))
                if sent == 0:
                    break
                offset    += sent
                remaining -= sent
            return
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOSYS):
                raise
            os.lseek(rfd, offset, os.SEEK_SET)

    while remaining > 0:
        if cancel.is_set():
            raise InterruptedError
        buf = os.read(rfd, min(remaining, _CHUNK))
        if not buf:
            break
        os.write(wfd, buf)
        remaining -= len(buf)


def _copy_file(src: str, dst: str, cancel: threading.Event) -> tuple[str, str]:
    tmp = f"{dst}.part.{os.getpid()}.{threading.get_ident()}"
    try:
        if cancel.is_set():
            return "skip", ""

        st = os.stat(src)

        try:
            dst_st = os.stat(dst)
            if st.st_size == dst_st.st_size and dst_st.st_mtime >= st.st_mtime:
                return "skip", "Up to date"
        except FileNotFoundError:
            pass

        if dst_dir := os.path.dirname(dst):
            os.makedirs(dst_dir, exist_ok=True)

        rfd = _open_src(src)
        try:
            if st.st_size > 0:
                try:
                    os.posix_fadvise(rfd, 0, st.st_size, os.POSIX_FADV_SEQUENTIAL)
                except OSError:
                    pass

            wfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, st.st_mode & 0o777)
            try:
                if st.st_size > 0:
                    try:
                        os.posix_fallocate(wfd, 0, st.st_size)
                    except OSError:
                        pass
                    _kernel_copy(rfd, wfd, st.st_size, cancel)
            finally:
                os.close(wfd)
        finally:
            os.close(rfd)

        os.utime(tmp, (st.st_atime, st.st_mtime))
        os.replace(tmp, dst)
        return "ok", dst

    except InterruptedError:
        _unlink(tmp)
        return "skip", ""
    except Exception as exc:
        _unlink(tmp)
        msg = f"OS Error: {exc.strerror}" if isinstance(exc, OSError) else f"Unexpected: {exc}"
        return "error", msg


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
        for t, pc in pre.items():
            with self._lock:
                ec = self._counts.setdefault(t, [0, 0, 0])
                for i in range(3):
                    ec[i] += pc[i]

    def emit_all(self, signal) -> None:
        for t, ec in self._counts.items():
            if t:
                signal.emit(t, ec[0], ec[1], ec[2])


class _Flusher:

    __slots__ = ("_signal", "_total", "_lock", "_ok", "_sk", "_er",
                 "done", "copied", "skipped", "errors")

    def __init__(self, signal, total: int, done: int = 0, copied: int = 0,
                 skipped: int = 0, errors: int = 0) -> None:
        self._signal  = signal
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
            payload_ok = self._ok[:]
            payload_sk = self._sk[:]
            payload_er = self._er[:]
            self.done    += n
            self.copied  += len(payload_ok)
            self.skipped += len(payload_sk)
            self.errors  += len(payload_er)
            self._ok.clear()
            self._sk.clear()
            self._er.clear()
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
        normalized: list[tuple[str, str, str]] = []
        for t in tasks:
            src, dst = t[0], t[1]
            title    = t[2] if len(t) > 2 else ""
            if isinstance(src, (list, tuple)) and isinstance(dst, (list, tuple)):
                if len(src) != len(dst):
                    logger.warning("CopyWorker: src/dst list length mismatch, skipping: %r", t)
                    continue
                for s, d in zip(src, dst):
                    s = os.path.expanduser(str(s))
                    d = os.path.expanduser(str(d))
                    if s and d:
                        normalized.append((s, d, str(title)))
                continue
            if isinstance(src, (list, tuple)):
                src = src[0] if src else ""
            if isinstance(dst, (list, tuple)):
                dst = dst[0] if dst else ""
            if not isinstance(src, str) or not isinstance(dst, str) or not src or not dst:
                logger.warning("CopyWorker: skipping malformed task: %r", t)
                continue
            normalized.append((os.path.expanduser(src), os.path.expanduser(dst), str(title)))
        self.tasks   = normalized
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        pw: "_SecurePw | None" = None
        try:
            user: str = ""
            guest: bool = False

            has_smb = any(is_smb(s) or is_smb(d) for s, d, _ in self.tasks)
            if has_smb:
                user, pw = _get_smb_credentials()

            self.scan_progress.emit("Scanning", 0)

            smb_tasks = [(s, d, t) for s, d, t in self.tasks if is_smb(s) or is_smb(d)]
            local_tasks = [(s, d, t) for s, d, t in self.tasks if not (is_smb(s) or is_smb(d))]

            if smb_tasks and not self._cancel.is_set():
                seen: set[tuple[str, str]] = set()
                for s, d, _ in smb_tasks:
                    h, sh, _ = _parse_smb(s if is_smb(s) else d)
                    if (h, sh) in seen:
                        continue
                    seen.add((h, sh))
                    if self._cancel.is_set():
                        break
                    probe = _probe_smb(h, sh, user, pw)
                    if isinstance(probe, tuple):
                        _, _, p_guest = probe
                        if p_guest:
                            user, pw, guest = "", None, True
                            break

            smb_expanded: list = []
            smb_errors: list = []
            local_pairs: list = []
            local_skipped: list = []
            local_errors: list = []

            if smb_tasks and not self._cancel.is_set():
                smb_expanded, smb_errors = _resolve_smb_jobs(
                    smb_tasks, self._cancel, user, pw, guest,
                    lambda n: self.scan_progress.emit("Scanning SMB", n),
                )

            if local_tasks and not self._cancel.is_set():
                local_pairs, local_skipped, local_errors = _scan_local(
                    local_tasks, self._cancel,
                    lambda n: self.scan_progress.emit("Scanning Local", n),
                )

            total_files = (
                    len(smb_expanded) + len(smb_errors)
                    + len(local_pairs) + len(local_skipped) + len(local_errors)
            )
            self.scan_finished.emit(total_files)

            if self._cancel.is_set():
                self.finished_work.emit(0, 0, 0, True)
                return

            done = copied = skipped = errors = 0

            if smb_expanded or smb_errors:
                done, copied, skipped, errors = self._run_smb(
                    smb_expanded, smb_errors, user, pw, guest,
                    done, total_files, self._cancel,
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
                    local_pairs, done, total_files,
                    copied, skipped, errors,
                    self._cancel, pre_counts,
                )

            self.finished_work.emit(copied, skipped, errors, self._cancel.is_set())

        except Exception as exc:
            logger.error("CopyWorker critical error: %s", exc, exc_info=True)
            self.finished_work.emit(0, 0, 0, False)
        finally:
            if pw is not None:
                pw.clear()

    def _run_smb(self, smb_expanded, smb_exp_errors, user, pw, guest, done_in: int, total: int, cancel) \
            -> tuple[int, int, int, int]:

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
        flusher = _Flusher(
            self.batch_update, total,
            done=done, copied=0, skipped=0, errors=len(formatted_errors),
        )
        tracker = _EntryTracker()

        def process_share(host, share, get_jobs, put_jobs):
            if cancel.is_set():
                return

            url_title: dict[str, tuple[str, int]] = {}
            for j in get_jobs:
                url_title[f"smb://{j.host}/{j.share}/{j.remote_path}"] = (j.title, j.remote_size)
            for j in put_jobs:
                url_title[j.src_url] = (j.title, _file_size(j.src_url))

            def _record(_ok_c, sk_c, _er_c):
                ok_with_sz = [(s, _d, url_title.get(s, ("", 0))[1]) for s, _d in _ok_c]
                sk_with_sz = [(s, r, url_title.get(s, ("", 0))[1]) for s, r in sk_c]
                er_with_sz = [(s, e, 0) for s, e in _er_c]
                flusher.push(ok=ok_with_sz, sk=sk_with_sz, er=er_with_sz)
                for url, _, _ in ok_with_sz: tracker.ok(url_title.get(url, ("", 0))[0])
                for url, _, _ in sk_with_sz: tracker.skip(url_title.get(url, ("", 0))[0])
                for url, _, _ in er_with_sz: tracker.err(url_title.get(url, ("", 0))[0])

            sk_immediate:    list = []
            get_to_transfer: list = []
            for j in get_jobs:
                if j.up_to_date():
                    sk_immediate.append((f"smb://{host}/{share}/{j.remote_path}", "Up to date"))
                else:
                    get_to_transfer.append(j)

            put_to_transfer: list = []
            if put_jobs:
                ri = _smb_remote_index(host, share, put_jobs, user, pw, guest, ri_cache, ri_lock)
                for j in put_jobs:
                    key  = j.remote_path.replace("\\", "/").lstrip("/")
                    meta = ri.get(key)
                    skip = bool(meta and _file_size(j.src_url) == meta[0])
                    if skip:
                        sk_immediate.append((j.src_url, "Up to date"))
                    else:
                        put_to_transfer.append(j)

            for i in range(0, len(sk_immediate), _SMB_CHUNK_SIZE):
                if cancel.is_set():
                    break
                _record([], sk_immediate[i: i + _SMB_CHUNK_SIZE], [])

            if get_to_transfer and not cancel.is_set():
                for d in {os.path.dirname(str(j.dst_path)) for j in get_to_transfer}:
                    if d:
                        os.makedirs(d, exist_ok=True)
                for i in range(0, len(get_to_transfer), _SMB_CHUNK_SIZE):
                    if cancel.is_set():
                        break
                    ok_c, er_c = [], []
                    _smb_transfer_chunk(
                        host, share, get_to_transfer[i: i + _SMB_CHUNK_SIZE],
                        user, pw, guest, cancel, ok_c, er_c, _build_smb_get_cmds,
                    )
                    _record(ok_c, [], er_c)

            if put_to_transfer and not cancel.is_set():
                for i in range(0, len(put_to_transfer), _SMB_CHUNK_SIZE):
                    if cancel.is_set():
                        break
                    ok_c, er_c = [], []
                    _smb_transfer_chunk(
                        host, share, put_to_transfer[i: i + _SMB_CHUNK_SIZE],
                        user, pw, guest, cancel, ok_c, er_c, _build_smb_put_cmds,
                    )
                    _record(ok_c, [], er_c)

        with concurrent.futures.ThreadPoolExecutor(max_workers=_SMB_SHARE_WORKERS) as pool:
            futs = {
                pool.submit(process_share, h, sh, grp["get"], grp["put"]): (h, sh)
                for (h, sh), grp in share_groups.items()
                if not cancel.is_set()
            }
            try:
                for fut in concurrent.futures.as_completed(futs):
                    if cancel.is_set():
                        for f in futs:
                            f.cancel()
                        break
                    try:
                        fut.result()
                    except Exception as exc:
                        logger.error("SMB share error: %s", exc)
            finally:
                pool.shutdown(wait=True, cancel_futures=True)

        flusher.flush()
        tracker.emit_all(self.entry_status)
        return flusher.done, flusher.copied, flusher.skipped, flusher.errors

    def _run_local(self, pairs, done_in: int, total: int, copied_in: int, skipped_in: int, errors_in: int,
                   cancel, pre_counts: dict | None = None) -> tuple[int, int, int, int]:

        flusher = _Flusher(
            self.batch_update, total,
            done=done_in, copied=copied_in, skipped=skipped_in, errors=errors_in,
        )
        tracker = _EntryTracker()
        if pre_counts:
            tracker.merge_pre(pre_counts)

        with concurrent.futures.ThreadPoolExecutor(max_workers=_COPY_WORKERS) as pool:
            futures = {pool.submit(_copy_file, s, d, cancel): (s, d, t) for s, d, t in pairs}
            try:
                for fut in concurrent.futures.as_completed(futures):
                    if cancel.is_set():
                        for f in futures:
                            f.cancel()
                        break
                    src, dst, title = futures[fut]
                    try:
                        sz     = _file_size(src)
                        status, aux = fut.result()
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
            finally:
                pool.shutdown(wait=True, cancel_futures=True)

        flusher.flush()
        tracker.emit_all(self.entry_status)
        return flusher.done, flusher.copied, flusher.skipped, flusher.errors


def _lbl(text: str, style: str) -> QLabel:
    w = QLabel(text)
    w.setStyleSheet(style)
    return w


def _make_card(color, title, val, size_title=18, size_val=32, bold_val=True,
               contents_margins=(16, 14, 16, 14), spacing=None):
    t = current_theme()
    frame = QFrame()
    style = f"QFrame {{background:{t['bg3']};border-radius:8px;"
    if color:
        style += f"border-left:4px solid {color};"
    style += "}"
    frame.setStyleSheet(style)

    inner = QVBoxLayout(frame)
    inner.setContentsMargins(*contents_margins)
    if spacing is not None:
        inner.setSpacing(spacing)

    title_lbl = QLabel(title)
    title_lbl.setStyleSheet(_mono_style(size_title, t["text_dim"], extra="border:none;"))

    val_lbl = QLabel(val)
    val_lbl.setStyleSheet(_mono_style(size_val, color or t["text"], bold=bold_val, extra="border:none;"))

    size_lbl = QLabel("")
    if color:
        size_lbl.setText("0 B")
        size_lbl.setStyleSheet(_mono_style(13, color, extra="border:none;"))

    inner.addWidget(title_lbl)

    val_row = QHBoxLayout()
    val_row.setSpacing(10)
    val_row.setContentsMargins(0, 0, 0, 0)
    val_row.addWidget(val_lbl,  0, Qt.AlignmentFlag.AlignBottom)
    val_row.addWidget(size_lbl, 0, Qt.AlignmentFlag.AlignBottom)
    val_row.addStretch()
    inner.addLayout(val_row)

    return {"frame": frame, "val": val_lbl, "size_lbl": size_lbl}


class CopyDialog(QDialog):
    def __init__(self, parent, tasks, operation: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(operation)
        screen = QApplication.primaryScreen()
        geo    = screen.availableGeometry() if screen else None
        if geo:
            self.setMinimumSize(
                min(1900, int(geo.width() * 0.9)),
                min(950,  int(geo.height() * 0.9)),
            )

        self._operation = operation
        self.worker     = CopyWorker(tasks)

        self.copied = self.skipped = self.errors = 0
        self._done  = self._total = 0
        self._final_elapsed = None
        self._pending_ok:    list = []
        self._pending_sk:    list = []
        self._pending_er:    list = []
        self._size_copied  = self._size_skipped = 0

        self._summary = _SummaryWidget()
        self._set_status_running()

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(2)

        self._w_copied  = _LogWidget(_COLOR_COPIED)
        self._w_skipped = _LogWidget(_COLOR_SKIPPED)
        self._w_errors  = _LogWidget(_COLOR_ERROR)

        summary_page = QWidget()
        sl = QVBoxLayout(summary_page)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(self._summary)

        self.tabs = QTabWidget()
        self.tabs.addTab(summary_page,      "📋 Summary")
        self.tabs.addTab(self._w_copied,    "⤵ Copied (0)")
        self.tabs.addTab(self._w_skipped,   "↷ Skipped (0)")
        self.tabs.addTab(self._w_errors,    "✗ Errors (0)")
        self.tabs.setStyleSheet("QTabBar::tab { width: 200px; }")

        self.cancel_btn = QPushButton("⏹ Cancel")
        self.cancel_btn.setFixedHeight(50)
        self._cancel_connected = True
        self.cancel_btn.clicked.connect(self.worker.cancel)

        layout = QVBoxLayout(self)
        layout.setSpacing(3)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.addWidget(sep)
        layout.addWidget(self.tabs)
        layout.addWidget(self.cancel_btn)

        self.timer = QElapsedTimer()
        self.timer.start()
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._update_ui_tick)
        self._tick.start(400)
        self._clock_tick = QTimer(self)
        self._clock_tick.timeout.connect(self._update_clock)
        self._clock_tick.start(500)

        self.worker.scan_finished.connect(self._on_scan_finished)
        self.worker.batch_update.connect(self._on_batch)
        self.worker.finished_work.connect(self._on_done)
        self.worker.scan_progress.connect(self._on_scan_progress)
        self.worker.entry_status.connect(self._summary.on_entry_status)
        self.worker.start()

        self._summary.refresh(self._operation, 0, 0, 0, 0, 0, 0, False)

    def _elapsed_s(self) -> int:
        return (
            self._final_elapsed
            if self._final_elapsed is not None
            else self.timer.elapsed() // 1000
        )

    @staticmethod
    def _status_html(icon, label, label_color, bg, border) -> str:
        return (
            f"<span style='display:inline-block;font-size:22px;font-weight:bold;"
            f"font-family:monospace;color:{label_color};background:{bg};"
            f"border-left:5px solid {border};border-radius:7px;"
            f"padding:6px 18px;'>{icon}&thinsp;{label}</span>"
        )

    def _set_status_running(self) -> None:
        t = current_theme()
        self._summary.set_status_html(
            self._status_html("⏳", f"{self._operation} running…",
                              label_color=t["cyan"], bg=t["bg2"], border=t["accent"]))

    def _set_status_scanning(self, phase: str, scanned: int) -> None:
        t = current_theme()
        self._summary.set_status_html(
            self._status_html("🔍", f"{phase}… ({scanned:,} found)",
                              label_color=t["accent2"], bg=t["bg2"], border=t["accent2"]))

    def _set_status_finished(self, icon: str, label: str, color: str) -> None:
        t = current_theme()
        self._summary.set_status_html(
            self._status_html(icon, label, label_color=color, bg=t["bg2"], border=color))

    def _on_scan_finished(self, total: int) -> None:
        t      = current_theme()
        suffix = "file" if total == 1 else "files"
        self._summary.set_status_html(
            self._status_html("📂", f"Scan complete — {total:,} {suffix} found",
                              label_color=t["accent"], bg=t["bg2"], border=t["accent"]))

    def _update_tab_labels(self) -> None:
        self.tabs.setTabText(1, f"⤵ Copied ({self.copied:,})")
        self.tabs.setTabText(2, f"↷ Skipped ({self.skipped:,})")
        self.tabs.setTabText(3, f"✗ Errors ({self.errors:,})")

    def _update_clock(self) -> None:
        self._summary.update_elapsed(self._elapsed_s(), self._done, self._total)

    @staticmethod
    def _fmt_ok(s, d) -> str:
        return f"{apply_replacements(s)}\n Copied to ⤵\n{apply_replacements(d)}"

    @staticmethod
    def _fmt_sk(p, r) -> str:
        return f"{apply_replacements(p)} ↷ {r}"

    @staticmethod
    def _fmt_er(p, m) -> str:
        return f"{apply_replacements(p)} ❌ {m}"

    def _update_ui_tick(self) -> None:
        max_per = 250 if len(self._pending_ok) > 5000 else 500
        changed = False

        for pending, widget, fmt in zip(
            (self._pending_ok, self._pending_sk, self._pending_er),
            (self._w_copied,   self._w_skipped,  self._w_errors),
            (self._fmt_ok,     self._fmt_sk,      self._fmt_er),
        ):
            if not pending:
                continue
            batch = pending[:max_per]
            del pending[:max_per]
            for args in batch:
                widget.bulk_add([fmt(*args)])
            changed = True

        if changed or (self._size_copied + self._size_skipped > 0):
            self._update_tab_labels()
            self._summary.card_copied["val"].setText(f"{self.copied:,}")
            self._summary.card_copied["size_lbl"].setText(_format_unit(self._size_copied))
            self._summary.card_skipped["val"].setText(f"{self.skipped:,}")
            self._summary.card_skipped["size_lbl"].setText(_format_unit(self._size_skipped))
            self._summary.card_errors["val"].setText(f"{self.errors:,}")
            total_bytes = self._size_copied + self._size_skipped
            self._summary.update_total_size(total_bytes)
            self._summary.refresh(
                self._operation, self._done, self._total,
                self.copied, self.skipped, self.errors,
                self._elapsed_s(), False,
            )

    def _on_scan_progress(self, phase: str, scanned: int) -> None:
        self._set_status_scanning(phase, scanned)

    def _on_batch(self, ok, sk, er, done, total) -> None:
        self._done, self._total = done, total
        self.copied  += len(ok)
        self.skipped += len(sk)
        self.errors  += len(er)

        for _, _, sz in ok:
            self._size_copied  += sz
        for _, _, sz in sk:
            self._size_skipped += sz

        self._pending_ok.extend([(s, d) for s, d, _sz in ok])
        self._pending_sk.extend([(s, r) for s, r, _sz in sk])
        self._pending_er.extend([(s, m) for s, m, _sz in er])

        if total > 0:
            self._summary.update_progress(done, total)
            self._summary.update_total_size(self._size_copied + self._size_skipped)

    def _on_done(self, c, s, e, cancelled) -> None:
        self._tick.stop()
        self._clock_tick.stop()
        self._final_elapsed = self.timer.elapsed() // 1000

        for pending, widget, fmt in zip(
            (self._pending_ok, self._pending_sk, self._pending_er),
            (self._w_copied,   self._w_skipped,  self._w_errors),
            (self._fmt_ok,     self._fmt_sk,      self._fmt_er),
        ):
            for args in pending:
                widget.bulk_add([fmt(*args)])
            pending.clear()

        if not cancelled:
            self.copied, self.skipped, self.errors = c, s, e

        disp_c = self.copied  if cancelled else c
        disp_s = self.skipped if cancelled else s
        disp_e = self.errors  if cancelled else e
        elapsed = self._final_elapsed
        tstr    = f"{elapsed // 60:02d}:{elapsed % 60:02d}"

        if cancelled:
            icon, label, col = "⏹", f"Cancelled after {tstr}", _COLOR_SKIPPED
        elif disp_e > 0:
            icon, label, col = "⚠", f"Done with errors ✗ — {tstr}", _COLOR_ERROR
        else:
            icon, label, col = "✓", f"Done — {tstr}", _COLOR_COPIED

        self._set_status_finished(icon, label, col)

        self._summary.update_total_size(self._size_copied + self._size_skipped)
        self._summary.refresh(
            self._operation, self._done, self._total,
            disp_c, disp_s, disp_e, elapsed, True, cancelled,
        )
        self._summary.card_copied["size_lbl"].setText(_format_unit(self._size_copied))
        self._summary.card_skipped["size_lbl"].setText(_format_unit(self._size_skipped))

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


class _SummaryWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)
        t = current_theme()

        self._op_name           = "-"
        self._status_html_content = ""
        self._total_size_bytes  = 0

        self.header_card = QFrame()
        self.header_card.setObjectName("headerCard")
        hdr = QHBoxLayout(self.header_card)
        hdr.setContentsMargins(20, 15, 20, 15)

        self.op_lbl = QLabel("-")
        self.op_lbl.setStyleSheet(_mono_style(24, t["text"], bold=True))

        self._status_center_lbl = QLabel()
        self._status_center_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._status_center_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_center_lbl.setStyleSheet("border:none; background:transparent;")

        self._state_lbl = QLabel()
        self._state_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._state_lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter)

        self.total_lbl = QLabel("")
        self.total_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.total_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.total_lbl.setStyleSheet("border:none; background:transparent;")

        hdr.addWidget(self.op_lbl, 0)
        hdr.addStretch(1)
        hdr.addWidget(self._status_center_lbl, 0)
        hdr.addStretch(1)
        right_col = QVBoxLayout()
        right_col.setSpacing(4)
        right_col.addWidget(self._state_lbl)
        right_col.addWidget(self.total_lbl)
        hdr.addLayout(right_col)

        stats_lay = QGridLayout()
        stats_lay.setSpacing(12)
        stats_lay.setColumnStretch(0, 1)
        stats_lay.setColumnStretch(1, 1)
        stats_lay.setColumnStretch(2, 1)
        self.card_copied  = _make_card(_COLOR_COPIED,  "⤵ Copied",  "0")
        self.card_skipped = _make_card(_COLOR_SKIPPED, "↷ Skipped", "0")
        self.card_errors  = _make_card(_COLOR_ERROR,   "✗ Errors",  "0")
        for col, card in enumerate((self.card_copied, self.card_skipped, self.card_errors)):
            stats_lay.addWidget(card["frame"], 0, col)

        self._progress_card = QFrame()
        self._progress_card.setObjectName("progressCard")
        prog_lay = QVBoxLayout(self._progress_card)
        prog_lay.setContentsMargins(20, 14, 20, 14)
        prog_lay.setSpacing(8)
        prog_hdr = QHBoxLayout()
        prog_hdr.addWidget(_lbl("Progress", _mono_style(16, t["text_dim"], extra="border:none;")))
        prog_hdr.addStretch()
        self._prog_pct = QLabel("0%")
        self._prog_pct.setStyleSheet(_mono_style(16, t["text"], bold=True, extra="border:none;"))
        prog_hdr.addWidget(self._prog_pct)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFormat("0%  —  0 / 0 files")
        self._progress_bar.setFixedHeight(32)
        prog_lay.addLayout(prog_hdr)
        prog_lay.addWidget(self._progress_bar)

        metrics_lay = QHBoxLayout()
        metrics_lay.setSpacing(12)
        kw = dict(size_title=16, size_val=24, contents_margins=(16, 12, 16, 12), spacing=4)
        self._card_elapsed = _make_card(None, "⏲️ Elapsed", "--:--", **kw)
        self._card_speed   = _make_card(None, "🚤 Speed",   "---",   **kw)
        self._card_eta     = _make_card(None, "🏁 ETA",     "--:--", **kw)
        for card in (self._card_elapsed, self._card_speed, self._card_eta):
            metrics_lay.addWidget(card["frame"])

        self._rate_card = QFrame()
        self._rate_card.setObjectName("rateCard")
        rate_lay = QVBoxLayout(self._rate_card)
        rate_lay.setContentsMargins(20, 14, 20, 14)
        rate_lay.setSpacing(8)
        bd_lbl = _lbl("File breakdown", _mono_style(16, t["text_dim"], extra="border:none;"))
        bd_lbl.setFixedHeight(22)
        rate_lay.addWidget(bd_lbl)

        self._seg_track = QFrame()
        self._seg_track.setFixedHeight(10)
        self._seg_track.setStyleSheet(f"background:{t['header_sep']};border-radius:5px;")
        seg_row = QHBoxLayout(self._seg_track)
        seg_row.setSpacing(0)
        seg_row.setContentsMargins(0, 0, 0, 0)
        self._seg_copied  = QFrame()
        self._seg_skipped = QFrame()
        self._seg_errors  = QFrame()
        for seg, color, radius in (
            (self._seg_copied,  _COLOR_COPIED,  "border-radius:5px 0 0 5px;"),
            (self._seg_skipped, _COLOR_SKIPPED, "border-radius:5px 0 0 5px;"),
            (self._seg_errors,  _COLOR_ERROR,   "border-radius:0 5px 5px 0;"),
        ):
            seg.setFixedHeight(10)
            seg.setFixedWidth(0)
            seg.setStyleSheet(f"background:{color};{radius}")
            seg_row.addWidget(seg, 0)
        seg_row.addStretch(1)

        legend_row = QHBoxLayout()
        legend_row.setSpacing(20)
        for color, text in ((_COLOR_COPIED, "Copied"), (_COLOR_SKIPPED, "Skipped"), (_COLOR_ERROR, "Errors")):
            dot = QLabel(f"<span style='color:{color}'>■</span>  {text}")
            dot.setStyleSheet(_mono_style(14, t["text"], extra="border:none;"))
            dot.setFixedHeight(20)
            legend_row.addWidget(dot)
        legend_row.addStretch()

        rate_lay.addWidget(self._seg_track)
        rate_lay.addLayout(legend_row)

        self._entry_card = QFrame()
        self._entry_card.setObjectName("entryCard")
        entry_lay = QVBoxLayout(self._entry_card)
        entry_lay.setContentsMargins(15, 10, 15, 10)
        entry_lay.setSpacing(5)
        entry_lay.addWidget(_lbl("Entries processed", _mono_style(16, t["text_dim"], extra="border:none;")))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        self._entry_scroll      = scroll
        self._entry_list_widget = QWidget()
        self._entry_list_widget.setStyleSheet("background:transparent;")
        self._entry_grid = QGridLayout(self._entry_list_widget)
        self._entry_grid.setContentsMargins(1, 0, 1, 10)
        self._entry_grid.setHorizontalSpacing(5)
        self._entry_grid.setVerticalSpacing(8)
        scroll.setWidget(self._entry_list_widget)
        entry_lay.addWidget(scroll)

        self._entry_row_labels: dict = {}
        self._entry_results:    dict = {}
        self._entry_grid_cols       = 1
        self._last_total            = 0

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

    def _refresh_op_lbl(self) -> None:
        t     = current_theme()
        parts = []
        if self._status_html_content:
            parts.append(self._status_html_content)
        parts.append(f"<span style='font-size:13px;font-family:monospace;"
                     f"color:{t['text_dim']};'></span>")
        self._status_center_lbl.setText("".join(parts))

    def _recalculate_grid(self) -> None:
        new_cols = max(1, (self._entry_card.width() - 40) // 280)
        if new_cols != self._entry_grid_cols:
            self._entry_grid_cols = new_cols
            old_count = self._entry_grid.columnCount()
            for i in range(max(old_count, new_cols + 1)):
                self._entry_grid.setColumnStretch(i, 0)
            for i in range(new_cols):
                self._entry_grid.setColumnStretch(i, 1)
            self._refresh_entry_labels()

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
        t      = current_theme()
        cols   = self._entry_grid_cols
        line_h = 20
        pad    = 5

        for title, ec in self._entry_results.items():
            ok, err, skip = ec
            parts = []
            if ok >= 0:   parts.append(f"<span style='color:{_COLOR_COPIED}'>⤵ {ok:,}</span>")
            if skip >= 0: parts.append(f"<span style='color:{_COLOR_SKIPPED}'>↷ {skip:,}</span>")
            if err:  parts.append(f"<span style='color:{_COLOR_ERROR}'>✗ {err:,}</span>")
            suffix     = "&nbsp; ".join(parts) if parts else f"<span style='color:{t['text_dim']}'>–</span>"
            safe_title = self._html_title(title)
            html       = f"<span style='color:{t['text']}'>{safe_title}</span><br>{suffix}"
            title_lines = self._count_html_lines(safe_title)
            min_h       = (title_lines + 1) * line_h + pad

            if title not in self._entry_row_labels:
                lbl = QLabel()
                lbl.setTextFormat(Qt.TextFormat.RichText)
                lbl.setWordWrap(False)
                lbl.setStyleSheet(_mono_style(13, t["text"], extra="border:none; padding:2px 0px;"))
                self._entry_row_labels[title] = lbl
            lbl = self._entry_row_labels[title]
            lbl.setText(html)
            lbl.setMinimumHeight(min_h)

        while self._entry_grid.count():
            item = self._entry_grid.takeAt(0)
            if item.widget():
                item.widget().hide()
        for idx, title in enumerate(sorted(self._entry_row_labels)):
            row, col = divmod(idx, cols)
            lbl = self._entry_row_labels[title]
            lbl.show()
            self._entry_grid.addWidget(lbl, row, col, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        for r in range(self._entry_grid.rowCount()):
            self._entry_grid.setRowStretch(r, 0)
        self._entry_list_widget.adjustSize()
        self._recalculate_grid()

    def _update_segments(self, copied: int, skipped: int, errors: int, finished: bool = False) -> None:
        if not finished:
            for seg in (self._seg_copied, self._seg_skipped, self._seg_errors):
                seg.setFixedWidth(0)
            return
        total = copied + skipped + errors
        if total == 0:
            for seg in (self._seg_copied, self._seg_skipped, self._seg_errors):
                seg.setFixedWidth(0)
            return
        avail = self._rate_card.width() - 40
        for seg, count in ((self._seg_copied, copied), (self._seg_skipped, skipped), (self._seg_errors, errors)):
            seg.setFixedWidth(max(0, int(avail * count / total)))

    def refresh(self, operation, done, total, copied, skipped, errors, elapsed_s, finished, cancelled=False) -> None:
        t      = current_theme()
        accent = t["accent"]
        self.header_card.setStyleSheet(
            f"#headerCard{{background:{t['bg3']};border-radius:10px;border-left:4px solid {accent};}}")
        for card in (self._progress_card, self._rate_card, self._entry_card):
            card.setStyleSheet(f"QFrame{{background:{t['bg3']};border-radius:8px;}}")
        self._op_name = operation
        self.op_lbl.setText(operation)
        self.op_lbl.setStyleSheet(_mono_style(24, accent, bold=True))
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

    def update_progress(self, done, total) -> None:
        self._update_progress_card(done, total)

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
            elif finished:
                eta_str = "Cancelled" if cancelled else "Done"
        self._card_elapsed["val"].setText(f"{mins:02d}:{secs:02d}")
        self._card_speed["val"].setText(speed_str)
        self._card_eta["val"].setText(eta_str)


class _LogWidget(QWidget):
    _PAGE = 500

    def __init__(self, color: str) -> None:
        super().__init__()
        self._items:    list[str] = []
        self._filtered: list[str] = []
        self._page      = 0
        self._finalized = False

        self._search = QLineEdit()
        self._search.setPlaceholderText(" 🔍  Search…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        self._search.setFixedHeight(44)

        self._view = QTextEdit()
        self._view.setReadOnly(True)
        self._view.setStyleSheet(f"font-family:monospace;font-size:14px;color:{color};")

        t = current_theme()
        self._first = QPushButton("««")
        self._prev  = QPushButton("‹ Prev")
        self._next  = QPushButton("Next ›")
        self._last  = QPushButton("»»")
        for btn, cb in (
            (self._first, lambda: self._go(0)),
            (self._prev,  lambda: self._go(self._page - 1)),
            (self._next,  lambda: self._go(self._page + 1)),
            (self._last,  lambda: self._go(self._pages() - 1)),
        ):
            btn.clicked.connect(cb)
            btn.setFixedHeight(28)

        self._spin = QSpinBox()
        self._spin.setMinimum(1)
        self._spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._spin.setStyleSheet(
            f"QSpinBox{{border:1px solid {t['header_sep']};border-radius:4px;"
            f"padding:2px 5px;background:{t['bg3']};color:{t['text']};font-weight:bold}}"
            f"QSpinBox:focus{{border:1px solid {t['accent']};background:{t['bg2']}}}"
        )
        self._spin.setFixedHeight(28)
        self._spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spin.editingFinished.connect(self._spin_changed)

        self._page_lbl  = QLabel("")
        self._page_lbl.setFixedHeight(28)
        self._total_lbl = QLabel("")
        self._total_lbl.setStyleSheet(f"color:{t['muted']};font-size:14px;margin-left:10px;")
        self._total_lbl.setFixedHeight(28)

        nav = QHBoxLayout()
        nav.setContentsMargins(5, 5, 5, 5)
        nav.setSpacing(8)
        nav.addWidget(self._first)
        nav.addWidget(self._prev)
        nav.addStretch(1)

        pg = QHBoxLayout()
        pg.setSpacing(5)
        for w in (QLabel("Page"), self._spin, QLabel("of"), self._page_lbl):
            w.setFixedHeight(28)
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

        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._render)

    def _pages(self) -> int:
        return max(1, (len(self._filtered) + self._PAGE - 1) // self._PAGE)

    def _go(self, page: int) -> None:
        self._page = max(0, min(page, self._pages() - 1))
        self._render()

    def _spin_changed(self) -> None:
        target = self._spin.value() - 1
        if target != self._page:
            self._go(target)

    def _render(self) -> None:
        pages  = self._pages()
        self._page = max(0, min(self._page, pages - 1))
        start  = self._page * self._PAGE
        chunk  = self._filtered[start: start + self._PAGE]
        lines: list[str] = []
        for i, item in enumerate(chunk):
            idx   = start + i + 1
            parts = item.split("\n")
            lines.append(f"{idx:,}: {parts[0]}")
            lines.extend(parts[1:])
            lines.append("")
        self._view.setPlainText("\n".join(lines))

        total = len(self._filtered)
        self._page_lbl.setText(f"<b>{pages}</b>")
        self._total_lbl.setText(f"({total:,} {'entry' if total == 1 else 'entries'})")
        self._spin.setMaximum(pages)
        self._spin.blockSignals(True)
        self._spin.setValue(self._page + 1)
        self._spin.blockSignals(False)
        can_back = self._page > 0
        can_fwd  = self._page < pages - 1
        self._first.setEnabled(can_back)
        self._prev.setEnabled(can_back)
        self._next.setEnabled(can_fwd)
        self._last.setEnabled(can_fwd)

    def bulk_add(self, entries: list[str]) -> None:
        if not entries:
            return
        self._items.extend(entries)
        if not self._finalized:
            self._filtered = self._items
        if not self._timer.isActive():
            self._timer.start()

    def flush_final(self) -> None:
        self._timer.stop()
        self._finalized = True
        self._items.sort()
        needle = self._search.text().lower()
        self._filtered = (
            [i for i in self._items if needle in i.lower()] if needle else self._items
        )
        self._render()

    def _on_search(self) -> None:
        self._page = 0
        needle     = self._search.text().lower()
        self._filtered = (
            [i for i in self._items if needle in i.lower()] if needle else self._items
        )
        self._render()