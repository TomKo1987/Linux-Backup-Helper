from pathlib import Path
from options import Options
import global_style, sys, json
from global_style import THEMES
from PyQt6.QtGui import QAction
from base_window import BaseWindow
from drive_manager import DriveManager
from settings_window import SettingsWindow
from system_info_window import SystemInfoWindow
from file_process import FileProcessDialog, SmbFileHandler
from PyQt6.QtCore import Qt, QCoreApplication, QTimer, pyqtSignal
from system_manager_launcher_dialog_thread import SystemManagerLauncher
from PyQt6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QMainWindow, QMenu, QMessageBox, QPushButton,
                             QSystemTrayIcon, QTextEdit, QVBoxLayout, QWidget)

from logging_config import get_log_file_path, setup_logger
logger = setup_logger(__name__)

sys.setrecursionlimit(5000)


class MainWindow(QMainWindow):

    settings_changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Backup Helper")
        self.setMinimumSize(375, 320)

        Options.set_main_window(self)

        self.drive_manager           = DriveManager()
        self.config:          dict   = {}
        self.backup_restore_window   = None
        self.settings_window         = None
        self.system_info_window      = None
        self.system_manager_launcher = None
        self.tray_icon               = None
        self.btn_exit                = QPushButton()

        self.settings_changed.connect(self.on_settings_changed)
        self.settings_changed.connect(self._refresh_exit_button)

        self._load_config()
        self._build_ui()
        self._setup_tray()

    def _load_config(self) -> None:
        Options.load_config()
        try:
            path = Options.active_profile_path()
            if path and path.exists():
                with open(path, "r", encoding="utf-8") as fh:
                    self.config = json.load(fh)
            else:
                self.config = {}
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning("_load_config: %s", exc)
            self.config = {}

    def _build_ui(self) -> None:
        central = QWidget()
        layout  = QVBoxLayout(central)

        nav_buttons = [
            ("Create Backup",  lambda: self.open_backup_restore("backup")),
            ("Restore Backup", lambda: self.open_backup_restore("restore")),
            ("System Manager", self.launch_system_manager),
            ("System Info",    self.open_system_info),
            ("Settings",       self.open_settings),
            ("View Logs",      self.open_log_viewer),
        ]

        for label, callback in nav_buttons:
            btn = QPushButton(label)
            btn.setFixedHeight(50)
            btn.setStyleSheet("font-size:17px")
            btn.clicked.connect(callback)
            layout.addWidget(btn)

        self.btn_exit.setFixedHeight(50)
        self.btn_exit.clicked.connect(self.confirm_exit)
        layout.addWidget(self.btn_exit)
        self._refresh_exit_button()
        self.setCentralWidget(central)

    def _refresh_exit_button(self) -> None:
        has_auto_unmount = (
            bool(getattr(Options, "run_mount_command_on_launch", False)) and
            bool(getattr(Options, "mount_options", []))
        )
        self.btn_exit.setText("Unmount and Exit" if has_auto_unmount else "Exit")
        self.btn_exit.setStyleSheet("font-size:17px")

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.info("System tray not available — skipping tray icon.")
            return

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(
            QApplication.style().standardIcon(QApplication.style().StandardPixmap.SP_DriveHDIcon)
        )
        self.tray_icon.setToolTip("Backup Helper")

        menu = QMenu()
        tray_items = [
            ("Show",           self._show_and_raise),
            ("Create Backup",  lambda: self.open_backup_restore("backup")),
            ("Restore Backup", lambda: self.open_backup_restore("restore")),
            ("Settings",       self.open_settings),
            ("View Logs",      self.open_log_viewer),
            (None, None),
            ("Exit",           self.confirm_exit),
        ]
        for label, callback in tray_items:
            if label is None:
                menu.addSeparator()
            else:
                act = QAction(label, self)
                act.triggered.connect(callback)
                menu.addAction(act)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_and_raise()

    def _open_window(self, attr: str, factory, error_title: str) -> None:
        self._close_window(attr)
        try:
            win = factory()
            win.destroyed.connect(lambda: setattr(self, attr, None))
            setattr(self, attr, win)
            win.show()
            self.hide()
        except Exception as exc:
            logger.error("%s: %s", error_title, exc)
            setattr(self, attr, None)
            QMessageBox.critical(self, "Error", f"Could not open window:\n{exc}")

    def _close_window(self, attr: str) -> None:
        win = getattr(self, attr, None)
        if not win:
            return
        for method in (win.close, win.deleteLater):
            try:
                method()
            except RuntimeError as exc:
                logger.debug("_close_window(%s) %s: %s", attr, method.__name__, exc)
        setattr(self, attr, None)

    def open_backup_restore(self, window_type: str) -> None:
        self._open_window(
            "backup_restore_window",
            lambda: BackupRestoreWindow(self, window_type),
            "open_backup_restore",
        )

    def open_settings(self) -> None:
        self._open_window("settings_window", lambda: SettingsWindow(self), "open_settings")

    def open_system_info(self) -> None:
        self._open_window("system_info_window", lambda: SystemInfoWindow(self), "open_system_info")

    def launch_system_manager(self) -> None:
        self.system_manager_launcher = SystemManagerLauncher(self)
        self.system_manager_launcher.launch()

    def start_backup_restoring(self, window_type: str) -> None:
        self.open_backup_restore(window_type)

    def open_log_viewer(self) -> None:
        log_path = get_log_file_path()

        display_path = str(log_path)
        for old, new in getattr(Options, "text_replacements", []):
            if old:
                display_path = display_path.replace(old, new)

        dlg = QDialog(self)
        dlg.setWindowTitle(display_path)
        dlg.setMinimumSize(1250, 800)
        layout = QVBoxLayout(dlg)

        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        text_edit.setStyleSheet("font-family:monospace;font-size:13px;")
        layout.addWidget(text_edit)

        def _load() -> None:
            try:
                if log_path.exists():
                    lines  = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    tail   = lines[-2000:]
                    prefix = f"[… showing last 2000 of {len(lines)} lines …]\n" if len(lines) > 2000 else ""
                    text_edit.setPlainText(prefix + "\n".join(tail))
                    cursor = text_edit.textCursor()
                    cursor.movePosition(cursor.MoveOperation.End)
                    text_edit.setTextCursor(cursor)
                    text_edit.ensureCursorVisible()
                else:
                    text_edit.setPlainText("No log file found yet.")
            except OSError as exc:
                text_edit.setPlainText(f"Could not read log file:\n{exc}")

        def _clear() -> None:
            if QMessageBox.question(
                dlg, "Clear Logs", "Delete all log entries? This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes:
                try:
                    log_path.write_text("", encoding="utf-8")
                    logger.info("Log file cleared by user.")
                    _load()
                except OSError as exc:
                    QMessageBox.critical(dlg, "Error", f"Could not clear log file:\n{exc}")

        def _copy() -> None:
            try:
                QApplication.clipboard().setText(text_edit.toPlainText())
                QMessageBox.information(dlg, "Copied", "Log content copied to clipboard.")
            except Exception as exc:
                QMessageBox.critical(dlg, "Error", f"Copy failed:\n{exc}")

        btn_row = QHBoxLayout()
        for label, fn in (("🗑️ Clear", _clear), ("📋 Copy", _copy), ("Close", dlg.accept)):
            btn = QPushButton(label)
            btn.clicked.connect(fn)
            btn_row.addWidget(btn)
        layout.addLayout(btn_row)

        _load()
        dlg.exec()

    def on_settings_changed(self) -> None:
        self._load_config()
        if self.backup_restore_window:
            try:
                self.backup_restore_window.settings_changed.emit()
            except RuntimeError:
                pass
        if self.system_manager_launcher and hasattr(self.system_manager_launcher, "config"):
            self.system_manager_launcher.config = self.config

    def confirm_exit(self) -> None:
        drives = [
            f"'{o.get('drive_name')}'"
            for o in Options.mount_options
            if isinstance(o, dict) and o.get("drive_name")
        ]
        auto_unmount = bool(getattr(Options, "run_mount_command_on_launch", False) and drives)

        text = (
            f"Unmount drive{'s' if len(drives) > 1 else ''} {' & '.join(drives)} and exit?"
            if auto_unmount else "Are you sure you want to exit?"
        )

        if not _confirm_dialog(self, "Exit Confirmation", text):
            return

        if auto_unmount:
            self.hide()
            self.drive_manager.drives_to_unmount = list(Options.mount_options)
            self.drive_manager.unmount_drives()

        QCoreApplication.exit(0)

    def closeEvent(self, event) -> None:
        drives = []
        if getattr(Options, "mount_options", None) and getattr(Options, "run_mount_command_on_launch", False):
            drives = [
                f"'{o.get('drive_name', 'Unknown')}'"
                for o in Options.mount_options
                if isinstance(o, dict) and o.get("drive_name")
            ]

        text = (
            f"Exit without unmounting drive{'s' if len(drives) > 1 else ''} {' & '.join(drives)}?"
            if drives else "Are you sure you want to exit?"
        )

        if _confirm_dialog(self, "Exit Confirmation", text):
            event.accept()
            QCoreApplication.exit(0)
        else:
            event.ignore()

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            focused = self.focusWidget()
            if isinstance(focused, QPushButton):
                focused.click()
        elif key == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)


class BackupRestoreWindow(BaseWindow):

    def __init__(self, parent=None, window_type: str = "backup") -> None:
        super().__init__(parent, window_type)
        self.drive_manager = DriveManager()

    def start_process(self) -> None:
        selected = self._selected_items()
        if not selected:
            self._show_op_error("Nothing selected.")
            return

        self.hide()

        paths  = [p for src, dst, _ in selected for p in _flatten(src, dst)]
        drives = self.drive_manager.check_drives_to_mount(paths)
        if drives and not self.drive_manager.mount_required_drives(drives, self):
            self.show()
            return

        processable, unprocessable = self._split_by_source(selected)
        if not processable:
            self._show_op_error(
                f"None of the selected entries could be "
                f"{'backed up' if self.window_type == 'backup' else 'restored'}."
            )
            return

        if unprocessable and not self._confirm_continue(unprocessable):
            self.show()
            return

        op              = "Backup" if self.window_type == "backup" else "Restore"
        processable_dirs = self._processable_checkbox_dirs()
        dialog          = FileProcessDialog(self, processable_dirs, operation_type=op)
        dialog.exec()

        self.show()
        self.drive_manager.unmount_drives()

    def _selected_items(self) -> list:
        return [(src, dst, uid) for cb, src, dst, uid in self.checkbox_dirs if cb.isChecked()]

    def _split_by_source(self, selected: list) -> tuple[list, list]:
        processable, unprocessable = [], []
        id_to_title = {e.get("unique_id"): e.get("title") for e in Options.entries_sorted}
        for src, dst, uid in selected:
            if self._source_exists(src):
                processable.append((src, dst, uid))
            else:
                unprocessable.append(id_to_title.get(uid, uid))
        return processable, unprocessable

    def _processable_checkbox_dirs(self) -> list:
        return [
            (cb, src, dst, uid)
            for cb, src, dst, uid in self.checkbox_dirs
            if cb.isChecked() and self._source_exists(src)
        ]

    @staticmethod
    def _source_exists(source) -> bool:
        sources = source if isinstance(source, list) else [source]
        return any(_path_exists(s) for s in sources)

    def _show_op_error(self, message: str) -> None:
        title = "Backup Error" if self.window_type == "backup" else "Restore Error"
        QMessageBox(QMessageBox.Icon.Information, title, message,
                    QMessageBox.StandardButton.Ok, self).exec()
        self.show()

    def _confirm_continue(self, unprocessable: list) -> bool:
        items = "\n".join(f"  • {item}" for item in unprocessable)
        return QMessageBox(
            QMessageBox.Icon.Warning,
            "Backup Warning" if self.window_type == "backup" else "Restore Warning",
            f"The following entries could not be found and will be skipped:\n\n"
            f"{items}\n\nContinue with the remaining entries?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        ).exec() == QMessageBox.StandardButton.Yes


def _flatten(*args):
    for item in args:
        if isinstance(item, list):
            yield from item
        else:
            yield item


def _path_exists(path) -> bool:
    try:
        if SmbFileHandler.is_smb_path(path):
            return True
        return Path(path).exists()
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        logger.warning("_path_exists: '%s': %s", path, exc)
        return False


def _confirm_dialog(parent, title: str, text: str) -> bool:
    dlg = QMessageBox(parent)
    dlg.setWindowTitle(title)
    dlg.setText(text)
    dlg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    dlg.setDefaultButton(QMessageBox.StandardButton.No)
    return dlg.exec() == QMessageBox.StandardButton.Yes


def main() -> None:
    app = QApplication(sys.argv)

    def _handle_exception(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        QMessageBox.critical(None, "Critical Error", f"An unexpected error occurred:\n{exc_value}")

    sys.excepthook = _handle_exception

    has_profile = Options.startup_load()
    theme_name  = Options.ui_settings.get("theme", "Tokyo Night")
    global_style.current_theme = theme_name if theme_name in THEMES else global_style.current_theme
    app.setStyleSheet(global_style.get_current_style())

    window = MainWindow()
    window.show()

    if not has_profile:
        def _first_run():
            window.open_settings()
            if window.settings_window:
                window.settings_window.open_profile_manager()
        QTimer.singleShot(300, _first_run)

    if getattr(Options, "run_mount_command_on_launch", False):
        window.drive_manager.mount_drives_at_launch()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
