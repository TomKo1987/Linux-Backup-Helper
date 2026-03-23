from typing import Optional
import os, shlex, subprocess, re, time

from PyQt6.QtWidgets import QMessageBox

from state import logger, _USER

_DRIVE_NAME_RE = re.compile(r"^[\w\- .()@:]+$")

_SHELL_INJECTION_SEQS = ("&&", "||", "$(", "${", ";", "|", "`", ">", "<", "\n", "\r", "\x00")

_ALLOWED_MOUNT_CMDS = frozenset({
    "mount", "umount", "mount.cifs", "udisksctl",
    "kdeconnect-cli", "sshfs", "fusermount3", "fusermount",
})

_session_managed_mounts: list[dict] = []


def get_session_managed_mounts() -> list[dict]:
    return list(_session_managed_mounts)


def _track_session_mount(drive: dict) -> None:
    if drive not in _session_managed_mounts:
        _session_managed_mounts.append(drive)


def _untrack_session_mount(drive: dict) -> None:
    try:
        _session_managed_mounts.remove(drive)
    except ValueError:
        pass


def _validate_cmd(cmd: str) -> tuple[bool, str, list[str]]:
    if not cmd.strip():
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
        if ".." in token.replace("\\", "/").split("/"):
            return False, f"Path traversal detected in token: {token!r}", []

    return True, "", expanded


def _valid_drive_name(name: str) -> bool:
    return bool(name and isinstance(name, str) and _DRIVE_NAME_RE.match(name) and len(name) <= 128)


def _mount_paths(name: str) -> tuple[str, ...]:
    if not _valid_drive_name(name):
        return ()
    return f"/run/media/{_USER}/{name}", f"/media/{_USER}/{name}", f"/mnt/{name}"


def _execute_drive_op(drive: dict, cmd_key: str, timeout: int) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    cmd  = drive.get(cmd_key, "").strip()
    if not cmd:
        return False, f"No {cmd_key.replace('_', ' ')} configured for '{name}'."

    ok, reason, tokens = _validate_cmd(cmd)
    if not ok:
        return False, f"Drive '{name}': {reason}"

    try:
        result = subprocess.run(tokens, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip() or f"exit code {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


def get_mount_output() -> str:
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), raw)
    except OSError as e:
        logger.warning("get_mount_output: %s", e)
        return ""


def is_mounted(opt: dict, mount_out: Optional[str] = None) -> bool:
    name = opt.get("drive_name", "")
    if not _valid_drive_name(name):
        return False
    if mount_out is None:
        mount_out = get_mount_output()

    for p in _mount_paths(name):
        if re.search(rf"{re.escape(p)}(?:\s|$)", mount_out):
            return True

    mount_path = opt.get("mount_path", "").strip()
    if not mount_path:
        return False

    if is_smb(mount_path):
        without_schema = re.sub(r"^(smb|cifs)://", "", mount_path).rstrip("/")
        parts = without_schema.split("/", 1)
        if len(parts) == 2:
            host, share = parts
            pattern = rf"(?://{re.escape(host)}/{re.escape(share)}|{re.escape(host)}:/{re.escape(share)})(?:\s|$|/)"
            if re.search(pattern, mount_out, re.IGNORECASE):
                return True
    elif re.search(rf"{re.escape(mount_path)}(?:\s|$)", mount_out):
        return True

    return False


def mount_drive(drive: dict) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    success, error_msg = _execute_drive_op(drive, "mount_command", 15)

    if success:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if is_mounted(drive, get_mount_output()):
                break
            time.sleep(0.5)
        else:
            logger.warning("mount_drive: '%s' still not visible after 1.5 s", name)
        if has_managed_mount_path(drive):
            _track_session_mount(drive)
        logger.info("Mounted '%s'", name)
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


def is_smb(path: str) -> bool:
    return path.startswith(("smb://", "cifs://"))


def check_drives_to_mount(paths: list[str]) -> list[dict]:
    from state import S
    mount_out = get_mount_output()

    def _on_local(mount_point: str, file_path: str) -> bool:
        m = os.path.normpath(mount_point)
        f = os.path.normpath(file_path)
        return f == m or f.startswith(m + os.sep)

    def _on_smb(mount_point: str, file_path: str) -> bool:
        mp = mount_point.rstrip("/")
        fp = file_path.rstrip("/")
        return fp == mp or fp.startswith(mp + "/")

    needed = []
    for opt in S.mount_options:
        if is_mounted(opt, mount_out):
            continue
        name       = opt.get("drive_name", "")
        mount_path = opt.get("mount_path", "").strip()

        if mount_path and is_smb(mount_path):
            if any(_on_smb(mount_path, p) for p in paths):
                needed.append(opt)
        elif mount_path:
            if any(_on_local(mount_path, p) for p in paths):
                needed.append(opt)
        elif _valid_drive_name(name):
            candidates = _mount_paths(name)
            if any(_on_local(c, p) for c in candidates for p in paths):
                needed.append(opt)

    return needed


def has_managed_mount_path(opt: dict) -> bool:
    mount_path = opt.get("mount_path", "").strip()
    return bool(mount_path) and mount_path not in _mount_paths(opt.get("drive_name", ""))


def mount_required_drives(drives: list[dict], parent=None) -> bool:
    for opt in drives:
        drive_name = opt.get("drive_name", "?")
        is_managed = has_managed_mount_path(opt)

        if is_managed:
            msg = (f"'{drive_name}' is required but cannot be detected automatically "
                   f"(external mount path).\n\nRun mount command now?")
        else:
            msg = f"'{drive_name}' is required but not mounted.\n\nRun mount command now?"

        answer = QMessageBox.question(
            parent, "Drive Required", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:
            if is_managed:
                logger.info("Skipping mount for managed drive '%s'", drive_name)
                continue
            QMessageBox.warning(
                parent, "Operation Cancelled",
                f"The required drive '{drive_name}' was not mounted.\n\nThe operation has been cancelled.",
            )
            return False

        success, error_msg = mount_drive(opt)
        if not success:
            QMessageBox.critical(
                parent, "Mount Failed",
                f"Could not mount '{drive_name}':\n\n{error_msg}\n\nThe operation has been cancelled.",
            )
            return False

    return True
