import sys, shutil
from pathlib import Path

from PyQt6.QtGui import QAction
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QInputDialog, QMainWindow, QVBoxLayout, QFileDialog,
    QHBoxLayout, QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QWidget
)

from themes import apply_style
from dialogs import LogViewer, SysInfoDialog
from backup_restore_settings import base_window
from state import S, _HOME, _PROFILES_DIR, _PROFILE_RE, save_profile, logger, startup_load
from drive_utils import get_mount_output, is_mounted, unmount_drive, get_session_managed_mounts

RESTART_DIALOG = 2

def _main_btn(text: str, fn) -> QPushButton:
    btn = QPushButton(text)
    btn.setFixedHeight(60)
    btn.setStyleSheet("font-size:18px;font-weight:bold;")
    btn.clicked.connect(fn)
    return btn

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Backup Helper")
        self.setFixedSize(400, 400)
        self._quitting = False
        self.menu_actions = [
            ("💾 Create Backup", lambda: self._open(base_window, "Backup"), False),
            ("📤 Restore Backup", lambda: self._open(base_window, "Restore"), False),
            ("🖥 System Manager", lambda: __import__("system_manager_options").SystemManagerLauncher(self).launch(), False),
            ("💻 System Info", lambda: self._open(SysInfoDialog), True),
            ("📋 View Logs", lambda: self._open(LogViewer), False),
            ("⚙️ Settings", self._open_settings, False),
            ("❌ Quit", self._exit, False),
        ]
        self._build_ui()
        self._setup_tray()

    def _build_ui(self) -> None:
        central, layout = QWidget(), QVBoxLayout()
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)
        it = iter(self.menu_actions)
        for label, fn, pair in it:
            if pair:
                try:
                    l2, f2, _ = next(it)
                    h = QHBoxLayout()
                    h.setSpacing(10)
                    for lab, func in [(label, fn), (l2, f2)]: h.addWidget(_main_btn(lab, func))
                    layout.addLayout(h)
                    continue
                except StopIteration: pass
            layout.addWidget(_main_btn(label, fn))
        central.setLayout(layout)
        self.setCentralWidget(central)

    def _open(self, cls, *args, setup_fn=None) -> None:
        self.hide()
        while True:
            dlg = cls(self, *args) if args else cls(self)
            if setup_fn: setup_fn(dlg)
            if dlg.exec() != RESTART_DIALOG: break
        self.show()

    def _open_settings(self) -> None:
        self._open(base_window, "Settings", setup_fn=lambda d: d.changed.connect(apply_style))
        self._build_ui()

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable(): return
        self.tray = QSystemTrayIcon(QApplication.style().standardIcon(QApplication.style().StandardPixmap.SP_DriveHDIcon), self)
        self.tray.setToolTip("Backup Helper")
        menu = QMenu()
        act = QAction("🏠 Show Backup Helper", self)
        act.triggered.connect(self._show_and_raise)
        menu.addAction(act)
        menu.addSeparator()
        for label, fn, _ in self.menu_actions:
            a = QAction(label, self); a.triggered.connect(fn); menu.addAction(a)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self._show_and_raise() if r == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()

    def _show_and_raise(self) -> None:
        self.show(); self.raise_(); self.activateWindow()

    def _exit(self) -> None:
        if self._quitting: return
        out = get_mount_output()
        std = [o for o in S.mount_options if is_mounted(o, out)]
        mounted = std + [o for o in get_session_managed_mounts() if id(o) not in {id(x) for x in std}]
        unmountable = [o for o in mounted if o.get("unmount_command")]
        info_only = [o for o in mounted if not o.get("unmount_command")]

        if unmountable:
            lines = [f"  • {o.get('drive_name', '?')}" for o in unmountable]
            if info_only:
                lines += ["", "These drives have no unmount command and will be left mounted:",
                          *[f"  • {o.get('drive_name', '?')}" for o in info_only]]
            ans = QMessageBox.question(self, "Quit — Drives Still Mounted", "The following drives are still mounted:\n\n" +
                                       "\n".join(lines) + "\n\nUnmount before quitting?\n",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
            if ans == QMessageBox.StandardButton.Cancel: return
            if ans == QMessageBox.StandardButton.Yes:
                failed = [f"• {o.get('drive_name', '?')}: {err}" for o in unmountable for ok, err in [unmount_drive(o)] if not ok]
                if failed: QMessageBox.warning(self, "Unmount Failed", "Could not unmount:\n\n" + "\n".join(failed))
        else:
            msg = ("The following drives are still mounted but have no unmount command:\n" +
                   "\n".join(f"  • {o.get('drive_name', '?')}" for o in info_only) + "\n\nQuit anyway?") if info_only else "Really quit Backup Helper?"
            if QMessageBox.question(self, "Quit", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes: return

        self._quitting = True
        self.hide()
        if hasattr(self, "tray"): self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        event.accept() if self._quitting else (event.ignore(), self._exit())

    def keyPressEvent(self, event) -> None:
        key = event.key()
        widget = self.focusWidget()
        if key in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
            if isinstance(widget, QPushButton):
                widget.click()
        elif key == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

def _first_run_wizard(p) -> bool:
    msg = QMessageBox(p)
    msg.setWindowTitle("Welcome to Backup Helper")
    msg.setText("<b>No profile found.</b><br><br>Would you like to import an existing profile (.json)?")
    msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    msg.button(QMessageBox.StandardButton.Yes).setText("Import profile")
    msg.button(QMessageBox.StandardButton.No).setText("Create empty profile")

    if msg.exec() == QMessageBox.StandardButton.Yes:
        path, _ = QFileDialog.getOpenFileName(p, "Select profile", str(_HOME), "JSON (*.json)")
        while path:
            name, ok = QInputDialog.getText(p, "Profile name", "Name:", text=Path(path).stem)
            if not ok: break
            name = name.strip()
            if not name or not _PROFILE_RE.match(name):
                QMessageBox.warning(p, "Invalid Name", "Profile name may only contain letters, digits, spaces, hyphens and dots.")
                continue
            dest = _PROFILES_DIR / f"{name}.json"
            if dest.exists() and QMessageBox.warning(p, "Overwrite", f"Profile '{name}' already exists. Overwrite?",
                                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.No:
                continue
            try:
                _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                return startup_load()
            except OSError as e: QMessageBox.critical(p, "Import Failed", f"Could not copy profile:\n{e}")
    S.profile_name, S.headers, S.entries = "Default", {}, []
    save_profile()
    return True


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Backup Helper")
    def _ex(t, v, tb):
        if issubclass(t, KeyboardInterrupt):
            return sys.__excepthook__(t, v, tb)
        logger.critical("Uncaught exception", exc_info=(t, v, tb))
        QMessageBox.critical(None, "Critical Error",
                             f"An unexpected error occurred:\n\n{v}\n\nCheck the logs for details.")
        return None
    sys.excepthook = _ex
    has_p, win = startup_load(), MainWindow()
    apply_style()
    win.show()
    if not has_p:
        QTimer.singleShot(200, lambda: _first_run_wizard(win))
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
