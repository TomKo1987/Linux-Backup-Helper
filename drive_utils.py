import os
import re
import shlex
import subprocess
import threading
import time
from functools import lru_cache
from typing import Optional

from PyQt6.QtWidgets import QMessageBox

from state import S, logger, _USER


_DRIVE_NAME_RE = re.compile(r"^[\w\- .()@:]+$")


_SHELL_INJECTION_SEQS = ("&&", "$(", "${", ";", "|", "`", ">", "<", "\n", "\r", "\x00")


_ALLOWED_MOUNT_CMDS = frozenset({
    "mount", "umount", "mount.cifs", "udisksctl",
    "kdeconnect-cli", "sshfs", "fusermount3", "fusermount",
})


_session_managed_mounts: list[dict] = []
_session_mounts_lock = threading.Lock()
_OCTAL_ESCAPE_RE = re.compile(r"\\(\d{3})")
_mounts_cache: tuple[float, list] = (0.0, [])
_mounts_cache_lock = threading.Lock()
_MOUNT_TIMEOUT_S   = 15
_UNMOUNT_TIMEOUT_S = 30


def get_mounts(max_age: float = 0.5) -> list[tuple[str, str]]:
    global _mounts_cache
    now = time.monotonic()
    with _mounts_cache_lock:
        if now - _mounts_cache[0] < max_age:
            return _mounts_cache[1]
    mounts: list[tuple[str, str]] = []
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    mounts.append((_decode_octal(parts[0]), _decode_octal(parts[1])))
    except (OSError, FileNotFoundError) as e:
        logger.warning("get_mounts: /proc/mounts not available: %s", e)
        with _mounts_cache_lock:
            return _mounts_cache[1]
    with _mounts_cache_lock:
        if now >= _mounts_cache[0]:
            _mounts_cache = (now, mounts)
        return _mounts_cache[1]


def _decode_octal(s: str) -> str:
    return _OCTAL_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), s)


def get_session_managed_mounts() -> list[dict]:
    with _session_mounts_lock:
        return _session_managed_mounts.copy()


def _track_session_mount(drive: dict) -> None:
    with _session_mounts_lock:
        if drive not in _session_managed_mounts:
            _session_managed_mounts.append(drive)


def _untrack_session_mount(drive: dict) -> None:
    with _session_mounts_lock:
        if drive in _session_managed_mounts:
            _session_managed_mounts.remove(drive)


def _validate_cmd(cmd: str) -> tuple[bool, str, list[str]]:
    if not cmd:
        return False, "Empty command", []
    if any(seq in cmd for seq in _SHELL_INJECTION_SEQS):
        return False, "Dangerous characters in command", []
    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        return False, str(e), []
    if not tokens:
        return False, "No command tokens found", []
    expanded = [os.path.expanduser(tok) for tok in tokens]
    base = os.path.basename(expanded[0])
    if base == "sudo":
        for tok in expanded[1:]:
            if tok in ("-u", "-H", "--user"):
                return False, "sudo user-switching flags are not permitted", []
            if not tok.startswith("-"):
                base = os.path.basename(tok)
                break
        else:
            return False, "sudo without a command", []
    if base not in _ALLOWED_MOUNT_CMDS:
        return False, f"'{base}' is not an allowed command", []
    for tok in expanded:
        if ".." in tok.split("/"):
            return False, f"Path traversal detected in token: {tok!r}", []
    return True, "", expanded


def _valid_drive_name(name: str) -> bool: return bool(name and isinstance(name, str) and _DRIVE_NAME_RE.match(name) and len(name) <= 128)


@lru_cache(maxsize=64)
def _mount_paths(name: str) -> tuple[str, ...]:
    if not _valid_drive_name(name):
        return ()
    return f"/run/media/{_USER}/{name}", f"/media/{_USER}/{name}", f"/mnt/{name}"


def _execute_drive_op(drive: dict, cmd_key: str, timeout: int) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    cmd = drive.get(cmd_key, "").strip()
    if not cmd:
        return False, f"No {cmd_key.replace('_', ' ')} configured for '{name}'."
    ok, reason, tokens = _validate_cmd(cmd)
    if not ok:
        return False, f"Drive '{name}': {reason}"
    try:
        result = subprocess.run(tokens, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


def is_mounted(opt: dict, mounts: Optional[list[tuple[str, str]]] = None) -> bool:
    name = opt.get("drive_name", "")
    mount_path = opt.get("mount_path", "").strip()

    if not _valid_drive_name(name) and not mount_path:
        return False

    if mounts is None:
        mounts = get_mounts()

    expected_paths = set(_mount_paths(name)) if _valid_drive_name(name) else set()
    smb_prefixes: tuple = ()

    if mount_path and is_smb(mount_path):
        without_schema = re.sub(r"^(smb|cifs)://", "", mount_path).rstrip("/")
        parts = without_schema.split("/", 2)
        if len(parts) >= 2:
            host = parts[0]
            share = parts[1]
            full_device = f"//{host}/{share}"
            if len(parts) == 3 and parts[2]:
                full_device = f"{full_device}/{parts[2]}"
            smb_prefixes = (full_device.lower(),)

    for dev, mnt in (mounts or []):
        if mnt in expected_paths or (mount_path and mnt == mount_path):
            return True
        if smb_prefixes:
            dev_clean = dev.lower().rstrip("/")
            if any(dev_clean == p.rstrip("/") for p in smb_prefixes):
                return True

    return False


def mount_drive(drive: dict) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    success, error_msg = _execute_drive_op(drive, "mount_command", _MOUNT_TIMEOUT_S)
    if success:
        mounted_confirmed = False
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if is_mounted(drive, mounts=get_mounts(max_age=0.0)):
                mounted_confirmed = True
                break
            time.sleep(0.1)
        if not mounted_confirmed:
            mounted_confirmed = is_mounted(drive)
        is_managed = has_managed_mount_path(drive)
        if not mounted_confirmed:
            if not is_managed:
                msg = f"Mount command succeeded but '{name}' not visible in /proc/mounts after 1 s"
                logger.error("mount_drive: %s", msg)
                return False, msg
            _track_session_mount(drive)
            logger.warning("mount_drive: '%s' not confirmed in /proc/mounts (managed mount path)", name)
        else:
            if is_managed:
                _track_session_mount(drive)
        logger.info("Mounted '%s' (confirmed=%s)", name, mounted_confirmed)
        return True, ""
    msg = f"Mount failed for '{name}': {error_msg}"
    logger.error("mount_drive: %s", msg)
    return False, msg


def unmount_drive(drive: dict) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    success, error_msg = _execute_drive_op(drive, "unmount_command", _UNMOUNT_TIMEOUT_S)
    if success:
        _untrack_session_mount(drive)
        logger.info("Unmounted '%s'", name)
        return True, ""
    msg = f"Unmount failed for '{name}': {error_msg}"
    logger.error("unmount_drive: %s", msg)
    return False, msg


def is_smb(path: str) -> bool: return path.startswith(("smb://", "cifs://"))


def _is_subpath(parent: str, child: str) -> bool: return (child.rstrip("/") + "/").startswith(parent.rstrip("/") + "/")


def check_drives_to_mount(paths: list[str]) -> list[dict]:
    mounts = get_mounts()
    needed = []
    for opt in S.mount_options:
        if is_mounted(opt, mounts):
            continue
        name       = opt.get("drive_name", "")
        mount_path = opt.get("mount_path", "").strip()

        if not mount_path and not _valid_drive_name(name):
            logger.warning(
                "check_drives_to_mount: Drive %r does not have a valid name or a "
                "mount_path — will be skipped. Please correct this in the settings.", name
            )
            continue

        candidates = [mount_path] if mount_path else (_mount_paths(name) if _valid_drive_name(name) else ())
        for candidate in candidates:
            if not candidate:
                continue
            if any(_is_subpath(candidate, p) for p in paths):
                needed.append(opt)
                break
    return needed


def has_managed_mount_path(opt: dict) -> bool:
    mount_path = opt.get("mount_path", "").strip()
    return bool(mount_path and mount_path not in _mount_paths(opt.get("drive_name", "")))


def mount_required_drives(drives: list[dict], parent=None) -> bool:
    for opt in drives:
        drive_name = opt.get("drive_name", "?")
        is_managed = has_managed_mount_path(opt)

        if is_managed:
            msg = (f"'{drive_name}' is required, but cannot be automatically detected due to an external mount path."
                   f"\n\nRun mount command now?")
        else:
            msg = f"'{drive_name}' is required but not mounted.\n\nRun mount command now?"

        answer = QMessageBox.question(parent, "Drive Required", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

        if answer != QMessageBox.StandardButton.Yes:
            if is_managed:
                logger.info("Skipping mount for managed drive '%s'; continuing operation.", drive_name)
                QMessageBox.information(
                    parent, "Skipping Drive", f"'{drive_name}' was not mounted.\n\n"
                                              f"Since this drive uses an external mount path, the operation will continue.\n"
                                              f"Files on this drive may be unavailable.")
                continue
            QMessageBox.warning(parent, "Operation Cancelled",
                                f"The required drive '{drive_name}' was not mounted.\n\nThe operation has been cancelled.")
            return False

        success, error_msg = mount_drive(opt)
        if not success:
            QMessageBox.critical(parent, "Mount Failed",
                                 f"Could not mount '{drive_name}':\n\n{error_msg}\n\nThe operation has been cancelled.")
            return False

    return True
