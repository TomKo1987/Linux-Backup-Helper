import concurrent.futures
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path as _Path
from typing import Callable, Protocol, TYPE_CHECKING

from drive_utils import is_smb
from state import logger

if TYPE_CHECKING:
    from copy_worker import _Flusher, _EntryTracker

from copy_worker_core import (
    _SMB_WORKERS, _SMB_TIMEOUT, _SMB_FILE_SECS, _SMB_CHUNK,
    _SHM_DIR, _smb_procs, _smb_procs_lock, _SMB_LINE_RE, _SKIP_RE, _CACHE_MISS,
    _is_unreachable, _parse_smb, _q, _run_futures, _ensure_dir, _silent_unlink
)


class _SecurePw(Protocol):
    def get(self) -> str: ...
    def get_bytes(self) -> bytearray: ...
    def clear(self) -> None: ...


def _get_smb_credentials() -> tuple[str, "_SecurePw | None"]:
    from samba_credentials import SambaPasswordManager
    u, pw, _ = SambaPasswordManager().get_credentials()
    return (u or ""), pw


def _smb_cred_file(user: str, pw: "_SecurePw") -> "tuple[str, str]":
    if _SHM_DIR is None:
        raise RuntimeError("/dev/shm unavailable — caller must check before calling")
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="smb_", dir=_SHM_DIR)
        cred_path = os.path.join(tmp_dir, "cred")
        fd = os.open(cred_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            f_obj = os.fdopen(fd, 'wb')
            fd = -1
        except BaseException:
            os.close(fd)
            raise
        with f_obj as f:
            if "\\" in user:
                domain, plain_user = user.split("\\", 1)
                f.write(f"username = {plain_user}\n".encode("utf-8"))
                f.write(f"domain = {domain}\n".encode("utf-8"))
            else:
                f.write(f"username = {user}\n".encode("utf-8"))

            pwd_bytes = pw.get_bytes()
            try:
                f.write(b"password = ")
                f.write(pwd_bytes)
                f.write(b"\n")
            finally:
                for i in range(len(pwd_bytes)):
                    pwd_bytes[i] = 0
            return tmp_dir, cred_path
    except Exception as exc:
        logger.error("Error creating the SMB credential file: %s", exc)
        if tmp_dir and os.path.isdir(tmp_dir):
            try:
                _cred = os.path.join(tmp_dir, "cred")
                if os.path.exists(_cred):
                    _wipe_smb_cred(tmp_dir, _cred)
                else:
                    shutil.rmtree(tmp_dir)
            except OSError:
                pass
        raise


def _wipe_smb_cred(tmp_dir: str, path: str) -> None:
    try:
        sz = os.stat(path).st_size
        fd = os.open(path, os.O_WRONLY)
        try:
            os.write(fd, b"\x00" * sz)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
    _silent_unlink(path)
    try:
        os.rmdir(tmp_dir)
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
    title:       str = ""

    def size_matches_local(self) -> bool:
        if self.kind != "smb_get" or self.remote_size < 0:
            return False
        try:
            return self.remote_size == os.stat(self.dst_path).st_size
        except OSError:
            return False


def _build_smb_get_cmds(jobs: list[_SmbJob]) -> str:
    lines = []
    def _key(x):
        return os.path.dirname(x.remote_path)
    for rdir, group in groupby(sorted(jobs, key=_key), key=_key):
        lines.append(f'cd "/{_q(str(rdir))}"' if rdir else 'cd "/"')
        for j in group:
            lines.append(f'get "{_q(os.path.basename(j.remote_path))}" "{_q(j.dst_path)}"')
    lines.append("exit\n")
    return "\n".join(lines)


def _build_smb_put_cmds(jobs: list[_SmbJob]) -> str:
    dirs_to_make = sorted({os.path.dirname(j.remote_path).replace("\\", "/").strip("/") for j in jobs})
    seen = set()
    cmds = []
    for d in dirs_to_make:
        if not d: continue
        parts = d.split("/")
        for i in range(1, len(parts) + 1):
            p = "/".join(parts[:i])
            if p not in seen:
                cmds.append(f'mkdir "{_q(p)}"')
                seen.add(p)
    curr_l, curr_r = None, None
    for j in sorted(jobs, key=lambda x: (os.path.dirname(x.remote_path), x.src_url)):
        local_dir = os.path.dirname(j.src_url)
        r_dir = os.path.dirname(j.remote_path).replace("\\", "/").strip("/")
        if local_dir != curr_l:
            cmds.append(f'lcd "{_q(local_dir)}"')
            curr_l = local_dir
        if r_dir != curr_r:
            cmds.append(f'cd "/{_q(r_dir)}"' if r_dir else 'cd "/"')
            curr_r = r_dir
        cmds.append(f'put "{_q(os.path.basename(j.src_url))}" "{_q(os.path.basename(j.remote_path))}"')
    cmds.append("exit\n")
    return "\n".join(cmds)


class _SmbClient:

    def __init__(self, host: str, share: str, user: str, pw: "_SecurePw | None", guest: bool = False) -> None:
        self.host   = host
        self.share  = share
        self._user  = user
        self._pw    = pw
        self._guest = guest
        _proto = ["-m", "SMB3"]
        base = ["smbclient", f"//{host}/{share}"]
        self._argv = ([*base, "-N", *_proto]) if guest else (base + _proto)

    def _spawn(self, argv: list[str], input_data: str, timeout: int, wipe_fn=None) -> "tuple[subprocess.Popen | None, str, str]":
        tid = threading.get_ident()
        env = dict(os.environ, LC_ALL="C", LANG="C")
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", env=env)
            with _smb_procs_lock:
                _smb_procs[tid] = proc
            try:
                out, err = proc.communicate(input=input_data, timeout=timeout)
                return proc, out, err
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                logger.warning("SMB timeout after %ds (//%s/%s)", timeout, self.host, self.share)
                return None, "", "timeout"
        except Exception as exc:
            logger.error("SMB run error: %s", exc)
            return None, "", str(exc)
        finally:
            with _smb_procs_lock:
                _smb_procs.pop(tid, None)
            if wipe_fn is not None:
                wipe_fn()

    def _argv_with_creds(self) -> "tuple[list[str], str | None, str | None]":
        if self._guest:
            return self._argv[:], None, None
        if not self._pw:
            if self._user:
                logger.warning("SMB //%s/%s: user '%s' set but no password available, falling back to guest.",
                               self.host, self.share, self._user)
            return [*self._argv, "-N"], None, None
        if _SHM_DIR is not None:
            try:
                tmp_dir, cred_path = _smb_cred_file(self._user, self._pw)
                return [*self._argv, "-A", cred_path], tmp_dir, cred_path
            except (OSError, RuntimeError) as exc:
                logger.warning("SMB //%s/%s: Secure pw failed (%s). Falling back to guest.", self.host, self.share, exc)
        else:
            logger.warning("SMB //%s/%s: /dev/shm not available. Falling back to guest.", self.host, self.share)
        return [*self._argv, "-N"], None, None

    def _run_with_creds(self, input_data: str, timeout: int) -> "tuple[subprocess.Popen | None, str, str]":
        argv, tmp_dir, cred_path = self._argv_with_creds()
        wipe_fn = (lambda: _wipe_smb_cred(str(tmp_dir), str(cred_path))) if (tmp_dir and cred_path) else None

        return self._spawn(argv, input_data, timeout, wipe_fn=wipe_fn)

    def run(self, cmds: str, timeout: int) -> tuple[bool, str]:
        proc, _, err = self._run_with_creds(cmds, timeout)
        if proc is None:
            return False, err
        ok = proc.returncode == 0
        return ok, ("" if ok else (err.strip() or f"exit {proc.returncode}"))

    def ls_index(self, base: str) -> "dict | None":
        base = base.replace("\\", "/").rstrip("/")
        cmd = (f'recurse on\nprompt off\ncd "{_q(base)}"\nls\n' if base else "recurse on\nprompt off\nls\n")
        proc, stdout, stderr = self._run_with_creds(cmd, _SMB_TIMEOUT)
        if proc is None:
            return None
        if proc.returncode != 0:
            return None if _is_unreachable(stderr) else {}
        index: dict = {}
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
        return index

    def probe(self) -> str:
        if self._user and self._pw:
            ok, err = self.run("exit\n", _SMB_TIMEOUT)
            if ok:
                return "ok"
            if _is_unreachable(err):
                logger.warning("SMB unreachable //%s/%s: %s", self.host, self.share, err)
                return "timeout"
            logger.warning("SMB auth failed //%s/%s: %s", self.host, self.share, err)
            return "auth"
        ok, err = _SmbClient(self.host, self.share, "", None, guest=True).run("exit\n", _SMB_TIMEOUT)
        if ok:
            return "guest"

        if _is_unreachable(err):
            logger.warning("SMB unreachable //%s/%s: %s", self.host, self.share, err)
            return "timeout"
        logger.warning("SMB auth failed //%s/%s: %s", self.host, self.share, err)
        return "auth"


class _SmbScanner:
    _CACHE_MAX = 1000

    def __init__(self, user: str, pw: "_SecurePw | None", guest: bool, cancel: threading.Event, progress_cb=None) -> None:
        self._user        = user
        self._pw          = pw
        self._guest       = guest
        self._cancel      = cancel
        self._progress_cb = progress_cb
        self._ls_cache:   dict = {}
        self._cache_lock  = threading.Lock()
        self._result_lock = threading.Lock()
        self._counter     = 0
        self._counter_lock = threading.Lock()

    def _client(self, host: str, share: str) -> _SmbClient: return _SmbClient(host, share, self._user, self._pw, self._guest)

    def _report(self, n: int) -> None:
        with self._counter_lock:
            self._counter += n
            cur = self._counter
        if self._progress_cb:
            self._progress_cb(cur)

    def resolve(self, jobs) -> tuple[list, list]:
        expanded: list = []
        errors: list = []
        seen_get: set = set()
        tasks: list[Callable[[], None]] = []

        def create_get_task(_h, _sh, _rp, _d, _ti):
            return lambda: self._do_get(_h, _sh, _rp, _d, _ti, expanded, errors)

        def create_put_task(_s, _h, _sh, _rp, _ti, _is_file):
            if _is_file:
                return lambda: self._do_put_file(_s, _h, _sh, _rp, _ti, expanded)
            return lambda: self._do_put_dir(_s, _h, _sh, _rp, _ti, expanded)

        for src, dst, *rest in jobs:
            if self._cancel.is_set():
                break
            title = rest[0] if rest else ""
            src_is_smb = is_smb(src)
            host, share, rpath = _parse_smb(src if src_is_smb else dst)

            if src_is_smb:
                key = (host, share, rpath, dst)
                if key in seen_get:
                    continue
                seen_get.add(key)
                tasks.append(create_get_task(host, share, rpath, dst, title))
            else:
                is_file = os.path.isfile(src)
                tasks.append(create_put_task(src, host, share, rpath, title, is_file))

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
            if ck in self._ls_cache:
                return self._ls_cache[ck]
        if self._cancel.is_set():
            return None
        idx = self._client(host, share).ls_index(rpath)
        with self._cache_lock:
            if ck not in self._ls_cache:
                if len(self._ls_cache) >= self._CACHE_MAX:
                    del self._ls_cache[next(iter(self._ls_cache))]
                self._ls_cache[ck] = idx
            else:
                idx = self._ls_cache[ck]
        return idx

    def _do_get(self, host, share, rpath, dst, title, expanded, errors) -> None:
        idx     = self._cached_index(host, share, rpath)
        src_url = f"smb://{host}/{share}/{rpath}"
        lexp:   list = []
        lerr:   list = []
        if idx is None:
            lerr.append((src_url, "NT_STATUS_HOST_UNREACHABLE"))
        elif idx:
            prefix = rpath.rstrip("/") + "/" if rpath else None
            for path, (sz,) in idx.items():
                if self._cancel.is_set():
                    break
                if not rpath:
                    rel = path
                elif prefix and path.startswith(prefix):
                    rel = os.path.relpath(path, rpath)
                else:
                    rel = os.path.basename(path)
                dst_path = str(_Path(dst) / str(rel))
                lexp.append(_SmbJob(src_url=src_url, dst_path=dst_path, kind="smb_get", host=host, share=share,
                                    remote_path=path, remote_size=sz, title=title))
        with self._result_lock:
            expanded.extend(lexp)
            errors.extend(lerr)
        self._report(len(lexp) + len(lerr))

    def _do_put_file(self, src: str, host: str, share: str, rpath: str, title: str, expanded: list) -> None:
        if _SKIP_RE.search(os.path.basename(src)):
            return
        rp = f"{rpath}/{os.path.basename(src)}".lstrip("/")
        with self._result_lock:
            expanded.append(_SmbJob(src, "", "smb_put", host, share, rp, title=title))
        self._report(1)

    def _do_put_dir(self, src, host, share, rpath, title, expanded) -> None:
        lexp: list = []
        stack: list = [src]

        try:
            st = os.stat(src)
            seen_inos: set = {(st.st_dev, st.st_ino)}
        except OSError:
            seen_inos: set = set()

        while stack:
            if self._cancel.is_set():
                break
            try:
                current_dir = stack.pop()
                with os.scandir(current_dir) as it:
                    for e in it:
                        if _SKIP_RE.search(e.name):
                            continue
                        try:
                            is_dir_eff = e.is_dir(follow_symlinks=True)
                            is_file_eff = e.is_file(follow_symlinks=True)
                        except OSError:
                            continue
                        if is_dir_eff:
                            try:
                                e_st = e.stat(follow_symlinks=True)
                                ino = (e_st.st_dev, e_st.st_ino)
                            except OSError:
                                continue
                            if ino in seen_inos:
                                continue
                            seen_inos.add(ino)
                            stack.append(e.path)
                        elif is_file_eff:
                            rel = os.path.relpath(e.path, src)
                            rp = f"{rpath}/{rel}".replace(os.sep, "/").lstrip("/")
                            lexp.append(_SmbJob(e.path, "", "smb_put", host, share, rp, title=title))
            except (PermissionError, FileNotFoundError, NotADirectoryError):
                pass
        with self._result_lock:
            expanded.extend(lexp)
        self._report(len(lexp))


class _ShareProcessor:

    def __init__(self, client: _SmbClient, cancel: threading.Event, flusher: "_Flusher",
                 tracker: "_EntryTracker", ri_cache: dict, ri_lock: threading.Lock) -> None:
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
            full_url = self._remote_url(j.remote_path)
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
                if meta and isinstance(meta, (tuple, list)) and len(meta) > 0 and local_sz == meta[0]:
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
                    _ensure_dir(str(d))
            get_batches = [get_transfer[i: i + _SMB_CHUNK] for i in range(0, len(get_transfer), _SMB_CHUNK)]

            def _run_get_batch(batch):
                if self._cancel.is_set() or self._unreachable.is_set():
                    self._fail_batch(batch, is_get=True)
                    return
                _ok_c, _er_c = self._transfer(batch, _build_smb_get_cmds)
                self._record(_ok_c, [], _er_c)

            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(4, len(get_batches) or 1)) as _gpool:
                gfuts = [_gpool.submit(_run_get_batch, b) for b in get_batches if not self._cancel.is_set()]
                _run_futures(gfuts, self._cancel, "smb get batch")

        if put_transfer and not self._cancel.is_set():
            put_batches = [put_transfer[i: i + _SMB_CHUNK] for i in range(0, len(put_transfer), _SMB_CHUNK)]

            def _run_put_batch(batch):
                if self._cancel.is_set() or self._unreachable.is_set():
                    self._fail_batch(batch, is_get=False)
                    return
                ok_c, er_c = self._transfer(batch, _build_smb_put_cmds)
                self._record(ok_c, [], er_c)

            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(4, len(put_batches) or 1)) as _ppool:
                pfuts = [_ppool.submit(_run_put_batch, b) for b in put_batches if not self._cancel.is_set()]
                _run_futures(pfuts, self._cancel, "smb put batch")

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
            if not isinstance(cached, dict):
                logger.error("SMB remote index: unexpected cache value type %s for key %s", type(cached), key)
                break
            merged.update(cached)
        return merged

    def _remote_url(self, remote_path: str) -> str:
        return f"smb://{self.host}/{self.share}/{remote_path}"

    def _job_src(self, job: "_SmbJob", is_get: bool) -> str:
        return self._remote_url(job.remote_path) if is_get else job.src_url

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
                er_list.extend((self._job_src(j, is_get), "NT_STATUS_HOST_UNREACHABLE") for j in batch)
                continue

            ok, err = self._client.run(build_fn(batch), max(_SMB_TIMEOUT, len(batch) * _SMB_FILE_SECS))
            if ok:
                for j in batch:
                    src = self._job_src(j, is_get)
                    dst = j.dst_path if is_get else self._remote_url(j.remote_path)
                    ok_list.append((src, dst))
            elif _is_unreachable(err):
                self._unreachable.set()
                er_list.extend((self._job_src(j, is_get), "NT_STATUS_HOST_UNREACHABLE") for j in batch)
            elif len(batch) == 1:
                er_list.append((self._job_src(batch[0], is_get), err))
            else:
                mid = len(batch) // 2
                stack.append(batch[mid:])
                stack.append(batch[:mid])

        return ok_list, er_list

    def _record(self, ok_c: list, sk_c: list, er_c: list) -> None:
        def _meta(url: str) -> tuple[str, int]:
            return self._url_title.get(url, ("", 0))

        batch_counts: dict = {}

        def _count(_title: str, slot: int) -> None:
            if _title:
                batch_counts.setdefault(_title, [0, 0, 0])[slot] += 1

        ok_w = []
        for s, d in ok_c:
            title, sz = _meta(s)
            ok_w.append((s, d, sz))
            _count(title, 0)

        sk_w = []
        for s, r in sk_c:
            title, sz = _meta(s)
            sk_w.append((s, r, sz))
            _count(title, 1)

        er_w = [(s, e, 0) for s, e in er_c]
        for s, _e, _ in er_w:
            _count(_meta(s)[0], 2)

        self._flusher.push(ok=ok_w, sk=sk_w, er=er_w)
        self._tracker.batch_update(batch_counts)

    def _fail_batch(self, remaining: list, *, is_get: bool) -> None:
        er_c = [(self._job_src(j, is_get), "NT_STATUS_HOST_UNREACHABLE") for j in remaining]
        if er_c:
            self._record([], [], er_c)
