from pathlib import Path

from PyQt6.QtCore    import Qt, QTimer
from PyQt6.QtWidgets import (
    QFormLayout, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget
)

from linux_distro_helper import LinuxDistroHelper, SESSIONS, USER_SHELLS
from state import S, _HOME, apply_replacements, generate_tooltip, save_profile
from themes import (
    current_theme, style_checkbox_muted, style_checkbox_select_all, style_label_info,
    style_label_info_bold, style_label_mono, style_op_label, style_sudo_checkbox, tri_styles
)

_STATE_ACTIVE   = Qt.CheckState.Checked
_STATE_DISABLED = Qt.CheckState.PartiallyChecked
_STATE_DELETE   = Qt.CheckState.Unchecked


class TriCheckBox(QCheckBox):

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setTristate(True)

    def nextCheckState(self) -> None:
        s = self.checkState()
        self.setCheckState(
            _STATE_DISABLED if s == _STATE_ACTIVE   else
            _STATE_DELETE   if s == _STATE_DISABLED else
            _STATE_ACTIVE
        )
        _update_tri_style(self)


def _build_op_text(distro: LinuxDistroHelper) -> dict[str, str]:
    def pkglist(fn) -> str:
        try:
            pkgs = fn()
            return ", ".join(pkgs) if pkgs else "—"
        except (OSError, AttributeError):
            return "—"

    has_yay     = distro.has_aur and distro.package_is_installed("yay")
    install_cmd = distro.get_pkg_install_cmd("…")
    session     = distro.detect_session() or "current session"
    pm_name     = distro.pkg_manager_name()
    cron_svc    = distro.get_cron_service_name()

    return {
        "copy_system_files":
            "Copy 'System Files' (Using 'sudo cp')",
        "update_mirrors":
            "Mirror update<br>"
            "(Install 'reflector' and get the 10 fastest servers in your country, "
            "or worldwide if location is not detected)",
        "set_user_shell":
            "Change shell for current user (Install package for the selected shell and set it as the default)",
        "update_system":
            f"System update (Using '{'yay --noconfirm' if has_yay else distro.get_update_system_cmd()}')",
        "install_kernel_header":
            f"Check kernel version and install corresponding headers ({distro.get_kernel_headers_pkg()})",
        "install_basic_packages":
            f"Install 'Basic Packages' (Using '{install_cmd}')",
        "install_yay":
            "Install 'yay' (required for 'AUR Packages')",
        "install_aur_packages":
            "Install 'AUR Packages' ('yay' required)",
        "install_specific_packages":
            f"Install 'Specific Packages' for {session}  (Using '{install_cmd}')",
        "install_flatpak":
            f"Enable Flatpak integration (Install '{pkglist(distro.get_flatpak_packages)}' and add Flathub remote)",
        "enable_printer_support":
            f"Initialise printer support<br>"
            f"(Install '{pkglist(distro.get_printer_packages)}'. Enable & start 'cups.service')",
        "enable_ssh_service":
            f"Initialise SSH server (Install '{pkglist(distro.get_ssh_packages)}'. "
            f"Enable & start '{distro.get_ssh_service_name()}.service')",
        "enable_samba_network_filesharing":
            f"Initialise Samba (network file-sharing). (Install '{pkglist(distro.get_samba_packages)}'. "
            f"Enable & start 'smb.service')",
        "enable_bluetooth_service":
            f"Initialise Bluetooth (Install '{pkglist(distro.get_bluetooth_packages)}'. "
            f"Enable & start 'bluetooth.service')",
        "enable_atd_service":
            f"Initialise atd (Install '{pkglist(distro.get_at_packages)}'. Enable & start 'atd.service')",
        "enable_cronie_service":
            f"Initialise {cron_svc} (Install '{pkglist(distro.get_cron_packages)}'. "
            f"Enable & start '{cron_svc}.service')",
        "install_snap":
            f"Initialise Snap (Install '{pkglist(distro.get_snap_packages)}'. Enable & start 'snapd.service')",
        "enable_firewall":
            f"Initialise firewall (Install '{pkglist(distro.get_firewall_packages)}'. "
            f"Enable & start 'ufw.service', set to 'deny all by default')",
        "remove_orphaned_packages":
            "Remove orphaned package(s)",
        "clean_cache":
            f"Clean cache (for '{pm_name}'" + (" and 'yay')" if distro.has_aur else ")"),
    }


def _make_tri_cb(text: str, disabled: bool, tooltip: str = "") -> TriCheckBox:
    cb = TriCheckBox(text)
    cb.setCheckState(_STATE_DISABLED if disabled else _STATE_ACTIVE)
    _update_tri_style(cb)
    if tooltip:
        cb.setToolTip(tooltip)
        cb.setToolTipDuration(600_000)
    return cb


def _update_tri_style(cb: QCheckBox) -> None:
    ss_active, ss_disabled, ss_delete = tri_styles()
    state = cb.checkState()
    if state == _STATE_ACTIVE:
        cb.setStyleSheet(ss_active)
    elif state == _STATE_DISABLED:
        cb.setStyleSheet(ss_disabled)
    else:
        cb.setStyleSheet(ss_delete)


def _scroll_dlg(parent, title: str, body: QWidget, on_save=None):
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    lay = QVBoxLayout(dlg)

    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    sa.setWidget(body)
    lay.addWidget(sa)

    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
    bb.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
    bb.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
    if on_save:
        bb.accepted.connect(lambda: on_save(dlg))
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)

    body.adjustSize()
    sz  = body.sizeHint()
    scr = QApplication.primaryScreen()
    if scr:
        sg     = scr.availableGeometry()
        width  = min(max(sz.width() + 80, 950), sg.width() - 50)
        height = min(sz.height() + 200, int(sg.height() * 0.9))
        dlg.resize(width, height)

    bb.button(QDialogButtonBox.StandardButton.Cancel).setFocus()
    return dlg, lay


def _pkg_checkboxes(packages: list, is_specific: bool) -> list[QCheckBox]:
    t   = current_theme()
    tip = (
        f"<span style='color:{t['green']};'>●</span>: Active<br>"
        f"<span style='color:{t['muted']};'>●</span>: Disabled<br>"
        f"<span style='color:{t['red']};'>●</span>: Delete"
    )
    result = []
    for p in packages:
        if is_specific and isinstance(p, dict):
            text     = f"{p['package']} ({p.get('session', '')})"
            disabled = p.get("disabled", False)
        elif isinstance(p, dict):
            text     = p.get("name", str(p))
            disabled = p.get("disabled", False)
        else:
            text, disabled = str(p), False
        cb = _make_tri_cb(text, disabled, tip)
        cb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        result.append(cb)
    return result


class SystemManagerOptions(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("System Manager Options")
        self.setMinimumSize(1200, 680)
        self._distro = LinuxDistroHelper()
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        yay = (
            f"   |   AUR Helper: 'yay' "
            f"{'detected' if self._distro.package_is_installed('yay') else 'not detected'}"
            if self._distro.has_aur else ""
        )
        info = QLabel(
            f"Recognized Linux distribution: {self._distro.distro_pretty_name}   |   "
            f"Session: {self._distro.detect_session()}{yay}"
        )
        info.setStyleSheet(style_label_info_bold())
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(info)

        cmd      = self._distro.get_pkg_install_cmd("")
        top_text = QLabel(
            f"First you can select 'System Files' in System Manager. These files will be copied using 'sudo', "
            f"for root privilege.\nIf you have 'System Files' selected, System Manager will copy these first. "
            f"This allows you to copy files\nsuch as 'pacman.conf' or 'smb.conf' to '/etc/'.\n\n\n"
            f"Under 'System Manager Operations' you can specify how you would like to proceed. "
            f"Each operation is executed\none after the other. Uncheck operations to disable them.\n\n"
            f"\nTips:\n\n"
            f"'Basic Packages' will be installed using '{cmd}PACKAGE'.\n\n"
            f"'AUR Packages' provides access to the Arch User Repository. "
            f"Therefore 'yay' must and will be installed."
            f"\nThis feature is available only on Arch Linux based distributions.\n\n"
            f"You can also define 'Specific Packages'. These packages will be installed only "
            f"(using '{cmd}PACKAGE') if the corresponding session has been recognized.\n"
            f"Both full desktop environments and window managers such as 'Hyprland' and others are supported."
        )
        top_text.setWordWrap(True)
        top_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        top_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(top_text)
        lay.addWidget(scroll)

        shell_row = QHBoxLayout()
        shell_row.addWidget(QLabel("User Shell:"))
        self._shell_cb = QComboBox()
        self._shell_cb.addItems(USER_SHELLS)
        if S.user_shell in USER_SHELLS:
            self._shell_cb.setCurrentText(S.user_shell)
        self._shell_cb.currentIndexChanged.connect(self._save_shell)
        shell_row.addWidget(self._shell_cb)
        shell_row.addStretch()
        lay.addLayout(shell_row)

        for row_items in [
            [
                ("System Manager Operations", self._edit_ops),
                ("System Files",              self._edit_sysfiles),
            ],
            [
                ("Basic Packages",    lambda: self._edit_pkgs("basic_packages")),
                ("AUR Packages",      lambda: self._edit_pkgs("aur_packages")),
                ("Specific Packages", lambda: self._edit_pkgs("specific_packages")),
            ],
        ]:
            row = QHBoxLayout()
            for label, fn in row_items:
                b = QPushButton(label)
                b.clicked.connect(fn)
                row.addWidget(b)
            lay.addLayout(row)

        close = QPushButton("Close")
        close.clicked.connect(self.close)
        lay.addWidget(close)

    def _save_shell(self):
        sel = self._shell_cb.currentText()
        if sel in USER_SHELLS and sel != S.user_shell:
            S.user_shell = sel
            save_profile()
            QMessageBox.information(self, "User Shell", f"Shell set to: {sel}")

    def _edit_ops(self):
        arch_only = {"update_mirrors", "install_yay", "install_aur_packages"}
        op_text = {k: v.replace("&", "&&").replace("<br>", "\n") for k, v in _build_op_text(self._distro).items()}
        widgets: list[tuple[QCheckBox, str]] = []

        body = QWidget()
        grid = QGridLayout(body)

        sa = QCheckBox("Check/Uncheck All")
        sa.setTristate(True)
        sa.setStyleSheet(style_checkbox_select_all())
        grid.addWidget(sa, 0, 0)

        for i, (key, text) in enumerate(op_text.items()):
            cb = QCheckBox(text)
            if key in arch_only and not self._distro.supports_aur():
                cb.setEnabled(False)
                cb.setStyleSheet(style_checkbox_muted())
            else:
                cb.setChecked(key in S.system_manager_ops)
            grid.addWidget(cb, i + 1, 0)
            widgets.append((cb, key))

        yay_cb = next((cb for cb, k in widgets if k == "install_yay"), None)
        aur_cb = next((cb for cb, k in widgets if k == "install_aur_packages"), None)

        def _sync_sa():
            enabled = [c for c, k in widgets if c.isEnabled()]
            if not enabled:
                return
            n = sum(c.isChecked() for c in enabled)
            sa.blockSignals(True)
            sa.setCheckState(Qt.CheckState.Checked if n == len(enabled) else Qt.CheckState.Unchecked if n == 0 else Qt.CheckState.PartiallyChecked)
            sa.blockSignals(False)

        def _handle_aur_dependency():
            if aur_cb and yay_cb and self._distro.supports_aur():
                if aur_cb.isChecked():
                    yay_cb.setChecked(True)
                    yay_cb.setEnabled(False)
                    yay_cb.setStyleSheet(style_checkbox_muted())
                else:
                    yay_cb.setEnabled(True)
                    yay_cb.setStyleSheet("")
            _sync_sa()

        def _toggle_all(state=None):
            checked = int(state) != Qt.CheckState.Unchecked.value if state is not None else False

            for _cb, _key in widgets:
                _cb.blockSignals(True)

            for _cb, _key in widgets:
                if not checked:
                    _cb.setChecked(False)
                else:
                    if _cb.isEnabled():
                        _cb.setChecked(True)

            for _cb, _key in widgets:
                _cb.blockSignals(False)

            _handle_aur_dependency()

        sa.stateChanged.connect(_toggle_all)
        for cb, key in widgets:
            cb.stateChanged.connect(_handle_aur_dependency if key == "install_aur_packages" else _sync_sa)

        _handle_aur_dependency()

        def _save(dlg):
            S.system_manager_ops = [k for _cb, k in widgets if _cb.isChecked()]
            save_profile()
            QMessageBox.information(self, "Saved", "Operations saved.")
            dlg.accept()

        _scroll_dlg(self, "System Manager Operations", body, _save)[0].exec()

    def _edit_sysfiles(self):
        files = [f for f in (S.system_files or []) if isinstance(f, dict) and f.get("source") and f.get("destination")]
        checkboxes: list[tuple[QCheckBox, dict]] = []

        body = QWidget()
        vlay = QVBoxLayout(body)
        vlay.setSpacing(4)

        for f in files:
            text = f"{apply_replacements(f['source'])}  →  {apply_replacements(f['destination'])}"
            tip = f"Source:\n  {f['source']}\n\nDestination:\n  {f['destination']} \n\n☑ Active  ▣ Disabled  ☐ Delete"
            cb = _make_tri_cb(text, f.get("disabled", False), tip)
            checkboxes.append((cb, f))
            vlay.addWidget(cb)

        if checkboxes:
            sa = TriCheckBox("Set All: Active / Disabled / Delete")
            sa.setStyleSheet(style_checkbox_select_all())
            all_active = all(cb.checkState() == _STATE_ACTIVE for cb, _ in checkboxes)
            sa.setCheckState(_STATE_ACTIVE if all_active else _STATE_DISABLED)
            _update_tri_style(sa)

            def _toggle_sa(_state=None):
                target = sa.checkState()
                for _cb, _ in checkboxes:
                    _cb.setCheckState(target)
                    _update_tri_style(_cb)

            sa.stateChanged.connect(_toggle_sa)
            vlay.addWidget(sa)

        def _save(_dlg):
            to_delete = [f for _cb, f in checkboxes if _cb.checkState() == _STATE_DELETE]
            if to_delete:
                names = "\n".join(f"  • {apply_replacements(f.get('source', '?'))}" for f in to_delete)
                ans = QMessageBox.question(_dlg, "Confirm Delete",
                                           f"The following system file(s) will be permanently removed:\n\n{names}\n\nContinue?",
                                           QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if ans != QMessageBox.StandardButton.Yes:
                    return
            S.system_files = []
            for _cb, f in checkboxes:
                s = _cb.checkState()
                if s != _STATE_DELETE:
                    S.system_files.append({**f, "disabled": s == _STATE_DISABLED})
            save_profile()
            QMessageBox.information(self, "Saved", "System files saved.")
            _dlg.accept()

        dlg, lay = _scroll_dlg(self, "System Files", body, _save)

        add_btn = QPushButton("Add System File")
        add_btn.clicked.connect(lambda: self._schedule_add_sysfile(dlg))
        lay.insertWidget(1, add_btn)

        io_row = QHBoxLayout()
        imp_btn = QPushButton("📥 Import (.txt/.csv)")
        exp_btn = QPushButton("📤 Export (.txt)")
        imp_btn.clicked.connect(lambda: (dlg.close(), QTimer.singleShot(0, self._import_sysfiles)))
        exp_btn.clicked.connect(lambda: self._export_sysfiles())
        io_row.addWidget(imp_btn)
        io_row.addWidget(exp_btn)
        lay.insertLayout(2, io_row)

        dlg.exec()

    def _import_sysfiles(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import System Files", str(_HOME), "Data (*.txt *.csv)")
        if not path:
            QTimer.singleShot(0, self._edit_sysfiles)
            return

        try:
            content = Path(path).read_text(encoding="utf-8").splitlines()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Read failed: {e}")
            QTimer.singleShot(0, self._edit_sysfiles)
            return

        S.system_files = S.system_files or []
        existing: set[tuple[str, str]] = {(f["source"], f["destination"]) for f in S.system_files if isinstance(f, dict)}

        added_count = 0
        skipped_dup = 0
        skipped_inv = 0

        for line in content:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if "\t" in line:
                parts = [p.strip() for p in line.split("\t", 1)]
            else:
                parts = [p.strip() for p in line.split(",", 1)]

            if len(parts) != 2:
                skipped_inv += 1
                continue

            src, dst = parts
            if not src or not dst:
                skipped_inv += 1
                continue
            if not (src.startswith("/") or src.startswith("~")) or \
                    not (dst.startswith("/") or dst.startswith("~")):
                skipped_inv += 1
                continue

            src = str(Path(src).expanduser())
            dst = str(Path(dst).expanduser())

            if (src, dst) in existing:
                skipped_dup += 1
                continue

            S.system_files.append({"source": src, "destination": dst, "disabled": False})
            existing.add((src, dst))
            added_count += 1

        if added_count > 0:
            S.system_files.sort(key=lambda x: x.get("source", "").lower())
            save_profile()

        parts_msg = [f"Imported: {added_count}"]
        if skipped_dup:
            parts_msg.append(f"Skipped (duplicate): {skipped_dup}")
        if skipped_inv:
            parts_msg.append(f"Skipped (invalid format): {skipped_inv}")
        QMessageBox.information(self, "Import Complete", "\n".join(parts_msg))

        QTimer.singleShot(0, self._edit_sysfiles)

    def _export_sysfiles(self):
        files = [f for f in (S.system_files or []) if isinstance(f, dict) and f.get("source") and f.get("destination")]
        if not files:
            QMessageBox.information(self, "Export", "No system files to export.")
            return

        path, _ = QFileDialog.getSaveFileName(self, "Export System Files", str(Path(_HOME) / "system_files.txt"),
                                              "Text (*.txt);;CSV (*.csv);;All (*)")
        if not path:
            return

        lines = ["# source,destination"]
        for f in files:
            src = f.get("source", "")
            dst = f.get("destination", "")
            if src and dst:
                lines.append(f"{src},{dst}")

        try:
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            QMessageBox.information(self, "Exported", f"Exported {len(files)} entry/entries to:\n{path}")
        except OSError as exc:
            QMessageBox.critical(self, "Export Error", str(exc))

    def _schedule_add_sysfile(self, parent_dlg):
        parent_dlg.close()
        QTimer.singleShot(100, self._add_sysfile)

    def _add_sysfile(self):
        box        = QMessageBox(self)
        box.setWindowTitle("Add System File/Folder")
        box.setText("Choose the source type:")
        file_btn   = box.addButton("File(s)",   QMessageBox.ButtonRole.YesRole)
        box.addButton("Directory",              QMessageBox.ButtonRole.NoRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked == cancel_btn:
            return

        if clicked == file_btn:
            sources = QFileDialog.getOpenFileNames(self, "Select file(s)")[0]
        else:
            dir_sel = QFileDialog.getExistingDirectory(self, "Select directory")
            sources = [dir_sel] if dir_sel else []

        sources = [s for s in sources if s]
        if not sources:
            return

        dst_dir = QFileDialog.getExistingDirectory(
            self, "Select Destination Directory (e.g. /etc/...)")
        if not dst_dir:
            return

        S.system_files = S.system_files or []
        added = []
        for s in sources:
            src_path = Path(s).resolve()
            dst_path = Path(dst_dir) / src_path.name
            if not any(f.get("source") == str(src_path)
                       for f in S.system_files if isinstance(f, dict)):
                S.system_files.append({"source": str(src_path), "destination": str(dst_path), "disabled": False})
                added.append(src_path.name)

        if added:
            S.system_files.sort(key=lambda x: x.get("source", "").lower())
            save_profile()
            QMessageBox.information(self, "Success", f"Added {len(added)} item(s).")

        QTimer.singleShot(0, self._edit_sysfiles)

    def _edit_pkgs(self, pkg_type: str):
        is_specific = pkg_type == "specific_packages"
        packages    = getattr(S, pkg_type, []) or []
        checkboxes  = _pkg_checkboxes(packages, is_specific)

        body = QWidget()
        grid = QGridLayout(body)
        grid.setSpacing(6)
        cols = 5
        for i, cb in enumerate(checkboxes):
            grid.addWidget(cb, i // cols, i % cols)

        if checkboxes:
            sa_row = (len(checkboxes) - 1) // cols + 1
            sa = TriCheckBox("Set All: Active / Disabled / Delete")
            sa.setStyleSheet(style_checkbox_select_all())
            all_active = all(cb.checkState() == _STATE_ACTIVE for cb in checkboxes)
            sa.setCheckState(_STATE_ACTIVE if all_active else _STATE_DISABLED)
            _update_tri_style(sa)

            def _toggle_sa(_state=None):
                target = sa.checkState()
                for _cb in checkboxes:
                    _cb.setCheckState(target)
                    _update_tri_style(_cb)

            sa.stateChanged.connect(_toggle_sa)
            grid.addWidget(sa, sa_row, 0, 1, cols)

        def _save(_dlg):
            to_delete = [p for _cb, p in zip(checkboxes, packages) if _cb.checkState() == _STATE_DELETE]
            if to_delete:
                if is_specific:
                    names = "\n".join(f"  • {p.get('package', '?')} ({p.get('session', '?')})"
                    if isinstance(p, dict) else f"  • {p}" for p in to_delete)
                else:
                    names = "\n".join(f"  • {p.get('name', '?')}" if isinstance(p, dict) else f"  • {p}"
                                      for p in to_delete)
                ans = QMessageBox.question(_dlg, "Confirm Delete",
                                           f"The following package(s) will be permanently removed:\n\n{names}\n\nContinue?",
                                           QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if ans != QMessageBox.StandardButton.Yes:
                    return
            new: list = []
            for _cb, p in zip(checkboxes, packages):
                s = _cb.checkState()
                if s == _STATE_DELETE:
                    continue
                disabled = s == _STATE_DISABLED
                new.append({**p, "disabled": disabled} if isinstance(p, dict) else {"name": str(p), "disabled": disabled})
            key_fn = ((lambda x: x.get("package", "").lower()) if is_specific else (lambda x: x.get("name", "").lower()))
            new.sort(key=key_fn)
            setattr(S, pkg_type, new)
            save_profile()
            QMessageBox.information(self, "Saved", "Packages saved.")
            _dlg.accept()
            QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))

        title    = f"Edit {pkg_type.replace('_', ' ').title()}"
        dlg, lay = _scroll_dlg(self, title, body, _save)

        search = QLineEdit()
        search.setPlaceholderText("Filter packages…")
        search.textChanged.connect(lambda txt: [_cb.setVisible(txt.lower() in _cb.text().lower()) for _cb in checkboxes])
        lay.insertWidget(1, search)

        add_lbl = pkg_type.replace("_", " ").title().rstrip("s")
        add_btn = QPushButton(f"Add {add_lbl}")
        add_btn.clicked.connect(lambda: (dlg.close(), QTimer.singleShot(0, lambda: self._add_pkg(pkg_type))))
        lay.insertWidget(2, add_btn)

        batch_row = QHBoxLayout()
        if not is_specific:
            batch = QPushButton("Batch Add")
            batch.clicked.connect(lambda: (dlg.close(), QTimer.singleShot(0, lambda: self._batch_add(pkg_type))))
            batch_row.addWidget(batch)

        imp = QPushButton("📥 Import (.txt/.csv)")
        exp = QPushButton("📤 Export (.txt)")
        imp.clicked.connect(lambda: (dlg.close(), QTimer.singleShot(0, lambda: self._import_pkgs(pkg_type))))
        exp.clicked.connect(lambda: self._export_pkgs(pkg_type))
        batch_row.addWidget(imp)
        batch_row.addWidget(exp)
        lay.insertLayout(3, batch_row)

        dlg.exec()

    def _add_pkg(self, pkg_type: str):
        if pkg_type == "specific_packages":
            self._add_specific_pkg()
            return
        label    = pkg_type.replace("_", " ").title().rstrip("s")
        name, ok = QInputDialog.getText(self, f"Add {label}", "               Package name:               ")
        if ok and name.strip():
            current  = getattr(S, pkg_type, []) or []
            existing = {p.get("name") if isinstance(p, dict) else p for p in current}
            if name.strip() not in existing:
                current.append({"name": name.strip(), "disabled": False})
                setattr(S, pkg_type, sorted(current, key=lambda x: x.get("name", "").lower()))
                save_profile()
                QMessageBox.information(self, "Added", f"'{name.strip()}' added.")
            else:
                QMessageBox.warning(self, "Duplicate", f"'{name.strip()}' already exists.")
        QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))

    def _add_specific_pkg(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Add Specific Package")
        dlg.setFixedWidth(620)
        lay     = QVBoxLayout(dlg)
        form    = QFormLayout()
        pkg_in  = QLineEdit()
        pkg_in.setFixedHeight(36)
        sess_cb = QComboBox()
        sess_cb.addItems(SESSIONS)
        sess_cb.setFixedHeight(36)
        form.addRow("Package:", pkg_in)
        form.addRow("Session:", sess_cb)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name, sess = pkg_in.text().strip(), sess_cb.currentText()
            if not name:
                QMessageBox.warning(self, "Error", "Package name required.")
                return
            S.specific_packages = S.specific_packages or []
            if not any(isinstance(p, dict) and p.get("package") == name and p.get("session") == sess for p in S.specific_packages):
                S.specific_packages.append({"package": name, "session": sess, "disabled": False})
                S.specific_packages.sort(key=lambda x: x.get("package", "").lower())
                save_profile()
                QMessageBox.information(self, "Added", f"'{name}' for '{sess}' added.")
            else:
                QMessageBox.warning(self, "Duplicate", f"'{name}' for '{sess}' already exists.")
        QTimer.singleShot(0, lambda: self._edit_pkgs("specific_packages"))

    def _batch_add(self, pkg_type: str):
        dlg = QDialog(self)
        dlg.setWindowTitle("Batch Add")
        dlg.setMinimumSize(700, 500)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("One package per line:"))
        te  = QTextEdit()
        lay.addWidget(te)
        bb  = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel) # type: ignore
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            current  = getattr(S, pkg_type, []) or []
            existing = {p.get("name") if isinstance(p, dict) else p for p in current}
            added, dupes = [], []
            for name in (l.strip() for l in te.toPlainText().splitlines() if l.strip()):
                if name in existing:
                    dupes.append(name)
                else:
                    added.append(name)
                    existing.add(name)
                    current.append({"name": name, "disabled": False})
            setattr(S, pkg_type, sorted(current, key=lambda x: x.get("name", "").lower()))
            save_profile()
            msg = f"Added: {len(added)}" + (f"\nSkipped (duplicate): {len(dupes)}" if dupes else "")
            QMessageBox.information(self, "Batch Add", msg)
        QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))

    def _import_pkgs(self, pkg_type: str):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import", str(_HOME), "Data (*.txt *.csv)"
        )
        if not path:
            QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))
            return

        try:
            content = Path(path).read_text(encoding="utf-8").splitlines()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Read failed: {e}")
            return

        is_specific = pkg_type == "specific_packages"
        current     = getattr(S, pkg_type, []) or []

        existing_spec: set[tuple] = set()
        existing_std:  set[str]   = set()
        if is_specific:
            existing_spec = {(p["package"], p["session"]) for p in current if isinstance(p, dict)}
        else:
            existing_std = {p["name"] if isinstance(p, dict) else p for p in current}

        added_count = 0
        for line in content:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip().strip("\"'") for p in line.split(",")]
            name  = parts[0]
            if not all(c.isalnum() or c in "-_.+" for c in name):
                continue
            if is_specific:
                sess = parts[1] if len(parts) > 1 else (SESSIONS[0] if SESSIONS else "unknown")
                if (name, sess) not in existing_spec:
                    current.append({"package": name, "session": sess, "disabled": False})
                    existing_spec.add((name, sess))
                    added_count += 1
            else:
                if name not in existing_std:
                    current.append({"name": name, "disabled": False})
                    existing_std.add(name)
                    added_count += 1

        if added_count > 0:
            key_fn = ((lambda x: x.get("package", "").lower()) if is_specific else (lambda x: x.get("name", "").lower()))
            current.sort(key=key_fn)
            setattr(S, pkg_type, current)
            save_profile()
            QMessageBox.information(self, "Import Complete",
                                    f"Successfully imported {added_count} packages.")

        QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))

    def _export_pkgs(self, pkg_type: str):
        packages = getattr(S, pkg_type, []) or []
        if not packages:
            QMessageBox.information(self, "Export", "No packages to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export", str(Path(_HOME) / f"{pkg_type}.txt"), "Text (*.txt);;All (*)")
        if not path:
            return
        is_specific = pkg_type == "specific_packages"
        lines = (
            [f"{p.get('package', '')},{p.get('session', '')}" if isinstance(p, dict) else str(p)
             for p in packages]
            if is_specific
            else [p.get("name", "") if isinstance(p, dict) else str(p) for p in packages]
        )
        try:
            Path(path).write_text("\n".join(ln for ln in lines if ln) + "\n", encoding="utf-8")
            QMessageBox.information(self, "Exported", f"Exported to:\n{path}")
        except OSError as exc:
            QMessageBox.critical(self, "Export Error", str(exc))


class SystemManagerLauncher:

    def __init__(self, parent=None):
        self.parent          = parent
        self.failed_attempts = getattr(parent, "sm_failed_attempts", 0)
        self._distro         = LinuxDistroHelper()
        self._distro_name    = self._distro.distro_pretty_name
        self._session        = self._distro.detect_session()
        self._sudo_checkbox: QCheckBox | None = None
        self._sm_thread = None
        self._sm_dialog = None

    def launch(self) -> None:
        if not S.system_manager_ops:
            QMessageBox.information(
                self.parent,
                "No Operations Configured",
                "System Manager has no operations selected yet.\n\n"
                "Please configure what should be executed under 'System Manager Operations' first.",
            )
            SystemManagerOptions(self.parent).exec()
            return

        if self.parent:
            self.parent.hide()
        try:
            self._confirm_and_start()
        finally:
            if self.parent:
                self.parent.show()

    def _confirm_and_start(self) -> None:
        ops     = S.system_manager_ops
        op_text = {k: v.replace("&&", "&") for k, v in _build_op_text(self._distro).items()}
        _, _, tips = generate_tooltip()

        dialog, content_widget, content_layout = self._build_confirmation_dialog()
        self._populate_operations(ops, op_text, tips, content_layout)

        if self._run_dialog(dialog, content_widget):
            self._on_dialog_accepted()

    def _build_confirmation_dialog(self):
        dialog = QDialog(self.parent)
        dialog.setWindowTitle("System Manager")
        outer  = QVBoxLayout(dialog)
        outer.setContentsMargins(0, 0, 0, 0)

        yay_info = ""
        if self._distro.has_aur:
            yay_info = (
                "   |   AUR Helper: 'yay' detected"
                if self._distro.package_is_installed("yay")
                else " | AUR Helper: 'yay' not detected"
            )
        distro_lbl = QLabel(
            f"Recognized Linux distribution: {self._distro_name}   |   "
            f"Session: {self._session}{yay_info}"
        )
        distro_lbl.setStyleSheet(style_label_info(font_size=20))
        distro_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.addWidget(distro_lbl)

        ops_lbl = QLabel(
            "<span style='font-size:20px;font-family:monospace;'>"
            "<br>System Manager will perform the following operations:<br></span>"
        )
        ops_lbl.setTextFormat(Qt.TextFormat.RichText)
        ops_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(ops_lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setWidget(content_widget)
        outer.addWidget(scroll)
        return dialog, content_widget, content_layout

    def _populate_operations(self, ops, op_text, tips, layout) -> None:
        for i, key in enumerate(ops):
            if key in op_text:
                self._add_operation_row(i, op_text[key], tips.get(key, ""), layout)

    @staticmethod
    def _add_operation_row(index: int, text: str, tooltip: str, layout) -> None:
        has_tip    = bool(tooltip)
        colour, decoration = style_op_label(has_tip)
        icon       = "󰔨 " if has_tip else ""
        html = (
            f"{icon}   <span style='font-size:16px;padding:5px;"
            f"color:{colour};{decoration}'>{text}</span>"
        )
        row = QHBoxLayout()
        num = QLabel(f"{index + 1}:")
        num.setStyleSheet(style_label_mono(font_size=16))
        lbl = QLabel(html)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setStyleSheet(style_label_mono(font_size=16))
        if has_tip:
            lbl.setToolTip(tooltip)
            lbl.setCursor(Qt.CursorShape.WhatsThisCursor)
            lbl.setToolTipDuration(60000)
        row.addWidget(num)
        row.addWidget(lbl)
        row.addStretch(1)
        layout.addLayout(row)

    def _run_dialog(self, dialog: QDialog, content_widget: QWidget) -> bool:
        confirm = QLabel(
            "<span style='font-size:16px;'>Start System Manager?<br>"
            "(Check 'Enter sudo password' if privileged commands require a password)<br></span>"
        )
        confirm.setTextFormat(Qt.TextFormat.RichText)
        confirm.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._sudo_checkbox = QCheckBox("Enter sudo password 󰔨")
        self._sudo_checkbox.setStyleSheet(style_sudo_checkbox(muted=False))
        if self.failed_attempts:
            self._sudo_checkbox.setText("Sudo password must be entered! 󰔨")
            self._sudo_checkbox.setChecked(True)
            self._sudo_checkbox.setEnabled(False)
            self._sudo_checkbox.setStyleSheet(style_sudo_checkbox(muted=True))

        _sudo_tip = (
            "<b>How your sudo password is used — and why it is safe:</b><br><br>"
            "Your password is held <b>only in memory</b> as a mutable <code>bytearray</code> "
            "(<code>SecureString</code>) and is <b>never written to disk in plain text</b>.<br><br>"
            "<b>Technical details:</b><br>"
            "A private temporary directory (<code>chmod 700</code>) is created. "
            "Inside it, the password file is opened with <code>O_CREAT | O_EXCL | O_WRONLY</code> "
            "and mode <code>0o600</code> — the file is <b>created with restricted permissions "
            "before any data is written</b>, so there is no window where it is world-readable.<br>"
            "The <code>SUDO_ASKPASS</code> environment variable points a minimal shell script "
            "to this file so <code>sudo -A</code> can read it non-interactively.<br>"
            "The in-memory password bytes are <b>zeroed immediately after writing</b> to disk.<br><br>"
            "<b>Cleanup:</b><br>"
            "When System Manager finishes, the password file is <b>overwritten with random bytes "
            "and then deleted</b>. The temporary directory is removed entirely afterwards.<br><br>"
            "<b>In memory:</b><br>"
            "The <code>SecureString</code> object's buffer is <b>zeroed with "
            "<code>memoryview</code></b> before the reference is released, "
            "minimising the time the password lingers in Python's garbage-collected heap.<br><br>"
            "<i>Your password is never logged, never sent over the network, "
            "and never stored beyond this session.</i>"
        )
        self._sudo_checkbox.setToolTip(_sudo_tip)
        self._sudo_checkbox.setToolTipDuration(600_000)
        self._sudo_checkbox.setCursor(Qt.CursorShape.WhatsThisCursor)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No) # type: ignore
        bb.accepted.connect(dialog.accept)
        bb.rejected.connect(dialog.reject)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._sudo_checkbox)
        btn_row.addWidget(bb)

        layout = content_widget.layout()
        if isinstance(layout, QVBoxLayout):
            layout.addWidget(confirm)
            layout.addLayout(btn_row)

        scr = QApplication.primaryScreen()
        if scr:
            sg = scr.availableGeometry()
            sz = content_widget.sizeHint()
            dialog.resize(
                min(sz.width() + 40, sg.width()),
                min(sz.height() + 40, sg.height()),
            )

        bb.button(QDialogButtonBox.StandardButton.No).setFocus()
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _on_dialog_accepted(self) -> None:
        if self._sudo_checkbox and self._sudo_checkbox.isChecked():
            self._show_sudo_dialog()
        else:
            self._start_thread("")

    def _start_thread(self, pw) -> None:
        from system_manager import SystemManagerDialog, SystemManagerThread
        self._sm_thread = SystemManagerThread(pw)
        self._sm_dialog = SystemManagerDialog(self.parent)
        t, d = self._sm_thread, self._sm_dialog
        t.thread_started.connect(lambda: d.exec())
        t.outputReceived.connect(d.on_output)
        t.taskStatusChanged.connect(d.on_task_status)
        t.passwordFailed.connect(lambda: self._on_fail(t, d))
        t.passwordSuccess.connect(self._on_ok)
        t.start()

    def _show_sudo_dialog(self) -> None:
        from sudo_password import SudoPasswordDialog
        dlg = SudoPasswordDialog(self.parent)
        dlg.sudo_password_entered.connect(self._start_thread)
        dlg.update_failed_attempts(self.failed_attempts)
        dlg.exec()

    def _on_fail(self, thread, dialog) -> None:
        t = current_theme()
        self.failed_attempts += 1
        if self.parent:
            self.parent.sm_failed_attempts = self.failed_attempts
        dialog.on_output(
            f"<p style='color:{t['error']};font-size:17px;font-weight:bold;'>"
            "Authentication failed. Cancelled to prevent account lockout.<br>"
            "Possible causes: incorrect password, user not in sudoers.</p>",
            "info",
        )
        dialog.mark_done(failed_count=self.failed_attempts)
        thread.terminated = True
        thread.quit()
        thread.wait(2000)

    def _on_ok(self) -> None:
        self.failed_attempts = 0
        if self.parent:
            self.parent.sm_failed_attempts = 0
