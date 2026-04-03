from pathlib import Path
from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from sudo_password import SecureString

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QTextEdit,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget,
)

from dialogs import _ask_text, _do_browse, _ok_cancel_buttons
from linux_distro_helper import LinuxDistroHelper, SESSIONS, USER_SHELLS
from state import S, _HOME, apply_replacements, generate_tooltip, save_profile
from themes import (
    style_label_info, style_label_info_bold, style_label_mono, style_op_label, tri_styles, apply_tooltip,
    current_theme, font_sz, style_checkbox_muted, style_checkbox_select_all, style_sudo_checkbox, tri_state_legend_html
)


def _get_pkg_name(p: dict | str, is_specific: bool) -> str:
    return p.get("package" if is_specific else "name", "") if isinstance(p, dict) else str(p)


def _sort_pkgs(pkg_list: list, is_specific: bool) -> None: pkg_list.sort(key=lambda x: _get_pkg_name(x, is_specific).lower())


_STATE_ACTIVE   = Qt.CheckState.Checked
_STATE_DISABLED = Qt.CheckState.PartiallyChecked
_STATE_DELETE   = Qt.CheckState.Unchecked


class TriCheckBox(QCheckBox):

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setTristate(True)

    def nextCheckState(self) -> None:
        s = self.checkState()
        next_state = (_STATE_DISABLED if s == _STATE_ACTIVE else _STATE_DELETE if s == _STATE_DISABLED else _STATE_ACTIVE)
        self.setCheckState(next_state)
        _update_tri_style(self)


def _update_tri_style(cb: QCheckBox) -> None:
    ss_active, ss_disabled, ss_delete = tri_styles()
    cb.setStyleSheet({_STATE_ACTIVE: ss_active, _STATE_DISABLED: ss_disabled, _STATE_DELETE: ss_delete}[cb.checkState()])


def _make_tri_cb(text: str, disabled: bool, tooltip: str = "") -> TriCheckBox:
    cb = TriCheckBox(text)
    cb.setCheckState(_STATE_DISABLED if disabled else _STATE_ACTIVE)
    _update_tri_style(cb)
    apply_tooltip(cb, tooltip)
    return cb


def _hsep() -> QFrame:
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setStyleSheet(f"color:{current_theme()['header_sep']};margin:4px 0;")
    return sep


def _scroll_dlg(parent, title: str, body: QWidget, on_save=None) -> tuple[QDialog, QVBoxLayout]:
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
    lay = QVBoxLayout(dlg)
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    sa.setWidget(body)
    lay.addWidget(sa)
    bb = _ok_cancel_buttons(dlg, lambda: on_save(dlg) if on_save else dlg.accept(), cancel_label="Close")
    lay.addWidget(bb)
    body.adjustSize()
    sz = body.sizeHint()
    scr = QApplication.primaryScreen()
    if scr:
        sg = scr.availableGeometry()
        width = min(max(sz.width() + 80, 950), sg.width() - 50)
        height = min(sz.height() + 200, int(sg.height() * 0.9))
        dlg.resize(width, height)
    cancel_btn = bb.button(QDialogButtonBox.StandardButton.Cancel)
    if cancel_btn:
        cancel_btn.setFocus()
    return dlg, lay


def _browse_field(parent: QWidget, editor: QLineEdit, btn_height: int = 36) -> QWidget:
    row  = QWidget()
    hlay = QHBoxLayout(row)
    hlay.setContentsMargins(0, 0, 0, 0)
    hlay.setSpacing(6)
    hlay.addWidget(editor)
    for lbl, mode in [("📄 File", "file"), ("📁 Directory", "dir")]:
        b = QPushButton(lbl)
        b.setMinimumHeight(btn_height)
        b.setMinimumWidth(70)
        b.clicked.connect(lambda _c=False, _e=editor, _m=mode: _do_browse(parent, _e, _m))
        hlay.addWidget(b)
    return row


def _pkg_checkboxes(packages: list, is_specific: bool) -> list[TriCheckBox]:
    result = []
    legend = tri_state_legend_html()
    for p in packages:
        if isinstance(p, dict):
            name = p.get("package" if is_specific else "name", str(p))
            disabled = p.get("disabled", False)
        else:
            name, disabled = str(p), False
        tip = f"<b>Package:</b> {name}<br><br><i>Left-click package to change status. Right-click to edit.</i><br><br>{legend}"
        cb = _make_tri_cb(name, disabled, tip)
        cb.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        result.append(cb)
    return result


def _add_select_all_tri(layout, checkboxes: list[TriCheckBox], cols: int = 1) -> None:
    sa = TriCheckBox("Set All: Active / Disabled / Delete")
    sa.setStyleSheet(style_checkbox_select_all())
    all_active = all(cb.checkState() == _STATE_ACTIVE for cb in checkboxes)
    sa.setCheckState(_STATE_ACTIVE if all_active else _STATE_DISABLED)
    _update_tri_style(sa)
    def _toggle(_state=None):
        target = sa.checkState()
        for cb in checkboxes:
            cb.setCheckState(target)
            _update_tri_style(cb)
    sa.stateChanged.connect(_toggle)
    if isinstance(layout, QGridLayout):
        row = layout.rowCount()
        layout.addWidget(_hsep(), row, 0, 1, cols)
        layout.addWidget(sa, row + 1, 0, 1, cols)
    else:
        layout.addWidget(_hsep())
        layout.addWidget(sa)


def _build_op_text(distro: LinuxDistroHelper, session: Optional[str] = None, has_yay: Optional[bool] = None) -> dict[str, str]:
    def pkglist(fn) -> str:
        try:
            pkgs = fn()
            return ", ".join(pkgs) if pkgs else "—"
        except (OSError, AttributeError):
            return "—"

    if has_yay is None:
        has_yay = distro.has_aur and distro.package_is_installed("yay")
    install_cmd = distro.get_pkg_install_cmd("…")
    if session is None:
        session = distro.detect_session() or "current session"
    pm_name  = distro.pkg_manager_name()
    cron_svc = distro.get_cron_service_name()
    return {
        "copy_system_files": "Copy 'System Files' (Using 'sudo cp')",
        "update_mirrors": "Mirror update<br>(Install 'reflector' and get the 10 fastest servers in your country, or worldwide if location is not detected)",
        "set_user_shell": "Change shell for current user (Install package for the selected shell and set it as the default)",
        "update_system": f"System update (Using '{'yay --noconfirm' if has_yay else distro.get_update_system_cmd()}')",
        "install_kernel_header": f"Check kernel version and install corresponding headers ({distro.get_kernel_headers_pkg()})",
        "install_basic_packages": f"Install 'Basic Packages' (Using '{install_cmd}')",
        "install_yay": "Install 'yay' (required for 'AUR Packages')",
        "install_aur_packages": "Install 'AUR Packages' ('yay' required)",
        "install_specific_packages": f"Install 'Specific Packages' for {session} (Using '{install_cmd}')",
        "enable_flatpak_integration": f"Enable Flatpak integration (Install '{pkglist(distro.get_flatpak_packages)}' and add Flathub remote)",
        "enable_printer_support": f"Initialise printer support<br>(Install '{pkglist(distro.get_printer_packages)}'. Enable & start 'cups.service')",
        "enable_ssh_service": f"Initialise SSH server (Install '{pkglist(distro.get_ssh_packages)}'. Enable & start '{distro.get_ssh_service_name()}.service')",
        "enable_samba_network_filesharing": f"Initialise Samba (network file-sharing). (Install '{pkglist(distro.get_samba_packages)}'. Enable & start 'smb.service')",
        "enable_bluetooth_service": f"Initialise Bluetooth (Install '{pkglist(distro.get_bluetooth_packages)}'. Enable & start 'bluetooth.service')",
        "enable_atd_service": f"Initialise atd (Install '{pkglist(distro.get_at_packages)}'. Enable & start 'atd.service')",
        "enable_cronie_service": f"Initialise {cron_svc} (Install '{pkglist(distro.get_cron_packages)}'. Enable & start '{cron_svc}.service')",
        "install_snap": f"Initialise Snap (Install '{pkglist(distro.get_snap_packages)}'. Enable & start 'snapd.service')",
        "enable_firewall": f"Initialise firewall (Install '{pkglist(distro.get_firewall_packages)}'. Enable & start 'ufw.service', set to 'deny all by default')",
        "remove_orphaned_packages": "Remove orphaned package(s)",
        "clean_cache": f"Clean cache (for '{pm_name}'" + (" and 'yay')" if distro.has_aur else ")")}


def _read_import_file(parent, path: str) -> list[str] | None:
    try:
        return Path(path).read_text(encoding="utf-8").splitlines()
    except Exception as e:
        QMessageBox.critical(parent, "Error", f"Read failed: {e}")
        return None


def _pkg_form_dialog(parent, title: str, *, prefill_name: str = "",
                     prefill_sess: Optional[str] = None) -> Optional[tuple[str, str] | tuple[str]]:

    with_session = prefill_sess is not None
    dlg     = QDialog(parent)
    dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(620)
    lay     = QVBoxLayout(dlg)
    form    = QFormLayout()
    name_ed = QLineEdit(prefill_name)
    name_ed.setMinimumHeight(36)
    form.addRow("Package:", name_ed)
    sess_cb: Optional[QComboBox] = None
    if with_session:
        sess_cb = QComboBox()
        sess_cb.addItems(SESSIONS)
        if prefill_sess:
            sess_cb.setCurrentText(prefill_sess)
        sess_cb.setMinimumHeight(36)
        form.addRow("Session:", sess_cb)
    lay.addLayout(form)
    lay.addWidget(_ok_cancel_buttons(dlg, dlg.accept))

    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    name = name_ed.text().strip()
    if not name:
        QMessageBox.warning(parent, "Error", "Package name required.")
        return None
    if not all(c.isalnum() or c in "-_.+" for c in name):
        QMessageBox.warning(parent, "Error", "Invalid package name.\n"
                                             "Only letters, digits, hyphens, dots, underscores and '+' are allowed.")
        return None
    return (name, sess_cb.currentText()) if with_session else (name,)


class SystemManagerOptions(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("System Manager Options")
        self.setMinimumSize(1200, 680)
        self._distro        = LinuxDistroHelper()
        self._session       = self._distro.detect_session()
        self._yay_installed = self._distro.has_aur and self._distro.package_is_installed("yay")
        self._build()

    def _build(self) -> None:
        lay      = QVBoxLayout(self)
        yay_info = (f" | AUR Helper: 'yay' {'detected' if self._yay_installed else 'not detected'}" if self._distro.has_aur else "")
        info = QLabel(f"Recognized Linux distribution: {self._distro.distro_pretty_name} | Session: {self._session}{yay_info}")
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
            f"Tips:\n\n"
            f"'Basic Packages' will be installed using '{cmd}PACKAGE'.\n\n"
            f"'AUR Packages' provides access to the Arch User Repository. "
            f"Therefore 'yay' must and will be installed."
            f"\nThis feature is available only on Arch Linux based distributions.\n\n"
            f"You can also define 'Specific Packages'. These packages will be installed only (using '{cmd}PACKAGE')\n"
            f"if the corresponding session has been recognized.\n"
            f"Both full desktop environments and window managers such as 'Hyprland' and others are supported.")
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

        for row_specs in [[("System Manager Operations", self._edit_ops), ("System Files", self._edit_sysfiles)],
                          [("Basic Packages",    lambda: self._edit_pkgs("basic_packages")),
                           ("AUR Packages",      lambda: self._edit_pkgs("aur_packages")),
                           ("Specific Packages", lambda: self._edit_pkgs("specific_packages"))]]:
            row = QHBoxLayout()
            for label, fn in row_specs:
                b = QPushButton(label)
                b.clicked.connect(fn)
                row.addWidget(b)
            lay.addLayout(row)

        close = QPushButton("Close")
        close.clicked.connect(self.close)
        lay.addWidget(close)

    def _save_shell(self) -> None:
        sel = self._shell_cb.currentText()
        if sel in USER_SHELLS and sel != S.user_shell:
            S.user_shell = sel
            save_profile()

    def _edit_ops(self) -> None:
        arch_only = {"update_mirrors", "install_yay", "install_aur_packages"}
        op_text = {k: v.replace("&&", "&")
                   for k, v in _build_op_text(self._distro, self._session, has_yay=self._yay_installed).items()}
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

        yay_cb = next((c for c, k in widgets if k == "install_yay"),          None)
        aur_cb = next((c for c, k in widgets if k == "install_aur_packages"), None)

        enabled_widgets = [c for c, _ in widgets if c.isEnabled()]

        def _sync_sa():
            if not enabled_widgets:
                return
            n = sum(c.isChecked() for c in enabled_widgets)
            sa.blockSignals(True)
            sa.setCheckState(Qt.CheckState.Checked
                             if n == len(enabled_widgets) else Qt.CheckState.Unchecked if n == 0 else Qt.CheckState.PartiallyChecked)
            sa.blockSignals(False)

        def _sync_aur_dep():
            if aur_cb and yay_cb and self._distro.supports_aur():
                force = aur_cb.isChecked()
                yay_cb.setChecked(force or yay_cb.isChecked())
                yay_cb.setEnabled(not force)
                yay_cb.setStyleSheet(style_checkbox_muted() if force else "")
            _sync_sa()

        def _toggle_all(state: int) -> None:
            checked = Qt.CheckState(state) != Qt.CheckState.Unchecked
            for _cb, _ in widgets:
                if not _cb.isEnabled():
                    continue
                _cb.blockSignals(True)
                _cb.setChecked(checked)
                _cb.blockSignals(False)
            _sync_aur_dep()

        sa.stateChanged.connect(_toggle_all)
        for cb, key in widgets:
            cb.stateChanged.connect(_sync_aur_dep if key == "install_aur_packages" else _sync_sa)
        _sync_aur_dep()

        def _save(dlg):
            S.system_manager_ops = [k for cb_, k in widgets if cb_.isChecked()]
            save_profile()
            QMessageBox.information(self, "Saved", "Operations saved.")
            dlg.accept()

        _scroll_dlg(self, "System Manager Operations", body, _save)[0].exec()

    def _edit_sysfiles(self) -> None:
        files = [f for f in (S.system_files or []) if isinstance(f, dict) and f.get("source") and f.get("destination")]
        checkboxes: list[tuple[TriCheckBox, dict]] = []
        legend = tri_state_legend_html()

        body = QWidget()
        vlay = QVBoxLayout(body)
        vlay.setSpacing(4)

        for f in files:
            text = f"{apply_replacements(f['source'])} 󰧂 {apply_replacements(f['destination'])}"
            tip = (f"<b>Source:</b><br>{f['source']}<br><br><b>Destination:</b><br>{f['destination']}<br><br>"
                   f"<i>Left-click file to change status. Right-click to edit.</i><br><br>{legend}")
            cb = _make_tri_cb(text, f.get("disabled", False), tip)
            checkboxes.append((cb, f))
            vlay.addWidget(cb)

        if checkboxes:
            _add_select_all_tri(vlay, [cb for cb, _ in checkboxes])

        def _save(_dlg):
            to_del = [f for _cb_, f in checkboxes if _cb_.checkState() == _STATE_DELETE]
            if to_del:
                names = "\n".join(f"  • {apply_replacements(f.get('source', '?'))}" for f in to_del)
                if QMessageBox.question(_dlg, "Confirm Delete",
                                        f"The following system file(s) will be permanently removed:\n\n{names}\n\nContinue?",
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                    return
            S.system_files = [{**f, "disabled": __cb.checkState() == _STATE_DISABLED}
                              for __cb, f in checkboxes if __cb.checkState() != _STATE_DELETE]
            save_profile()
            _dlg.accept()

        dlg, lay = _scroll_dlg(self, "System Files", body, _save)

        def _set_ctx(widget, f_dict, d):
            widget.contextMenuEvent = lambda _e: self._edit_sysfile_entry(f_dict, d)

        for cb, f in checkboxes:
            _set_ctx(cb, f, dlg)

        search = QLineEdit()
        search.setPlaceholderText("Filter files...")

        def _apply_search(txt: str) -> None:
            txt_lower = txt.lower()
            for _cb, _ in checkboxes:
                _cb.setVisible(txt_lower in _cb.text().lower())

        search.textChanged.connect(_apply_search)
        btn_row = QHBoxLayout()
        add_btn = QPushButton("➕ Add System File")
        add_btn.clicked.connect(lambda: (dlg.close(), QTimer.singleShot(100, self._add_sysfile)))
        btn_row.addWidget(add_btn)

        io_row = QHBoxLayout()
        for lbl, fn in [("📥 Import (.txt/.csv)", lambda: (dlg.close(), QTimer.singleShot(0, self._import_sysfiles))),
                        ("📤 Export (.txt)", lambda: self._export_sysfiles())]:
            b = QPushButton(lbl)
            b.clicked.connect(fn)
            io_row.addWidget(b)

        lay.insertWidget(1, search)
        lay.insertLayout(2, btn_row)
        lay.insertLayout(3, io_row)
        lay.setStretch(0, 1)
        dlg.exec()

    def _add_sysfile(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Add System File/Folder")
        box.setText("Choose the source type:")
        file_btn   = box.addButton("📄 File(s)", QMessageBox.ButtonRole.YesRole)
        box.addButton("📁 Directory",  QMessageBox.ButtonRole.NoRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == cancel_btn:
            return

        sources = (QFileDialog.getOpenFileNames(self, "Select file(s)")[0] if clicked == file_btn
                   else [d] if (d := QFileDialog.getExistingDirectory(self, "Select directory")) else [])
        sources = [s for s in sources if s]
        if not sources:
            return

        dst_dir = QFileDialog.getExistingDirectory(self, "Select Destination Directory (e.g. /etc/...)")
        if not dst_dir:
            return

        S.system_files = S.system_files or []
        added = []
        for s in sources:
            src_path = Path(s).resolve()
            dst_path = Path(dst_dir) / src_path.name
            if not any(f.get("source") == str(src_path) for f in S.system_files if isinstance(f, dict)):
                S.system_files.append({"source": str(src_path), "destination": str(dst_path), "disabled": False})
                added.append(src_path.name)

        if added:
            S.system_files.sort(key=lambda x: x.get("source", "").lower())
            save_profile()
            QMessageBox.information(self, "Success", f"Added {len(added)} item(s).")
        QTimer.singleShot(0, self._edit_sysfiles)

    def _edit_sysfile_entry(self, f: Optional[dict], parent_dlg) -> None:
        if not f:
            return
        from PyQt6.QtGui import QFont, QFontMetrics
        fm = QFontMetrics(QFont("monospace"))
        max_len = max(len(f.get("source", "")), len(f.get("destination", "")))
        needed = fm.horizontalAdvance("m") * max_len + 400
        scr = QApplication.primaryScreen()
        max_w = (scr.availableGeometry().width() - 80) if scr else 1600
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit System File")
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setMinimumWidth(min(max(1000, needed), max_w))
        dlg.setMinimumHeight(280)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(12)
        lay.setContentsMargins(16, 16, 16, 16)
        src_ed = QLineEdit(f.get("source", ""))
        dst_ed = QLineEdit(f.get("destination", ""))
        for ed in (src_ed, dst_ed):
            ed.setMinimumHeight(36)
        for label, ed in [("Source:", src_ed), ("Destination:", dst_ed)]:
            lay.addWidget(QLabel(label))
            lay.addWidget(_browse_field(dlg, ed))
        lay.addStretch()
        lay.addWidget(_ok_cancel_buttons(dlg, dlg.accept))
        if dlg.exec() == QDialog.DialogCode.Accepted:
            src, dst = src_ed.text().strip(), dst_ed.text().strip()
            if src and dst:
                f["source"], f["destination"] = src, dst
                save_profile()
                parent_dlg.accept()
                QTimer.singleShot(0, self._edit_sysfiles)

    def _import_sysfiles(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import System Files", str(_HOME), "Data (*.txt *.csv)")
        if not path:
            QTimer.singleShot(0, self._edit_sysfiles)
            return
        lines = _read_import_file(self, path)
        if lines is None:
            QTimer.singleShot(0, self._edit_sysfiles)
            return

        S.system_files = S.system_files or []
        existing: set[tuple] = {(f["source"], f["destination"]) for f in S.system_files if isinstance(f, dict)}
        added = skipped_dup = skipped_inv = 0

        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in (line.split("\t", 1) if "\t" in line else line.split(",", 1))]
            if len(parts) != 2:
                skipped_inv += 1
                continue
            src, dst = parts
            if not src or not dst:
                skipped_inv += 1
                continue
            if not (src.startswith("/") or src.startswith("~")) or not (dst.startswith("/") or dst.startswith("~")):
                skipped_inv += 1
                continue
            src = str(Path(src).expanduser())
            dst = str(Path(dst).expanduser())
            if (src, dst) in existing:
                skipped_dup += 1
                continue
            S.system_files.append({"source": src, "destination": dst, "disabled": False})
            existing.add((src, dst))
            added += 1

        if added:
            S.system_files.sort(key=lambda x: x.get("source", "").lower())
            save_profile()

        parts_msg = [f"Imported: {added}"]
        if skipped_dup: parts_msg.append(f"Skipped (duplicate): {skipped_dup}")
        if skipped_inv: parts_msg.append(f"Skipped (invalid format): {skipped_inv}")
        QMessageBox.information(self, "Import Complete", "\n".join(parts_msg))
        QTimer.singleShot(0, self._edit_sysfiles)

    def _export_sysfiles(self) -> None:
        files = [f for f in (S.system_files or []) if isinstance(f, dict) and f.get("source") and f.get("destination")]
        if not files:
            QMessageBox.information(self, "Export", "No system files to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export System Files", str(_HOME / "system_files.txt"), "Text (*.txt);;CSV (*.csv);;All (*)")
        if not path:
            return
        lines = ["# source\tdestination"] + [f"{f['source']}\t{f['destination']}" for f in files]
        try:
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            QMessageBox.information(self, "Exported", f"Exported {len(files)} entry/entries to:\n{path}")
        except OSError as exc:
            QMessageBox.critical(self, "Export Error", str(exc))

    def _edit_pkgs(self, pkg_type: str) -> None:
        is_specific = pkg_type == "specific_packages"
        packages = getattr(S, pkg_type, []) or []
        checkboxes = _pkg_checkboxes(packages, is_specific)

        body = QWidget()
        grid = QGridLayout(body)
        grid.setSpacing(6)
        cols = 5

        if is_specific:
            from collections import defaultdict
            groups: dict = defaultdict(list)
            for p, cb in zip(packages, checkboxes):
                groups[p.get("session", "") if isinstance(p, dict) else ""].append((cb, p))
            row = 0
            t = current_theme()
            for idx, sess in enumerate(sorted(groups)):
                hdr = QLabel(sess or "Unknown")
                border = f"border-top:1px solid {t['header_sep']};" if idx > 0 else ""
                hdr.setStyleSheet(
                    f"font-size:{font_sz(-1)}px;font-weight:bold;color:{t['accent2']};padding:6px 2px 2px;{border}")
                grid.addWidget(hdr, row, 0, 1, cols)
                row += 1
                for j, (cb, _) in enumerate(groups[sess]):
                    grid.addWidget(cb, row + j // cols, j % cols)
                row += (len(groups[sess]) - 1) // cols + 1
        else:
            for i, cb in enumerate(checkboxes):
                grid.addWidget(cb, i // cols, i % cols)

        if checkboxes:
            _add_select_all_tri(grid, checkboxes, cols)

        def _save(_dlg):
            to_del = [pkg for _cb, pkg in zip(checkboxes, packages) if _cb.checkState() == _STATE_DELETE]
            if to_del:
                names = [(f"{pkg.get('package', '')} [{pkg.get('session', '')}]"
                          if is_specific else pkg.get("name", "")) if isinstance(pkg, dict) else str(pkg) for pkg in to_del]
                if (QMessageBox.question(_dlg, "Confirm Delete", f"Delete package(s)?\n\n  • " + "\n  • ".join(names),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes):
                    return
            updated = []
            for _cb, pkg in zip(checkboxes, packages):
                if _cb.checkState() == _STATE_DELETE:
                    continue
                d = pkg if isinstance(pkg, dict) else {"name": str(pkg)}
                updated.append({**d, "disabled": _cb.checkState() == _STATE_DISABLED})
            setattr(S, pkg_type, updated)
            save_profile()
            _dlg.accept()

        title = f"Edit {pkg_type.replace('_', ' ').title()}"
        dlg, lay = _scroll_dlg(self, title, body, _save)

        search = QLineEdit()
        search.setPlaceholderText("Filter...")

        def _apply_search(txt: str) -> None:
            txt_lower = txt.lower()
            for _cb in checkboxes:
                _cb.setVisible(txt_lower in _cb.text().lower())

        search.textChanged.connect(_apply_search)
        btn_add_row = QHBoxLayout()
        for lbl, fn in [("➕ Add", lambda: self._add_pkg(pkg_type)), ("➕➕ Batch Add", lambda: self._batch_add(pkg_type))]:
            b = QPushButton(lbl)
            b.clicked.connect(lambda _, f=fn: (dlg.close(), QTimer.singleShot(0, f)))
            btn_add_row.addWidget(b)

        io_row = QHBoxLayout()
        for lbl, fn in [("📥 Import", lambda: self._import_pkgs(pkg_type)), ("📤 Export", lambda: self._export_pkgs(pkg_type))]:
            b = QPushButton(lbl)
            b.clicked.connect(lambda _, f=fn, l=lbl: (dlg.close(), QTimer.singleShot(0, f)) if "Import" in l else f())
            io_row.addWidget(b)

        lay.insertWidget(1, search)
        lay.insertLayout(2, btn_add_row)
        lay.insertLayout(3, io_row)
        lay.setStretch(0, 1)

        def _set_p_ctx(widget, p_data, d):
            widget.contextMenuEvent = lambda _e: self._edit_pkg_entry((widget, p_data), pkg_type, d)

        for cb, p in zip(checkboxes, packages):
            _set_p_ctx(cb, p, dlg)
        dlg.exec()

    def _add_pkg(self, pkg_type: str) -> None:
        is_specific = pkg_type == "specific_packages"
        if is_specific:
            result = _pkg_form_dialog(self, "Add Specific Package", prefill_sess=SESSIONS[0] if SESSIONS else "")
            if result is None:
                QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))
                return
            name, sess = result
            S.specific_packages = S.specific_packages or []
            if any(isinstance(p, dict) and p.get("package") == name and p.get("session") == sess
                   for p in S.specific_packages):
                QMessageBox.warning(self, "Duplicate", f"'{name}' for '{sess}' already exists.")
            else:
                S.specific_packages.append({"package": name, "session": sess, "disabled": False})
                S.specific_packages.sort(key=lambda x: x.get("package", "").lower())
                save_profile()
                QMessageBox.information(self, "Added", f"Added:\n\n  • {name} [{sess}]")
        else:
            label    = pkg_type.replace("_", " ").title().rstrip("s")
            name, ok = _ask_text(self, f"Add {label}", "Package name:")
            if ok and name.strip():
                name    = name.strip()
                current = getattr(S, pkg_type, []) or []
                existing = {p.get("name") if isinstance(p, dict) else p for p in current}
                if name in existing:
                    QMessageBox.warning(self, "Duplicate", f"'{name}' already exists.")
                else:
                    current.append({"name": name, "disabled": False})
                    setattr(S, pkg_type, sorted(current, key=lambda x: x.get("name", "").lower()))
                    save_profile()
                    QMessageBox.information(self, "Added", f"Added package(s):\n\n  • {name}")
        QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))

    def _edit_pkg_entry(self, cb_pkg: tuple, pkg_type: str, parent_dlg) -> None:
        cb, p = cb_pkg
        if p is None:
            return
        is_specific = pkg_type == "specific_packages"
        current_name = p.get("package" if is_specific else "name", "")
        current_sess = p.get("session", "") if is_specific else None
        result = _pkg_form_dialog(self, "Edit Package", prefill_name=current_name, prefill_sess=current_sess if is_specific else None)
        if result is None:
            return
        name = result[0]
        if is_specific:
            p["package"], p["session"] = name, result[1]
        else:
            p["name"] = name
        pkg_list = getattr(S, pkg_type, [])
        _sort_pkgs(pkg_list, is_specific)
        save_profile()
        parent_dlg.accept()
        QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))

    def _batch_add(self, pkg_type: str) -> None:
        is_specific = pkg_type == "specific_packages"
        dlg = QDialog(self)
        dlg.setWindowTitle("Batch Add")
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setMinimumSize(700, 500)
        lay = QVBoxLayout(dlg)
        sess_cb: Optional[QComboBox] = None
        if is_specific:
            row = QHBoxLayout()
            row.addWidget(QLabel("Session:"))
            sess_cb = QComboBox()
            sess_cb.addItems(SESSIONS)
            sess_cb.setMinimumHeight(32)
            row.addWidget(sess_cb)
            row.addStretch()
            lay.addLayout(row)
        te = QTextEdit()
        te.setPlaceholderText("One package per line")
        lay.addWidget(te)
        lay.addWidget(_ok_cancel_buttons(dlg, dlg.accept))

        if dlg.exec() != QDialog.DialogCode.Accepted:
            QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))
            return

        current = getattr(S, pkg_type, []) or []
        names = [line.strip() for line in te.toPlainText().splitlines() if line.strip() and all(c.isalnum() or c in "-_.+" for c in line.strip())]
        added, dupes = [], []

        if is_specific and sess_cb:
            sess = sess_cb.currentText()
            existing = {(p.get("package"), p.get("session")) for p in current if isinstance(p, dict)}
            for name in names:
                if (name, sess) in existing:
                    dupes.append(name)
                else:
                    current.append({"package": name, "session": sess, "disabled": False})
                    existing.add((name, sess))
                    added.append(name)
        else:
            existing = {p.get("name") if isinstance(p, dict) else p for p in current}
            for name in names:
                if name in existing:
                    dupes.append(name)
                else:
                    current.append({"name": name, "disabled": False})
                    existing.add(name)
                    added.append(name)

        _sort_pkgs(current, is_specific)
        setattr(S, pkg_type, current)
        save_profile()

        msg = f"Added {len(added)} package(s):\n\n" + "\n".join(f"  • {n}" for n in added)
        if dupes:
            msg += f"\n\nSkipped (duplicate): {len(dupes)}\n" + "\n".join(f"  • {n}" for n in dupes)
        QMessageBox.information(self, "Batch Add", msg)
        QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))

    def _import_pkgs(self, pkg_type: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import", str(_HOME), "Data (*.txt *.csv)")
        if not path:
            QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))
            return

        lines = _read_import_file(self, path)
        if lines is None:
            QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))
            return

        is_specific = (pkg_type == "specific_packages")
        current = getattr(S, pkg_type, []) or []

        existing: set

        if is_specific:
            existing = {(p.get("package"), p.get("session")) for p in current if isinstance(p, dict)}
        else:
            existing = {p.get("name") if isinstance(p, dict) else p for p in current}

        added = 0

        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            parts = [p.strip().strip("\"'") for p in line.split(",")]
            if not parts:
                continue

            name = parts[0]
            if not all(c.isalnum() or c in "-_.+" for c in name):
                continue

            if is_specific:
                sess = parts[1] if len(parts) > 1 else (SESSIONS[0] if SESSIONS else "unknown")
                disabled = len(parts) > 2 and parts[2].strip().lower() == "disabled"  # NEU
                if (name, sess) not in existing:
                    current.append({"package": name, "session": sess, "disabled": disabled})
                    existing.add((name, sess))
                    added += 1
            else:
                if name not in existing:
                    current.append({"name": name, "disabled": False})
                    existing.add(name)
                    added += 1

        if added:
            _sort_pkgs(current, is_specific)
            setattr(S, pkg_type, current)
            save_profile()
            QMessageBox.information(self, "Import Complete", f"Successfully imported {added} packages.")

        QTimer.singleShot(0, lambda: self._edit_pkgs(pkg_type))

    def _export_pkgs(self, pkg_type: str) -> None:
        packages = getattr(S, pkg_type, []) or []
        if not packages:
            QMessageBox.information(self, "Export", "No packages to export.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export", str(_HOME / f"{pkg_type}.txt"), "Text (*.txt);;All (*)")
        if not path:
            return
        is_specific = pkg_type == "specific_packages"
        lines = [(f"{p.get('package', '')},{p.get('session', '')}" + (",disabled" if p.get("disabled") else ""))
        if is_specific else (p.get("name", "")) for p in packages]
        try:
            Path(path).write_text("\n".join(entry for entry in lines if entry) + "\n", encoding="utf-8")
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
        self._yay_installed  = self._distro.has_aur and self._distro.package_is_installed("yay")
        self._sudo_checkbox: QCheckBox | None = None
        self._sm_thread = None
        self._sm_dialog = None

    def launch(self) -> None:
        if not S.system_manager_ops:
            QMessageBox.information(self.parent, "No Operations Configured",
                                    "System Manager has no operations selected yet.\n\n"
                                    "Please configure what should be executed under 'System Manager Operations' first.")
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
        ops = S.system_manager_ops
        op_text = {k: v.replace("&&", "&") for k, v in _build_op_text(self._distro, self._session).items()}
        _, _, tips = generate_tooltip()
        dialog = QDialog(self.parent)
        dialog.setWindowTitle("System Manager")
        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(0, 0, 0, 0)
        yay_info = ""
        if self._distro.has_aur:
            yay_info = ("   |   AUR Helper: 'yay' detected" if self._yay_installed else "   |   AUR Helper: 'yay' not detected")

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        distro_lbl = QLabel(f"Recognized Linux distribution: {self._distro_name}   |   Session: {self._session}{yay_info}")
        distro_lbl.setStyleSheet(style_label_info(font_size=font_sz(6)))
        distro_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(distro_lbl)
        ops_lbl = QLabel(f"<span style='font-size:{font_sz(6)}px;font-family:monospace;'>"
                         "<br>System Manager will perform the following operations:<br></span>")
        ops_lbl.setTextFormat(Qt.TextFormat.RichText)
        ops_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(ops_lbl)

        display_num = 0
        for key in ops:
            if key not in op_text:
                continue
            display_num += 1
            tooltip = tips.get(key, "")
            has_tip = bool(tooltip)
            colour, decoration = style_op_label(has_tip)
            icon = "󰔨 " if has_tip else ""
            html = f"{icon}   <span style='font-size:{font_sz(2)}px;padding:5px; color:{colour};{decoration}'>{op_text[key]}</span>"
            row = QHBoxLayout()
            num = QLabel(f"{display_num}:")
            num.setStyleSheet(style_label_mono(font_size=font_sz(2)))
            lbl = QLabel(html)
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setStyleSheet(style_label_mono(font_size=font_sz(2)))
            apply_tooltip(lbl, tooltip)
            row.addWidget(num)
            row.addWidget(lbl)
            row.addStretch(1)
            content_layout.addLayout(row)

        confirm = QLabel(f"<span style='font-size:{font_sz(2)}px;'>Start System Manager?<br>"
                         "(Check 'Enter sudo password' if privileged commands require a password)<br></span>")
        confirm.setTextFormat(Qt.TextFormat.RichText)
        confirm.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._sudo_checkbox = QCheckBox("Enter sudo password 󰔨")
        self._sudo_checkbox.setStyleSheet(style_sudo_checkbox(muted=False))
        if self.failed_attempts:
            self._sudo_checkbox.setText("Sudo password must be entered! 󰔨")
            self._sudo_checkbox.setChecked(True)
            self._sudo_checkbox.setEnabled(False)
            self._sudo_checkbox.setStyleSheet(style_sudo_checkbox(muted=True))

        apply_tooltip(self._sudo_checkbox,
                      "<b>How your sudo password is used — and why it is safe:</b><br><br>"
                      "Your password is held <b>only in memory</b> as a mutable <code>bytearray</code> "
                      "(via <code>SecureString</code>) — it is <b>never written to any file</b>, "
                      "not even to a RAM-backed <code>tmpfs</code> such as <code>/dev/shm</code>.<br><br>"
                      "All privileged operations (package installs, service activation, file copies…) "
                      "run through <code>subprocess.Popen</code> with a dedicated writer thread: "
                      "a <code>bytearray</code> copy of the password is written to the "
                      "<b>kernel pipe buffer</b> (stdin of <code>sudo -S</code>) and then "
                      "<b>zeroed byte-by-byte inside that thread</b>, immediately after the pipe is flushed. "
                      "The password never touches a file, an environment variable, or a command-line argument.<br><br>"
                      "Simple status checks (e.g. <code>systemctl is-active</code>) that may also "
                      "require sudo use <code>subprocess.run</code> with a transient <code>bytes</code> "
                      "object — it cannot be actively zeroed, but exists only for the duration of the "
                      "blocking call and is then garbage-collected.<br><br>"
                      "The original <code>SecureString</code> buffer is zeroed in the <code>finally</code> "
                      "block of the worker thread once all tasks are complete.<br><br>"
                      "<b>Credential cache:</b><br>"
                      "After the single successful authentication <code>sudo</code> stores a credential "
                      "timestamp (in <code>/run/sudo/ts/</code>). A background keepalive thread calls "
                      "<code>sudo -v</code> every 4 min so the cache never expires during a long session — "
                      "no further password input or file I/O is ever required.<br><br>"
                      "<b>Cleanup:</b><br>"
                      "When System Manager finishes, <code>sudo -k</code> is called to <b>immediately "
                      "invalidate</b> the credential cache, and the <code>SecureString</code> buffer "
                      "is zeroed.<br><br>"
                      "<i>Your password is never logged, never sent over the network, "
                      "never written to any file, and never stored beyond this session.</i>")

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)  # type: ignore
        bb.accepted.connect(dialog.accept)
        bb.rejected.connect(dialog.reject)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._sudo_checkbox)
        btn_row.addWidget(bb)
        content_layout.addWidget(confirm)
        content_layout.addLayout(btn_row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setWidget(content_widget)
        outer.addWidget(scroll)

        scr = QApplication.primaryScreen()
        if scr:
            sg = scr.availableGeometry()
            sz = content_widget.sizeHint()
            dialog.resize(min(sz.width() + 40, sg.width()), min(sz.height() + 40, sg.height()))
        bb.button(QDialogButtonBox.StandardButton.No).setFocus()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if self._sudo_checkbox and self._sudo_checkbox.isChecked():
            self._show_sudo_dialog()
        else:
            self._start_thread("")

    def _start_thread(self, pw: "SecureString | str") -> None:
        from system_manager import SystemManagerDialog, SystemManagerThread
        self._sm_thread = SystemManagerThread(pw, distro=self._distro)
        self._sm_dialog = SystemManagerDialog(self.parent)
        t, d = self._sm_thread, self._sm_dialog
        t.thread_started.connect(lambda: d.exec())
        t.outputReceived.connect(d.on_output)
        t.taskListReady.connect(d.on_task_list)
        t.taskStatusChanged.connect(d.on_task_status)
        t.passwordFailed.connect(lambda: self._on_fail(t, d))
        t.passwordSuccess.connect(self._on_ok)
        t.finished.connect(d.mark_done)
        d.cancelRequested.connect(lambda: setattr(t, "terminated", True))
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
        dialog.on_output(f"<p style='color:{t['error']};font-size:17px;font-weight:bold;'>"
                         "Authentication failed. Cancelled to prevent account lockout.<br>"
                         "Possible causes: incorrect password, user not in sudoers.</p>", "info")
        dialog.mark_done(failed_count=self.failed_attempts)
        thread.terminated = True

    def _on_ok(self) -> None:
        self.failed_attempts = 0
        if self.parent:
            self.parent.sm_failed_attempts = 0
