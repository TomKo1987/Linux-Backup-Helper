from typing import Optional
import os, shlex, subprocess, re, time

from state import logger, _USER

_DRIVE_NAME_RE = re.compile(r"^[\w\- .()@:]+$")
_DANGER_SEQS   = ("&&", "||", "$(", ";", "|", "`", ">", "<", "&", "\n", "\r", "\x00")
_ALLOWED_CMDS  = frozenset({"mount", "umount", "udisksctl", "kdeconnect-cli", "sshfs", "fusermount3"})


def _validate_cmd(cmd: str) -> tuple[bool, str]:
    if not cmd.strip():
        return False, "Empty command"
    if any(s in cmd for s in _DANGER_SEQS):
        return False, "Dangerous characters in command"
    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        return False, str(e)
    if not tokens:
        return False, "No command tokens found"

    base = os.path.basename(tokens[0])
    if base == "sudo" and len(tokens) > 1:
        base = os.path.basename(tokens[1])
    if base not in _ALLOWED_CMDS:
        return False, f"'{base}' is not an allowed command"

    expanded = [os.path.expanduser(t) for t in tokens]
    for tok in expanded:
        norm = os.path.normpath(tok)
        if norm.startswith("/../") or norm == "/..":
            return False, f"Path traversal detected in token: {tok!r}"

    return True, ""


def _valid_drive_name(name: str) -> bool:
    return bool(name and isinstance(name, str) and _DRIVE_NAME_RE.match(name) and len(name) <= 128)


def _mount_paths(name: str) -> tuple[str, ...]:
    if not _valid_drive_name(name):
        return ()
    return f"/run/media/{_USER}/{name}", f"/media/{_USER}/{name}", f"/mnt/{name}"


def _expand_home_in_cmd(cmd: str) -> list[str]:
    return [os.path.expanduser(t) for t in shlex.split(cmd)]


def _execute_drive_op(drive: dict, key: str, timeout: int) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    cmd  = drive.get(key, "").strip()

    if not cmd:
        return False, f"No {key.replace('_', ' ')} configured for '{name}'."

    ok, reason = _validate_cmd(cmd)
    if not ok:
        return False, f"Drive '{name}': {reason}"

    try:
        result = subprocess.run(_expand_home_in_cmd(cmd), capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip() or f"exit code {result.returncode}"
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


def get_mount_output() -> str:
    try:
        return subprocess.check_output(["mount"], text=True, timeout=5)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
            FileNotFoundError, OSError) as e:
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
    else:
        if re.search(rf"{re.escape(mount_path)}(?:\s|$)", mount_out):
            return True

    return False


def mount_drive(drive: dict) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    success, error_msg = _execute_drive_op(drive, "mount_command", 15)

    if success:
        logger.info("Mounted '%s' — waiting 1 s for filesystem to settle", name)
        time.sleep(1)
        return True, ""

    msg = f"Mount failed for '{name}': {error_msg}"
    logger.error("mount_drive: %s", msg)
    return False, msg


def unmount_drive(drive: dict) -> tuple[bool, str]:
    name = drive.get("drive_name", "?")
    success, error_msg = _execute_drive_op(drive, "unmount_command", 30)

    if success:
        logger.info("Unmounted '%s'", name)
        return True, ""

    msg = f"Unmount failed for '{name}': {error_msg}"
    logger.error("unmount_drive: %s", msg)
    return False, msg


def is_smb(path: str) -> bool:
    return path.startswith(("smb://", "cifs://"))


def check_drives_to_mount(paths: list[str]) -> list[dict]:
    from state import S
    needed    = []
    mount_out = get_mount_output()

    def _is_on_mount(mount_point: str, file_path: str) -> bool:
        m = os.path.normpath(mount_point)
        f = os.path.normpath(file_path)
        return f == m or f.startswith(m + os.sep)

    def _is_on_smb_mount(_mount_path: str, file_path: str) -> bool:
        base = _mount_path if _mount_path.endswith("/") else _mount_path + "/"
        return file_path == _mount_path or file_path.startswith(base)

    for opt in S.mount_options:
        if is_mounted(opt, mount_out):
            continue

        name       = opt.get("drive_name", "")
        mount_path = opt.get("mount_path", "").strip()

        if mount_path and is_smb(mount_path):
            if any(_is_on_smb_mount(mount_path, p) for p in paths):
                needed.append(opt)
        elif mount_path:
            if any(_is_on_mount(mount_path, p) for p in paths):
                needed.append(opt)
        elif _valid_drive_name(name):
            candidates = _mount_paths(name)
            if any(any(_is_on_mount(c, p) for c in candidates) for p in paths):
                needed.append(opt)

    return needed


def has_managed_mount_path(opt: dict) -> bool:
    mount_path = opt.get("mount_path", "").strip()
    if not mount_path:
        return False
    name = opt.get("drive_name", "")
    return mount_path not in _mount_paths(name)


def mount_required_drives(drives: list[dict], parent=None) -> tuple[bool, list[dict]]:
    from PyQt6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QFrame, QLabel, QPushButton, QVBoxLayout

    if not drives:
        return True, []

    drives_to_unmount = []

    for opt in drives:
        drive_name  = opt.get("drive_name", "?")
        has_unmount = bool(opt.get("unmount_command"))
        is_managed  = has_managed_mount_path(opt)

        dlg = QDialog(parent)
        dlg.setWindowTitle("Drive Required")
        dlg.setMinimumWidth(420)

        layout = QVBoxLayout(dlg)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title_lbl = QLabel(f"<b>'{drive_name}'</b> is required but not mounted.")
        title_lbl.setWordWrap(True)
        layout.addWidget(title_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        if is_managed:
            info_lbl = QLabel("This drive uses an external mount path and cannot be detected automatically. Run mount command now?")
        else:
            info_lbl = QLabel("Run mount command now?")
        info_lbl.setWordWrap(True)
        layout.addWidget(info_lbl)

        cb_no_unmount = None
        if has_unmount:
            cb_no_unmount = QCheckBox("Don't run unmount command after completion")
            layout.addWidget(cb_no_unmount)

        layout.addSpacing(4)

        btn_box = QDialogButtonBox()
        yes_btn = QPushButton("Run Mount Command")
        no_btn  = QPushButton("Skip" if is_managed else "Cancel")
        btn_box.addButton(yes_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        btn_box.addButton(no_btn,  QDialogButtonBox.ButtonRole.RejectRole)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        layout.addWidget(btn_box)

        yes_btn.setFocus()
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            if is_managed:
                logger.info("Skipping mount for managed drive '%s'", drive_name)
                continue
            else:
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.warning(parent, "Operation Cancelled", f"The required drive '{drive_name}' was not mounted.\n\n"
                                                                   "The operation has been cancelled.")
                return False, []

        success, error_msg = mount_drive(opt)
        if not success:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(parent, "Mount Failed", f"Could not mount '{drive_name}':\n\n{error_msg}\n\n"
                                                         "The operation has been cancelled.")
            return False, []
        if has_unmount and not (cb_no_unmount and cb_no_unmount.isChecked()):
            drives_to_unmount.append(opt)

    return True, drives_to_unmount