import os
import re
import shlex
import subprocess
import time
from functools import lru_cache
from typing import Optional

from PyQt6.QtWidgets import QMessageBox

from state import S, logger, _USER

_DRIVE_NAME_RE = re.compile(r"^[\w\- .()@:]+$")

_SHELL_INJECTION_SEQS = ("&&", "||", "$(", "${", ";", "|", "`", ">", "<", "\n")

_ALLOWED_MOUNT_CMDS = frozenset({
    "mount", "umount", "mount.cifs", "udisksctl",
    "kdeconnect-cli", "sshfs", "fusermount3", "fusermount",
})

_session_managed_mounts: list[dict] = []


def get_session_managed_mounts() -> list[dict]: return _session_managed_mounts.copy()


def _track_session_mount(drive: dict) -> None:
    if drive not in _session_managed_mounts:
        _session_managed_mounts.append(drive)


def _untrack_session_mount(drive: dict) -> None:
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
    base = os.path.basename(tokens[0])
    if base == "sudo" and len(tokens) > 1:
        base = os.path.basename(tokens[1])
    if base not in _ALLOWED_MOUNT_CMDS:
        return False, f"'{base}' is not an allowed command", []
    expanded = [os.path.expanduser(t) for t in tokens]
    for token in expanded:
        if ".." in token.split("/"):
            return False, f"Path traversal detected in token: {token!r}", []
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


def get_mounts() -> list[tuple[str, str]]:
    mounts = []
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    dev = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), parts[0])
                    mnt = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), parts[1])
                    mounts.append((dev, mnt))
    except OSError as e:
        logger.warning("get_mounts: %s", e)
    return mounts


def is_mounted(opt: dict, mounts: Optional[list[tuple[str, str]]] = None) -> bool:
    name = opt.get("drive_name", "")
    if not _valid_drive_name(name):
        return False

    if mounts is None:
        mounts = get_mounts()

    expected_paths = set(_mount_paths(name))
    mount_path = opt.get("mount_path", "").strip()
    smb_prefixes: tuple = ()

    if mount_path and is_smb(mount_path):
        without_schema = re.sub(r"^(smb|cifs)://", "", mount_path).rstrip("/")
        parts = without_schema.split("/", 1)
        if len(parts) == 2:
            host, share = parts
            smb_prefixes = (f"//{host}/{share}".lower(), f"{host}:/{share}".lower())

    for dev, mnt in mounts:
        if mnt in expected_paths or (mount_path and mnt == mount_path):
            return True
        if smb_prefixes:
            dev_clean = dev.lower().rstrip("/")
            if any(dev_clean == p.rstrip("/") for p in smb_prefixes):
                return True

    return False


def mount_drive(drive: dict) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    success, error_msg = _execute_drive_op(drive, "mount_command", 15)
    if success:
        time.sleep(0.3)
        mounted_confirmed = is_mounted(drive)
        if not mounted_confirmed:
            logger.warning("mount_drive: '%s' mount command succeeded but drive not visible after 0.3 s", name)
        if has_managed_mount_path(drive):
            _track_session_mount(drive)
        logger.info("Mounted '%s' (confirmed=%s)", name, mounted_confirmed)
        return True, ""
    msg = f"Mount failed for '{name}': {error_msg}"
    logger.error("mount_drive: %s", msg)
    return False, msg


def unmount_drive(drive: dict) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    success, error_msg = _execute_drive_op(drive, "unmount_command", 30)
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
        name = opt.get("drive_name", "")
        mount_path = opt.get("mount_path", "").strip()
        candidates = [mount_path] if mount_path else _mount_paths(name) if _valid_drive_name(name) else []
        for candidate in candidates:
            if not candidate: continue
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
            msg = (f"'{drive_name}' is required but cannot be detected automatically "
                   f"(external mount path).\n\nRun mount command now?")
        else:
            msg = f"'{drive_name}' is required but not mounted.\n\nRun mount command now?"

        answer = QMessageBox.question(parent, "Drive Required", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

        if answer != QMessageBox.StandardButton.Yes:
            if is_managed:
                logger.info("Skipping mount for managed drive '%s'", drive_name)
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
