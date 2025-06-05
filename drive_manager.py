from pathlib import Path
import subprocess, pwd, os
from PyQt6.QtWidgets import QMessageBox, QCheckBox

user = pwd.getpwuid(os.getuid()).pw_name


class DriveManager:
    def __init__(self):
        self.drives_to_unmount = []

    @staticmethod
    def is_drive_mounted(opt):
        try:
            output = subprocess.check_output(['mount'], text=True)
            name = opt.get('drive_name', '')
            paths = [f"/run/media/{user}/{name}", f"/media/{user}/{name}", f"/mnt/{name}", name]
            return any(p in output for p in paths)
        except Exception as e:
            print(f"Error checking mount: {e}")
            return False

    def check_path_requires_mounting(self, path):
        if not path or not isinstance(path, (str, Path)):
            return None
        try:
            if isinstance(path, str) and not path.strip():
                return None
            path_obj = Path(path)
            if not path_obj.is_absolute():
                path_obj = path_obj.expanduser().resolve()
            else:
                path_obj = path_obj.resolve()
            path = str(path_obj)
        except (OSError, ValueError, RuntimeError) as e:
            print(f"Error resolving path {path}: {e}")
            return None

        from options import Options
        if not hasattr(Options, 'mount_options') or not Options.mount_options:
            return None

        for opt in Options.mount_options:
            if not isinstance(opt, dict):
                continue
            name = opt.get('drive_name', '')
            if not name or not isinstance(name, str):
                continue
            mount_paths = [f"/run/media/{user}/{name}", f"/media/{user}/{name}", f"/mnt/{name}", name]
            if any(p in path for p in mount_paths) and not self.is_drive_mounted(opt):
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

    def mount_drive(self, drive, parent=None, remember_unmount=True):
        if not isinstance(drive, dict):
            return False

        name = drive.get('drive_name', '')
        cmd = drive.get('mount_command', '')

        if not name or not cmd or not isinstance(name, str) or not isinstance(cmd, str):
            self._show_message("Mount Error", "Invalid drive configuration: missing or invalid name/command", QMessageBox.Icon.Warning, parent)
            return False

        if len(cmd.strip()) == 0 or cmd.strip().startswith(';') or '&&' in cmd or '||' in cmd:
            self._show_message("Mount Error", f"Invalid mount command for drive '{name}'", QMessageBox.Icon.Warning, parent)
            return False

        try:
            print(f"Mounting drive: {name}")
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False, timeout=30)
            if result.returncode != 0:
                error_msg = f"Drive '{name}' could not be mounted.\nReturn code: {result.returncode}"
                if result.stderr:
                    error_msg += f"\nError: {result.stderr[:200]}"
                self._show_message("Mount Error", error_msg, QMessageBox.Icon.Warning, parent)
                return False
            if not self.is_drive_mounted(drive):
                self._show_message("Mount Error", f"Drive '{name}' could not be mounted.", QMessageBox.Icon.Warning, parent)
                return False
            if remember_unmount and drive.get('unmount_command'):
                self.drives_to_unmount.append(drive)
            return True
        except subprocess.TimeoutExpired:
            self._show_message("Mount Error", f"Mount command for drive '{name}' timed out.", QMessageBox.Icon.Warning, parent)
            return False
        except Exception as e:
            self._show_message("Mount Error", f"Drive '{name}' could not be mounted.\nError: {str(e)[:200]}", QMessageBox.Icon.Critical, parent)
            return False

    def unmount_drive(self, drive, parent=None):
        name, cmd = drive.get('drive_name', ''), drive.get('unmount_command', '')
        if not cmd:
            return False
        try:
            print(f"Unmounting drive: {name}")
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            self._show_message("Unmount Error", f"Drive '{name}' unmount timed out.", QMessageBox.Icon.Warning, parent)
            return False
        except Exception as e:
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
            if msg.exec() != QMessageBox.StandardButton.Yes:
                self._show_message("Operation Cancelled", f"Cannot continue without mounting drive '{name}'.", QMessageBox.Icon.Information, parent)
                success = False
                continue
            if not self.mount_drive(drive, parent, checkbox.isChecked()):
                success = False
        return success

    def unmount_drives(self, parent=None):
        success = True
        for drive in self.drives_to_unmount:
            if not self.unmount_drive(drive, parent):
                success = False
        if success:
            self.drives_to_unmount.clear()
        return success

    def mount_drives_at_launch(self):
        from options import Options
        
        if getattr(Options, 'mount_options', None) and getattr(Options, 'run_mount_command_on_launch', False):
            for opt in Options.mount_options:
                if not self.is_drive_mounted(opt):
                    self.mount_drive(opt)

    @staticmethod
    def _show_message(title, text, icon, parent):
        QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, parent).exec()
