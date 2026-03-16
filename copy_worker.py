from urllib.parse import urlparse
import os, re, subprocess, threading, concurrent.futures

from PyQt6.QtCore import Qt, QElapsedTimer, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QWidget, QSpinBox,
    QProgressBar, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QApplication, QTextEdit
)

from drive_utils import is_smb
from themes import current_theme
from state import apply_replacements, logger

_CHUNK              = 64 * 1024 * 1024
_COPY_WORKERS       = min(16, (os.cpu_count() or 4) * 2)
_SMB_SHARE_WORKERS  = 25
_SMB_SCAN_WORKERS   = 35
_SMB_PROBE_TO       = 5
_SMB_BASE_TIMEOUT   = 15
_SMB_SECS_PER_FILE  = 3
_SMB_LS_TIMEOUT     = 15
_FLUSH_THRESHOLD    = 75
_SMB_PROGRESS_CHUNK = 50

_SKIP_RE = re.compile(r"(^\.?lock$|\.lock$|lockfile$|Singleton$|cookies\.sqlite-wal$|\.lck$)", re.I)

_COLOR_COPIED  = "#00ff80"
_COLOR_SKIPPED = "#ddff03"
_COLOR_ERROR   = "#aa0000"

_SMB_UNREACHABLE = ("timeout", "NT_STATUS_HOST_UNREACHABLE", "NT_STATUS_IO_TIMEOUT", "NT_STATUS_CONNECTION_REFUSED",
                    "NT_STATUS_NETWORK_UNREACHABLE", "NT_STATUS_CONNECTION_RESET", "NT_STATUS_CONNECTION_DISCONNECTED",
                    "Connection refused", "No route to host", "Network is unreachable", "Connection timed out", "Host is down")

_SMB_LINE_RE = re.compile(
    r"^(.+?)"
    r"\s+([ADRHNSV]*)"
    r"\s*(?:\(.*?\)\s*)?"
    r"(\d+)"
    r"\s+\w{3}\s+\w{3}\s+[\s\d]\d"
    r"\s+[\d:]+\s*\d*$"
)


def _mono_style(size, color, bold=False, extra=""):
    w = "font-weight:bold;" if bold else ""
    return f"font-size:{size}px;{w}color:{color};font-family:monospace;{extra}"

def _compare_stats(src_size, src_mtime, dst_size, dst_mtime):
    return src_size == dst_size and int(dst_mtime) >= int(src_mtime)

def _is_unreachable(err):
    up = err.upper()
    return any(s.upper() in up for s in _SMB_UNREACHABLE)

def _parse_smb(url):
    p     = urlparse(url)
    host  = p.hostname or p.netloc
    parts = [x for x in p.path.split("/") if x]
    return host, (parts[0] if parts else ""), "/".join(parts[1:])

def _q(s):
    return s.replace("\\", "/").replace('"', '\\"')

def _count_by(iterable, key):
    result = {}
    for item in iterable:
        k = key(item)
        result[k] = result.get(k, 0) + 1
    return result

def _session_timeout(n_files):
    return max(_SMB_BASE_TIMEOUT, n_files * _SMB_SECS_PER_FILE)

def _get_smb_credentials():
    try:
        from samba_credentials import SambaPasswordManager
        from sudo_password import SecureString
        u, p, _ = SambaPasswordManager().get_credentials()
        pw = SecureString(p) if p else None
        return (u or ""), pw
    except Exception as exc:
        logger.warning("SMB credentials unavailable: %s", exc)
        return "", None

def _smb_env(pw):
    env = os.environ.copy()
    if pw is not None:
        env["PASSWD"] = pw.get()
    else:
        env.pop("PASSWD", None)
    return env

def _smb_argv(host, share, user, guest=False):
    base = ["smbclient", f"//{host}/{share}"]
    return base + (["-N"] if guest else ["-U", user])

def _smb_run(host, share, user, pw, cmds, timeout, guest=False):
    try:
        r = subprocess.run(_smb_argv(host, share, user, guest), input=cmds, text=True, capture_output=True,
                           timeout=timeout, env=_smb_env(pw))
        return r.returncode == 0, (r.stderr.strip() or f"exit {r.returncode}")
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as exc:
        return False, str(exc)

def _probe_smb(host, share, user, pw):
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

def _smb_ls_index(host, share, base, user, pw, guest=False):
    base = base.replace("\\", "/").rstrip("/")
    if base:
        cmd = f'recurse on\nprompt off\ncd "{_q(base)}"\nls\n'
    else:
        cmd = 'recurse on\nprompt off\nls\n'
    index = {}
    try:
        out = subprocess.check_output(_smb_argv(host, share, user, guest), input=cmd, text=True, stderr=subprocess.DEVNULL,
                                      timeout=_SMB_LS_TIMEOUT, env=_smb_env(pw))
        cur_dir = base
        for raw in out.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("\\"):
                path = line.replace("\\", "/").strip("/")
                if not base or path.startswith(base + "/") or path == base:
                    cur_dir = path
                else:
                    cur_dir = f"{base}/{path}"
                continue
            m = _SMB_LINE_RE.match(line)
            if not m:
                continue
            name, flags, size_s = m.groups()
            name = name.strip()
            if name in (".", "..") or "D" in flags or _SKIP_RE.search(name):
                continue
            index[f"{cur_dir}/{name}".lstrip("/")] = (int(size_s),)
    except Exception as exc:
        logger.debug("SMB ls index failed: %s", exc)
    return index


class _SmbJob:
    __slots__ = ("src_url", "dst_path", "kind", "host", "share", "remote_path", "remote_size", "title")

    def __init__(self, src_url, dst_path, kind, host, share, r_path, r_size=-1, title=""):
        self.src_url     = src_url
        self.dst_path    = dst_path
        self.kind        = kind
        self.host        = host
        self.share       = share
        self.remote_path = r_path
        self.remote_size = r_size
        self.title       = title

    def size_matches_local(self):
        if self.kind != "smb_get" or self.remote_size < 0:
            return False
        try:
            return self.remote_size == os.stat(self.dst_path).st_size
        except OSError:
            return False


def _get_or_build_ri(host, share, put_jobs, user, pw, guest, ri_cache, ri_lock):
    needed = set()
    for j in put_jobs:
        rdir = os.path.dirname(j.remote_path).replace("\\", "/")
        needed.add(rdir.split("/")[0] if rdir else "")
    merged = {}
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

def _build_smb_get_cmds(jobs):
    lines = []
    cur_rdir = None
    for j in sorted(jobs, key=lambda x: os.path.dirname(x.remote_path)):
        rdir  = os.path.dirname(j.remote_path).replace("\\", "/").strip("/")
        fname = os.path.basename(j.remote_path)
        if rdir != cur_rdir:
            lines.append(f'cd "/{_q(rdir)}"' if rdir else 'cd "/"')
            cur_rdir = rdir
        lines.append(f'get "{_q(fname)}" "{_q(str(j.dst_path))}"')
    lines.append("exit\n")
    return "\n".join(lines)

def _build_smb_put_cmds(jobs):
    rdirs = sorted({os.path.dirname(j.remote_path).replace("\\", "/").strip("/") for j in jobs})
    mkdir_lines = []
    for rdir in rdirs:
        if not rdir:
            continue
        parts = [p for p in rdir.split("/") if p]
        for i in range(len(parts)):
            mkdir_lines.append(f'mkdir "{_q("/".join(parts[:i + 1]))}"')
    transfer_lines = []
    cur_local_dir = cur_rdir = None
    for j in sorted(jobs, key=lambda x: (
            os.path.dirname(x.remote_path),
            os.path.dirname(os.path.abspath(str(x.src_url))))):
        src_abs   = os.path.abspath(str(j.src_url))
        local_dir = os.path.dirname(src_abs)
        rdir      = os.path.dirname(j.remote_path).replace("\\", "/").strip("/")
        if local_dir != cur_local_dir:
            transfer_lines.append(f'lcd "{_q(local_dir)}"')
            cur_local_dir = local_dir
        if rdir != cur_rdir:
            transfer_lines.append(f'cd "/{_q(rdir)}"' if rdir else 'cd "/"')
            cur_rdir = rdir
        transfer_lines.append(f'put "{_q(os.path.basename(src_abs))}" "{_q(os.path.basename(j.remote_path))}"')
    return "\n".join(mkdir_lines + transfer_lines + ["exit\n"])


def _do_smb_chunk_iterative(host, share, jobs, user, pw, guest, cancel, ok_list, er_list, build_fn):
    stack = [list(jobs)]
    while stack:
        batch = stack.pop()
        if not batch or cancel.is_set():
            return
        ok, err = _smb_run(host, share, user, pw, build_fn(batch), _session_timeout(len(batch)), guest)
        if ok:
            if build_fn is _build_smb_get_cmds:
                for j in batch:
                    ok_list.append((f"smb://{host}/{share}/{j.remote_path}", j.dst_path))
            else:
                for j in batch:
                    ok_list.append((j.src_url, f"smb://{host}/{share}/{j.remote_path}"))
        elif len(batch) == 1:
            j = batch[0]
            src = f"smb://{host}/{share}/{j.remote_path}" if build_fn is _build_smb_get_cmds else j.src_url
            logger.warning("SMB op failed %s/%s/%s: %s", host, share, j.remote_path, err)
            er_list.append((src, err))
        else:
            if cancel.is_set():
                return
            mid = len(batch) // 2
            stack.append(batch[mid:])
            stack.append(batch[:mid])


def _do_smb_get_chunk(host, share, jobs, user, pw, guest, cancel, ok_list, er_list):
    _do_smb_chunk_iterative(host, share, jobs, user, pw, guest, cancel, ok_list, er_list, _build_smb_get_cmds)

def _do_smb_put_chunk(host, share, jobs, user, pw, guest, cancel, ok_list, er_list):
    _do_smb_chunk_iterative(host, share, jobs, user, pw, guest, cancel, ok_list, er_list, _build_smb_put_cmds)


def _resolve_smb_jobs_parallel(jobs, cancel, user, pw, guest=False, progress_cb=None):
    cache       = {}
    cache_lock  = threading.Lock()
    expanded    = []
    errors      = []
    result_lock = threading.Lock()
    counter     = [0]

    scan_tasks, seen_get = [], set()
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
            scan_tasks.append(("smb_put_dir",  src, dst, host, share, rpath, title))

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
        lexp, lerr = [], []
        src_url = f"smb://{_host}/{_share}/{_rpath}"
        if not idx:
            lerr.append((src_url, "SMB empty or not found"))
        else:
            prefix = _rpath.rstrip("/") + "/"
            for path, (sz,) in idx.items():
                if cancel.is_set():
                    break
                rel = (os.path.relpath(path, _rpath)
                       if path.startswith(prefix) else os.path.basename(path))
                lexp.append(_SmbJob(src_url, os.path.join(_dst, rel), "smb_get", _host, _share, path, sz, _title))
        with result_lock:
            expanded.extend(lexp)
            errors.extend(lerr)
            counter[0] += len(lexp) + len(lerr)
        if progress_cb:
            progress_cb(counter[0])

    def do_put_file(_src, _dst, _host, _share, _rpath, _title):
        if _SKIP_RE.search(os.path.basename(_src)):
            return
        rp = f"{_rpath}/{os.path.basename(_src)}".lstrip("/")
        with result_lock:
            expanded.append(_SmbJob(_src, _dst, "smb_put", _host, _share, rp, title=_title))
            counter[0] += 1
        if progress_cb:
            progress_cb(counter[0])

    def do_put_dir(_src, _dst, _host, _share, _rpath, _title):
        lexp = []
        for dp, _, filenames in os.walk(_src):
            if cancel.is_set():
                break
            for fn in filenames:
                if _SKIP_RE.search(fn):
                    continue
                lp  = os.path.join(dp, fn)
                rel = os.path.relpath(str(lp), str(_src))
                rp  = f"{_rpath}/{rel}".replace(os.sep, "/").lstrip("/")
                lexp.append(_SmbJob(lp, _dst, "smb_put", _host, _share, rp, title=_title))
        with result_lock:
            expanded.extend(lexp)
            counter[0] += len(lexp)
        if progress_cb:
            progress_cb(counter[0])

    _dispatch = {"smb_get": lambda t: do_get(*t[1:]), "smb_put_file": lambda t: do_put_file(*t[1:]),
                 "smb_put_dir":  lambda t: do_put_dir(*t[1:])}

    with concurrent.futures.ThreadPoolExecutor(max_workers=_SMB_SCAN_WORKERS) as pool:
        futs = [pool.submit(_dispatch[task[0]], task)
                for task in scan_tasks if not cancel.is_set()]
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

    if cancel.is_set():
        return [], []
    return expanded, errors


def _scan_local(scan_jobs, cancel, progress_cb=None):
    pairs, skipped, errors = [], [], []
    lock    = threading.Lock()
    counter = [0]

    def scan_one(src_root, dst_root, title=""):
        lp, ls = [], []
        if os.path.isfile(src_root):
            if _SKIP_RE.search(os.path.basename(src_root)):
                ls.append((src_root, "Lock/Temp file", title))
            else:
                lp.append((src_root, dst_root, title))
        else:
            for dp, _, filenames in os.walk(src_root):
                if cancel.is_set():
                    break
                for fn in filenames:
                    sp = os.path.join(dp, fn)
                    if _SKIP_RE.search(fn):
                        ls.append((sp, "Lock/Temp file", title))
                    else:
                        lp.append((str(sp), os.path.join(str(dst_root), os.path.relpath(str(sp), str(src_root))), title))
        with lock:
            pairs.extend(lp)
            skipped.extend(ls)
            counter[0] += len(lp) + len(ls)
        if progress_cb:
            progress_cb(counter[0])

    if not scan_jobs:
        return pairs, skipped, errors

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(scan_jobs))) as pool:
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
                        errors.append(("scan error", str(exc)))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    return pairs, skipped, errors

def _copy_file(src, dst, cancel, lock, b_ok, b_sk, b_er):
    tmp_dst = dst + ".part"
    try:
        if cancel.is_set():
            return "skip"
        st = os.stat(src)
        try:
            dst_st = os.stat(dst)
            if _compare_stats(st.st_size, st.st_mtime, dst_st.st_size, dst_st.st_mtime):
                with lock:
                    b_sk.append((src, "Up to date"))
                return "skip"
        except FileNotFoundError:
            pass
        dst_dir = os.path.dirname(dst)
        if dst_dir:
            os.makedirs(dst_dir, exist_ok=True)
        with open(src, "rb") as f_src, open(tmp_dst, "wb") as f_dst:
            offset, remaining = 0, st.st_size
            use_sendfile = True
            while remaining > 0:
                if cancel.is_set():
                    raise InterruptedError()
                if use_sendfile:
                    try:
                        sent = os.sendfile(f_dst.fileno(), f_src.fileno(), offset, min(remaining, _CHUNK))
                        if sent == 0:
                            break
                        offset    += sent
                        remaining -= sent
                        continue
                    except OSError:
                        use_sendfile = False
                        f_src.seek(offset)
                        f_dst.seek(offset)
                chunk = f_src.read(min(remaining, _CHUNK))
                if not chunk:
                    break
                f_dst.write(chunk)
                offset    += len(chunk)
                remaining -= len(chunk)
            os.fchmod(f_dst.fileno(), st.st_mode)
        os.utime(tmp_dst, (st.st_atime, st.st_mtime))
        os.replace(tmp_dst, dst)
        with lock:
            b_ok.append((src, dst))
        return "ok"
    except InterruptedError:
        try:
            os.unlink(tmp_dst)
        except OSError:
            pass
        return "skip"
    except Exception as exc:
        msg = f"OS Error: {exc.strerror}" if isinstance(exc, OSError) else f"Unexpected: {exc}"
        with lock:
            b_er.append((src, msg))
        try:
            os.unlink(tmp_dst)
        except OSError:
            pass
        return "error"


class CopyWorker(QThread):
    batch_update  = pyqtSignal(list, list, list, int, int)
    finished_work = pyqtSignal(int, int, int, bool)
    scan_progress = pyqtSignal(str, int)
    entry_status  = pyqtSignal(str, int, int, int)
    scan_finished = pyqtSignal(int)

    def __init__(self, tasks):
        super().__init__()
        self.tasks   = [(t[0], t[1], t[2] if len(t) > 2 else "") for t in tasks]
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()
        self.requestInterruption()

    def run(self):
        c = self._cancel
        tasks_snapshot = list(self.tasks)

        local_jobs, smb_jobs = [], []
        for srcs, dsts, title in tasks_snapshot:
            for s, d in zip(srcs, dsts):
                e = (str(s), str(d), title)
                (smb_jobs if is_smb(str(s)) or is_smb(str(d)) else local_jobs).append(e)

        smb_probe_failed = ""
        smb_expanded = []
        smb_exp_errors = []
        smb_user, smb_pw, smb_guest = "", None, False

        if smb_jobs and not c.is_set():
            smb_user, smb_pw = _get_smb_credentials()
            seen_shares: set[tuple[str, str]] = set()
            failed_shares: dict[tuple[str, str], str] = {}
            probe_result: tuple | str | None = None
            for _s, _d, _t in smb_jobs:
                smb_url = _s if is_smb(_s) else _d
                h, sh, _ = _parse_smb(smb_url)
                key = (h, sh)
                if key in seen_shares or key in failed_shares:
                    continue
                seen_shares.add(key)
                result = _probe_smb(h, sh, smb_user, smb_pw)
                if isinstance(result, str):
                    failed_shares[key] = result
                elif probe_result is None:
                    probe_result = result

            if failed_shares and not (seen_shares - failed_shares.keys()):
                smb_probe_failed = next(iter(failed_shares.values()))
            elif probe_result is not None:
                smb_user, smb_pw, smb_guest = probe_result
                if not c.is_set():
                    smb_expanded, smb_exp_errors = _resolve_smb_jobs_parallel(
                        smb_jobs, c, smb_user, smb_pw, smb_guest,
                        progress_cb=lambda n: self.scan_progress.emit("Scanning SMB", n))
                for _s, _d, _t in smb_jobs:
                    smb_url = _s if is_smb(_s) else _d
                    h, sh, _ = _parse_smb(smb_url)
                    if (h, sh) in failed_shares:
                        reason = ("SMB host unreachable (connection timed out)" if failed_shares[(h, sh)] == "timeout"
                                  else "SMB access denied (credentials failed)")
                        smb_exp_errors.append((_s, reason))

        pairs, scan_skip, scan_err = [], [], []
        if local_jobs and not c.is_set():
            pairs, scan_skip, scan_err = _scan_local(
                local_jobs, c,
                progress_cb=lambda n: self.scan_progress.emit("Scanning local files", n))

        if c.is_set():
            self.finished_work.emit(0, 0, 0, True)
            return

        smb_count = len(smb_jobs) if smb_probe_failed else len(smb_expanded) + len(smb_exp_errors)
        total = smb_count + len(pairs) + len(scan_skip) + len(scan_err)
        self.scan_finished.emit(total)

        done_global = 0
        copied = skipped = errors = 0

        if smb_jobs and not c.is_set():
            if smb_probe_failed:
                reason = ("SMB host unreachable (connection timed out)" if smb_probe_failed == "timeout"
                          else "SMB access denied (credentials failed)")
                er_list = [(s, reason) for s, _d, _t in smb_jobs]
                done_global += len(er_list)
                errors += len(er_list)
                self.batch_update.emit([], [], er_list, done_global, total)
                for t, cnt in _count_by(smb_jobs, key=lambda x: x[2]).items():
                    self.entry_status.emit(t, 0, cnt, 0)
            else:
                done_global, copied, skipped, errors = self._run_smb(smb_expanded, smb_exp_errors, smb_user, smb_pw,
                                                                     smb_guest, done_global, total, c)

        if c.is_set():
            self.finished_work.emit(0, 0, 0, True)
            return

        if scan_skip or scan_err:
            done_global += len(scan_skip) + len(scan_err)
            self.batch_update.emit([], [(s, r) for s, r, *_ in scan_skip], [(s, r) for s, r, *_ in scan_err], done_global, total)

        scan_pre_by_title: dict[str, list[int]] = {}
        for _s, _r, *rest in scan_skip:
            t = rest[0] if rest else ""
            if t:
                scan_pre_by_title.setdefault(t, [0, 0, 0])[2] += 1
        for _s, _r, *rest in scan_err:
            t = rest[0] if rest else ""
            if t:
                scan_pre_by_title.setdefault(t, [0, 0, 0])[1] += 1

        scan_skipped = len(scan_skip)
        scan_errors  = len(scan_err)

        if pairs and not c.is_set():
            done_global, copied, skipped, errors = self._run_local(pairs, done_global, total, copied,
                                                                   skipped + scan_skipped, errors + scan_errors, c,
                                                                   pre_counts=scan_pre_by_title)
        else:
            if scan_pre_by_title:
                for t, ec in scan_pre_by_title.items():
                    self.entry_status.emit(t, ec[0], ec[1], ec[2])
            skipped += scan_skipped
            errors  += scan_errors

        if c.is_set():
            self.finished_work.emit(0, 0, 0, True)
            return

        self.finished_work.emit(copied, skipped, errors, False)

    def _make_flush(self, b_ok, b_sk, b_er, lock, done_ref, total, ok_ref, sk_ref, er_ref):
        def flush(force=False):
            with lock:
                n = len(b_ok) + len(b_sk) + len(b_er)
                if n > 0 and (force or n >= _FLUSH_THRESHOLD):
                    done_ref[0] += n
                    ok_ref[0]   += len(b_ok)
                    sk_ref[0]   += len(b_sk)
                    er_ref[0]   += len(b_er)
                    self.batch_update.emit(list(b_ok), list(b_sk), list(b_er), done_ref[0], total)
                    b_ok.clear(); b_sk.clear(); b_er.clear()
        return flush

    def _run_smb(self, smb_expanded, smb_exp_errors, user, pw, guest, done_global, total, cancel):
        if smb_exp_errors:
            done_global += len(smb_exp_errors)
            self.batch_update.emit([], [], smb_exp_errors, done_global, total)

        if not smb_expanded or cancel.is_set():
            return done_global, 0, 0, len(smb_exp_errors)

        share_groups: dict = {}
        for job in smb_expanded:
            grp = share_groups.setdefault((job.host, job.share), {"get": [], "put": []})
            grp["get" if job.kind == "smb_get" else "put"].append(job)

        ri_cache, ri_lock = {}, threading.Lock()
        b_ok, b_sk, b_er  = [], [], []
        lock              = threading.Lock()
        done_ref = [done_global]
        ok_ref   = [0]
        sk_ref   = [0]
        er_ref   = [len(smb_exp_errors)]
        flush    = self._make_flush(b_ok, b_sk, b_er, lock, done_ref, total, ok_ref, sk_ref, er_ref)
        entry_counts: dict = {}
        ec_lock = threading.Lock()

        def process_share(host, share, get_jobs, put_jobs):
            if cancel.is_set():
                return

            sk_immediate: list = []
            get_to_transfer = []
            put_to_transfer = []

            for j in get_jobs:
                if j.size_matches_local():
                    sk_immediate.append((f"smb://{host}/{share}/{j.remote_path}", "Up to date"))
                else:
                    get_to_transfer.append(j)

            if put_jobs:
                ri = _get_or_build_ri(host, share, put_jobs, user, pw, guest, ri_cache, ri_lock)
                for j in put_jobs:
                    key  = j.remote_path.replace("\\", "/").lstrip("/")
                    meta = ri.get(key)
                    skip = False
                    if meta:
                        try:
                            st   = os.stat(j.src_url)
                            skip = (st.st_size == meta[0])
                        except OSError:
                            pass
                    if skip:
                        sk_immediate.append((j.src_url, "Up to date"))
                    else:
                        put_to_transfer.append(j)

            url_title: dict = {}
            for j in get_jobs:
                url_title[f"smb://{j.host}/{j.share}/{j.remote_path}"] = j.title
            for j in put_jobs:
                url_title[j.src_url] = j.title

            def _record_and_flush(_ok_c, sk_c, _er_c):
                with lock:
                    b_ok.extend(_ok_c)
                    b_sk.extend(sk_c)
                    b_er.extend(_er_c)
                with ec_lock:
                    for url, _ in _ok_c:
                        t = url_title.get(str(url), "")
                        if t: entry_counts.setdefault(t, [0, 0, 0])[0] += 1
                    for url, _ in sk_c:
                        t = url_title.get(str(url), "")
                        if t: entry_counts.setdefault(t, [0, 0, 0])[2] += 1
                    for url, _ in _er_c:
                        t = url_title.get(str(url), "")
                        if t: entry_counts.setdefault(t, [0, 0, 0])[1] += 1
                flush()

            if sk_immediate:
                for i in range(0, len(sk_immediate), _SMB_PROGRESS_CHUNK):
                    if cancel.is_set():
                        break
                    _record_and_flush([], sk_immediate[i: i + _SMB_PROGRESS_CHUNK], [])

            if get_to_transfer and not cancel.is_set():
                dirs_done: set = set()
                for j in get_to_transfer:
                    d = os.path.dirname(str(j.dst_path))
                    if d and d not in dirs_done:
                        os.makedirs(d, exist_ok=True)
                        dirs_done.add(d)
                for i in range(0, len(get_to_transfer), _SMB_PROGRESS_CHUNK):
                    if cancel.is_set():
                        break
                    chunk = get_to_transfer[i: i + _SMB_PROGRESS_CHUNK]
                    ok_c, er_c = [], []
                    _do_smb_get_chunk(host, share, chunk, user, pw, guest, cancel, ok_c, er_c)
                    _record_and_flush(ok_c, [], er_c)

            if put_to_transfer and not cancel.is_set():
                for i in range(0, len(put_to_transfer), _SMB_PROGRESS_CHUNK):
                    if cancel.is_set():
                        break
                    chunk = put_to_transfer[i: i + _SMB_PROGRESS_CHUNK]
                    ok_c, er_c = [], []
                    _do_smb_put_chunk(host, share, chunk, user, pw, guest, cancel, ok_c, er_c)
                    _record_and_flush(ok_c, [], er_c)

        with concurrent.futures.ThreadPoolExecutor(max_workers=_SMB_SHARE_WORKERS) as pool:
            futs = {pool.submit(process_share, h, sh, grp["get"], grp["put"]): (h, sh)
                    for (h, sh), grp in share_groups.items() if not cancel.is_set()}
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

        flush(True)
        for title, ec in entry_counts.items():
            if title:
                self.entry_status.emit(title, ec[0], ec[1], ec[2])
        return done_ref[0], ok_ref[0], sk_ref[0], er_ref[0]

    def _run_local(self, pairs, done_global, total, copied, skipped, errors, cancel, pre_counts=None):
        b_ok, b_sk, b_er = [], [], []
        lock    = threading.Lock()
        ec_lock = threading.Lock()
        entry_counts: dict = {}
        done_ref = [done_global]
        ok_ref   = [copied]
        sk_ref   = [skipped]
        er_ref   = [errors]
        flush    = self._make_flush(b_ok, b_sk, b_er, lock, done_ref, total, ok_ref, sk_ref, er_ref)

        with concurrent.futures.ThreadPoolExecutor(max_workers=_COPY_WORKERS) as pool:
            futures = {pool.submit(_copy_file, s, d, cancel, lock, b_ok, b_sk, b_er): (s, d, t)
                       for s, d, t in pairs}
            try:
                for fut in concurrent.futures.as_completed(futures):
                    if cancel.is_set():
                        for f in futures:
                            f.cancel()
                        break
                    _, _, title = futures[fut]
                    try:
                        res = fut.result()
                        if title:
                            with ec_lock:
                                ec = entry_counts.setdefault(title, [0, 0, 0])
                                if   res == "ok":    ec[0] += 1
                                elif res == "error": ec[1] += 1
                                else:                ec[2] += 1
                        flush()
                    except Exception as exc:
                        with lock:
                            b_er.append(("Pool execution error", str(exc)))
                        flush()
            finally:
                pool.shutdown(wait=True, cancel_futures=True)

        flush(True)
        if pre_counts:
            for t, pc in pre_counts.items():
                ec = entry_counts.setdefault(t, [0, 0, 0])
                for i in range(3):
                    ec[i] += pc[i]
        for t, ec in entry_counts.items():
            self.entry_status.emit(t, ec[0], ec[1], ec[2])
        return done_ref[0], ok_ref[0], sk_ref[0], er_ref[0]


def _lbl(text, style):
    w = QLabel(text)
    w.setStyleSheet(style)
    return w

def _make_card(color, title, val, size_title=18, size_val=32, bold_val=True, contents_margins=(16, 14, 16, 14), spacing=None):
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
    inner.addWidget(title_lbl)
    inner.addWidget(val_lbl)
    return {"frame": frame, "val": val_lbl}


class CopyDialog(QDialog):
    def __init__(self, parent, tasks, operation):
        super().__init__(parent)
        self.setWindowTitle("Backup" if operation == "Backup" else "Restore")
        screen = QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else None
        if geo:
            self.resize(min(1900, int(geo.width() * 0.9)), min(900, int(geo.height() * 0.9)))

        self._operation = operation
        self.worker     = CopyWorker(tasks)

        self.copied = self.skipped = self.errors = 0
        self._done = self._total = 0
        self._final_elapsed = None
        self._pending_ok, self._pending_sk, self._pending_er = [], [], []

        self.status_lbl = QLabel()
        self.status_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_lbl.setFixedHeight(70)
        self._set_status_running()

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(2)

        self._summary   = _SummaryWidget()
        self._w_copied  = _LogWidget(_COLOR_COPIED)
        self._w_skipped = _LogWidget(_COLOR_SKIPPED)
        self._w_errors  = _LogWidget(_COLOR_ERROR)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._summary,   "📋 Summary")
        self.tabs.addTab(self._w_copied,  "⤵ Copied (0)")
        self.tabs.addTab(self._w_skipped, "↷ Skipped (0)")
        self.tabs.addTab(self._w_errors,  "✗ Errors (0)")
        self.tabs.setStyleSheet("QTabBar::tab { width: 200px; }")

        self.cancel_btn = QPushButton("⏹ Cancel")
        self.cancel_btn.setFixedHeight(50)
        self._cancel_connected = True
        self.cancel_btn.clicked.connect(self.worker.cancel)

        layout = QVBoxLayout(self)
        layout.setSpacing(3); layout.setContentsMargins(3, 3, 3, 3)
        layout.addWidget(self.status_lbl); layout.addWidget(sep)
        layout.addWidget(self.tabs); layout.addWidget(self.cancel_btn)

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

    def _elapsed_s(self):
        return self._final_elapsed if self._final_elapsed is not None else self.timer.elapsed() // 1000

    @staticmethod
    def _status_html(icon, label, label_color, bg, border):
        return (f"<span style='display:inline-block;font-size:22px;font-weight:bold;"
                f"font-family:monospace;color:{label_color};background:{bg};"
                f"border-left:5px solid {border};border-radius:7px;"
                f"padding:6px 18px;'>{icon}&thinsp;{label}</span>")

    def _set_status_running(self):
        t = current_theme()
        self.status_lbl.setText(self._status_html("⏳", f"{self._operation} running…",
                                                  label_color=t["cyan"], bg=t["bg2"], border=t["accent"]))
        self.status_lbl.setStyleSheet("padding:8px 16px;")

    def _set_status_scanning(self, phase, scanned):
        t = current_theme()
        self.status_lbl.setText(self._status_html("🔍", f"{phase}… ({scanned:,} found)",
                                                  label_color=t["accent2"], bg=t["bg2"], border=t["accent2"]))

    def _on_scan_finished(self, total):
        t = current_theme()
        suffix = "file" if total == 1 else "files"
        msg = f"Scan complete — {total:,} {suffix} found"
        self.status_lbl.setText(self._status_html("📂", msg, label_color=t["accent"], bg=t["bg2"], border=t["accent"]))

    def _set_status_finished(self, icon, label, color):
        t = current_theme()
        self.status_lbl.setText(self._status_html(icon, label, label_color=color, bg=t["bg2"], border=color))

    def _update_tab_labels(self):
        self.tabs.setTabText(1, f"⤵ Copied ({self.copied:,})")
        self.tabs.setTabText(2, f"↷ Skipped ({self.skipped:,})")
        self.tabs.setTabText(3, f"✗ Errors ({self.errors:,})")

    def _update_clock(self):
        self._summary.update_elapsed(self._elapsed_s(), self._done, self._total)

    @staticmethod
    def _fmt_ok(s, d):
        return f"{apply_replacements(s)}\n Copied to ⤵\n{apply_replacements(d)}"

    @staticmethod
    def _fmt_sk(p, r):
        return f"{apply_replacements(p)} ↷ {r}"

    @staticmethod
    def _fmt_er(p, m):
        return f"{apply_replacements(p)} ❌ {m}"

    def _update_ui_tick(self):
        max_per = 1000 if len(self._pending_ok) > 5000 else 500
        changed = False
        fmt_fns = (self._fmt_ok, self._fmt_sk, self._fmt_er)
        for pending, widget, fmt in zip((self._pending_ok, self._pending_sk, self._pending_er),
                                        (self._w_copied, self._w_skipped, self._w_errors), fmt_fns):
            if not pending:
                continue
            batch = pending[:max_per]
            del pending[:max_per]
            for args in batch:
                widget.add(fmt(*args))
            changed = True
        if changed:
            self._update_tab_labels()
            self._summary.card_copied["val"].setText(f"{self.copied:,}")
            self._summary.card_skipped["val"].setText(f"{self.skipped:,}")
            self._summary.card_errors["val"].setText(f"{self.errors:,}")

    def _on_scan_progress(self, phase, scanned):
        self._set_status_scanning(phase, scanned)

    def _on_batch(self, ok, sk, er, done, total):
        self._done, self._total = done, total
        self.copied  += len(ok)
        self.skipped += len(sk)
        self.errors  += len(er)
        self._pending_ok.extend(ok)
        self._pending_sk.extend(sk)
        self._pending_er.extend(er)
        if total > 0:
            self._summary.update_progress(done, total)

    def _on_done(self, c, s, e, cancelled):
        self._tick.stop(); self._clock_tick.stop()
        self._final_elapsed = self.timer.elapsed() // 1000
        fmt_fns = (self._fmt_ok, self._fmt_sk, self._fmt_er)
        for pending, widget, fmt in zip((self._pending_ok, self._pending_sk, self._pending_er),
                                        (self._w_copied, self._w_skipped, self._w_errors), fmt_fns):
            for args in pending:
                widget.add(fmt(*args))
            pending.clear()
        if not cancelled:
            self.copied, self.skipped, self.errors = c, s, e
        disp_c = self.copied  if cancelled else c
        disp_s = self.skipped if cancelled else s
        disp_e = self.errors  if cancelled else e
        elapsed  = self._final_elapsed
        time_str = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        if cancelled:      icon, label, col = "⏹", f"Cancelled after {time_str}", _COLOR_SKIPPED
        elif disp_e > 0:   icon, label, col = "⚠", f"Done with errors ✗ — {time_str}", _COLOR_ERROR
        else:              icon, label, col = "✓", f"Done — {time_str}", _COLOR_COPIED
        self._set_status_finished(icon, label, col)
        self._summary.refresh(self._operation, self._done, self._total, disp_c, disp_s, disp_e, elapsed, True, cancelled)
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

    def keyPressEvent(self, event):
        k = event.key()
        if k in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            focused = self.focusWidget()
            if isinstance(focused, QPushButton):
                focused.click()
        elif k == Qt.Key.Key_Escape:
            self.worker.cancel() if self.worker.isRunning() else self.accept()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        if self.worker.isRunning():
            self.worker.cancel()
            event.ignore()
        else:
            super().closeEvent(event)


class _SummaryWidget(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)
        t = current_theme()

        self.header_card = QFrame()
        self.header_card.setObjectName("headerCard")
        hdr = QHBoxLayout(self.header_card)
        hdr.setContentsMargins(20, 15, 20, 15)
        self.op_lbl = QLabel("-")
        self.op_lbl.setStyleSheet(_mono_style(24, t["text"], bold=True))
        self._state_lbl = QLabel()
        self._state_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._state_lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.total_lbl = QLabel("")
        self.total_lbl.setStyleSheet(_mono_style(18, t["text_dim"]))
        hdr.addWidget(self.op_lbl); hdr.addSpacing(20)
        hdr.addWidget(self._state_lbl); hdr.addStretch()
        hdr.addWidget(self.total_lbl)

        stats_lay = QGridLayout()
        stats_lay.setSpacing(12)
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
        self._card_elapsed = _make_card(None, "⏲️  Elapsed", "--:--", **kw)
        self._card_speed   = _make_card(None, "🚤  Speed",   "---",   **kw)
        self._card_eta     = _make_card(None, "🏁  ETA",     "--:--", **kw)
        for card in (self._card_elapsed, self._card_speed, self._card_eta):
            metrics_lay.addWidget(card["frame"])

        self._rate_card = QFrame()
        self._rate_card.setObjectName("rateCard")
        rate_lay = QVBoxLayout(self._rate_card)
        rate_lay.setContentsMargins(20, 14, 20, 14)
        rate_lay.setSpacing(8)
        rate_lay.addWidget(_lbl("File breakdown", _mono_style(16, t["text_dim"], extra="border:none;")))
        seg_row = QHBoxLayout()
        seg_row.setSpacing(0)
        self._seg_copied  = QFrame()
        self._seg_skipped = QFrame()
        self._seg_errors  = QFrame()
        for seg, color, radius in ((self._seg_copied,  _COLOR_COPIED,  "border-radius:5px 0 0 5px;"),
                                   (self._seg_skipped, _COLOR_SKIPPED, "border-radius:0;"),
                                   (self._seg_errors,  _COLOR_ERROR,   "border-radius:0 5px 5px 0;")):
            seg.setFixedHeight(10)
            seg.setStyleSheet(f"background:{color};{radius}")
            seg_row.addWidget(seg, 0)
        legend_row = QHBoxLayout()
        legend_row.setSpacing(20)
        for color, text in ((_COLOR_COPIED, "Copied"), (_COLOR_SKIPPED, "Skipped"), (_COLOR_ERROR, "Errors")):
            dot = QLabel(f"<span style='color:{color}'>■</span>  {text}")
            dot.setStyleSheet(_mono_style(14, t["text"], extra="border:none;"))
            legend_row.addWidget(dot)
        legend_row.addStretch()
        rate_lay.addLayout(seg_row)
        rate_lay.addLayout(legend_row)

        self._entry_card = QFrame()
        self._entry_card.setObjectName("entryCard")
        entry_lay = QVBoxLayout(self._entry_card)
        entry_lay.setContentsMargins(20, 14, 20, 14)
        entry_lay.setSpacing(6)
        entry_lay.addWidget(_lbl("Entries processed", _mono_style(16, t["text_dim"], extra="border:none;")))
        scroll = QScrollArea()
        scroll.setMinimumHeight(175)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        self._entry_scroll = scroll
        self._entry_list_widget = QWidget()
        self._entry_list_widget.setStyleSheet("background:transparent;")
        self._entry_grid = QGridLayout(self._entry_list_widget)
        self._entry_grid.setContentsMargins(0, 0, 0, 0)
        self._entry_grid.setHorizontalSpacing(25)
        self._entry_grid.setVerticalSpacing(8)
        scroll.setWidget(self._entry_list_widget)
        entry_lay.addWidget(scroll)

        self._entry_row_labels: dict = {}
        self._entry_results:    dict = {}
        self._entry_grid_cols  = 1
        self._last_total       = 0

        lay.addWidget(self.header_card)
        lay.addLayout(stats_lay)
        lay.addLayout(metrics_lay)
        lay.addWidget(self._progress_card)
        lay.addWidget(self._rate_card)
        lay.addWidget(self._entry_card)
        lay.addStretch()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._recalculate_grid()

    def _recalculate_grid(self):
        new_cols = max(1, (self._entry_card.width() - 48) // 280)
        if new_cols != self._entry_grid_cols:
            self._entry_grid_cols = new_cols
            old_count = self._entry_grid.columnCount()
            for i in range(max(old_count, new_cols + 1)):
                self._entry_grid.setColumnStretch(i, 0)
            for i in range(new_cols):
                self._entry_grid.setColumnStretch(i, 1)
            self._refresh_entry_labels()

    def on_entry_status(self, title, ok, err, skip=0):
        ec = self._entry_results.setdefault(title, [0, 0, 0])
        ec[0] += ok; ec[1] += err; ec[2] += skip
        self._refresh_entry_labels()

    @staticmethod
    def _html_title(title: str) -> str:
        return (title.replace("&", "&amp;").replace("<br>", "\x00").replace("<BR>", "\x00")
        .replace("<", "&lt;").replace(">", "&gt;").replace("\r\n", "<br>").replace("\x00", "<br>")
        .replace("\r", "<br>").replace("\n", "<br>"))

    @staticmethod
    def _count_html_lines(html: str) -> int:
        return html.count("<br>") + 1

    def _refresh_entry_labels(self):
        t = current_theme()
        cols = self._entry_grid_cols
        line_h = 20
        pad = 6

        for title, ec in self._entry_results.items():
            ok, err, skip = ec
            parts = []
            if ok:   parts.append(f"<span style='color:{_COLOR_COPIED}'>⤵ {ok:,}</span>")
            if skip: parts.append(f"<span style='color:{_COLOR_SKIPPED}'>↷ {skip:,}</span>")
            if err:  parts.append(f"<span style='color:{_COLOR_ERROR}'>✗ {err:,}</span>")
            suffix = "&nbsp; ".join(parts) if parts else f"<span style='color:{t['text_dim']}'>–</span>"
            safe_title = self._html_title(title)
            html = f"<span style='color:{t['text']}'>{safe_title}</span><br>{suffix}"

            title_lines = self._count_html_lines(safe_title)
            min_h = (title_lines + 1) * line_h + pad

            if title not in self._entry_row_labels:
                lbl = QLabel()
                lbl.setTextFormat(Qt.TextFormat.RichText)
                lbl.setWordWrap(False)
                lbl.setMinimumWidth(200)
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

    def _update_segments(self, copied, skipped, errors):
        total = copied + skipped + errors
        if total == 0:
            for seg in (self._seg_copied, self._seg_skipped, self._seg_errors):
                seg.setFixedWidth(0)
            return
        avail = self._rate_card.width() - 40
        for seg, count in ((self._seg_copied, copied), (self._seg_skipped, skipped), (self._seg_errors, errors)):
            seg.setFixedWidth(max(0, int(avail * count / total)))

    def refresh(self, operation, done, total, copied, skipped, errors, elapsed_s, finished, cancelled=False):
        t      = current_theme()
        accent = t["accent"]
        self.header_card.setStyleSheet(f"#headerCard{{background:{t['bg3']};border-radius:10px;border-left:4px solid {accent};}}")
        for card in (self._progress_card, self._rate_card, self._entry_card):
            card.setStyleSheet(f"QFrame{{background:{t['bg3']};border-radius:8px;}}")
        self.op_lbl.setText(operation)
        self.op_lbl.setStyleSheet(_mono_style(24, accent, bold=True))
        if not finished:   sc, si, st = t["cyan"],      "⏳", "Running"
        elif cancelled:    sc, si, st = _COLOR_SKIPPED, "⏹", "Cancelled"
        else:              sc, si, st = _COLOR_COPIED,  "✓",  "Done"
        self._state_lbl.setText(f"<span style='font-size:15px;font-weight:bold;font-family:monospace;"
                                f"color:{t['bg']};background:{sc};border-radius:5px;"
                                f"padding:3px 10px;'>&nbsp;{si}&thinsp;{st}&nbsp;</span>")
        self.total_lbl.setText(f"{total:,} files total" if total > 0 else "")
        self.card_copied["val"].setText(f"{copied:,}")
        self.card_skipped["val"].setText(f"{skipped:,}")
        self.card_errors["val"].setText(f"{errors:,}")
        self._last_total = total
        self._update_progress_card(done, total, finished, cancelled)
        self._update_segments(copied, skipped, errors)
        self._update_time_lbl(elapsed_s, done, total, finished, cancelled)

    def _update_progress_card(self, done, total, finished=False, cancelled=False):
        if finished and cancelled:
            self._progress_bar.setRange(0, 1); self._progress_bar.setValue(0)
            self._prog_pct.setText("—"); self._progress_bar.setFormat("Cancelled")
        elif total > 0:
            pct = int(done * 100 / total)
            self._progress_bar.setRange(0, total); self._progress_bar.setValue(done)
            self._prog_pct.setText(f"{pct}%")
            self._progress_bar.setFormat(f"{pct}%  —  {done:,} / {total:,} files")
        else:
            self._progress_bar.setRange(0, 0); self._progress_bar.setValue(0)
            self._prog_pct.setText("…"); self._progress_bar.setFormat("Scanning…")

    def update_elapsed(self, elapsed_s, done, total, finished=False):
        self._update_progress_card(done, total, finished)
        self._update_time_lbl(elapsed_s, done, total, finished=finished)

    def update_progress(self, done, total):
        self._update_progress_card(done, total)

    def _update_time_lbl(self, elapsed_s, done, total, finished, cancelled=False):
        mins, secs = divmod(elapsed_s, 60)
        speed_str, eta_str = "---", "--:--"
        if elapsed_s > 0 and done > 0:
            rate = done / elapsed_s
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
    _PAGE_SIZE = 500

    def __init__(self, color):
        super().__init__()
        self._items: list = []
        self._filtered: list = []
        self._sorted_cache: list = []
        self._cache_dirty = True
        self._dirty = False
        self._page = 0

        self._search = QLineEdit()
        self._search.setPlaceholderText(" 🔍  Search…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        self._search.setFixedHeight(44)

        self._view = QTextEdit()
        self._view.setReadOnly(True)
        self._view.setStyleSheet(f"font-family:monospace;font-size:14px;color:{color};")

        self._first_btn = QPushButton("««")
        self._prev_btn  = QPushButton("‹ Prev")
        self._next_btn  = QPushButton("Next ›")
        self._last_btn  = QPushButton("»»")

        self._first_btn.clicked.connect(self._first_page)
        self._prev_btn.clicked.connect(self._prev_page)
        self._next_btn.clicked.connect(self._next_page)
        self._last_btn.clicked.connect(self._last_page)

        for btn in (self._first_btn, self._prev_btn, self._next_btn, self._last_btn):
            btn.setFixedWidth(60 if len(btn.text()) < 3 else 80)
            btn.setFixedHeight(28)

        self._page_spin = QSpinBox()
        self._page_spin.setMinimum(1)
        self._page_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._page_spin.setStyleSheet(
            "QSpinBox {border: 1px solid #555; border-radius: 4px; padding: 2px 5px;"
            "background: #2b2b2b; color: #fff; font-weight: bold} "
            "QSpinBox:focus {border: 1px solid #888; background: #333}")
        self._page_spin.setFixedWidth(55)
        self._page_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_spin.editingFinished.connect(self._go_to_page)

        self._page_lbl  = QLabel("")
        self._total_lbl = QLabel("")
        self._total_lbl.setStyleSheet("color: #888; font-size: 14px; margin-left: 10px;")

        nav = QHBoxLayout()
        nav.setContentsMargins(5, 5, 5, 5)
        nav.setSpacing(8)
        nav.addWidget(self._first_btn)
        nav.addWidget(self._prev_btn)
        nav.addStretch(1)

        page_group = QHBoxLayout()
        page_group.setSpacing(5)
        page_group.addWidget(QLabel("Page"))
        page_group.addWidget(self._page_spin)
        page_group.addWidget(QLabel("of"))
        page_group.addWidget(self._page_lbl)
        nav.addLayout(page_group)

        nav.addStretch(1)
        nav.addWidget(self._total_lbl)
        nav.addWidget(self._next_btn)
        nav.addWidget(self._last_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(3)
        lay.addWidget(self._search)
        lay.addWidget(self._view)
        lay.addLayout(nav)

        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(750)
        self._flush_timer.timeout.connect(self._flush)

    def _render_page(self):
        total = len(self._filtered)
        pages = max(1, (total + self._PAGE_SIZE - 1) // self._PAGE_SIZE)
        self._page = max(0, min(self._page, pages - 1))

        start = self._page * self._PAGE_SIZE
        chunk = self._filtered[start: start + self._PAGE_SIZE]
        lines = []
        for rel_idx, item in enumerate(chunk):
            idx = start + rel_idx + 1
            parts = item.split("\n")
            lines.append(f"{idx}: {parts[0]}")
            lines.extend(parts[1:])
            lines.append("")
        self._view.setPlainText("\n".join(lines))

        self._page_lbl.setText(f"<b>{pages}</b>")
        self._total_lbl.setText(f"({total:,} {'entry' if total == 1 else 'entries'})")

        self._page_spin.setMaximum(pages)
        self._page_spin.blockSignals(True)
        self._page_spin.setValue(self._page + 1)
        self._page_spin.blockSignals(False)

        can_back    = self._page > 0
        can_forward = self._page < pages - 1
        self._first_btn.setEnabled(can_back)
        self._prev_btn.setEnabled(can_back)
        self._next_btn.setEnabled(can_forward)
        self._last_btn.setEnabled(can_forward)

    def _first_page(self): self._page = 0; self._render_page()

    def _last_page(self):
        self._page = max(0, (len(self._filtered) + self._PAGE_SIZE - 1) // self._PAGE_SIZE - 1)
        self._render_page()

    def _prev_page(self):
        if self._page > 0:
            self._page -= 1; self._render_page()

    def _next_page(self):
        pages = max(1, (len(self._filtered) + self._PAGE_SIZE - 1) // self._PAGE_SIZE)
        if self._page < pages - 1:
            self._page += 1; self._render_page()

    def _go_to_page(self):
        target = self._page_spin.value() - 1
        if target != self._page:
            self._page = target; self._render_page()

    def add(self, entry):
        self._items.append(entry)
        self._cache_dirty = True
        self._dirty = True
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def flush_final(self):
        self._flush_timer.stop()
        self._apply_filter()
        self._render_page()

    def _flush(self):
        if self._dirty:
            self._dirty = False
            self._apply_filter()
            self._render_page()
        else:
            self._flush_timer.stop()

    def _on_search(self):
        self._page = 0
        self._apply_filter()
        self._render_page()

    def _apply_filter(self):
        if self._cache_dirty:
            self._sorted_cache = sorted(self._items)
            self._cache_dirty = False
        needle = self._search.text().lower()
        self._filtered = ([i for i in self._sorted_cache if needle in i.lower()] if needle else list(self._sorted_cache))