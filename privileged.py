import os
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace
from typing import Any, Sequence, cast

from state import logger
from translations import tr

__all__ = [
    "AuthRequired", "SudoSession", "authenticate", "auth_required_message",
    "is_root", "invalidate_sudo_probe", "passwordless_sudo", "release_session",
    "run_privileged", "run_user_command", "split_sudo", "sudo_available",
]

_PROBE_TTL = 30.0
_KEEPALIVE_INTERVAL = 240
_VERIFY_TIMEOUT = 15

_probe_lock = threading.Lock()
_probe_cache: tuple[float, bool] | None = None


class AuthRequired(RuntimeError):
    """Raised/returned when a command needs privileges we are not allowed to obtain."""


def auth_required_message() -> str:
    return tr("Administrator rights are required, but no authenticated sudo session is "
              "available. Start the System Manager once to authenticate, or configure "
              "passwordless sudo for this command.")


def _zero(buf: bytearray | None) -> None:
    if buf:
        buf[:] = bytearray(len(buf))


def _pw_stdin(pw: Any) -> bytearray:
    if not pw:
        return bytearray()
    buf = pw.get_bytes()
    buf.append(0x0A)
    return buf


def is_root() -> bool:
    return os.geteuid() == 0


def sudo_available() -> bool:
    return shutil.which("sudo") is not None


def clean_env() -> dict:
    env = os.environ.copy()
    env.update({"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"})
    env.pop("SUDO_ASKPASS", None)
    return env


def invalidate_sudo_probe() -> None:
    global _probe_cache
    with _probe_lock:
        _probe_cache = None


def _account_has_no_password() -> bool:
    passwd_bin = shutil.which("passwd")
    if not passwd_bin:
        return False
    try:
        r = subprocess.run([passwd_bin, "-S"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env=clean_env(), timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    fields = r.stdout.split()
    return len(fields) >= 2 and fields[1] == "NP"


def passwordless_sudo(force: bool = False) -> bool:
    global _probe_cache
    if is_root():
        return True
    if not sudo_available():
        return False
    now = time.monotonic()
    if not force:
        with _probe_lock:
            if _probe_cache and (now - _probe_cache[0]) < _PROBE_TTL:
                return _probe_cache[1]
    try:
        ok = subprocess.run(["sudo", "-n", "true"], capture_output=True,
                            stdin=subprocess.DEVNULL, env=clean_env(),
                            timeout=5, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok and _account_has_no_password():
        try:
            ok = subprocess.run(["sudo", "-S", "-p", "", "true"], input="\n", capture_output=True,
                                text=True, env=clean_env(), timeout=5, check=False).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    with _probe_lock:
        _probe_cache = (time.monotonic(), ok)
    return ok


def split_sudo(tokens: Sequence[str]) -> list[str]:
    out = list(tokens)
    if not out or os.path.basename(out[0]) != "sudo":
        return out
    out.pop(0)
    while out and out[0].startswith("-"):
        flag = out.pop(0)
        if flag in ("-u", "--user", "-g", "--group", "-p", "--prompt", "-C", "--close-from"):
            if out:
                out.pop(0)
    return out


class SudoSession:

    _instance: "SudoSession | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pw: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def instance(cls) -> "SudoSession":
        with cls._instance_lock:
            inst = cls._instance
            if inst is None:
                inst = cls._instance = cls()
            return inst

    def has_password(self) -> bool:
        with self._lock:
            return bool(self._pw)

    def password(self) -> Any:
        with self._lock:
            return self._pw if self._pw else None

    def attach(self, pw: Any) -> None:
        with self._lock:
            if self._pw is not None and self._pw is not pw:
                try:
                    self._pw.clear()
                except AttributeError:
                    pass
            self._pw = pw if pw else None
            if self._pw is None:
                return
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._stop.clear()
                thread = threading.Thread(target=self._keepalive, name="sudo-keepalive", daemon=True)
                self._thread = thread
                thread.start()

    def release(self, invalidate: bool = True) -> None:
        with self._lock:
            self._stop.set()
            thread = self._thread
            self._thread = None
            pw = self._pw
            self._pw = None
        if thread is not None and thread is not threading.current_thread():
            cast(threading.Thread, thread).join(timeout=5)
        if pw is not None:
            try:
                pw.clear()
            except AttributeError:
                pass
        if invalidate:
            try:
                subprocess.run(["sudo", "-k"], capture_output=True,
                               stdin=subprocess.DEVNULL, timeout=5, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        invalidate_sudo_probe()

    def _keepalive(self) -> None:
        while not self._stop.wait(_KEEPALIVE_INTERVAL):
            pw = self.password()
            if pw is None:
                return
            try:
                r = subprocess.run(["sudo", "-n", "-v"], capture_output=True,
                                   stdin=subprocess.DEVNULL, env=clean_env(), timeout=10, check=False)
                if r.returncode != 0:
                    buf = _pw_stdin(pw)
                    try:
                        subprocess.run(["sudo", "-S", "-p", "", "-v"], input=buf,
                                       capture_output=True, env=clean_env(), timeout=10, check=False)
                    finally:
                        _zero(buf)
            except Exception as exc:
                logger.debug("sudo keepalive: %s", exc)

    @staticmethod
    def verify(pw: Any) -> bool:
        if is_root():
            return True
        if not pw:
            return passwordless_sudo(force=True)
        if not sudo_available():
            return False
        try:
            subprocess.run(["sudo", "-k"], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        token = secrets.token_hex(16)
        buf = _pw_stdin(pw)
        proc = None
        ok = False
        try:
            proc = subprocess.Popen(
                ["sudo", "-S", "-p", "", "sh", "-c", f"printf '%s\\n' {shlex.quote(token)}"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=clean_env())
            try:
                out, _err = proc.communicate(input=buf, timeout=_VERIFY_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _err = proc.communicate()
            ok = proc.returncode == 0 and out.strip() == token.encode()
        except Exception as exc:
            logger.error("sudo verification failed: %s", exc)
        finally:
            _zero(buf)
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        invalidate_sudo_probe()
        return ok


def _build_argv(args: Sequence[str]) -> tuple[list[str] | None, Any]:
    argv = list(args)
    if not argv:
        return None, None
    if is_root():
        return argv, None
    if not sudo_available():
        return None, None
    pw = SudoSession.instance().password()
    if pw is not None:
        return ["sudo", "-S", "-p", "", *argv], pw
    if passwordless_sudo():
        if _account_has_no_password():
            return ["sudo", "-S", "-p", "", *argv], ""
        return ["sudo", "-n", *argv], None
    return None, None


def run_privileged(args: Sequence[str], *, timeout: int = 15, text: bool = True,
                   cwd: str | None = None) -> subprocess.CompletedProcess | None:

    argv, pw = _build_argv(args)
    if argv is None:
        logger.debug("run_privileged: no non-interactive privilege path for %r", args[:1])
        return None
    buf = _pw_stdin(pw) if pw is not None else None
    try:
        r = subprocess.run(
            argv,
            input=buf,
            stdin=None if buf is not None else subprocess.DEVNULL,
            capture_output=True, env=clean_env(), timeout=timeout,
            cwd=cwd, check=False)
        if not text:
            return r
        return subprocess.CompletedProcess(
            r.args, r.returncode,
            (r.stdout or b"").decode("utf-8", "replace"),
            (r.stderr or b"").decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        logger.warning("run_privileged: timeout after %ds: %r", timeout, args[:1])
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("run_privileged: %s", exc)
        return None
    finally:
        _zero(buf)


def run_user_command(tokens: Sequence[str], *, timeout: int = 30,
                     cwd: str | None = None) -> SimpleNamespace:

    argv = list(tokens)
    if not argv:
        return SimpleNamespace(returncode=1, stdout="", stderr=tr("Empty command"))

    needs_root = os.path.basename(argv[0]) == "sudo"
    if needs_root:
        inner = split_sudo(argv)
        if not inner:
            return SimpleNamespace(returncode=1, stdout="", stderr=tr("sudo without a command"))
        r = run_privileged(inner, timeout=timeout, cwd=cwd)
        if r is None:
            return SimpleNamespace(returncode=126, stdout="", stderr=auth_required_message())
        return SimpleNamespace(returncode=r.returncode, stdout=r.stdout or "", stderr=r.stderr or "")

    try:
        r = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           env=clean_env(), timeout=timeout, cwd=cwd, check=False)
        return SimpleNamespace(returncode=r.returncode, stdout=r.stdout or "", stderr=r.stderr or "")
    except subprocess.TimeoutExpired:
        return SimpleNamespace(returncode=124, stdout="",
                               stderr=tr("Timed out after {timeout}s", timeout=timeout))
    except (OSError, subprocess.SubprocessError) as exc:
        return SimpleNamespace(returncode=1, stdout="", stderr=str(exc))


def release_session() -> None:
    SudoSession.instance().release()


def authenticate(parent=None, *, force: bool = False) -> bool:

    session = SudoSession.instance()
    if not force:
        if is_root() or session.has_password():
            return True
        if passwordless_sudo(force=True):
            return True
    if not sudo_available():
        return False

    from sudo_password import SudoPasswordDialog

    holder: list[Any] = []
    dlg = SudoPasswordDialog(parent)
    dlg.sudo_password_entered.connect(holder.append)
    dlg.exec()
    if not holder:
        return False
    pw = holder[0]
    if SudoSession.verify(pw):
        session.attach(pw)
        return True
    try:
        pw.clear()
    except AttributeError:
        pass
    return False
