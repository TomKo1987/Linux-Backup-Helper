from pathlib import Path
from options import Options
import json, sys, logging.handlers
from base_window import BaseWindow
from drive_manager import DriveManager
from file_process import SmbFileHandler
from settings_window import SettingsWindow
from system_info_window import SystemInfoWindow
from PyQt6.QtCore import Qt, pyqtSignal, QCoreApplication, QTimer
from package_installer_launcher_dialog_thread import PackageInstallerLauncher
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QMessageBox, QMainWindow

sys.setrecursionlimit(5000)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)


# noinspection PyUnresolvedReferences
class MainWindow(QMainWindow):
    settings_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Backup Helper")
        Options.set_main_window(self)
        self.config = {}
        self.drive_manager = DriveManager()
        self.system_info_window = None
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
            logger.warning(f"Error loading config: {e}")
            self.config = {}

    def init_ui(self):
        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        self.setMinimumSize(400, 300)
        button_height = 50
        buttons = [("Create Backup", lambda: self.start_backup_restoring("backup")),
                   ("Restore Backup", lambda: self.start_backup_restoring("restore")),
                   ("Package Installer", self.launch_package_installer),
                   ("System Info", self.open_system_info),
                   ("Settings", self.open_settings)]
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

    def open_system_info(self):
        if self.system_info_window:
            try:
                if not self.system_info_window.isVisible():
                    self.system_info_window.close()
                self.system_info_window.deleteLater()
                self.system_info_window = None
            except (RuntimeError, AttributeError):
                self.system_info_window = None
            finally:
                self.system_info_window = None
        try:
            self.system_info_window = SystemInfoWindow(self)
            self.system_info_window.show()
            self.hide()
        except Exception as e:
            logger.error(f"Error creating backup/restore window: {e}")
            QMessageBox.critical(self, "Error", f"Could not open window: {e}")

    def start_backup_restoring(self, window_type):
        if self.backup_restore_window:
            try:
                if not self.backup_restore_window.isVisible():
                    self.backup_restore_window.close()
                self.backup_restore_window.deleteLater()
                self.backup_restore_window = None
            except (RuntimeError, AttributeError):
                self.backup_restore_window = None
            finally:
                self.backup_restore_window = None
        try:
            self.backup_restore_window = BackupRestoreWindow(self, window_type)
            self.backup_restore_window.show()
            self.hide()
        except Exception as e:
            logger.error(f"Error creating backup/restore window: {e}")
            QMessageBox.critical(self, "Error", f"Could not open window: {e}")

    def open_settings(self):
        if self.settings_window:
            try:
                self.settings_window.close()
            except RuntimeError:
                pass
            try:
                self.settings_window.deleteLater()
            except RuntimeError:
                pass
            finally:
                self.settings_window = None
        try:
            self.settings_window = SettingsWindow(self)
            self.settings_window.show()
            self.hide()
        except Exception as e:
            logger.error(f"Error creating Settings Window: {e}")
            QMessageBox.critical(self, "Error", f"Could not open window: {e}")

    def launch_package_installer(self):
        self.package_installer_launcher = PackageInstallerLauncher(self)
        self.package_installer_launcher.launch()

    def on_settings_changed(self):
        self.load_config()
        self.set_exit_button()
        if self.backup_restore_window and hasattr(self.backup_restore_window, 'settings_changed'):
            try:
                self.backup_restore_window.settings_changed.emit()
            except RuntimeError:
                pass
        if self.settings_window and hasattr(self.settings_window, 'settings_changed'):
            try:
                self.settings_window.settings_changed.emit()
            except RuntimeError:
                pass
        if self.package_installer_launcher and hasattr(self.package_installer_launcher, 'config'):
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
        try:
            drives = []
            if hasattr(Options, 'mount_options') and Options.mount_options:
                drives = [f"'{opt.get('drive_name', 'Unknown')}'"
                          for opt in Options.mount_options
                          if isinstance(opt, dict) and opt.get('drive_name')]

            text = (f"Exit without unmounting drive{'s' if len(drives) > 1 else ''} "
                    f"{' & '.join(drives)}?"
                    if Options.run_mount_command_on_launch and drives
                    else "Are you sure you want to exit?")

            if self._confirm_dialog("Exit Confirmation", text):
                event.accept()
                QCoreApplication.exit(0)
            else:
                event.ignore()
        except Exception as e:
            logger.error(f"Error in closeEvent: {e}")
            event.accept()
            QCoreApplication.exit(0)

    def _confirm_dialog(self, title, text):
        dlg = QMessageBox(self)
        dlg.setWindowTitle(title)
        dlg.setText(text)
        dlg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        dlg.setDefaultButton(QMessageBox.StandardButton.No)
        return dlg.exec() == QMessageBox.StandardButton.Yes


class BackupRestoreWindow(BaseWindow):
    def __init__(self, parent=None, window_type="backup"):
        super().__init__(parent, window_type)
        self.drive_manager = DriveManager()

    def _get_selected_items(self):
        return [(source_dirs, dest_dirs, label) for checkbox, source_dirs, dest_dirs, label in self.checkbox_dirs if checkbox.isChecked()]

    def start_process(self):
        self.hide()
        selected_items = self._get_selected_items()
        if not selected_items:
            QTimer.singleShot(0, lambda: self._show_error_and_return("Cannot start the process. Nothing selected."))
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
        from file_process import FileProcessDialog
        dialog = FileProcessDialog(self, processable_checkbox_dirs, operation_type=operation_type)
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
        try:
            if SmbFileHandler.is_smb_path(path):
                return True
            else:
                return Path(path).exists()
        except (OSError, ValueError, TypeError) as e:
            logger.warning(f"Error checking path existence for '{path}': {e}")
            return False

    def _separate_processable_items(self, selected_items):
        processable = []
        unprocessable = []
        label_to_title = {entry.get('unique_id'): entry.get('title') for entry in Options.entries_sorted}

        for source_dirs, dest_dirs, label in selected_items:
            if self._has_existing_source(source_dirs):
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
            if checkbox.isChecked() and self._has_existing_source(source_dirs):
                result.append((checkbox, source_dirs, dest_dirs, label))
        return result

    def _has_existing_source(self, source_dirs):
        sources = source_dirs if isinstance(source_dirs, list) else [source_dirs]
        return any(self._check_path_exists(src) for src in sources)


def main():
    app = QApplication(sys.argv)

    # Load config first
    Options.load_config(Options.config_file_path)

    # Apply theme from settings
    from global_style import THEMES
    import global_style
    theme_name = Options.ui_settings.get("theme", "Tokyo Night")
    if theme_name in THEMES:
        global_style.current_theme = theme_name
        app.setStyleSheet(THEMES[theme_name])
    else:
        app.setStyleSheet(global_style.global_style)

    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
        QMessageBox.critical(None, "Critical Error", f"An unexpected error occurred:\n{exc_value}")

    sys.excepthook = handle_exception

    Options.load_config(Options.config_file_path)

    window = MainWindow()
    window.show()

    Options.mount_drives_on_startup()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
