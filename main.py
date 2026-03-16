import sys, shutil
from pathlib import Path

from PyQt6.QtGui import QAction
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QWidget,
    QApplication, QFileDialog, QInputDialog, QMainWindow, QVBoxLayout
)

from themes import apply_style
from dialogs import LogViewer, SysInfoDialog
from backup_restore_settings import base_window
from drive_utils import get_mount_output, is_mounted, unmount_drive
from state import S, _HOME, _PROFILES_DIR, _PROFILE_RE, save_profile, startup_load, logger

RESTART_DIALOG = 2


def _main_btn(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setFixedHeight(50)
    btn.setStyleSheet("font-size:18px;font-weight:bold;")
    return btn


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Backup Helper")
        self.setFixedSize(425, 425)
        self._quitting = False

        self.menu_actions = [
            ("💾 Create Backup", lambda: self._open(base_window, "Backup")),
            ("📤 Restore Backup", lambda: self._open(base_window, "Restore")),
            ("🖥 System Manager", self._open_system_manager),
            ("💻 System Information", lambda: self._open(SysInfoDialog)),
            ("📋 View Log", lambda: self._open(LogViewer)),
            ("⚙️ Settings", self._open_settings),
            ("❌ Quit", self._exit),
        ]

        self._build_ui()
        self._setup_tray()

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        for label, fn in self.menu_actions:
            btn = _main_btn(label)
            btn.clicked.connect(fn)
            layout.addWidget(btn)

        self.setCentralWidget(central)

    def _open(self, cls, *args) -> None:
        self.hide()
        while True:
            dlg = cls(self, *args) if args else cls(self)
            if dlg.exec() != RESTART_DIALOG:
                break
        self.show()

    def _open_system_manager(self) -> None:
        from system_manager_options import SystemManagerLauncher
        SystemManagerLauncher(self).launch()

    def _open_settings(self) -> None:
        self.hide()
        while True:
            dlg = base_window(self, "Settings")
            dlg.changed.connect(apply_style)
            if dlg.exec() != RESTART_DIALOG:
                break
        self._build_ui()
        self.show()

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(QApplication.style().standardIcon(QApplication.style().StandardPixmap.SP_DriveHDIcon))
        self.tray.setToolTip("Backup Helper")

        menu = QMenu()

        show_act = QAction("🏠 Show Backup Helper", self)
        show_act.triggered.connect(self._show_and_raise)
        menu.addAction(show_act)
        menu.addSeparator()

        for label, fn in self.menu_actions:
            a = QAction(label, self)
            a.triggered.connect(fn)
            menu.addAction(a)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: (self._show_and_raise() if r == QSystemTrayIcon.ActivationReason.DoubleClick else None))
        self.tray.show()

    def _show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _exit(self) -> None:
        if self._quitting:
            return

        mount_out = get_mount_output()
        mounted_drives = [o for o in S.mount_options if is_mounted(o, mount_out)]
        unmountable = [o for o in mounted_drives if o.get("unmount_command")]
        info_only = [o for o in mounted_drives if not o.get("unmount_command")]

        if unmountable:
            lines = [f"  • {o['drive_name']}" for o in unmountable]
            if info_only:
                lines += ["", "These drives have no unmount command and will be left mounted:"]
                lines += [f"  • {o['drive_name']}" for o in info_only]
            ans = QMessageBox.question(self, "Quit — Drives Still Mounted",
                                       "The following drives are still mounted:\n" + "\n".join(lines) +
                                       "\n\nUnmount them before quitting?", QMessageBox.StandardButton.Yes
                                       | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
            if ans == QMessageBox.StandardButton.Cancel:
                return
            if ans == QMessageBox.StandardButton.Yes:
                failed = [f"• {o.get('drive_name', '?')}: {err}" for o in unmountable for ok, err in [unmount_drive(o)] if not ok]
                if failed:
                    QMessageBox.warning(self, "Unmount Failed", "Could not unmount:\n\n" + "\n".join(failed))

        else:
            msg = ("The following drives are still mounted but have no unmount command:\n" +
                   "\n".join(f"  • {o['drive_name']}" for o in info_only) + "\n\nQuit anyway?" if info_only else "Really quit Backup Helper?")
            if QMessageBox.question(self, "Quit", msg, QMessageBox.StandardButton.Yes
                                                       | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return

        self._quitting = True
        QApplication.quit()

    def closeEvent(self, event) -> None:
        if self._quitting:
            event.accept()
        else:
            event.ignore()
            self._exit()

    def keyPressEvent(self, event) -> None:
        k = event.key()
        if k in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            focused = self.focusWidget()
            if isinstance(focused, QPushButton):
                focused.click()
        elif k == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)


def _first_run_wizard(parent) -> bool:
    from state import load_profile

    msg = QMessageBox(parent)
    msg.setWindowTitle("Welcome to Backup Helper")
    msg.setText("<b>No profile found.</b><br><br>Would you like to import an existing profile (.json)?")
    msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    msg.button(QMessageBox.StandardButton.Yes).setText("Import profile")
    msg.button(QMessageBox.StandardButton.No).setText("Create empty profile")

    if msg.exec() == QMessageBox.StandardButton.Yes:
        path, _ = QFileDialog.getOpenFileName(parent, "Select profile", str(_HOME), "JSON (*.json)")
        if path:
            while True:
                name, ok = QInputDialog.getText(parent, "Profile name", "Name:", text=Path(path).stem)
                if not ok:
                    break
                clean_name = name.strip()
                if not clean_name or not _PROFILE_RE.match(clean_name):
                    QMessageBox.warning(parent, "Invalid Name", "Profile name may only contain letters, digits, spaces, hyphens and dots.")
                    continue
                _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
                dest = _PROFILES_DIR / f"{clean_name}.json"
                if dest.exists():
                    confirm = QMessageBox.warning(parent, "Overwrite", f"Profile '{clean_name}' already exists. Overwrite?",
                                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    if confirm == QMessageBox.StandardButton.No:
                        continue
                shutil.copy2(path, dest)
                return load_profile(dest)

    S.profile_name = "Default"
    S.headers = {}
    S.entries = []
    save_profile()
    return True


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Backup Helper")

    def _handle_exc(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return

        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))

        try:
            QMessageBox.critical(None, "Critical Error", f"An unexpected error occurred:\n\n{exc_value}\n\nCheck the logs for details.")
        except Exception as dialog_exc:
            logger.error("Failed to show error dialog: %s", dialog_exc)
            print(f"FATAL ERROR: {exc_value}", file=sys.stderr)

    sys.excepthook = _handle_exc

    has_profile = startup_load()
    apply_style()

    win = MainWindow()
    win.show()

    if not has_profile:
        QTimer.singleShot(200, lambda: _first_run_wizard(win))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()