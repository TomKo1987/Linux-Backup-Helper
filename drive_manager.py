from __future__ import annotations
from pathlib import Path
import os, pwd, shlex, subprocess, threading, time
from PyQt6.QtWidgets import QCheckBox, QMessageBox

from logging_config import setup_logger
logger = setup_logger(__name__)

__all__ = ["DriveManager"]

_USER = pwd.getpwuid(os.getuid()).pw_name

MOUNT_CHECK_DELAY    = 0.5
MOUNT_TIMEOUT        = 45
UNMOUNT_TIMEOUT      = 30
PROCESS_KILL_TIMEOUT = 10

_ALLOWED_COMMANDS = {"mount", "umount", "udisksctl"}

_DANGEROUS_SEQUENCES: tuple[str, ...] = ("&&", "||", "$(", ";;", ";", "|", "`", ">", "<", "&", "\n", "\r", "\x00")

_SUSPICIOUS_ARGS          = {"--exec", "--command", "-c", "--eval"}
_SUSPICIOUS_PATH_PATTERNS = {"..", ";", "|", "&", "$(", "`", "<", ">", "\x00", "\n", "\r"}


def _mount_paths(name: str) -> tuple[str, ...]:
    return (
        f"/run/media/{_USER}/{name}",
        f"/media/{_USER}/{name}",
        f"/mnt/{name}",
        name,
    )


class DriveManager:

    def __init__(self) -> None:
        self.drives_to_unmount: list[dict] = []
        self._lock = threading.Lock()

    @staticmethod
    def get_mount_output() -> str:
        try:
            return subprocess.check_output(["mount"], text=True)
        except (subprocess.SubprocessError, OSError, ValueError):
            return ""

    @staticmethod
    def is_drive_mounted(opt: dict, mount_output: str | None = None) -> bool:
        try:
            output = mount_output if mount_output is not None else DriveManager.get_mount_output()
            name = opt.get("drive_name", "").strip()
            if not name:
                return False
            paths = _mount_paths(name)
            return any(
                f"{path} " in output or f"{path}\n" in output or output.endswith(path)
                for path in paths
            )
        except Exception as exc:
            logger.exception("is_drive_mounted: error checking '%s': %s", opt.get("drive_name"), exc)
            return False

    @staticmethod
    def _validate_command(cmd: str) -> tuple[bool, str]:
        if not cmd or not cmd.strip():
            return False, "Empty command."
        if any(seq in cmd for seq in _DANGEROUS_SEQUENCES):
            return False, "Command contains dangerous characters or sequences."
        try:
            tokens = shlex.split(cmd)
        except ValueError as exc:
            return False, f"Invalid shell syntax: {exc}"
        if not tokens:
            return False, "No tokens after parsing."
        base = os.path.basename(tokens[0])
        if base == "sudo" and len(tokens) > 1:
            base = os.path.basename(tokens[1])
        if base not in _ALLOWED_COMMANDS:
            return False, f"Command '{base}' is not in the allowlist."
        if _SUSPICIOUS_ARGS.intersection(tokens):
            return False, "Command contains suspicious arguments."
        return True, ""

    @staticmethod
    def _sanitise_path(path: str | Path) -> str | None:
        try:
            raw = str(path).strip()
            if not raw:
                return None
            if any(pat in raw for pat in _SUSPICIOUS_PATH_PATTERNS):
                logger.warning("Suspicious path rejected: %s", raw)
                return None
            resolved = Path(raw).expanduser()
            try:
                return str(resolved.resolve())
            except (OSError, RuntimeError):
                return str(resolved)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning("Could not sanitise path '%s': %s", path, exc)
            return None

    def check_path_requires_mounting(self, path: str | Path, mount_output: str | None = None) -> dict | None:
        if not path:
            return None
        path_str = self._sanitise_path(path)
        if path_str is None:
            return None
        try:
            from options import Options
            opts = getattr(Options, "mount_options", None) or []
        except (ImportError, AttributeError):
            return None
        for opt in opts:
            if not isinstance(opt, dict):
                continue
            name = opt.get("drive_name", "").strip()
            if not name:
                continue
            if any(mp in path_str for mp in _mount_paths(name)[:-1]) and not self.is_drive_mounted(opt, mount_output):
                return opt
        return None

    def check_drives_to_mount(self, paths: list) -> list[dict]:
        drives: list[dict] = []
        seen:   set[str]   = set()
        mount_output = self.get_mount_output()
        for item in filter(None, paths):
            for p in (item if isinstance(item, list) else [item]):
                drive = self.check_path_requires_mounting(p, mount_output)
                if drive:
                    key = drive.get("drive_name", "")
                    if key and key not in seen:
                        seen.add(key)
                        drives.append(drive)
        return drives

    def mount_drive(self, drive: dict, parent=None, remember_unmount: bool = True) -> bool:
        if not isinstance(drive, dict):
            logger.error("mount_drive: drive must be a dict.")
            return False

        name = drive.get("drive_name", "").strip()
        cmd  = drive.get("mount_command", "").strip()

        if not name or not cmd:
            self._alert("Mount Error", "Missing drive name or mount command.", QMessageBox.Icon.Warning, parent)
            return False

        ok, err = self._validate_command(cmd)
        if not ok:
            self._alert("Mount Error", f"Invalid mount command for '{name}': {err}", QMessageBox.Icon.Warning, parent)
            return False

        if self.is_drive_mounted(drive, self.get_mount_output()):
            logger.info("mount_drive: '%s' already mounted.", name)
            return True

        logger.info("mount_drive: mounting '%s'  cmd=%s", name, cmd)
        try:
            result = subprocess.run(
                shlex.split(cmd), shell=False, capture_output=True, text=True,
                check=False, timeout=MOUNT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            self._kill_process(cmd)
            msg = f"Mount timed out after {MOUNT_TIMEOUT}s for '{name}'."
            logger.error("mount_drive: %s", msg)
            self._alert("Mount Error", msg, QMessageBox.Icon.Warning, parent)
            return False
        except (OSError, ValueError, RuntimeError) as exc:
            msg = f"Unexpected error mounting '{name}': {exc}"
            logger.exception("mount_drive: %s", msg)
            self._alert("Mount Error", msg, QMessageBox.Icon.Critical, parent)
            return False

        if result.returncode != 0:
            stderr = result.stderr[:300].strip()
            msg = f"Mount failed for '{name}' (exit {result.returncode})"
            if stderr:
                msg += f"\n{stderr}"
            logger.error("mount_drive: %s", msg)
            self._alert("Mount Error", msg, QMessageBox.Icon.Warning, parent)
            return False

        time.sleep(MOUNT_CHECK_DELAY)

        if not self.is_drive_mounted(drive):
            msg = f"'{name}' mount command succeeded but drive not detected as mounted."
            logger.warning("mount_drive: %s", msg)
            self._alert("Mount Error", msg, QMessageBox.Icon.Warning, parent)
            return False

        if remember_unmount and drive.get("unmount_command"):
            with self._lock:
                if drive not in self.drives_to_unmount:
                    self.drives_to_unmount.append(drive)

        logger.info("mount_drive: '%s' mounted successfully.", name)
        return True

    def unmount_drive(self, drive: dict, parent=None) -> bool:
        name = drive.get("drive_name", "")
        cmd  = drive.get("unmount_command", "").strip()
        if not cmd:
            return False
        ok, err = self._validate_command(cmd)
        if not ok:
            logger.warning("unmount_drive: invalid command for '%s': %s", name, err)
            return False
        logger.info("unmount_drive: unmounting '%s'  cmd=%s", name, cmd)
        try:
            result = subprocess.run(
                shlex.split(cmd), shell=False, capture_output=True, text=True,
                check=False, timeout=UNMOUNT_TIMEOUT,
            )
            if result.returncode != 0:
                logger.warning("unmount_drive: '%s' returned exit code %d.", name, result.returncode)
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            msg = f"Unmount timed out for '{name}'."
            logger.error("unmount_drive: %s", msg)
            self._alert("Unmount Error", msg, QMessageBox.Icon.Warning, parent)
            return False
        except (OSError, ValueError, RuntimeError) as exc:
            msg = f"Error unmounting '{name}': {exc}"
            logger.exception("unmount_drive: %s", msg)
            self._alert("Unmount Error", msg, QMessageBox.Icon.Critical, parent)
            return False

    def mount_required_drives(self, drives: list[dict], parent=None) -> bool:
        all_ok = True
        for drive in drives:
            name = drive.get("drive_name", "Unknown")
            box = QMessageBox(
                QMessageBox.Icon.Question,
                "Drive Mount Required",
                f"Drive '{name}' needs to be mounted.\nMount now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                parent,
            )
            cb = QCheckBox("Unmount drive when finished")
            cb.setChecked(True)
            box.setCheckBox(cb)

            if box.exec() != QMessageBox.StandardButton.Yes:
                logger.info("mount_required_drives: user declined to mount '%s'.", name)
                self._alert("Operation Cancelled", f"Cannot continue without mounting '{name}'.",
                            QMessageBox.Icon.Information, parent)
                all_ok = False
                continue

            if not self.mount_drive(drive, parent, remember_unmount=cb.isChecked()):
                all_ok = False

        return all_ok

    def unmount_drives(self, parent=None) -> bool:
        with self._lock:
            queued = self.drives_to_unmount.copy()

        failed: list[dict] = []
        for d in queued:
            if self.unmount_drive(d, parent):
                with self._lock:
                    try:
                        self.drives_to_unmount.remove(d)
                    except ValueError:
                        pass
            else:
                failed.append(d)

        if not failed:
            logger.info("unmount_drives: all drives unmounted successfully.")
        else:
            names = [f.get("drive_name", "?") for f in failed]
            logger.warning("unmount_drives: failed to unmount: %s", names)

        return not failed

    def mount_drives_at_launch(self) -> None:
        from options import Options
        if not (getattr(Options, "mount_options", None) and
                getattr(Options, "run_mount_command_on_launch", False)):
            return
        output = self.get_mount_output()
        for opt in Options.mount_options:
            if not self.is_drive_mounted(opt, output):
                logger.info("mount_drives_at_launch: mounting '%s'.", opt.get("drive_name"))
                self.mount_drive(opt, parent=None, remember_unmount=False)

    @staticmethod
    def _alert(title: str, text: str, icon: QMessageBox.Icon, parent) -> None:
        QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, parent).exec()

    @staticmethod
    def _kill_process(cmd: str) -> None:
        try:
            stripped = cmd.strip()
            if not stripped:
                return
            tokens = stripped.split()
            if not tokens:
                return
            pattern_tokens = [t for t in tokens if t != "sudo"]
            if not pattern_tokens:
                return
            pattern = " ".join(pattern_tokens[:2])[:80]
            subprocess.run(
                ["pkill", "-f", pattern],
                timeout=PROCESS_KILL_TIMEOUT,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("Could not kill hung mount process: %s", exc)
