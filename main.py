from pathlib import Path
from options import Options
from global_style import THEMES
from base_window import BaseWindow
from drive_manager import DriveManager
from file_process import SmbFileHandler
from settings_window import SettingsWindow
from system_info_window import SystemInfoWindow
import json, sys, logging.handlers, global_style
from PyQt6.QtCore import Qt, pyqtSignal, QCoreApplication, QTimer
from system_manager_launcher_dialog_thread import SystemManagerLauncher
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QMessageBox, QMainWindow

sys.setrecursionlimit(5000)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

def flatten_paths(*args):
    for item in args:
        if isinstance(item, list):
            yield from item
        else:
            yield item


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
        self.system_manager_launcher = None
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
        self.setMinimumSize(375, 250)
        button_height = 50
        buttons = [("Create Backup", lambda: self.start_backup_restoring("backup")),
                   ("Restore Backup", lambda: self.start_backup_restoring("restore")),
                   ("System Manager", self.launch_system_manager),
                   ("System Info", self.open_system_info),
                   ("Settings", self.open_settings)]
        for text, callback in buttons:
            btn = QPushButton(text)
            btn.setFixedHeight(button_height)
            btn.setStyleSheet("font-size: 17px")
            btn.clicked.connect(callback)
            layout.addWidget(btn)
        self.btn_exit.setFixedHeight(button_height)
        self.btn_exit.clicked.connect(self.confirm_exit)
        layout.addWidget(self.btn_exit)
        self.set_exit_button()
        self.setCentralWidget(central_widget)

    def set_exit_button(self):
        self.btn_exit.setText("Unmount and Exit" if Options.run_mount_command_on_launch and Options.mount_options else "Exit")
        self.btn_exit.setStyleSheet("font-size: 17px")

    def _safe_close_window(self, window_attr):
        win = getattr(self, window_attr, None)
        if win:
            try:
                win.close()
            except RuntimeError as e:
                logger.warning(f"RuntimeError while closing window: {e}")
            except Exception as e:
                logger.error(f"Error while closing window: {e}")
            try:
                win.deleteLater()
            except RuntimeError as e:
                logger.warning(f"RuntimeError in deleteLater: {e}")
            except Exception as e:
                logger.error(f"Error in deleteLater: {e}")
            setattr(self, window_attr, None)

    def start_backup_restoring(self, window_type):
        self._safe_close_window('backup_restore_window')
        try:
            self.backup_restore_window = BackupRestoreWindow(self, window_type)
            self.backup_restore_window.show()
            self.hide()
        except Exception as e:
            logger.error(f"Error creating backup/restore window: {e}")
            QMessageBox.critical(self, "Error", f"Could not open window: {e}")

    def open_settings(self):
        self._safe_close_window('settings_window')
        try:
            self.settings_window = SettingsWindow(self)
            self.settings_window.show()
            self.hide()
        except Exception as e:
            logger.error(f"Error creating Settings Window: {e}")
            QMessageBox.critical(self, "Error", f"Could not open window: {e}")

    def launch_system_manager(self):
        self.system_manager_launcher = SystemManagerLauncher(self)
        self.system_manager_launcher.launch()

    def open_system_info(self):
        self._safe_close_window('system_info_window')
        try:
            self.system_info_window = SystemInfoWindow(self)
            self.system_info_window.show()
            self.hide()
        except Exception as e:
            logger.error(f"Error creating System Info window: {e}")
            QMessageBox.critical(self, "Error", f"Could not open window: {e}")

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
        if self.system_manager_launcher and hasattr(self.system_manager_launcher, 'config'):
            self.system_manager_launcher.config = self.config

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            w = self.focusWidget()
            if isinstance(w, QPushButton):
                w.click()
        elif event.key() == Qt.Key.Key_Escape:
            self.closeEvent(event)
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
            if (hasattr(Options, 'mount_options') and
                    Options.mount_options and
                    hasattr(Options, 'run_mount_command_on_launch') and
                    Options.run_mount_command_on_launch):
                drives = [f"'{opt.get('drive_name', 'Unknown')}'"
                          for opt in Options.mount_options
                          if isinstance(opt, dict) and opt.get('drive_name')]
            text = (f"Exit without unmounting drive{'s' if len(drives) > 1 else ''} "
                    f"{' & '.join(drives)}?"
                    if drives
                    else "Are you sure you want to exit?")
            if self._confirm_dialog("Exit Confirmation", text):
                event.accept()
                QCoreApplication.exit(0)
            else:
                event.ignore()
        except Exception as e:
            logger.error(f"Error in closeEvent: {e}")
            if self._confirm_dialog("Exit Confirmation", "Are you sure you want to exit?"):
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
        return [p for source, dest, _ in selected_items for p in flatten_paths(source, dest)]

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
    Options.load_config(Options.config_file_path)
    theme_name = Options.ui_settings.get("theme", "Tokyo Night")
    global_style.current_theme = theme_name if theme_name in THEMES else global_style.current_theme
    app.setStyleSheet(global_style.get_current_style())
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
        QMessageBox.critical(None, "Critical Error", f"An unexpected error occurred:\n{exc_value}")
    sys.excepthook = handle_exception
    window = MainWindow()
    window.show()
    Options.mount_drives_on_startup()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
