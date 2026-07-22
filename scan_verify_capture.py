from pathlib import Path

from PyQt6.QtCore import Qt, QThread
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget
)

from drive_utils import check_drives_to_mount, mount_required_drives
from linux_distro_helper import LinuxDistroHelper, SESSIONS
from state import S, _HOME, save_profile, all_profile_pkg_names, sort_pkg_list, sort_specific_pkg_list, apply_replacements
from themes import current_theme, font_sz
from ui_utils import sep

from scan_verify_helpers import (
    _collect_verify_paths, _get_arch_de_deps, _get_sm_managed_packages,
    _make_progress_widget, _CaptureWorker, _IGNORE_EXACT, _IGNORE_PREFIXES
)


class _CaptureTab(QWidget):
    def __init__(self, helper: LinuxDistroHelper) -> None:
        super().__init__()
        self._helper = helper
        self._worker: _CaptureWorker | None = None
        self._cbs: list[tuple[QCheckBox, str, str]] = []
        self._svc_cbs: list[tuple[QCheckBox, str, str]] = []
        self._sm_pkgs = _get_sm_managed_packages(helper)
        self._de_deps = _get_arch_de_deps(helper)
        self._current_session = helper.detect_session() or SESSIONS[0]
        self._build_ui()
        self._spec_sess_cb: QComboBox | None = None
        self._spec_cbs: list = []
        self._start()

    def _build_ui(self) -> None:
        t = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        self._prog_widget, self._prog_label, self._prog_bar = _make_progress_widget("Scanning…")
        lay.addWidget(self._prog_widget, 1)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.hide()
        lay.addWidget(self._scroll, 1)

        self._btns = QWidget()
        self._btns.hide()
        br = QHBoxLayout(self._btns)
        br.setContentsMargins(0, 0, 0, 0)

        btn_row_w = QHBoxLayout()
        btn_row_w.addStretch()
        refresh_btn = QPushButton("🔄  Re-run Check")
        refresh_btn.clicked.connect(self._start)
        export_btn = QPushButton("💾  Export Report")
        export_btn.clicked.connect(self._export_report)
        btn_row_w.addWidget(export_btn)
        btn_row_w.addWidget(refresh_btn)
        lay.addLayout(btn_row_w)

        self._last_result: dict = {}

        self._sel_all_btn = QPushButton("Select All New")
        self._sel_all_btn.clicked.connect(self._select_all_new)

        self._add_btn = QPushButton("⬆  Add Selected to Profile")
        self._add_btn.setStyleSheet(f"font-weight:bold;color:{t['accent']};border-color:{t['accent']};")
        self._add_btn.clicked.connect(self._add_to_profile)

        br.addWidget(self._sel_all_btn)
        br.addStretch()
        br.addWidget(self._add_btn)
        lay.addWidget(self._btns)

    def _start(self) -> None:
        if isinstance(self._worker, QThread) and self._worker.isRunning():
            return
        self._prog_widget.show()
        self._scroll.hide()
        self._btns.hide()

        needed = check_drives_to_mount(_collect_verify_paths())
        if needed:
            if not mount_required_drives(needed, parent=self.window()):
                t = current_theme()
                self._prog_label.setText("⚠  Capture cancelled: required drive(s) not mounted.")
                self._prog_label.setStyleSheet(f"color:{t['warning']};font-weight:bold;")
                self._prog_bar.hide()
                return

        worker = _CaptureWorker(self._helper)
        worker.progress.connect(self._prog_label.setText)
        worker.done.connect(self._on_done)
        worker.start()
        self._worker = worker

    def _on_done(self, res: dict) -> None:
        t = current_theme()
        if res.get("error"):
            self._last_result = {}
            self._prog_label.setText(f"⚠  {res['error']}")
            self._prog_bar.hide()
            return
        self._last_result = res

        self._prog_widget.hide()
        self._cbs.clear()
        self._svc_cbs.clear()

        profile_all = all_profile_pkg_names()
        _excluded = self._sm_pkgs | _IGNORE_EXACT | self._de_deps
        new_basic = [p for p in res["basic"]
                     if p not in profile_all and p not in _excluded
                     and not any(p.startswith(pfx) for pfx in _IGNORE_PREFIXES)]
        new_aur   = [p for p in res["aur"]
                     if p not in profile_all and p not in _excluded
                     and not any(p.startswith(pfx) for pfx in _IGNORE_PREFIXES)]
        has_new_pkgs = bool(new_basic or new_aur)
        total = sum(1 for p in res["basic"] + res["aur"]
                    if p not in _excluded
                    and not any(p.startswith(pfx) for pfx in _IGNORE_PREFIXES))
        already = total - len(new_basic) - len(new_aur)

        installed_basic_set = res.get("profile_installed_basic", set())
        installed_aur_set = res.get("profile_installed_aur", set())

        container = QWidget()
        cl = QVBoxLayout(container)
        cl.setContentsMargins(8, 8, 8, 8)
        cl.setSpacing(12)

        summary = QLabel(f"<b>{total}</b> packages detected  •  "
                         f"<span style='color:{t['success']};'><b>{already}</b> already in profile</span>  •  "
                         f"<span style='color:{t['accent']};'><b>{len(new_basic) + len(new_aur)}</b> new</span>")
        summary.setTextFormat(Qt.TextFormat.RichText)
        summary.setStyleSheet(f"font-size:{font_sz()}px; padding:4px 2px;")

        if not has_new_pkgs:
            ok_lbl = QLabel("✅  All packages from this system are already in the profile.")
            ok_lbl.setStyleSheet(f"color:{t['success']}; font-weight:bold; padding:4px;")
            ok_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(ok_lbl)

        cl.addWidget(summary)

        if has_new_pkgs:
            cl.addWidget(sep())
            for title, items, kind in [("New Basic Packages", new_basic, "basic"), ("New AUR Packages", new_aur, "aur")]:
                if not items:
                    continue
                hdr_lbl = QLabel(f"<b>{title}</b>  "
                                 f"<span style='color:{t['accent']}; background:{t['bg3']}; "
                                 f"padding:1px 8px; border-radius:10px; font-size:{font_sz(-1)}px;'>"
                                 f"{len(items)} new</span>")
                hdr_lbl.setTextFormat(Qt.TextFormat.RichText)
                hdr_lbl.setStyleSheet(f"font-size:{font_sz(1)}px; padding:2px 0;")
                cl.addWidget(hdr_lbl)
                grid = QWidget()
                gl = QGridLayout(grid)
                gl.setContentsMargins(8, 2, 8, 4)
                gl.setSpacing(4)
                cols = 4
                for i, name in enumerate(items):
                    cb = QCheckBox(name)
                    cb.setChecked(False)
                    gl.addWidget(cb, i // cols, i % cols)
                    self._cbs.append((cb, name, kind))
                cl.addWidget(grid)

        active_basic = [
            {"name": p.get("name", ""), "installed": p.get("name", "") in installed_basic_set}
            for p in S.basic_packages if isinstance(p, dict) and not p.get("disabled") and p.get("name")]
        self._add_profile_pkg_grid(cl, t, "Basic Packages (Profile)", active_basic, t["accent"])

        active_aur = [{"name": p.get("name", ""), "installed": p.get("name", "") in installed_aur_set}
                      for p in S.aur_packages if isinstance(p, dict) and not p.get("disabled") and p.get("name")]
        self._add_profile_pkg_grid(cl, t, "AUR Packages (Profile)", active_aur, t["accent2"])

        specific = res.get("specific", [])
        self._add_profile_pkg_grid(cl, t, "Specific Packages (Profile)", specific, t["accent2"], cols=3,
                                   suffix_fn=lambda p: f" [{p['session']}]" if p.get("session") else "")

        cl.addWidget(sep())
        cl.addWidget(self._build_specific_section(res["basic"], _excluded, t))

        sys_files = res.get("sys_files", [])
        if sys_files:
            cl.addWidget(sep())
            n_ok = sum(1 for f in sys_files if f["status"] == "ok")
            n_bad = len(sys_files) - n_ok
            color = t["error"] if n_bad else t["success"]
            hdr = QLabel(f"<b>📄  Dotfiles</b>  —  "
                         f"<span style='color:{color};'>{n_ok}/{len(sys_files)} up to date</span>")
            hdr.setTextFormat(Qt.TextFormat.RichText)
            hdr.setStyleSheet(f"font-size:{font_sz(1)}px; padding:2px 0;")
            cl.addWidget(hdr)
            _status_map = {"changed": ("⚠", "Changed", "warning"), "dst_missing": ("❌", "Not backed up", "error"),
                           "src_missing": ("❓", "Source missing", "muted")}
            sf_frame = QWidget()
            sf_vl = QVBoxLayout(sf_frame)
            sf_vl.setContentsMargins(0, 0, 0, 0)
            sf_vl.setSpacing(0)
            for i, f in enumerate(sys_files):
                if f["status"] == "ok":
                    icon, label, ck = "✅", "OK", "success"
                else:
                    icon, label, ck = _status_map.get(f["status"], ("?", f["status"], "text"))
                row_w = QWidget()
                row_w.setStyleSheet(f"background:{t['bg3'] if i % 2 == 0 else 'transparent'};")
                row_lay = QHBoxLayout(row_w)
                row_lay.setContentsMargins(8, 5, 8, 5)
                row_lay.setSpacing(10)
                ic_lbl = QLabel(icon)
                ic_lbl.setMinimumWidth(30)
                ic_lbl.setStyleSheet("background:transparent;")
                name_lbl = QLabel(f"<b>{f['name']}</b>")
                name_lbl.setTextFormat(Qt.TextFormat.RichText)
                name_lbl.setMinimumWidth(250)
                name_lbl.setStyleSheet(f"color:{t['text']};font-family:monospace;background:transparent;")
                path_lbl = QLabel(f"{apply_replacements(f['src'])} 🢥 {apply_replacements(f['dst'])}")
                path_lbl.setStyleSheet(f"color:{t['muted']};font-size:{font_sz(-2)}px;"
                                       f"font-family:monospace;background:transparent;")
                path_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                status_lbl = QLabel(f"[{label}]")
                status_lbl.setStyleSheet(f"color:{t[ck]};font-size:{font_sz(-1)}px;"
                                         f"font-weight:bold;background:transparent;")
                status_lbl.setMinimumWidth(100)
                status_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                row_lay.addWidget(ic_lbl)
                row_lay.addWidget(name_lbl)
                row_lay.addWidget(path_lbl)
                row_lay.addWidget(status_lbl)
                sf_vl.addWidget(row_w)
            cl.addWidget(sf_frame)
        elif S.dotfiles:
            cl.addWidget(sep())
            info = QLabel("📄  <i>No active dotfiles configured.</i>")
            info.setTextFormat(Qt.TextFormat.RichText)
            info.setStyleSheet(f"color:{t['muted']};")
            cl.addWidget(info)

        services = res.get("services", [])
        if services:
            cl.addWidget(sep())
            cl.addWidget(self._build_service_section(services, t))
            cl.addWidget(sep())

        cl.addStretch()
        self._scroll.setWidget(container)
        self._scroll.show()
        win = self.window()
        if hasattr(win, "fit_to_content"):
            win.fit_to_content()
        self._sel_all_btn.setVisible(has_new_pkgs)
        self._add_btn.setVisible(has_new_pkgs)
        if has_new_pkgs:
            self._btns.show()
        else:
            self._btns.hide()

    @staticmethod
    def _add_profile_pkg_grid(cl: QVBoxLayout, t: dict, title: str, pkgs: list[dict], accent: str,
                              cols: int = 4, suffix_fn=None) -> None:
        if not pkgs:
            return
        cl.addWidget(sep())
        n_ok = sum(1 for p in pkgs if p["installed"])
        n_bad = len(pkgs) - n_ok
        color = t["error"] if n_bad else t["success"]
        hdr = QLabel(f"<b>{title}</b>  —  <span style='color:{color};'>{n_ok}/{len(pkgs)} installed</span>")
        hdr.setTextFormat(Qt.TextFormat.RichText)
        hdr.setStyleSheet(f"font-size:{font_sz(1)}px; color:{accent}; padding:2px 0;")
        cl.addWidget(hdr)
        grid = QWidget()
        gl = QGridLayout(grid)
        gl.setContentsMargins(8, 2, 8, 4)
        gl.setSpacing(4)
        for i, p in enumerate(pkgs):
            suffix = suffix_fn(p) if suffix_fn else ""
            lbl = QLabel(f"{'✅' if p['installed'] else '❌'}  {p['name']}"
                        + (f"<span style='color:{t['muted']}; font-size:{font_sz(-2)}px;'>{suffix}</span>" if suffix else ""))
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setStyleSheet(f"color:{t['success'] if p['installed'] else t['error']};")
            gl.addWidget(lbl, i // cols, i % cols)
        cl.addWidget(grid)

    def _build_specific_section(self, all_basic: list[str], excluded: frozenset, t: dict) -> QWidget:
        wrapper = QWidget()
        vl = QVBoxLayout(wrapper)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(4)

        hdr = QLabel("<b>Mark as Specific Package</b> — install only for a certain session")
        hdr.setTextFormat(Qt.TextFormat.RichText)
        hdr.setStyleSheet(f"font-size:{font_sz(1)}px;color:{t['accent2']};")
        vl.addWidget(hdr)

        hint = QLabel("Select packages below and choose a session. They will be added to <i>Specific Packages</i> and "
                      "removed from Basic Packages if present.")
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{t['muted']};font-size:{font_sz(-1)}px;")
        vl.addWidget(hint)

        sess_row = QHBoxLayout()
        sess_row.addWidget(QLabel("Target session:"))
        spec_sess_cb = QComboBox()
        spec_sess_cb.addItems(SESSIONS)
        if self._current_session in SESSIONS:
            spec_sess_cb.setCurrentText(self._current_session)
        self._spec_sess_cb = spec_sess_cb
        sess_row.addWidget(spec_sess_cb)
        sess_row.addStretch()
        vl.addLayout(sess_row)

        existing_specific = {p.get("package", "") for p in S.specific_packages if isinstance(p, dict)}
        existing_aur = {p.get("name", "") for p in S.aur_packages if isinstance(p, dict)}

        eligible = [p for p in all_basic
                    if p not in excluded
                    and not any(p.startswith(pfx) for pfx in _IGNORE_PREFIXES)
                    and p not in existing_specific
                    and p not in existing_aur]

        if not eligible:
            no_lbl = QLabel("— No eligible packages found —")
            no_lbl.setStyleSheet(f"color:{t['muted']};")
            vl.addWidget(no_lbl)
            return wrapper

        search = QLineEdit()
        search.setPlaceholderText("Filter packages…")
        search.setMaximumWidth(300)
        vl.addWidget(search)

        grid_w = QWidget()
        spec_grid = QGridLayout(grid_w)
        spec_grid.setContentsMargins(8, 2, 8, 2)
        spec_grid.setSpacing(3)
        self._spec_cbs: list[tuple[QCheckBox, str]] = []
        cols = 4
        for i, name in enumerate(eligible):
            cb = QCheckBox(name)
            cb.setChecked(False)
            spec_grid.addWidget(cb, i // cols, i % cols)
            self._spec_cbs.append((cb, name))
        vl.addWidget(grid_w)

        def _filter(text: str) -> None:
            lo = text.lower()
            for _cb, _name in self._spec_cbs:
                _cb.setVisible(not lo or lo in _name.lower())

        search.textChanged.connect(_filter)

        add_spec_btn = QPushButton("⬆  Add Selected as Specific")
        add_spec_btn.clicked.connect(self._add_specific_to_profile)
        btn_row_w = QHBoxLayout()
        btn_row_w.addStretch()
        btn_row_w.addWidget(add_spec_btn)
        vl.addLayout(btn_row_w)

        return wrapper

    def _build_service_section(self, services: list[dict], t: dict) -> QWidget:
        wrapper = QWidget()
        vl = QVBoxLayout(wrapper)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(4)

        hdr = QLabel("<b>⚙️  System Services</b> — enable via System Manager Operations")
        hdr.setTextFormat(Qt.TextFormat.RichText)
        hdr.setStyleSheet(f"font-size:{font_sz(1)}px;color:{t['accent2']};")
        vl.addWidget(hdr)

        hint = QLabel("Active services are ticked. Check services you want to add to the profile (System Manager Operations)."
                      "<br>Services already in your profile are shown with a ✓ in profile badge.")
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{t['muted']};font-size:{font_sz(-1)}px;")
        vl.addWidget(hint)

        _OP_LABELS: dict[str, str] = {"enable_bluetooth_service": "Bluetooth",
                                      "enable_atd_service": "atd (at-daemon)",
                                      "enable_firewall": "Firewall (ufw)",
                                      "enable_printer_support": "Printer (CUPS)",
                                      "enable_ssh_service": "SSH server",
                                      "enable_samba_network_filesharing": "Samba (file sharing)",
                                      "enable_cronie_service": "Cron (cronie/cron)",
                                      "install_snap": "Snapd",
                                      "enable_flatpak_integration": "Flatpak"}

        svc_frame = QWidget()
        svc_vl = QVBoxLayout(svc_frame)
        svc_vl.setContentsMargins(0, 0, 0, 0)
        svc_vl.setSpacing(0)

        for i, svc_info in enumerate(services):
            op    = svc_info["op"]
            svc   = svc_info["service"]
            active     = svc_info["active"]
            in_profile = svc_info["in_profile"]

            label       = _OP_LABELS.get(op, op)
            active_icon = "✅" if active else "⬜"

            row_w = QWidget()
            row_w.setStyleSheet(f"background:{t['bg3'] if i % 2 == 0 else 'transparent'};")
            row_lay = QHBoxLayout(row_w)
            row_lay.setContentsMargins(8, 6, 8, 6)
            row_lay.setSpacing(10)

            ic_lbl = QLabel(active_icon)
            ic_lbl.setMinimumWidth(30)
            ic_lbl.setStyleSheet("background:transparent;")

            if in_profile:
                name_lbl = QLabel(f"<b>{label}</b>")
                name_lbl.setTextFormat(Qt.TextFormat.RichText)
                name_lbl.setMinimumWidth(250)
                name_lbl.setStyleSheet(f"color:{t['text']};background:transparent;")
                svc_lbl = QLabel(f"({svc}.service)")
                svc_lbl.setStyleSheet(f"color:{t['muted']};font-size:{font_sz(-2)}px;"
                                      f"font-family:monospace;background:transparent;")
                svc_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                badge = QLabel("✓ in profile")
                badge.setStyleSheet(f"color:{t['success']};font-size:{font_sz(-2)}px;"
                                    f"background:{t['bg2']};padding:1px 6px;border-radius:3px;")
                row_lay.addWidget(ic_lbl)
                row_lay.addWidget(name_lbl)
                row_lay.addWidget(svc_lbl)
                row_lay.addWidget(badge)
            else:
                cb = QCheckBox(label)
                cb.setChecked(active)
                cb.setMinimumWidth(250)
                cb.setStyleSheet(f"color:{t['success'] if active else t['muted']};background:transparent;")
                cb.setToolTip(f"{svc}.service is currently {'active' if active else 'inactive'}")
                svc_lbl = QLabel(f"({svc}.service)")
                svc_lbl.setStyleSheet(f"color:{t['muted']};font-size:{font_sz(-2)}px;"
                                      f"font-family:monospace;background:transparent;")
                svc_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                row_lay.addWidget(ic_lbl)
                row_lay.addWidget(cb)
                row_lay.addWidget(svc_lbl)
                self._svc_cbs.append((cb, op, svc))

            svc_vl.addWidget(row_w)

        vl.addWidget(svc_frame)

        add_svc_btn = QPushButton("⬆  Add Selected Services to Profile")
        add_svc_btn.clicked.connect(self._add_services_to_profile)
        btn_row_w = QHBoxLayout()
        btn_row_w.addStretch()
        btn_row_w.addWidget(add_svc_btn)
        vl.addLayout(btn_row_w)

        return wrapper

    def _select_all_new(self) -> None:
        for cb, _, _ in self._cbs:
            if cb.isEnabled():
                cb.setChecked(True)

    def _export_report(self) -> None:
        from datetime import datetime

        res = self._last_result
        if not res:
            QMessageBox.information(self.window(), "No Data", "Run a check first.")
            return

        lines: list[str] = ["Backup Helper — Capture Report",
                            f"Profile : {S.profile_name or '—'}",
                            f"Date    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                            "=" * 60]

        def _section(title: str, rows: list[tuple[str, str]]) -> None:
            lines.append(f"\n[{title}]")
            if not rows:
                lines.append("  —")
                return
            for _status, detail in rows:
                lines.append(f"  {_status}  {detail}")

        profile_all = all_profile_pkg_names()
        _excluded = self._sm_pkgs | _IGNORE_EXACT | self._de_deps
        basic_pkgs = res.get("basic", [])
        aur_pkgs = res.get("aur", [])
        pkg_rows: list[tuple[str, str]] = []
        for name in basic_pkgs:
            if name in _excluded or any(name.startswith(p) for p in _IGNORE_PREFIXES):
                continue
            status = "✓" if name in profile_all else "new"
            pkg_rows.append((status, f"{name} (basic)"))
        for name in aur_pkgs:
            if name in _excluded or any(name.startswith(p) for p in _IGNORE_PREFIXES):
                continue
            status = "✓" if name in profile_all else "new"
            pkg_rows.append((status, f"{name} (aur)"))
        for p in res.get("specific", []):
            session = p.get("session") or "—"
            pkg_rows.append(("✓" if p["installed"] else "✗", f"{p['name']} (specific / {session})"))
        _section("Packages", pkg_rows)

        _status_labels = {"ok": "OK", "changed": "Changed", "dst_missing": "Not backed up",
                          "src_missing": "Source missing"}
        _section("Dotfiles", [("✓" if f["status"] == "ok" else "⚠",
                                   f"{f['name']}  [{_status_labels.get(f['status'], f['status'])}]  {f['src']} 🢥 {f['dst']}")
                                  for f in res.get("sys_files", [])])

        services = res.get("services", [])
        _section("Services", [("✓" if s["active"] else "⚠", f"{s['service']}.service ({s['op']})") for s in services])

        text = "\n".join(lines)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(self.window(), "Export Capture Report",
                                              str(_HOME / f"capture_report_{ts}.txt"), "Text (*.txt)")
        if not path:
            return
        try:
            Path(path).write_text(text, encoding="utf-8")
            QMessageBox.information(self.window(), "Exported", f"Report saved to:\n{path}")
        except OSError as exc:
            QMessageBox.critical(self.window(), "Export Failed", str(exc))

    def _add_to_profile(self) -> None:
        new_basic = [n for cb, n, k in self._cbs if cb.isChecked() and cb.isEnabled() and k == "basic"]
        new_aur = [n for cb, n, k in self._cbs if cb.isChecked() and cb.isEnabled() and k == "aur"]
        if not new_basic and not new_aur:
            QMessageBox.information(self.window(), "Nothing Selected", "No packages selected.")
            return

        existing_basic = {p.get("name", "") for p in S.basic_packages if isinstance(p, dict) and p.get("name")}
        existing_aur = {p.get("name", "") for p in S.aur_packages if isinstance(p, dict) and p.get("name")}

        added = 0
        for name in new_basic:
            if name not in existing_basic:
                S.basic_packages.append({"name": name, "disabled": False})
                added += 1
        for name in new_aur:
            if name not in existing_aur:
                S.aur_packages.append({"name": name, "disabled": False})
                added += 1

        sort_pkg_list(S.basic_packages)
        sort_pkg_list(S.aur_packages)

        if save_profile():
            for cb, _, _ in self._cbs:
                if cb.isChecked() and cb.isEnabled():
                    cb.setText(cb.text() + "  ✓")
                    cb.setEnabled(False)
            QMessageBox.information(self.window(), "Done", f"{added} package(s) added to profile and saved.")
        else:
            QMessageBox.warning(self.window(), "Save Failed", "Could not save profile.")

    def _add_specific_to_profile(self) -> None:
        spec_sess_cb = self._spec_sess_cb
        if spec_sess_cb is None or not self._spec_cbs:
            return

        session = spec_sess_cb.currentText()
        selected = [name for cb, name in self._spec_cbs if cb.isChecked() and cb.isEnabled()]

        if not selected:
            QMessageBox.information(self.window(), "Nothing Selected", "No packages selected for Specific Packages.")
            return

        existing_specific = {p.get("package", "") for p in S.specific_packages if isinstance(p, dict)}
        added = 0
        for name in selected:
            if name not in existing_specific:
                S.specific_packages.append({"package": name, "session": session, "disabled": False})
                S.basic_packages = [p for p in S.basic_packages if p.get("name") != name]
                added += 1

        sort_specific_pkg_list(S.specific_packages)

        if save_profile():
            for cb, name in self._spec_cbs:
                if cb.isChecked() and cb.isEnabled() and name in selected:
                    cb.setText(cb.text() + f"  ✓ [{session}]")
                    cb.setEnabled(False)
            QMessageBox.information(self.window(), "Done",
                                    f"{added} package(s) added as Specific for session '{session}'.")
        else:
            QMessageBox.warning(self.window(), "Save Failed", "Could not save profile.")

    def _add_services_to_profile(self) -> None:
        selected_ops = [op for cb, op, _svc in self._svc_cbs if cb.isChecked() and cb.isEnabled()]
        if not selected_ops:
            QMessageBox.information(self.window(), "Nothing Selected", "No services selected.")
            return
        existing = set(S.system_manager_ops)
        added = 0
        for op in selected_ops:
            if op not in existing:
                S.system_manager_ops.append(op)
                added += 1
        if save_profile():
            for cb, op, _svc in self._svc_cbs:
                if cb.isChecked() and cb.isEnabled() and op in selected_ops:
                    cb.setText(cb.text() + "  ✓")
                    cb.setEnabled(False)
            QMessageBox.information(self.window(), "Done",
                                    f"{added} service operation(s) added to System Manager profile.")
        else:
            QMessageBox.warning(self.window(), "Save Failed", "Could not save profile.")
