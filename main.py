import shutil
import subprocess
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QWidget,
    QApplication, QInputDialog, QMainWindow, QGridLayout, QFileDialog
)

from scan_verify import ScanVerifyDialog
from dialogs import LogViewer, SysInfoDialog
from drive_utils import get_mounts, is_mounted, unmount_drive, get_session_managed_mounts
from state import S, _HOME, _PROFILES_DIR, _PROFILE_RE, RESTART_DIALOG, save_profile, logger, startup_load
from themes import apply_style, register_style_listener, unregister_style_listener
from ui_utils import _StandardKeysMixin
from windows import base_window


def _notify(title: str, body: str, urgency: str = "normal") -> None:
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.Popen(["notify-send", f"--urgency={urgency}", "--app-name=Backup Helper", "--icon=drive-harddisk",
                          title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


class MainWindow(_StandardKeysMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Backup Helper")
        self.setMinimumSize(425, 400)
        self.sm_failed_attempts = 0
        self._quitting = False

        self.menu_actions = [("💾 Create Backup", lambda: self._open(base_window, "Backup"), False),
                             ("📤 Restore Backup", lambda: self._open(base_window, "Restore"), False),
                             ("🖥 System Manager", self._launch_system_manager, False),
                             ("🔍 Scan && Verify", self._launch_scan_verify, True), ("💻 System Info", lambda: self._open(SysInfoDialog), False),
                             ("📜 History", self._open_history, True), ("📋 View Logs", lambda: self._open(LogViewer), False),
                             ("⚙️ Settings", self._open_settings, False),
                             ("❌ Quit", self._exit, False)]

        self._build_ui()
        self._setup_tray()
        register_style_listener(self._build_ui)

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QGridLayout(central)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)
        row = 0
        it = iter(self.menu_actions)
        for label, fn, pair in it:
            btn1 = self._main_btn(label, fn)
            if pair:
                try:
                    l2, f2, _ = next(it)
                    layout.addWidget(btn1, row, 0)
                    layout.addWidget(self._main_btn(l2, f2), row, 1)
                    row += 1
                    continue
                except StopIteration:
                    pass
            layout.addWidget(btn1, row, 0, 1, 2)
            row += 1
        self.setCentralWidget(central)

    @staticmethod
    def _main_btn(text: str, fn) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("mainMenuBtn")
        btn.clicked.connect(fn)
        return btn

    def _open(self, cls, *args, setup_fn=None) -> None:
        self.hide()
        while True:
            dlg = cls(self, *args)
            if setup_fn:
                setup_fn(dlg)
            result = dlg.exec()
            if result != RESTART_DIALOG:
                break
        self.show()

        if cls is base_window and args and args[0] in ("Backup", "Restore"):
            op = args[0]
            if getattr(dlg, "_quitting", False):
                return
            errors = getattr(dlg, "_last_errors", 0)
            copied = getattr(dlg, "_last_copied", 0)
            if errors:
                _notify(f"{op} completed with errors", f"{copied} files copied, {errors} error(s)", urgency="critical")
            elif copied > 0:
                _notify(f"{op} completed successfully", f"{copied} file(s) copied", urgency="normal")

    def _open_settings(self) -> None:
        self._open(base_window, "Settings", setup_fn=lambda d: d.changed.connect(apply_style))
        self._build_ui()

    def _launch_scan_verify(self) -> None:
        self._open(ScanVerifyDialog)

    def _open_history(self) -> None:
        from history import HistoryDialog
        HistoryDialog(self).exec()

    def _launch_system_manager(self) -> None:
        from system_manager_options import SystemManagerLauncher
        SystemManagerLauncher(self).launch()

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        from PyQt6.QtGui import QIcon
        from PyQt6.QtWidgets import QStyle
        _style = QApplication.style()
        icon = _style.standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon) if _style else QIcon()
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Backup Helper")
        menu = QMenu()
        show_act = QAction("🏠 Show Backup Helper", self)
        show_act.triggered.connect(self._show_and_raise)
        menu.addAction(show_act)
        menu.addSeparator()
        for label, fn, _ in self.menu_actions:
            act = QAction(label, self)
            act.triggered.connect(fn)
            menu.addAction(act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, r: QSystemTrayIcon.ActivationReason) -> None:
        if r == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_and_raise()

    def _show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _exit(self) -> None:
        if self._quitting:
            return

        mount_out = get_mounts()
        all_mounted = [o for o in S.mount_options if is_mounted(o, mount_out)]
        known_names = {x.get("drive_name") for x in all_mounted if x.get("drive_name")}
        all_mounted.extend(o for o in get_session_managed_mounts() if o.get("drive_name") and o.get("drive_name") not in known_names)

        unmountable = [o for o in all_mounted if o.get("unmount_command")]
        info_only = [o for o in all_mounted if not o.get("unmount_command")]

        def _name(o):
            return o.get("drive_name", "?")

        if unmountable:
            lines = [f"  • {_name(o)}" for o in unmountable]
            if info_only:
                lines += ["", "These drives have no unmount command and will be left mounted:"]
                lines += [f"  • {_name(o)}" for o in info_only]

            msg = "The following drives are still mounted:\n\n" + "\n".join(lines) + "\n\nUnmount before quitting?\n"
            ans = QMessageBox.question(self, "Quit — Drives Still Mounted", msg,
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
                                       QMessageBox.StandardButton.Cancel)

            if ans == QMessageBox.StandardButton.Cancel:
                return
            if ans == QMessageBox.StandardButton.Yes:
                failed = []
                for o in unmountable:
                    success, err_msg = unmount_drive(o)
                    if not success:
                        failed.append(f"• {_name(o)}: {err_msg}")
                if failed:
                    QMessageBox.warning(self, "Unmount Failed", "Could not unmount:\n\n" + "\n".join(failed))
                    return
        elif info_only:
            msg = "The following drives are still mounted but have no unmount command:\n"
            msg += "\n".join(f"  • {_name(o)}" for o in info_only) + "\n\nQuit anyway?"
            if (QMessageBox.question(self, "Quit", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            != QMessageBox.StandardButton.Yes):
                return
        else:
            if (QMessageBox.question(self, "Quit", "Really quit Backup Helper?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    != QMessageBox.StandardButton.Yes):
                return

        self._quitting = True
        QApplication.quit()

    def closeEvent(self, event) -> None:
        if self._quitting:
            unregister_style_listener(self._build_ui)
            event.accept()
        else:
            event.ignore()
            self._exit()


def _first_run_wizard(parent) -> bool:
    msg = QMessageBox(parent)
    msg.setWindowTitle("Welcome to Backup Helper")
    msg.setText("<b>No profile found.</b><br><br>"
                "How would you like to set up your profile?<br><br>"
                "<b>🔍 Scan System:</b> Detect installed packages on this machine and add them "
                "to a new profile automatically — the recommended way to get started.<br><br>"
                "<b>📥 Import Profile:</b> Load an existing <code>.json</code> profile from disk.<br><br>"
                "<b>➕ Start Empty:</b> Create a blank profile you can fill in manually later.")

    msg.setTextFormat(Qt.TextFormat.RichText)
    scan_btn   = msg.addButton("🔍 Scan System",    QMessageBox.ButtonRole.ActionRole)
    import_btn = msg.addButton("📥 Import Profile", QMessageBox.ButtonRole.ActionRole)
    _empty_btn = msg.addButton("➕ Start Empty",    QMessageBox.ButtonRole.RejectRole)
    msg.setDefaultButton(scan_btn)
    msg.exec()
    clicked = msg.clickedButton()

    if clicked == scan_btn:
        from ui_utils import ask_profile_name
        name = ask_profile_name("New Profile Name", "Default", parent)
        if not name:
            S.reset_to_fresh()
            S.profile_name = "Default"
            save_profile()
            return True

        _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
        S.reset_to_fresh()
        S.profile_name = name
        save_profile()

        QMessageBox.information(parent, "System Scan — How It Works",
                                "<b>System Capture will now open.</b><br><br>"
                                "The <b>System Capture</b> tab scans your installed packages and active services "
                                "and shows what is not yet tracked in your profile.<br><br>"
                                "<b>Packages:</b> Select new packages and click <b>Add Selected to Profile</b>.<br>"
                                "<b>Specific Packages:</b> Mark packages that should only be installed for a "
                                "certain desktop session (e.g. KDE-only or Hyprland-only packages).<br>"
                                "<b>Services:</b> Active system services (SSH, Samba, Bluetooth …) are listed. "
                                "Check any you want System Manager to handle, then click "
                                "<b>Add Selected Services to Profile</b>.<br><br>"
                                "The <b>Verify Profile</b> tab checks that all paths, services and packages "
                                "defined in your profile actually exist on this system.<br><br>"
                                "<b>Tips:</b><br>"
                                "• Use <i>Select All New</i> to add all packages at once, then deselect "
                                "what you don't need (e.g. temporary build tools).<br>"
                                "• System-critical packages (kernel, base, firmware) are excluded automatically.<br>"
                                "• You can re-open Capture &amp; Verify at any time from the main menu.<br><br>"
                                "Click <b>OK</b> to start the scan.")

        ScanVerifyDialog(parent).exec()
        return True

    if clicked == import_btn:
        path, _ = QFileDialog.getOpenFileName(parent, "Select profile", str(_HOME), "JSON (*.json)")
        while path:
            name, ok = QInputDialog.getText(parent, "Profile name", "Name:", text=Path(path).stem)
            if not ok:
                break
            name = name.strip()
            if not name or not _PROFILE_RE.match(name):
                QMessageBox.warning(parent, "Invalid profile name",
                                    "Name may only contain letters, digits, spaces, hyphens, underscores and dots.")
                continue
            dest = _PROFILES_DIR / f"{name}.json"
            if dest.exists():
                ans = QMessageBox.warning(parent, "Overwrite", f"Profile '{name}' already exists. Overwrite?",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if ans == QMessageBox.StandardButton.No:
                    continue
            try:
                _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                return startup_load()
            except OSError as e:
                QMessageBox.critical(parent, "Import Failed", f"Could not copy profile:\n{e}")
                break

    S.reset_to_fresh()
    S.profile_name = "Default"
    save_profile()
    return True


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Backup Helper")

    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        QMessageBox.critical(None, "Critical Error", f"Unexpected error:\n\n{exc_value}\n\nCheck logs for details.")

    def _thread_excepthook(args):
        if args.exc_type and issubclass(args.exc_type, KeyboardInterrupt):
            return
        logger.critical("Uncaught thread exception", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    has_profile = startup_load()
    win = MainWindow()
    apply_style()
    win.show()
    if not has_profile:
        QTimer.singleShot(200, lambda: _first_run_wizard(win))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
