import atexit
import errno
import fcntl
import os
import tempfile
from pathlib import Path

from state import _CONFIG_DIR, _USER, logger


def _runtime_dir() -> Path:
    for candidate in (
        os.environ.get("XDG_RUNTIME_DIR"),
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


def acquire_backup_lock() -> bool:
    global _lock_fd
    if _lock_fd is not None:
        return True

    try:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
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
    atexit.register(release_backup_lock)
    return True


def release_backup_lock() -> None:
    global _lock_fd
    if _lock_fd is None:
        return
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        st_fd = os.fstat(_lock_fd)
        st_path = os.stat(_LOCK_PATH)
        if (st_fd.st_dev, st_fd.st_ino) == (st_path.st_dev, st_path.st_ino):
            _LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        os.close(_lock_fd)
    except OSError:
        pass
    _lock_fd = None


def backup_lock_holder_pid() -> int | None:
    try:
        raw = _LOCK_PATH.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None
