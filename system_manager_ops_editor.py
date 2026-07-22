import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QLabel, QMessageBox, QPushButton,
    QGridLayout, QHBoxLayout,
    QCheckBox, QComboBox, QWidget
)

from firewall_rules import firewall_rules_tooltip, FirewallSettingsDialog
from linux_distro_helper import USER_SHELLS, ARCH_KERNEL_VARIANTS
from state import S, _USER, logger, save_profile
from themes import (
    apply_tooltip, style_checkbox_muted, style_checkbox_select_all,
    current_theme, font_sz
)
from ui_utils import sep

from system_manager_helpers import (
    _detect_boot_info, _compute_op_status, _build_op_text,
    _raw_to_checkbox_text, _raw_to_tips, _BR_RE, _scroll_dlg,
    _detect_effective_aur_helper, _is_helper_present
)

if TYPE_CHECKING:
    from linux_distro_helper import LinuxDistroHelper
    _OpsMixinBase = QWidget
else:
    _OpsMixinBase = object


class _OpsEditorMixin(_OpsMixinBase):
    # These attributes/properties are provided by the concrete class this mixin
    # is combined with (see SystemManagerOptions in system_manager_options.py).
    # Declared here only for the static type checker; no effect at runtime.
    if TYPE_CHECKING:
        _distro: "LinuxDistroHelper"
        _session: str | None
        aur_helper_installed: bool

    def _edit_ops(self):
        bootloader, current_variant, _system_default_variant = _detect_boot_info()
        bl_label = {"grub": "GRUB", "systemd-boot": "systemd-boot"}.get(bootloader, "unknown bootloader")

        _saved_default_variant = S.default_kernel or _system_default_variant

        arch_only = {"update_mirrors", "install_aur_helper", "install_aur_packages", "install_kernels",
                     "set_default_kernel"}
        _installed_kernels = self._distro.detect_installed_kernel_variants()
        _installed_kernels.add(current_variant)

        if self._distro.family() == "arch":
            _variants_with_headers: set[str] = {v for v, pkgs in ARCH_KERNEL_VARIANTS.items()
                                                if len(pkgs) >= 2 and self._distro.package_is_installed(pkgs[1])}
        else:
            _variants_with_headers = set()

        _op_status = _compute_op_status(self._distro, self.aur_helper_installed, _system_default_variant,
                                        installed_kernels=_installed_kernels)

        _raw_op = _build_op_text(self._distro, self._session, aur_helper_installed=self.aur_helper_installed,
                                 system_default_variant=_system_default_variant, op_status=_op_status,
                                 installed_kernels=_installed_kernels)

        op_text = _raw_to_checkbox_text(_raw_op)
        op_tips = _raw_to_tips(_raw_op)
        widgets: list[tuple[QCheckBox, str]] = []

        _OP_GROUPS = [("🖥  System", ["copy_dotfiles", "update_mirrors", "update_system", "set_user_shell",
                                     "install_ucode", "install_kernels", "install_kernel_headers",
                                     "set_default_kernel"]),
                      ("📦  Packages", ["install_basic_packages", "install_aur_helper", "install_aur_packages",
                                       "install_specific_packages", "enable_flatpak_integration"]),
                      ("🔧  Services",
                       ["enable_printer_support", "enable_ssh_service", "enable_samba_network_filesharing",
                        "enable_bluetooth_service", "enable_atd_service", "enable_cronie_service",
                        "install_snap", "enable_ntp_sync", "enable_firewall"]),
                      ("🧹  Maintenance",
                       ["remove_orphaned_packages", "clean_cache", "clean_journal_logs", "enable_fstrim_timer"])]

        _KERNEL_VARIANTS = list(ARCH_KERNEL_VARIANTS.keys())

        body = QWidget()
        grid = QGridLayout(body)
        grid.setVerticalSpacing(3)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 1)

        t = current_theme()

        sa = QCheckBox("Check/Uncheck All")
        sa.setTristate(True)
        sa.setStyleSheet(style_checkbox_select_all())

        bl_color = t["success"] if bootloader != "unknown" else t["warning"]
        if self._distro.has_aur:
            _eff_helper, _helper_ok = _detect_effective_aur_helper(self._distro)
            _aur_color = t["success"] if _helper_ok else t["warning"]
            _aur_status = "detected" if _helper_ok else "not detected"
            _aur_prefix = f"AUR Helper: <b style='color:{_aur_color};'>'{_eff_helper}' {_aur_status}</b>   |   "
        else:
            _aur_prefix = ""
        bl_lbl = QLabel(f"{_aur_prefix}Detected bootloader: <b style='color:{bl_color};'>{bl_label}</b>   |   "
                        f"Running kernel: <b style='color:{t['accent2']};'>{current_variant}</b>")
        bl_lbl.setTextFormat(Qt.TextFormat.RichText)
        bl_lbl.setStyleSheet(f"font-size:{font_sz(-1)}px; padding:2px 4px;")
        bl_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        header_row_w = QWidget()
        header_row_l = QHBoxLayout(header_row_w)
        header_row_l.setContentsMargins(0, 0, 0, 0)
        header_row_l.addWidget(sa)
        header_row_l.addStretch()
        header_row_l.addWidget(bl_lbl)
        grid.addWidget(header_row_w, 0, 0, 1, 2)

        grid_row = 1
        _set_default_cb: QCheckBox | None = None
        _default_kernel_combo: QComboBox | None = None
        _install_kernels_cb: QCheckBox | None = None
        _kernel_sub_cbs: dict[str, QCheckBox] = {}

        _set_shell_cb: QCheckBox | None = None
        _user_shell_combo: QComboBox | None = None
        _aur_helper_combo: QComboBox | None = None

        for group_label, keys in _OP_GROUPS:
            grid.addWidget(sep(), grid_row, 0, 1, 2)
            grid_row += 1

            hdr = QLabel(group_label)
            hdr.setStyleSheet(f"font-size:{font_sz(1)}px;font-weight:bold;color:{t['accent']};padding:4px 2px 2px 4px;")
            grid.addWidget(hdr, grid_row, 0, 1, 2)
            grid_row += 1

            for key in keys:
                text = op_text.get(key)
                if text is None:
                    continue

                _cb_tip = firewall_rules_tooltip() if key == "enable_firewall" else op_tips.get(key, "")
                icon = "󰔨  " if _cb_tip else ""

                cb = QCheckBox(f"{icon}{text}")
                cb.setStyleSheet("margin-left:14px;")
                if _cb_tip:
                    apply_tooltip(cb, _cb_tip, wrap=(key != "enable_firewall"))

                is_arch = key in arch_only
                unsupported = (is_arch and self._distro.family() != "arch") or \
                              (key == "enable_firewall" and not self._distro.firewall_supported()) or \
                              (key == "enable_ntp_sync" and not self._distro.ntp_supported()) or \
                              (key == "enable_fstrim_timer" and not shutil.which("systemctl"))

                if unsupported:
                    cb.setVisible(False)
                else:
                    cb.setChecked(key in S.system_manager_ops)

                if key == "install_aur_helper":
                    if not unsupported:
                        _paru_inst = _is_helper_present(self._distro, "paru")
                        _yay_inst = _is_helper_present(self._distro, "yay")
                        _default_combo_helper = "paru" if _paru_inst else ("yay" if _yay_inst else S.aur_helper)

                        aur_combo = QComboBox()
                        aur_combo.addItems(["yay", "paru"])
                        aur_combo.setCurrentText(_default_combo_helper)
                        aur_combo.setMinimumHeight(30)
                        aur_combo.setFixedWidth(200)
                        aur_combo.setEnabled(cb.isEnabled() and cb.isChecked())

                        grid.addWidget(cb, grid_row, 0)
                        grid.addWidget(aur_combo, grid_row, 1)
                        _aur_helper_combo = aur_combo
                        grid_row += 1

                elif key == "install_kernels":
                    _install_kernels_cb = cb
                    if not unsupported:
                        grid.addWidget(cb, grid_row, 0, 1, 2)
                        grid_row += 1

                        saved_kti = set(S.kernels_to_install or [])
                        sub_enabled = cb.isEnabled() and cb.isChecked()

                        sub_widget = QWidget()
                        sub_grid = QGridLayout(sub_widget)
                        sub_grid.setHorizontalSpacing(6)
                        sub_grid.setVerticalSpacing(2)
                        sub_grid.setContentsMargins(28, 0, 0, 0)

                        for i in range(0, len(_KERNEL_VARIANTS), 2):
                            for col_off, variant in enumerate(_KERNEL_VARIANTS[i:i + 2]):
                                is_current = (variant == current_variant)
                                is_installed = (variant in _installed_kernels)

                                suffixes = []
                                if is_current: suffixes.append("running")
                                if variant == _system_default_variant: suffixes.append("default")
                                if is_installed and not is_current: suffixes.append("installed")

                                if is_current:
                                    tip = "Currently running kernel — already installed, will be skipped."
                                elif is_installed:
                                    tip = f"{variant} is already installed — will be skipped."
                                else:
                                    tip = f"Include {variant} in the kernel installation."

                                icon_sub = "󰔨  " if tip else ""

                                display_label = variant + (f"   ← {' ← '.join(suffixes)}" if suffixes else "")
                                sub_cb = QCheckBox(f"{icon_sub}{display_label}")
                                never_saved = not S.kernels_to_install
                                sub_cb.setChecked(variant in saved_kti or (never_saved and is_installed))
                                sub_cb.setEnabled(sub_enabled)

                                apply_tooltip(sub_cb, tip, wrap=False)

                                if is_installed:
                                    sub_cb.setStyleSheet(style_checkbox_muted() + "QCheckBox{font-style:italic;}")
                                sub_grid.addWidget(sub_cb, i // 2, col_off)
                                _kernel_sub_cbs[variant] = sub_cb

                        grid.addWidget(sub_widget, grid_row, 0, 1, 2)
                        grid_row += (len(_KERNEL_VARIANTS) + 1) // 2

                elif key == "set_default_kernel":
                    _set_default_cb = cb
                    if not unsupported:
                        grid.addWidget(cb, grid_row, 0)
                        combo = QComboBox()
                        combo.setMinimumHeight(30)
                        combo.setFixedWidth(200)
                        combo.setEnabled(cb.isEnabled() and cb.isChecked())
                        grid.addWidget(combo, grid_row, 1)
                        _default_kernel_combo = combo
                        grid_row += 1

                elif key == "set_user_shell":
                    _set_shell_cb = cb
                    grid.addWidget(cb, grid_row, 0)
                    combo = QComboBox()
                    combo.addItems(USER_SHELLS)
                    if S.user_shell in USER_SHELLS:
                        combo.setCurrentText(S.user_shell)
                    combo.setMinimumHeight(30)
                    combo.setFixedWidth(200)
                    combo.setEnabled(cb.isEnabled() and cb.isChecked())
                    grid.addWidget(combo, grid_row, 1)
                    _user_shell_combo = combo
                    grid_row += 1

                elif key == "enable_firewall":
                    if not unsupported:
                        grid.addWidget(cb, grid_row, 0)
                        fw_btn = QPushButton("Firewall Settings")
                        fw_btn.setEnabled(cb.isEnabled() and cb.isChecked())
                        fw_btn.setMinimumHeight(30)
                        fw_btn.setFixedWidth(200)
                        cb.stateChanged.connect(
                            lambda state, b=fw_btn: b.setEnabled(state == Qt.CheckState.Checked.value))
                        fw_btn.clicked.connect(lambda: FirewallSettingsDialog(self).exec())
                        grid.addWidget(fw_btn, grid_row, 1)
                        grid_row += 1

                else:
                    if not unsupported:
                        grid.addWidget(cb, grid_row, 0, 1, 2)
                        grid_row += 1

                widgets.append((cb, key))

        def _refresh_labels() -> None:
            try:
                currently_selected = [v for v, sub in _kernel_sub_cbs.items() if sub.isChecked() and sub.isEnabled()]
                dk_override = (_default_kernel_combo.currentData() or "") if _default_kernel_combo is not None else None

                aur_override = None
                if _aur_helper_combo is not None:
                    aur_override = _aur_helper_combo.currentText()

                _dyn_status = dict(_op_status)
                if aur_override is not None:
                    _dyn_status["aur_helper_installed"] = (
                            self._distro.package_is_installed(aur_override)
                            or shutil.which(aur_override) is not None
                    )
                _dyn_status["kernels_all_installed"] = not any(
                    v for v in currently_selected if v not in _installed_kernels)
                if self._distro.family() == "arch":
                    _future = _installed_kernels | set(currently_selected)
                    _dyn_status["kernel_headers_installed"] = not any(
                        ARCH_KERNEL_VARIANTS.get(v) and len(
                            ARCH_KERNEL_VARIANTS[v]) >= 2 and v not in _variants_with_headers for v in _future
                    )
                if dk_override is not None:
                    _eff = (dk_override or _system_default_variant or "").strip()
                    _dyn_status["default_kernel_ok"] = bool(
                        _eff and _system_default_variant and _eff == _system_default_variant)

                if _user_shell_combo is not None:
                    shell_override = _user_shell_combo.currentText()
                    try:
                        import pwd as _pwd
                        binary = self._distro.get_shell_binary_name(shell_override)
                        current = _pwd.getpwnam(_USER).pw_shell
                        _dyn_status["shell_ok"] = (Path(current).name == binary)
                    except (KeyError, ImportError, AttributeError):
                        _dyn_status["shell_ok"] = False

                _refreshed_raw = _build_op_text(
                    self._distro, self._session, aur_helper_installed=None,
                    system_default_variant=_system_default_variant, op_status=_dyn_status,
                    installed_kernels=_installed_kernels,
                    kernels_to_install_override=currently_selected,
                    default_kernel_override=dk_override,
                    aur_helper_override=aur_override
                )
                _refreshed_text = _raw_to_checkbox_text(_refreshed_raw)

                _dyn_keys = {"install_kernels", "install_kernel_headers", "set_default_kernel",
                             "set_user_shell", "install_aur_helper", "install_aur_packages",
                             "clean_cache"}
                for _cb, _key in widgets:
                    if _key not in _dyn_keys or _key not in _refreshed_text:
                        continue
                    _icon = "󰔨  " if op_tips.get(_key) else ""
                    _text = _refreshed_text[_key]
                    _cb.setText(f"{_icon}{_BR_RE.sub(' ', _text).replace('&', '&&')}")
            except Exception as e:
                logger.error("_refresh_labels: %s", e, exc_info=True)

        if _aur_helper_combo is not None:
            _aur_helper_combo.currentIndexChanged.connect(lambda _: _refresh_labels())

        def _populate_default_combo():
            if _default_kernel_combo is None: return
            prev = _default_kernel_combo.currentData()
            _default_kernel_combo.blockSignals(True)
            _default_kernel_combo.clear()
            for _v in _KERNEL_VARIANTS:
                sub = _kernel_sub_cbs.get(_v)
                if _v in _installed_kernels or (sub and sub.isEnabled() and sub.isChecked()):
                    _default_kernel_combo.addItem(_v, _v)
            for cand in (prev, _saved_default_variant, current_variant):
                idx_ = _default_kernel_combo.findData(cand)
                if idx_ >= 0:
                    _default_kernel_combo.setCurrentIndex(idx_)
                    break
            _default_kernel_combo.blockSignals(False)
            _refresh_labels()

        if _install_kernels_cb is not None:
            _ikc = _install_kernels_cb
            _skc = _kernel_sub_cbs

            def _sync_kernel_subs():
                enabled = _ikc.isEnabled() and _ikc.isChecked()
                for _variant, sub_cb_ in _skc.items():
                    sub_cb_.setEnabled(enabled)
                _populate_default_combo()

            _ikc.stateChanged.connect(lambda _: _sync_kernel_subs())
            for _sub_cb in _kernel_sub_cbs.values():
                _sub_cb.stateChanged.connect(lambda _: _populate_default_combo())
            _sync_kernel_subs()

        _populate_default_combo()

        if _set_default_cb is not None and _default_kernel_combo is not None:
            _dk_cb, _dk_cmb = _set_default_cb, _default_kernel_combo

            def _sync_dk_combo():
                _dk_cmb.setEnabled(_dk_cb.isChecked() and _dk_cb.isEnabled())
                _refresh_labels()

            _dk_cb.stateChanged.connect(lambda _: _sync_dk_combo())
            _default_kernel_combo.currentIndexChanged.connect(lambda _: _refresh_labels())
            _sync_dk_combo()

        if _set_shell_cb is not None and _user_shell_combo is not None:
            _sh_cb, _sh_cmb = _set_shell_cb, _user_shell_combo

            def _sync_sh_combo():
                _sh_cmb.setEnabled(_sh_cb.isChecked() and _sh_cb.isEnabled())
                _refresh_labels()

            _sh_cb.stateChanged.connect(lambda _: _sync_sh_combo())
            _user_shell_combo.currentIndexChanged.connect(lambda _: _refresh_labels())
            _sync_sh_combo()

        _found_aur_cb = next((c for c, k in widgets if k == "install_aur_helper"), None)
        if _aur_helper_combo is not None and _found_aur_cb is not None:
            _aur_cmb, _aur_cb = _aur_helper_combo, _found_aur_cb

            def _sync_aur_combo():
                _aur_cmb.setEnabled(_aur_cb.isChecked() and _aur_cb.isEnabled())
                _refresh_labels()

            _aur_cb.stateChanged.connect(lambda _: _sync_aur_combo())
            _sync_aur_combo()

        def _sync_sa():
            currently_enabled = [c for c, _ in widgets if c.isEnabled() and c.isVisible()]
            if not currently_enabled: return
            n = sum(c.isChecked() for c in currently_enabled)
            sa.blockSignals(True)
            sa.setCheckState(Qt.CheckState.Checked if n == len(currently_enabled) else Qt.CheckState.Unchecked if n == 0
            else Qt.CheckState.PartiallyChecked)
            sa.blockSignals(False)

        helper_cb = next((c for c, k in widgets if k == "install_aur_helper"), None)
        aur_cb = next((c for c, k in widgets if k == "install_aur_packages"), None)

        def _sync_aur_dep():
            if aur_cb and helper_cb and self._distro.has_aur:
                force = aur_cb.isChecked()
                helper_cb.setChecked(force or helper_cb.isChecked())
                helper_cb.setEnabled(not force)
                helper_cb.setStyleSheet(
                    style_checkbox_muted() + "QCheckBox{margin-left:14px;}" if force else "QCheckBox{margin-left:14px;}")
            _sync_sa()

        def _toggle_all(state=None):
            checked = Qt.CheckState(state if state is not None else 0) != Qt.CheckState.Unchecked
            for _cb, _ in widgets:
                _cb.blockSignals(True)
                if checked and _cb.isEnabled() and _cb.isVisible():
                    _cb.setChecked(True)
                elif not checked:
                    _cb.setChecked(False)
                _cb.blockSignals(False)
            _sync_aur_dep()
            if _install_kernels_cb is not None:
                _sync_kernel_subs()

        sa.stateChanged.connect(_toggle_all)
        for cb, key in widgets:
            cb.stateChanged.connect(_sync_aur_dep if key == "install_aur_packages" else _sync_sa)
        _sync_aur_dep()

        def _save(dlg):
            S.system_manager_ops = [k for cb_, k in widgets if cb_.isChecked() and cb_.isVisible()]
            if _aur_helper_combo is not None:
                S.aur_helper = _aur_helper_combo.currentText()
            S.kernels_to_install = [v for v in _KERNEL_VARIANTS if
                                    _kernel_sub_cbs.get(v) and _kernel_sub_cbs[v].isChecked()
                                    and _kernel_sub_cbs[v].isEnabled()]
            if _default_kernel_combo and _set_default_cb:
                S.default_kernel = (_default_kernel_combo.currentData() or "") if _set_default_cb.isChecked() else ""
            if _user_shell_combo:
                S.user_shell = _user_shell_combo.currentText()
            save_profile()
            QMessageBox.information(self, "Saved", "Operations saved.")
            dlg.accept()

        _scroll_dlg_obj, _ = _scroll_dlg(self, "System Manager Operations", body, _save)
        QTimer.singleShot(0, _sync_sa)
        _scroll_dlg_obj.exec()
