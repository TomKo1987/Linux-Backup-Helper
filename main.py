import binascii
import sys

if sys.platform != "linux":
    print("This program can only be run on Linux.")
    sys.exit(1)

import base64 as _b64
import shutil
import threading
from pathlib import Path

from PyQt6.QtCore import QByteArray
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (
    QMenu, QMessageBox, QPushButton, QSystemTrayIcon, QWidget, QApplication,
    QInputDialog, QMainWindow, QGridLayout, QFileDialog, QVBoxLayout
)

from backup_stats import BackupStatsDialog
from dialogs import LogViewer, SysInfoDialog, NotesDialog
from drive_utils import get_mounts, is_mounted, unmount_drive, get_session_managed_mounts
from dry_run import DryRunDialog
from icons import _ICON_B64
from integrity_checker import IntegrityCheckerDialog
from scan_verify import ScanVerifyDialog
from state import S, _HOME, _PROFILES_DIR, _PROFILE_RE, RESTART_DIALOG, save_profile, logger, startup_load
from status_panel import StatusPanel
from themes import apply_style, register_style_listener, unregister_style_listener
from ui_utils import _StandardKeysMixin, ask_profile_name
from windows import base_window


def _make_icon() -> QIcon:
    try:
        raw = _b64.b64decode(_ICON_B64)
        pix = QPixmap()
        if not pix.loadFromData(QByteArray(raw), "PNG"):
            return QIcon()
        return QIcon(pix)
    except (binascii.Error, TypeError) as e:
        logger.debug("Icon could not be decoded: %s", e)
        return QIcon()


class MainWindow(_StandardKeysMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Backup Helper")
        self.setMinimumSize(550, 450)
        self._quitting = False

        self.menu_actions = [
            ("💾 Create Backup",  lambda: self._open(base_window, "Backup"),  False),
            ("📤 Restore Backup", lambda: self._open(base_window, "Restore"), False),
            ("🔎 Dry Run",        self._launch_dry_run,                             False),
            ("🖥 System Manager",  self._launch_system_manager,                      False),
            ("🔍 Scan && Verify",  self._launch_scan_verify,                        True),
            ("💻 System Info",     lambda: self._open(SysInfoDialog),               False),
            ("📊 Backup Stats",    self._open_stats,                                True),
            ("🔬 Integrity Check", self._open_integrity,                            False),
            ("📜 History",         self._open_history,                              True),
            ("📋 View Logs",       lambda: self._open(LogViewer),                   False),
            ("📝 Notes",           self._open_notes,                                True),
            ("⚙️ Settings",        self._open_settings,                             False),
            ("❌ Quit",             self._exit,                                      False),
        ]

        self._status_panel: StatusPanel | None = None

        self._build_ui()
        self._setup_tray()
        register_style_listener(self._build_ui)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_status_panel)
        self._refresh_timer.start(60_000)

    def _build_ui(self) -> None:
        central = QWidget()
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        btn_widget = QWidget()
        layout = QGridLayout(btn_widget)
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

        vbox.addWidget(btn_widget, 1)

        panel = StatusPanel()
        self._status_panel = panel
        vbox.addWidget(panel)

        self.setCentralWidget(central)
        panel.refresh()

    @staticmethod
    def _main_btn(text: str, fn) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("mainMenuBtn")
        btn.clicked.connect(fn)
        return btn

    def _open_notes(self) -> None:
        NotesDialog(self).show()

    def _refresh_status_panel(self) -> None:
        if self._status_panel is not None:
            try:
                self._status_panel.refresh()
            except Exception as exc:
                logger.debug("status panel refresh: %s", exc)

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
        self._refresh_status_panel()

    def _open_settings(self) -> None:
        self._open(base_window, "Settings", setup_fn=lambda d: d.changed.connect(apply_style))
        self._build_ui()
        if hasattr(self, "tray"):
            self._build_tray_menu()
        self._refresh_status_panel()

    def _launch_scan_verify(self) -> None:
        self._open(ScanVerifyDialog)

    def _open_history(self) -> None:
        from history import HistoryDialog
        HistoryDialog(self).exec()

    def _open_stats(self) -> None:
        BackupStatsDialog(self).exec()

    def _open_integrity(self) -> None:
        IntegrityCheckerDialog(self).exec()

    def _launch_dry_run(self) -> None:
        DryRunDialog(self).exec()

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
        self._build_tray_menu()
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _build_tray_menu(self) -> None:
        menu = QMenu()

        show_act = QAction("🏠 Show Backup Helper", self)
        show_act.triggered.connect(self._show_and_raise)
        menu.addAction(show_act)
        menu.addSeparator()

        headers = sorted({e.get("header", "") for e in S.entries if e.get("header")})
        if headers:
            quick_menu = QMenu("⚡ Quick Backup", menu)
            for hdr in headers:
                act = QAction(hdr, self)
                act.triggered.connect(lambda checked=False, h=hdr: self._quick_backup(h))
                quick_menu.addAction(act)
            all_act = QAction("▶ All groups", self)
            all_act.triggered.connect(lambda: self._quick_backup(None))
            quick_menu.addSeparator()
            quick_menu.addAction(all_act)
            menu.addMenu(quick_menu)
            menu.addSeparator()

        for label, fn, _ in self.menu_actions:
            act = QAction(label, self)
            act.triggered.connect(fn)
            menu.addAction(act)

        old_menu = self.tray.contextMenu()
        self.tray.setContextMenu(menu)
        if old_menu is not None:
            old_menu.deleteLater()

    def _quick_backup(self, header: str | None) -> None:
        from copy_worker import CopyDialog
        tasks = []
        for e in S.entries:
            details = e.get("details", {})
            if details.get("no_backup"):
                continue
            if header is not None and e.get("header") != header:
                continue
            tasks.append((
                e["source"], e["destination"], e["title"],
                details.get("exclude_paths", {}),
                details.get("pre_hooks", []),
                details.get("post_hooks", []),
            ))
        if not tasks:
            QMessageBox.information(None, "Quick Backup", "No backup entries found for this group.")
            return
        label = header or "All groups"
        CopyDialog(None, tasks, f"Quick Backup — {label}").exec()
        self._refresh_status_panel()

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

        mount_out   = get_mounts()
        all_mounted = [o for o in S.mount_options if is_mounted(o, mount_out)]
        known_names = {x.get("drive_name") for x in all_mounted if x.get("drive_name")}
        all_mounted.extend(
            o for o in get_session_managed_mounts()
            if o.get("drive_name") and o.get("drive_name") not in known_names
        )

        unmountable = [o for o in all_mounted if o.get("unmount_command")]
        info_only   = [o for o in all_mounted if not o.get("unmount_command")]

        def _name(o):
            return o.get("drive_name", "?")

        if unmountable:
            lines = [f"  • {_name(o)}" for o in unmountable]
            if info_only:
                lines += ["", "These drives have no unmount command and will be left mounted:"]
                lines += [f"  • {_name(o)}" for o in info_only]

            msg = "The following drives are still mounted:\n\n" + "\n".join(lines) + "\n\nUnmount before quitting?\n"
            ans = QMessageBox.question(
                self, "Quit — Drives Still Mounted", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            )
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
            if (QMessageBox.question(self, "Quit", msg,
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
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
            self._refresh_timer.stop()
            unregister_style_listener(self._build_ui)
            event.accept()
        else:
            event.ignore()
            self._exit()


def _first_run_wizard(parent) -> bool:
    msg = QMessageBox(parent)
    msg.setWindowTitle("Welcome to Backup Helper")
    msg.setText(
        "<b>No profile found.</b><br><br>"
        "How would you like to set up your profile?<br><br>"
        "<b>🔍 Scan System:</b> Detect installed packages on this machine and add them "
        "to a new profile automatically — the recommended way to get started.<br><br>"
        "<b>📥 Import Profile:</b> Load an existing <code>.json</code> profile from disk.<br><br>"
        "<b>➕ Start Empty:</b> Create a blank profile you can fill in manually later."
    )
    msg.setTextFormat(Qt.TextFormat.RichText)
    scan_btn   = msg.addButton("🔍 Scan System",    QMessageBox.ButtonRole.ActionRole)
    import_btn = msg.addButton("📥 Import Profile", QMessageBox.ButtonRole.ActionRole)
    _          = msg.addButton("➕ Start Empty",    QMessageBox.ButtonRole.RejectRole)
    msg.setDefaultButton(scan_btn)
    msg.exec()
    clicked = msg.clickedButton()

    if clicked == scan_btn:
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

        QMessageBox.information(
            parent, "System Scan — How It Works",
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
            "Click <b>OK</b> to start the scan."
        )
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
                ans = QMessageBox.warning(
                    parent, "Overwrite", f"Profile '{name}' already exists. Overwrite?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
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
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--headless-backup",     dest="headless_headers", default=None)
    parser.add_argument("--headless-backup-b64", dest="headless_b64",     default=None)
    args, _ = parser.parse_known_args()

    app = QApplication(sys.argv)
    app.setWindowIcon(_make_icon())
    app.setApplicationName("Backup Helper")

    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        try:
            QMessageBox.critical(None, "Critical Error",
                                 f"Unexpected error:\n\n{exc_value}\n\nCheck logs for details.")
        except RuntimeError as error:
            logger.error("Unable to display the GUI error dialog (RuntimeError): %s", error)
        except Exception as error:
            logger.error("Secondary error when displaying the dialog: %s", error)

    def _thread_excepthook(_args):
        if _args.exc_type and issubclass(_args.exc_type, KeyboardInterrupt):
            return
        logger.critical("Uncaught thread exception",
                        exc_info=(_args.exc_type, _args.exc_value, _args.exc_traceback))

    sys.excepthook    = _excepthook
    threading.excepthook = _thread_excepthook

    has_profile = startup_load()

    if args.headless_headers is not None or args.headless_b64 is not None:
        import json
        import base64 as b64_
        import binascii

        if args.headless_b64 is not None:
            try:
                headers = json.loads(b64_.b64decode(args.headless_b64).decode())
                if not isinstance(headers, list):
                    headers = []
            except (json.JSONDecodeError, ValueError, binascii.Error, UnicodeDecodeError):
                headers = []
        else:
            _raw = str(args.headless_headers)
            try:
                _parsed = json.loads(_raw)
                headers = _parsed if isinstance(_parsed, list) else []
            except (json.JSONDecodeError, ValueError):
                headers = [h.strip() for h in _raw.split(",") if h.strip()]

        from copy_worker import CopyDialog
        tasks = []
        for e in S.entries:
            details = e.get("details", {})
            if details.get("no_backup"):
                continue
            if headers and e.get("header") not in headers:
                continue
            tasks.append((
                e["source"], e["destination"], e["title"],
                details.get("exclude_paths", {}),
                details.get("pre_hooks", []),
                details.get("post_hooks", []),
            ))
        if tasks:
            CopyDialog(None, tasks, "Backup (scheduled)").exec()
        else:
            logger.warning("Headless backup: no matching tasks found (headers=%s)", headers)
        sys.exit(0)

    win = MainWindow()
    apply_style()
    win.show()
    if not has_profile:
        QTimer.singleShot(200, lambda: _first_run_wizard(win))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
