import json, sys
from pathlib import Path
from options import Options
from global_style import global_style
from PyQt6.QtCore import Qt, pyqtSignal, QCoreApplication
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QMessageBox, QMainWindow

sys.setrecursionlimit(5000)

Options.load_config(Options.config_file_path)
Options.mount_drives_on_startup()


# noinspection PyUnresolvedReferences
class MainWindow(QMainWindow):
    settings_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Backup Helper")
        Options.set_main_window(self)
        self.config = {}
        from drive_manager import DriveManager
        self.drive_manager = DriveManager()
        self.backup_restore_window = None
        self.settings_window = None
        self.package_installer_launcher = None
        self.btn_exit = QPushButton()
        self.settings_changed.connect(self.set_exit_button)
        self.settings_changed.connect(self.on_settings_changed)
        self.load_config()
        self.init_ui()

    def load_config(self):
        Options.load_config(Options.config_file_path)
        try:
            with open(Options.config_file_path, 'r') as file:
                self.config = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Error loading config: {e}")
            self.config = {}

    def init_ui(self):
        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        self.setMinimumSize(400, 300)
        button_height = 50
        buttons = [("Create Backup", lambda: self.start_backup_restoring("backup")), ("Restore Backup", lambda: self.start_backup_restoring("restore")),
                   ("Package Installer", self.launch_package_installer), ("Settings", self.open_settings)]
        for text, callback in buttons:
            btn = QPushButton(text)
            btn.setFixedHeight(button_height)
            btn.clicked.connect(callback)
            layout.addWidget(btn)
        self.btn_exit.setFixedHeight(button_height)
        self.btn_exit.clicked.connect(self.confirm_exit)
        layout.addWidget(self.btn_exit)
        self.set_exit_button()
        self.setCentralWidget(central_widget)

    def set_exit_button(self):
        self.btn_exit.setText("Unmount and Exit" if Options.run_mount_command_on_launch and Options.mount_options else "Exit")

    def start_backup_restoring(self, window_type):
        if self.backup_restore_window:
            self.backup_restore_window.close()
            self.backup_restore_window = None
        self.backup_restore_window = BackupRestoreWindow(self, window_type)
        self.backup_restore_window.show()
        self.hide()

    def open_settings(self):
        from settings_window import SettingsWindow
        if self.settings_window:
            self.settings_window.close()
            self.settings_window = None
        self.settings_window = SettingsWindow(self)
        self.settings_window.show()
        self.hide()

    def launch_package_installer(self):
        from package_installer_launcher_dialog_thread import PackageInstallerLauncher
        self.package_installer_launcher = PackageInstallerLauncher(self)
        self.package_installer_launcher.launch()

    def on_settings_changed(self):
        self.load_config()
        self.set_exit_button()
        if self.backup_restore_window:
            self.backup_restore_window.settings_changed.emit()
        if self.settings_window:
            self.settings_window.settings_changed.emit()
        if self.package_installer_launcher:
            self.package_installer_launcher.config = self.config

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            w = self.focusWidget()
            if isinstance(w, QPushButton):
                w.click()
        elif event.key() == Qt.Key.Key_Escape:
            self.confirm_exit()
        else:
            super().keyPressEvent(event)

    def confirm_exit(self):
        drives = [f"'{opt.get('drive_name')}'" for opt in Options.mount_options]
        is_unmounting = Options.run_mount_command_on_launch and drives
        text = f"Unmount drive{'s' if len(drives) > 1 else ''} {' & '.join(drives)} and exit?" if is_unmounting else "Are you sure you want to exit?"
        if self._confirm_dialog("Exit Confirmation", text):
            if is_unmounting:
                self.hide()
                self.drive_manager.drives_to_unmount = Options.mount_options
                self.drive_manager.unmount_drives()
            QCoreApplication.exit(0)

    def closeEvent(self, event):
        drives = [f"'{opt.get('drive_name')}'" for opt in Options.mount_options]
        text = f"Exit without unmounting drive{'s' if len(drives) > 1 else ''} {' & '.join(drives)}?" if Options.run_mount_command_on_launch and drives else "Are you sure you want to exit?"
        if self._confirm_dialog("Exit Confirmation", text):
            event.accept()
            QCoreApplication.exit(0)
        else:
            event.ignore()

    def _confirm_dialog(self, title, text):
        dlg = QMessageBox(self)
        dlg.setWindowTitle(title)
        dlg.setText(text)
        dlg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        dlg.setDefaultButton(QMessageBox.StandardButton.No)
        return dlg.exec() == QMessageBox.StandardButton.Yes


from base_window import BaseWindow
class BackupRestoreWindow(BaseWindow):
    def __init__(self, parent=None, window_type="backup"):
        super().__init__(parent, window_type)
        from drive_manager import DriveManager
        self.drive_manager = DriveManager()

    def _get_selected_items(self):
        return [(source_dirs, dest_dirs, label) for checkbox, source_dirs, dest_dirs, label in self.checkbox_dirs if checkbox.isChecked()]

    def start_process(self):
        self.hide()
        selected_items = self._get_selected_items()
        if not selected_items:
            self._show_error_and_return("Cannot start the process. Nothing selected.")
            return
        paths_to_check = self._extract_paths_to_check(selected_items)
        drives_to_mount = self.drive_manager.check_drives_to_mount(paths_to_check)
        if drives_to_mount and not self.drive_manager.mount_required_drives(drives_to_mount, self):
            self.show()
            return
        processable_items, unprocessable_items = self._separate_processable_items(selected_items)
        if not processable_items:
            self._show_error_and_return(f"Selected items cannot be {'copied' if self.window_type == 'backup' else 'restored'}.")
            return
        if unprocessable_items and not self._confirm_continue_with_missing(unprocessable_items):
            self.show()
            return
        processable_checkbox_dirs = self._get_processable_checkbox_dirs()
        operation_type = "Backup" if self.window_type == "backup" else "Restore"
        dialog = BackupRestoreProcessDialog(self, processable_checkbox_dirs, operation_type=operation_type)
        dialog.exec()
        self.show()
        self.drive_manager.unmount_drives()

    @staticmethod
    def _extract_paths_to_check(selected_items):
        paths = []
        for source_dirs, dest_dirs, _ in selected_items:
            if isinstance(source_dirs, list):
                paths.extend(source_dirs)
            else:
                paths.append(source_dirs)
            if isinstance(dest_dirs, list):
                paths.extend(dest_dirs)
            else:
                paths.append(dest_dirs)
        return paths

    @staticmethod
    def _check_path_exists(path):
        from file_process import SmbFileHandler
        if SmbFileHandler.is_smb_path(path):
            return True
        else:
            return Path(path).exists()

    def _separate_processable_items(self, selected_items):
        processable = []
        unprocessable = []
        label_to_title = {entry.get('unique_id'): entry.get('title') for entry in Options.entries_sorted}
        for source_dirs, dest_dirs, label in selected_items:
            sources = source_dirs if isinstance(source_dirs, list) else [source_dirs]
            source_exists = False
            for src in sources:
                if self._check_path_exists(src):
                    source_exists = True
                    break
            if source_exists:
                processable.append((source_dirs, dest_dirs, label))
            else:
                unprocessable.append(label_to_title.get(label, label))
        return processable, unprocessable

    def _show_error_and_return(self, message):
        error_title = f"{'Backup' if self.window_type == 'backup' else 'Restore'} Error"
        QMessageBox(QMessageBox.Icon.Information, error_title, message, QMessageBox.StandardButton.Ok, self).exec()
        self.show()

    def _confirm_continue_with_missing(self, unprocessable_items):
        warning_message = "The following entries could not be processed because they do not exist:\n\n"
        warning_message += "\n".join([f"• {item}" for item in unprocessable_items])
        warning_message += f"\n\nDo you want to continue with the available entries?"
        warning_title = f"{'Backup' if self.window_type == 'backup' else 'Restore'} Warning"
        warning_box = QMessageBox(QMessageBox.Icon.Warning, warning_title, warning_message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        warning_box.setDefaultButton(QMessageBox.StandardButton.Yes)
        return warning_box.exec() == QMessageBox.StandardButton.Yes

    def _get_processable_checkbox_dirs(self):
        result = []
        for checkbox, source_dirs, dest_dirs, label in self.checkbox_dirs:
            if not checkbox.isChecked():
                continue
            sources = source_dirs if isinstance(source_dirs, list) else [source_dirs]
            source_exists = False
            for src in sources:
                if self._check_path_exists(src):
                    source_exists = True
                    break
            if source_exists:
                result.append((checkbox, source_dirs, dest_dirs, label))
        return result


from file_process import FileProcessDialog
# noinspection PyUnresolvedReferences
class BackupRestoreProcessDialog(FileProcessDialog):
    def __init__(self, parent, checkbox_dirs, operation_type="Backup"):
        super().__init__(parent, checkbox_dirs, operation_type)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(global_style)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()