from PyQt6.QtCore import Qt, QThread
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget
)

from drive_utils import check_drives_to_mount, mount_required_drives
from linux_distro_helper import LinuxDistroHelper
from state import S, apply_replacements
from themes import current_theme, font_sz

from scan_verify_helpers import (
    _clean_title, _collect_verify_paths, _make_progress_widget, _Section, _VerifyWorker
)


class _VerifyTab(QWidget):
    def __init__(self, helper: LinuxDistroHelper) -> None:
        super().__init__()
        self._helper = helper
        self._worker: _VerifyWorker | None = None
        self._build_ui()
        self._start()

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        self._summary = QLabel()
        self._summary.setTextFormat(Qt.TextFormat.RichText)
        self._summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._summary.setStyleSheet(f"font-size:{font_sz(1)}px;font-weight:bold;padding:8px;border-radius:4px;")
        self._summary.hide()
        lay.addWidget(self._summary)

        self._prog_widget, self._prog_label, self._prog_bar = _make_progress_widget("Preparing…")
        lay.addWidget(self._prog_widget, 1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.hide()
        lay.addWidget(self._scroll, 1)

        refresh_btn = QPushButton("🔄  Re-run Check")
        refresh_btn.clicked.connect(self._start)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(refresh_btn)
        lay.addLayout(row)

    def _start(self) -> None:
        if isinstance(self._worker, QThread) and self._worker.isRunning():
            return
        self._scroll.hide()
        self._summary.hide()
        self._prog_widget.show()
        self._prog_label.setText("Preparing…")

        needed = check_drives_to_mount(_collect_verify_paths())
        if needed:
            if not mount_required_drives(needed, parent=self.window()):
                self._prog_widget.hide()
                t = current_theme()
                self._summary.setStyleSheet(f"font-size:{font_sz(1)}px;font-weight:bold;padding:8px;"
                                            f"border-radius:4px;background:{t['bg2']};color:{t['warning']};")
                self._summary.setText("⚠  Verify cancelled: required drive(s) not mounted.")
                self._summary.show()
                return

        worker = _VerifyWorker(self._helper)
        worker.progress.connect(self._prog_label.setText)
        worker.done.connect(self._on_done)
        worker.start()
        self._worker = worker

    def _on_done(self, res: dict) -> None:
        t = current_theme()
        self._prog_widget.hide()

        container = QWidget()
        cl = QVBoxLayout(container)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4)

        total_issues = 0

        pkgs = res.get("pkgs", [])
        if pkgs:
            n_ok = sum(1 for p in pkgs if p["installed"])
            n_bad = len(pkgs) - n_ok
            total_issues += n_bad
            sec = _Section("📦", "Packages", n_ok, len(pkgs), t["success"], t["error"])
            for p in pkgs:
                if p["installed"]:
                    sec.add_row("✅", p["name"], f"Installed  ({p['kind']})", t["success"])
                else:
                    sec.add_row("❌", p["name"], f"Missing  ({p['kind']})", t["error"])
            cl.addWidget(sec)
        elif not (S.basic_packages or S.aur_packages or S.specific_packages):
            sec = _Section("📦", "Packages", 0, 0, t["muted"], t["muted"])
            sec.add_row("—", "No packages in profile", "", t["muted"])
            cl.addWidget(sec)

        sys_files = res.get("sys_files", [])
        if sys_files:
            n_ok = sum(1 for f in sys_files if f["status"] == "ok")
            n_bad = len(sys_files) - n_ok
            total_issues += n_bad
            sec = _Section("📄", "Dotfiles", n_ok, len(sys_files), t["success"], t["warning"])
            _status_map = {"changed": ("⚠", "Changed", "warning"), "dst_missing": ("❌", "Not backed up", "error"),
                           "src_missing": ("❓", "Source missing", "muted")}
            for f in sys_files:
                if f["status"] == "ok":
                    sec.add_row("✅", f["name"], f"{apply_replacements(f['src'])} 🢥 {apply_replacements(f['dst'])}", t["success"])
                else:
                    ic, lbl, ck = _status_map.get(f["status"], ("?", f["status"], "text"))
                    sec.add_row(ic, f["name"], f"{lbl}  —  {apply_replacements(f['src'])} 🢥 {apply_replacements(f['dst'])}", t[ck])
            cl.addWidget(sec)
        else:
            sec = _Section("📄", "Dotfiles", 0, 0, t["muted"], t["muted"])
            msg = "No active dotfiles" if S.dotfiles else "No dotfiles in profile"
            sec.add_row("—", msg, "", t["muted"])
            cl.addWidget(sec)

        backups = res.get("backups", [])
        if backups:
            n_ok = sum(1 for b in backups if b["status"] == "ok")
            n_bad = len(backups) - n_ok
            total_issues += n_bad
            sec = _Section("💾", "Backup Entries", n_ok, len(backups), t["success"], t["warning"])
            for b in backups:
                title = _clean_title(b["title"])
                label = f"[{b['header']}]  {title}"
                if b["status"] == "ok":
                    sec.add_row("✅", label, "OK", t["success"])
                else:
                    sec.add_entry_group(label, b["issues"], t["warning"])
            cl.addWidget(sec)
        elif not S.entries:
            sec = _Section("💾", "Backup Entries", 0, 0, t["muted"], t["muted"])
            sec.add_row("—", "No backup entries in profile", "", t["muted"])
            cl.addWidget(sec)

        services = res.get("services", [])
        if services:
            n_ok = sum(1 for s in services if s["active"])
            n_bad = len(services) - n_ok
            total_issues += n_bad
            sec = _Section("⚙️", "Services", n_ok, len(services), t["success"], t["warning"])
            for s in services:
                if s["active"]:
                    sec.add_row("✅", f"{s['service']}.service", "Active", t["success"])
                else:
                    sec.add_row("⚠", f"{s['service']}.service", "Inactive", t["warning"])
            cl.addWidget(sec)
        elif S.system_manager_ops:
            sec = _Section("⚙️", "Services", 0, 0, t["muted"], t["muted"])
            sec.add_row("—", "No trackable services configured in profile", "", t["muted"])
            cl.addWidget(sec)
        else:
            sec = _Section("⚙️", "Services", 0, 0, t["muted"], t["muted"])
            sec.add_row("—", "No System Manager operations in profile", "", t["muted"])
            cl.addWidget(sec)

        cl.addStretch()
        self._scroll.setWidget(container)
        self._scroll.show()
        win = self.window()
        if hasattr(win, "fit_to_content"):
            win.fit_to_content()

        if total_issues == 0:
            self._summary.setStyleSheet(f"font-size:{font_sz(1)}px;font-weight:bold;padding:8px;"
                                        f"border-radius:4px;background:{t['bg2']};color:{t['success']};")
            self._summary.setText("✅  Everything looks good — system matches profile.")
        else:
            self._summary.setStyleSheet(f"font-size:{font_sz(1)}px;font-weight:bold;padding:8px;"
                                        f"border-radius:4px;background:{t['bg2']};color:{t['warning']};")
            self._summary.setText(f"⚠  {total_issues} issue(s) found — click section headers to expand details.")
        self._summary.show()
