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

        from options import Options

        path = str(path)
        for opt in Options.mount_options:
            if not isinstance(opt, dict):
                continue
            name = opt.get('drive_name', '')
            if not name:
                continue
            mount_paths = [f"/run/media/{user}/{name}", f"/media/{user}/{name}", f"/mnt/{name}", name]
            if any(p in path for p in mount_paths) and not self.is_drive_mounted(opt):
                return opt
        return None

    def check_drives_to_mount(self, paths_to_check):
        drives, seen = [], set()
        for path in filter(None, paths_to_check):
            for sub_path in (path if isinstance(path, list) else [path]):
                drive = self.check_path_requires_mounting(sub_path)
                if drive and id(drive) not in seen:
                    seen.add(id(drive))
                    drives.append(drive)
        return drives

    def mount_drive(self, drive, parent=None, remember_unmount=True):
        if not isinstance(drive, dict):
            return False

        name = drive.get('drive_name', '')
        cmd = drive.get('mount_command', '')

        if not name or not cmd:
            self._show_message("Mount Error", "Invalid drive configuration: missing name or command", QMessageBox.Icon.Warning, parent)
            return False
        try:
            print(f"Mounting drive: {name}")
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                self._show_message("Mount Error", f"Drive '{name}' could not be mounted.\nCommand failed with return code {result.returncode}", QMessageBox.Icon.Warning, parent)
                return False
            if not self.is_drive_mounted(drive):
                self._show_message("Mount Error", f"Drive '{name}' could not be mounted.", QMessageBox.Icon.Warning, parent)
                return False
            if remember_unmount and drive.get('unmount_command'):
                self.drives_to_unmount.append(drive)
            return True
        except Exception as e:
            self._show_message("Mount Error", f"Drive '{name}' could not be mounted.\nError: {e}", QMessageBox.Icon.Critical, parent)
            return False

    def unmount_drive(self, drive, parent=None):
        name, cmd = drive.get('drive_name', ''), drive.get('unmount_command', '')
        if not cmd:
            return False
        try:
            print(f"Unmounting drive: {name}")
            subprocess.Popen(cmd, shell=True)
            return True
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
        # Import here to avoid circular imports
        from options import Options
        
        if getattr(Options, 'mount_options', None) and getattr(Options, 'run_mount_command_on_launch', False):
            for opt in Options.mount_options:
                if not self.is_drive_mounted(opt):
                    self.mount_drive(opt)

    @staticmethod
    def _show_message(title, text, icon, parent):
        QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, parent).exec()