import atexit
import errno
import fcntl
import os
import tempfile
import threading
from pathlib import Path

from state import _CONFIG_DIR, _USER, logger


def _runtime_dir() -> Path:
    for candidate in (
        _CONFIG_DIR,
        os.environ.get("XDG_RUNTIME_DIR"),
        f"/run/user/{os.getuid()}" if os.path.isdir(f"/run/user/{os.getuid()}") else None,
        tempfile.gettempdir(),
    ):
        if not candidate:
            continue
        p = Path(candidate)
        try:
            p.mkdir(parents=True, exist_ok=True)
            if os.access(p, os.W_OK):
                return p
        except OSError:
            continue
    return _CONFIG_DIR


_LOCK_PATH = _runtime_dir() / f"backup-helper-{_USER}.lock"

_lock_fd: int | None = None
_state_lock = threading.Lock()
_atexit_registered = False


def acquire_backup_lock() -> bool:
    global _lock_fd, _atexit_registered
    with _state_lock:
        if _lock_fd is not None:
            return False

        try:
            _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            logger.warning("backup_lock: could not open lock file: %s", exc)
            return True

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            logger.warning("backup_lock: unexpected flock error: %s", exc)
            return True

        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
        except OSError:
            pass

        _lock_fd = fd
        if not _atexit_registered:
            _atexit_registered = True
            atexit.register(release_backup_lock)
        return True


def release_backup_lock() -> None:
    global _lock_fd
    with _state_lock:
        fd, _lock_fd = _lock_fd, None
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def backup_lock_holder_pid() -> int | None:
    try:
        raw = _LOCK_PATH.read_text(encoding="utf-8", errors="replace").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None
