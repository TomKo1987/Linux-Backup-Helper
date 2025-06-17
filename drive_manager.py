from pathlib import Path
from PyQt6.QtWidgets import QMessageBox, QCheckBox
import subprocess, pwd, os, logging.handlers, shlex, threading, time

user = pwd.getpwuid(os.getuid()).pw_name

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)


class DriveManager:
    def __init__(self):
        self.drives_to_unmount = []
        self._lock = threading.Lock()

    @staticmethod
    def is_drive_mounted(opt):
        try:
            output = subprocess.check_output(['mount'], text=True)
            name = opt.get('drive_name', '')
            paths = [f"/run/media/{user}/{name}", f"/media/{user}/{name}", f"/mnt/{name}", name]
            return any(p in output for p in paths)
        except Exception as e:
            logger.exception(f"[is_drive_mounted] Error checking mount status for drive '{opt.get('drive_name', '')}': {e}")
            return False

    def check_path_requires_mounting(self, path):
        if not path or not isinstance(path, (str, Path)):
            return None

        try:
            if isinstance(path, str):
                if not path.strip():
                    return None

                suspicious_patterns = ['..', ';', '|', '&', '$(', '`', '<', '>', '\x00', '\n', '\r']
                if any(pattern in path for pattern in suspicious_patterns):
                    logger.warning(f"Suspicious path detected: {path}")
                    return None

                try:
                    path_obj = Path(path).expanduser()
                    path_str = str(path_obj.resolve())
                except (OSError, RuntimeError) as e:
                    logger.debug(f"Could not resolve path {path}, using as-is: {e}")
                    path_str = str(Path(path).expanduser())
            else:
                path_str = str(path.resolve())

        except (OSError, ValueError, RuntimeError) as e:
            logger.warning(f"Error processing path '{path}': {e}")
            return None

        try:
            from options import Options
            if not hasattr(Options, 'mount_options') or not Options.mount_options:
                return None
        except (ImportError, AttributeError) as e:
            logger.warning(f"Could not access Options.mount_options: {e}")
            return None

        for opt in Options.mount_options:
            if not isinstance(opt, dict):
                continue

            name = opt.get('drive_name')
            if not name or not isinstance(name, str) or not name.strip():
                continue

            mount_paths = [
                f"/run/media/{user}/{name}",
                f"/media/{user}/{name}",
                f"/mnt/{name}"
            ]

            if any(mount_path in path_str for mount_path in mount_paths):
                if not self.is_drive_mounted(opt):
                    return opt

        return None

    def check_drives_to_mount(self, paths_to_check):
        drives = []
        seen_drive_ids = set()

        for path in filter(None, paths_to_check):
            path_list = path if isinstance(path, list) else [path]
            for sub_path in path_list:
                drive = self.check_path_requires_mounting(sub_path)
                if drive:
                    drive_id = drive.get('drive_name', '')
                    if drive_id and drive_id not in seen_drive_ids:
                        seen_drive_ids.add(drive_id)
                        drives.append(drive)
        return drives

    @staticmethod
    def _validate_mount_command(cmd):
        if not cmd or not isinstance(cmd, str) or not cmd.strip():
            return False, "Empty or invalid command"

        dangerous_patterns = [';', '&&', '||', '|', '$(', '`', '>', '<', '&', '\n', '\r', '\x00']
        if any(pattern in cmd for pattern in dangerous_patterns):
            return False, "Contains dangerous characters"

        try:
            tokens = shlex.split(cmd)
            if not tokens:
                return False, "No valid tokens"

            allowed_commands = ['mount', 'umount', 'udisksctl']
            base_cmd = os.path.basename(tokens[0])

            if base_cmd == 'sudo' and len(tokens) > 1:
                base_cmd = os.path.basename(tokens[1])

            if base_cmd not in allowed_commands:
                return False, f"Command not allowed: {base_cmd}"

            suspicious_args = ['--exec', '--command', '-c', '--eval']
            if any(arg in tokens for arg in suspicious_args):
                return False, "Contains suspicious arguments"

        except ValueError as e:
            return False, f"Invalid command syntax: {e}"
        except Exception as e:
            return False, f"Command validation error: {e}"

        return True, ""

    def mount_drive(self, drive, parent=None, remember_unmount=True):
        if not isinstance(drive, dict):
            logger.error("[mount_drive] Drive parameter is not a dictionary")
            return False

        name = drive.get('drive_name', '').strip()
        cmd = drive.get('mount_command', '').strip()

        if not name or not cmd:
            error_msg = "Invalid drive configuration: missing name or command"
            self._show_message("Mount Error", error_msg, QMessageBox.Icon.Warning, parent)
            logger.warning(f"[mount_drive] {error_msg} for drive: {drive}")
            return False

        is_valid, error_msg = self._validate_mount_command(cmd)
        if not is_valid:
            full_error = f"Invalid mount command for drive '{name}': {error_msg}"
            self._show_message("Mount Error", full_error, QMessageBox.Icon.Warning, parent)
            logger.warning(f"[mount_drive] {full_error}")
            return False

        if self.is_drive_mounted(drive):
            logger.info(f"[mount_drive] Drive '{name}' is already mounted")
            return True

        try:
            logger.info(f"[mount_drive] Mounting drive '{name}' with command: {cmd}")

            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                check=False,
                timeout=45
            )

            if result.returncode != 0:
                error_msg = f"Drive '{name}' could not be mounted (exit code: {result.returncode})"
                if result.stderr:
                    error_msg += f"\nError: {result.stderr[:300]}"
                logger.error(f"[mount_drive] {error_msg}")
                self._show_message("Mount Error", error_msg, QMessageBox.Icon.Warning, parent)
                return False

            time.sleep(0.5)

            if not self.is_drive_mounted(drive):
                error_msg = f"Drive '{name}' mount command succeeded but drive not detected as mounted"
                logger.warning(f"[mount_drive] {error_msg}")
                self._show_message("Mount Error", error_msg, QMessageBox.Icon.Warning, parent)
                return False

            if remember_unmount and drive.get('unmount_command'):
                with self._lock:
                    if drive not in self.drives_to_unmount:
                        self.drives_to_unmount.append(drive)

            logger.info(f"[mount_drive] Successfully mounted drive '{name}'")
            return True

        except subprocess.TimeoutExpired:
            error_msg = f"Mount command for drive '{name}' timed out after 45 seconds"
            logger.error(f"[mount_drive] {error_msg}")

            try:
                subprocess.run(['pkill', '-f', cmd[:50]], timeout=10, check=False)
            except Exception as kill_error:
                logger.warning(f"[mount_drive] Could not kill mount process: {kill_error}")

            self._show_message("Mount Error", error_msg, QMessageBox.Icon.Warning, parent)
            return False

        except Exception as e:
            error_msg = f"Unexpected error mounting drive '{name}': {str(e)[:200]}"
            logger.exception(f"[mount_drive] {error_msg}")
            self._show_message("Mount Error", error_msg, QMessageBox.Icon.Critical, parent)
            return False

    def unmount_drive(self, drive, parent=None):
        name, cmd = drive.get('drive_name', ''), drive.get('unmount_command', '')
        if not cmd:
            return False
        try:
            logger.info(f"[unmount_drive] Unmounting drive '{name}' with command: {cmd}")
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                logger.warning(f"[unmount_drive] Unmount command for drive '{name}' returned code {result.returncode}")
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            logger.error(f"[unmount_drive] Unmount command for drive '{name}' timed out.")
            self._show_message("Unmount Error", f"Drive '{name}' unmount timed out.", QMessageBox.Icon.Warning, parent)
            return False
        except Exception as e:
            logger.exception(f"[unmount_drive] Error unmounting drive '{name}': {e}")
            self._show_message("Unmount Error", f"Drive '{name}' could not be unmounted.\nError: {e}", QMessageBox.Icon.Critical, parent)
            return False

    def mount_required_drives(self, drives, parent=None):
        if not drives:
            return True
        success = True
        for drive in drives:
            name = drive.get('drive_name', '')
            msg = QMessageBox(QMessageBox.Icon.Question, "Drive Mount Required", f"Drive '{name}' needs to be mounted.\nMount now?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
            checkbox = QCheckBox("Unmount drive when finished")
            checkbox.setChecked(True)
            msg.setCheckBox(checkbox)
            # noinspection PyUnresolvedReferences
            if msg.exec() != QMessageBox.StandardButton.Yes:
                logger.info(f"[mount_required_drives] User declined to mount drive '{name}'")
                self._show_message("Operation Cancelled", f"Cannot continue without mounting drive '{name}'.", QMessageBox.Icon.Information, parent)
                success = False
                continue
            if not self.mount_drive(drive, parent, checkbox.isChecked()):
                success = False
        return success

    def unmount_drives(self, parent=None):
        success = True
        with self._lock:
            drives_to_process = self.drives_to_unmount.copy()

        for drive in drives_to_process:
            if not self.unmount_drive(drive, parent):
                success = False

        with self._lock:
            if success:
                logger.info("[unmount_drives] All drives unmounted successfully. Clearing unmount list.")
                self.drives_to_unmount.clear()
        return success

    def mount_drives_at_launch(self):
        from options import Options

        if getattr(Options, 'mount_options', None) and getattr(Options, 'run_mount_command_on_launch', False):
            for opt in Options.mount_options:
                if not self.is_drive_mounted(opt):
                    logger.info(f"[mount_drives_at_launch] Mounting drive '{opt.get('drive_name', '')}' at launch.")
                    self.mount_drive(opt)

    @staticmethod
    def _show_message(title, text, icon, parent):
        QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, parent).exec()
