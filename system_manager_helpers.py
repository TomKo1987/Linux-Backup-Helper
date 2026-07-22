import concurrent.futures
import json
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout,
    QLineEdit, QMessageBox, QScrollArea, QSizePolicy, QVBoxLayout, QWidget
)

from linux_distro_helper import LinuxDistroHelper, SESSIONS, ARCH_KERNEL_VARIANTS, is_valid_pkg_name
from state import S, _USER, save_profile, sort_pkg_list, sort_specific_pkg_list, logger
from themes import (
    tri_styles, apply_tooltip, style_checkbox_select_all, tri_state_legend_html
)
from tooltips import sm_tooltips
from ui_utils import ok_cancel_buttons, sep

_STATE_ACTIVE = Qt.CheckState.Checked
_STATE_DISABLED = Qt.CheckState.PartiallyChecked
_STATE_DELETE = Qt.CheckState.Unchecked
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _raw_to_label_html(raw: dict[str, tuple[str, str]]) -> dict[str, str]:
    import html as _html_mod
    return {k: _html_mod.escape(text).replace("&lt;br&gt;", "<br>") for k, (text, _) in raw.items()}


def _raw_to_checkbox_text(raw: dict[str, tuple[str, str]]) -> dict[str, str]:
    return {k: _BR_RE.sub(" ", text).replace("&", "&&") for k, (text, _) in raw.items()}


def _raw_to_tips(raw: dict[str, tuple[str, str]]) -> dict[str, str]:
    return {k: tip for k, (_, tip) in raw.items()}


class TriCheckBox(QCheckBox):

    def __init__(self, text: str = "", _parent=None):
        super().__init__(text, _parent)
        self.setTristate(True)

    def nextCheckState(self) -> None:
        s = self.checkState()
        next_state = (
            _STATE_DISABLED if s == _STATE_ACTIVE else _STATE_DELETE if s == _STATE_DISABLED else _STATE_ACTIVE)
        self.setCheckState(next_state)
        _update_tri_style(self)


def _update_tri_style(cb: QCheckBox) -> None:
    ss_active, ss_disabled, ss_delete = tri_styles()
    cb.setStyleSheet(
        {_STATE_ACTIVE: ss_active, _STATE_DISABLED: ss_disabled, _STATE_DELETE: ss_delete}[cb.checkState()])


def _make_tri_cb(text: str, disabled: bool, tooltip: str = "") -> TriCheckBox:
    cb = TriCheckBox(text)
    cb.setCheckState(_STATE_DISABLED if disabled else _STATE_ACTIVE)
    _update_tri_style(cb)
    apply_tooltip(cb, tooltip, wrap=False)
    return cb


def _scroll_dlg(_parent, title: str, body: QWidget, on_save=None) -> tuple[QDialog, QVBoxLayout]:
    dlg = QDialog(_parent)
    dlg.setWindowTitle(title)
    dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
    lay = QVBoxLayout(dlg)
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    sa.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    sa.setWidget(body)
    lay.addWidget(sa)
    bb = ok_cancel_buttons(dlg, lambda: on_save(dlg) if on_save else dlg.accept(), cancel_label="Close")
    lay.addWidget(bb)
    body.adjustSize()
    sz = body.sizeHint()
    scr = QApplication.primaryScreen()
    if scr:
        sg = scr.availableGeometry()
        width = min(max(sz.width() + 150, 950), sg.width() - 50)
        height = min(sz.height() + 225, int(sg.height() * 0.9))
        dlg.resize(width, height)
    cancel_btn = bb.button(QDialogButtonBox.StandardButton.Cancel)
    if cancel_btn:
        cancel_btn.setFocus()
    return dlg, lay


def _detect_boot_info() -> tuple[str, str, str]:
    bootloader = LinuxDistroHelper.detect_bootloader()
    current_variant = LinuxDistroHelper.detect_running_kernel_variant()
    system_default = LinuxDistroHelper.detect_system_default_kernel(bootloader) or current_variant
    return bootloader, current_variant, system_default


def _is_specific(pkg_type: str) -> bool:
    return pkg_type == "specific_packages"


def _commit_pkgs(pkg_type: str, updated: list) -> None:
    if _is_specific(pkg_type):
        sort_specific_pkg_list(updated)
    else:
        sort_pkg_list(updated)
    setattr(S, pkg_type, updated)
    save_profile()


def _pkg_checkboxes(packages: list, is_specific: bool) -> list[TriCheckBox]:
    result = []
    legend = tri_state_legend_html()
    for p in packages:
        if isinstance(p, dict):
            name = p.get("package" if is_specific else "name", str(p))
            disabled = p.get("disabled", False)
        else:
            name, disabled = str(p), False
        tip = (f"<b>Package:</b> {name}<br><br><i>Left-click package to change status. "
               f"Right-click to edit.</i><br><br>{legend}")
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

    def _sync_sa(*_):
        states = {cb.checkState() for cb in checkboxes}
        if len(states) == 1:
            new_state = states.pop()
        else:
            new_state = _STATE_DISABLED
        sa.blockSignals(True)
        sa.setCheckState(new_state)
        sa.blockSignals(False)
        _update_tri_style(sa)

    def _toggle(_state=None):
        target = sa.checkState()
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setCheckState(target)
            _update_tri_style(cb)
            cb.blockSignals(False)
        _sync_sa()

    sa.stateChanged.connect(_toggle)
    for cb in checkboxes:
        cb.stateChanged.connect(_sync_sa)

    if isinstance(layout, QGridLayout):
        row = layout.rowCount()
        layout.addWidget(sep(), row, 0, 1, cols)
        layout.addWidget(sa, row + 1, 0, 1, cols)
    else:
        layout.addWidget(sep())
        layout.addWidget(sa)


def _compute_op_status(distro: LinuxDistroHelper, aur_helper_installed: bool, system_default_variant: str | None,
                       installed_kernels: set | None = None) -> dict[str, bool]:
    import pwd as _pwd

    status: dict[str, bool] = {}

    try:
        target = S.effective_shell
        binary = distro.get_shell_binary_name(target)
        current = _pwd.getpwnam(_USER).pw_shell
        status["shell_ok"] = (Path(current).name == binary)
    except (KeyError, OSError):
        status["shell_ok"] = False

    ucode_pkg = distro.get_ucode_package()
    status["ucode_installed"] = bool(ucode_pkg and distro.package_is_installed(ucode_pkg))

    _ik: set = installed_kernels if installed_kernels is not None else distro.detect_installed_kernel_variants()
    targets_kti = S.effective_kernels
    status["kernels_all_installed"] = (
            not targets_kti or all(v in _ik for v in targets_kti if v in ARCH_KERNEL_VARIANTS))

    if distro.family() == "arch":
        future_kernels = _ik.union(set(targets_kti))

        def _header_ok(v: str) -> bool:
            pkgs = ARCH_KERNEL_VARIANTS.get(v)
            return pkgs is None or len(pkgs) < 2 or distro.package_is_installed(pkgs[1])

        status["kernel_headers_installed"] = all(_header_ok(v) for v in future_kernels)
    else:
        hpkg = distro.get_kernel_headers_pkg()
        status["kernel_headers_installed"] = bool(hpkg and distro.package_is_installed(hpkg))

    status["aur_helper_installed"] = aur_helper_installed

    effective_dk = (S.default_kernel or system_default_variant or "").strip()
    status["default_kernel_ok"] = bool(
        effective_dk and system_default_variant and effective_dk == system_default_variant)

    return status


def _build_op_text(distro: LinuxDistroHelper, session: str | None = None, aur_helper_installed: bool | None = None,
                   system_default_variant: str | None = None, op_status: dict | None = None,
                   installed_kernels: set | None = None, kernels_to_install_override: list | None = None,
                   default_kernel_override: str | None = None, aur_helper_override: str | None = None) -> dict[
    str, tuple[str, str]]:
    helper = aur_helper_override or S.aur_helper
    tips = sm_tooltips()
    _NO_CHANGE = " (No changes necessary.)"

    def _done(key: str) -> str:
        return _NO_CHANGE if (op_status and op_status.get(key)) else ""

    def _tip(key: str) -> str:
        return tips.get(key, "") if tips is not None else ""

    def pkglist(fn) -> str:
        try:
            pkgs = fn()
            return ", ".join(pkgs) if pkgs else "—"
        except (OSError, AttributeError):
            return "—"

    if aur_helper_installed is None:
        aur_helper_installed = distro.has_aur and (
                distro.package_is_installed(helper) or shutil.which(helper) is not None
        )
        if op_status is not None:
            op_status.setdefault("aur_helper_installed", aur_helper_installed)

    _installed_helper_for_update = None
    _installed_helpers_for_cache: list[str] = []
    if distro.has_aur:
        for _candidate in ("paru", "yay"):
            if _is_helper_present(distro, _candidate):
                if _installed_helper_for_update is None:
                    _installed_helper_for_update = _candidate
                _installed_helpers_for_cache.append(_candidate)
        if aur_helper_override and aur_helper_override not in _installed_helpers_for_cache:
            _installed_helpers_for_cache.append(aur_helper_override)

    install_cmd = distro.get_pkg_install_cmd("...")
    if session is None:
        session = distro.detect_session() or "current session"

    pm_name = distro.pkg_manager_name()
    cron_svc = distro.get_cron_service_name()

    cpu_vendor = LinuxDistroHelper.detect_cpu_vendor() or "unknown"
    ucode_pkg = {"intel": "intel-ucode", "amd": "amd-ucode"}.get(cpu_vendor, "intel-ucode / amd-ucode")
    cpu_label = {"intel": "Intel", "amd": "AMD"}.get(cpu_vendor, cpu_vendor.capitalize())

    _ik = installed_kernels if installed_kernels is not None else distro.detect_installed_kernel_variants()
    kti = kernels_to_install_override if kernels_to_install_override is not None else S.effective_kernels

    missing_kernels = sorted(str(k) for k in kti if k and k not in _ik)

    if not missing_kernels:
        kernels_text = "Install kernel(s): (No changes necessary.)"
    else:
        kernels_list = ", ".join(missing_kernels)
        kernels_text = f"Install kernel(s): {kernels_list}{_done('kernels_all_installed')}"

    if distro.family() == "arch":
        _future_hdr = set(_ik) | set(kti)
        _missing_hdrs = [
            ARCH_KERNEL_VARIANTS[v][1]
            for v in sorted(_future_hdr)
            if ARCH_KERNEL_VARIANTS.get(v) and len(ARCH_KERNEL_VARIANTS[v]) >= 2 and not distro.package_is_installed(
                ARCH_KERNEL_VARIANTS[v][1])
        ]
        headers_text = (
            "Install kernel headers: (Headers for installed kernel(s) already installed. No changes necessary.)" if not _missing_hdrs
            else f"Install kernel headers: {', '.join(_missing_hdrs)}")
    else:
        _hpkg = distro.get_kernel_headers_pkg() or "linux-headers"
        _hdr_done = " (No changes necessary)" if (op_status and op_status.get("kernel_headers_installed")) else ""
        headers_text = f"Install kernel header(s) (Package: '{_hpkg}'){_hdr_done}"

    dk = ((default_kernel_override if default_kernel_override is not None else S.default_kernel) or "")
    dk_pkg = dk or system_default_variant or "(not selected)"
    sys_def_info = f" [System default: {system_default_variant}]" if system_default_variant and system_default_variant != dk_pkg else ""
    dk_note = " (Is already default. No changes necessary.)" if (
            op_status and op_status.get("default_kernel_ok")) else sys_def_info

    return {"copy_dotfiles": ("Copy 'Dotfiles' (Using 'sudo cp')", _tip("copy_dotfiles")),
            "update_mirrors": (
                "Mirror update<br>(Install 'reflector' and get the 10 fastest servers in your country, or worldwide if location is not detected)",
                _tip("update_mirrors")),
            "set_user_shell": (
                f"Change shell for current user (Install shell package and set as default.){_done('shell_ok')}",
                _tip("set_user_shell")),
            "update_system": (
                f"System update (Using '{_installed_helper_for_update} -Syu --noconfirm')" if _installed_helper_for_update else
                f"System update (Using '{distro.get_update_system_cmd()}')",
                _tip("update_system")),
            "install_ucode": (
                f"Install {cpu_label} CPU microcode updates (Package: '{ucode_pkg}'){_done('ucode_installed')}",
                _tip("install_ucode")),
            "install_kernels": (kernels_text, _tip("install_kernels")),
            "install_kernel_headers": (headers_text, _tip("install_kernel_headers")),
            "set_default_kernel": (f"Set default boot kernel to: {dk_pkg}{dk_note}", _tip("set_default_kernel")),
            "install_basic_packages": (f"Install 'Basic Packages' (Using '{install_cmd}')",
                                       _tip("install_basic_packages")),
            "install_aur_helper": (f"Install '{helper}' (required for 'AUR Packages'){_done('aur_helper_installed')}",
                                   _tip("install_aur_helper")),
            "install_aur_packages": (f"Install 'AUR Packages' ('{helper}' required. Using '{helper} -S --needed ...')",
                                     _tip("install_aur_packages")),
            "install_specific_packages": (f"Install 'Specific Packages' for {session} (Using '{install_cmd}')",
                                          _tip("install_specific_packages")),
            "enable_flatpak_integration": (
                f"Enable Flatpak integration (Install '{pkglist(distro.get_flatpak_packages)}' and add Flathub remote)",
                _tip("enable_flatpak_integration")),
            "enable_printer_support": (
                f"Initialise printer support<br>(Install '{pkglist(distro.get_printer_packages)}'. Enable & start 'cups.service')",
                _tip("enable_printer_support")),
            "enable_ssh_service": (
                f"Initialise SSH server (Install '{pkglist(distro.get_ssh_packages)}'. Enable & start '{distro.get_ssh_service_name()}.service')",
                _tip("enable_ssh_service")),
            "enable_samba_network_filesharing": (
                f"Initialise Samba (network file-sharing). (Install '{pkglist(distro.get_samba_packages)}'. "
                f"Enable & start '{distro.get_samba_service_name()}.service')",
                _tip("enable_samba_network_filesharing")),
            "enable_bluetooth_service": (
                f"Initialise Bluetooth (Install '{pkglist(distro.get_bluetooth_packages)}'. Enable & start 'bluetooth.service')",
                _tip("enable_bluetooth_service")),
            "enable_atd_service": (
                f"Initialise atd (Install '{pkglist(distro.get_at_packages)}'. Enable & start 'atd.service')",
                _tip("enable_atd_service")),
            "enable_cronie_service": (
                f"Initialise {cron_svc} (Install '{pkglist(distro.get_cron_packages)}'. Enable & start '{cron_svc}.service')",
                _tip("enable_cronie_service")),
            "install_snap": (
                f"Initialise Snap (Install '{pkglist(distro.get_snap_packages)}'. Enable & start 'snapd.service')",
                _tip("install_snap")),
            "enable_ntp_sync": (
                (f"Enable network time synchronisation (Install '{pkglist(distro.get_ntp_packages)}'. "
                 f"Enable & start '{distro.get_ntp_service_name()}.service')")
                if distro.get_ntp_packages() else
                (f"Enable network time synchronisation (Already provided by the base system. "
                 f"Enable & start '{distro.get_ntp_service_name()}.service')"),
                _tip("enable_ntp_sync")),
            "enable_firewall": (
                f"Initialise firewall (Install '{pkglist(distro.get_firewall_packages)}'. "
                f"Enable & start '{distro.get_firewall_service_name()}.service' and apply rules.)",
                _tip("enable_firewall")),
            "enable_fstrim_timer": ("Enable periodic SSD TRIM (Enable & start 'fstrim.timer')",
                                    _tip("enable_fstrim_timer")),
            "remove_orphaned_packages": ("Remove orphaned package(s) (and unused Flatpak runtimes if installed)",
                                         _tip("remove_orphaned_packages")),
            "clean_cache": ("Clean cache (for '" + pm_name + "'"
                            + (", '" + "', '".join(_installed_helpers_for_cache[:-1]) + "' and '" +
                               _installed_helpers_for_cache[
                                   -1] + "'" if len(_installed_helpers_for_cache) > 1 else (
                " and '" + _installed_helpers_for_cache[0] + "'"
                if _installed_helpers_for_cache else "")) + ")", _tip("clean_cache")),
            "clean_journal_logs": (
                "Clean systemd journal logs (Limit size to 100M using 'sudo journalctl --vacuum-size=100M')",
                _tip("clean_journal_logs"))}


def _read_import_file(_parent, path: str) -> list[str] | None:
    try:
        return Path(path).read_text(encoding="utf-8").splitlines()
    except Exception as e:
        QMessageBox.critical(_parent, "Error", f"Read failed: {e}")
        return None


def _pkg_form_dialog(_parent, title: str, *, prefill_name: str = "", prefill_sess: str | None = None) -> tuple | None:
    with_session = prefill_sess is not None
    dlg = QDialog(_parent)
    dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(620)
    lay = QVBoxLayout(dlg)
    form = QFormLayout()
    name_ed = QLineEdit(prefill_name)
    name_ed.setMinimumHeight(36)
    form.addRow("Package:", name_ed)

    sess_cb = None
    if with_session:
        sess_cb = QComboBox()
        sess_cb.addItems(SESSIONS)
        if prefill_sess:
            sess_cb.setCurrentText(prefill_sess)
        sess_cb.setMinimumHeight(36)
        form.addRow("Session:", sess_cb)

    lay.addLayout(form)
    lay.addWidget(ok_cancel_buttons(dlg, dlg.accept))

    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None

    name = name_ed.text().strip()
    if not name:
        QMessageBox.warning(_parent, "Error", "Package name required.")
        return None
    if not is_valid_pkg_name(name):
        QMessageBox.warning(
            _parent, "Error",
            f"'{name}' is not a valid package name.\n\n"
            "Allowed: letters, digits, '.', '_', '+', '-' (must not start with a separator)."
        )
        return None

    if with_session and sess_cb is not None:
        return name, sess_cb.currentText()
    return (name,)


def _check_aur_helper_installed(distro: LinuxDistroHelper) -> bool:
    return distro.has_aur and _is_helper_present(distro, S.aur_helper)


def _is_helper_present(distro: LinuxDistroHelper, helper: str) -> bool:
    return distro.package_is_installed(helper) or shutil.which(helper) is not None


def _detect_effective_aur_helper(distro: LinuxDistroHelper) -> tuple[str, bool]:
    helper = S.aur_helper
    if _is_helper_present(distro, helper):
        return helper, True
    alt = "paru" if helper == "yay" else "yay"
    if _is_helper_present(distro, alt):
        return alt, True
    return helper, False


class PackageVerifierThread(QThread):
    progress = pyqtSignal(int, int)
    result = pyqtSignal(list, list)

    def __init__(self, packages, pkg_type, distro_family):
        super().__init__()
        self.packages = packages
        self.pkg_type = pkg_type
        self.distro_family = distro_family
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        valid = []
        invalid = []

        names = []
        is_specific = (self.pkg_type == "specific_packages")
        for p in self.packages:
            name = p.get("package" if is_specific else "name", "") if isinstance(p, dict) else str(p)
            if name:
                names.append(name)
        names = list(set(names))
        total = len(names)
        processed = 0

        if not names:
            self.result.emit([], [])
            return

        if self.pkg_type == "aur_packages":
            batch_size = 100
            for i in range(0, total, batch_size):
                if self._is_cancelled: break
                batch = names[i:i + batch_size]
                args = "&".join(f"arg[]={urllib.parse.quote(p)}" for p in batch)
                url = f"https://aur.archlinux.org/rpc/v5/info?{args}"

                try:
                    req = urllib.request.Request(url, headers={'User-Agent': 'BackupHelper-Verifier'})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        data = json.loads(r.read().decode("utf-8"))
                        found = {res.get("Name") for res in data.get("results", [])}
                        for p in batch:
                            if p in found:
                                valid.append(p)
                            else:
                                invalid.append(p)
                except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
                    logger.warning("AUR Verify error: %s", e)
                    valid.extend(batch)

                processed += len(batch)
                self.progress.emit(processed, total)
        else:
            def check_pkg(_pkg):
                if self._is_cancelled: return _pkg, True
                if not is_valid_pkg_name(_pkg):
                    return _pkg, False
                cmd = None
                fam = self.distro_family

                if fam == "arch":
                    cmd = ["pacman", "-Si", _pkg]
                elif fam == "debian":
                    cmd = ["apt-cache", "show", _pkg]
                elif fam == "fedora":
                    cmd = ["dnf", "info", _pkg]
                elif fam == "suse":
                    cmd = ["zypper", "info", _pkg]
                elif fam == "void":
                    cmd = ["xbps-query", "-R", _pkg]
                elif fam == "alpine":
                    cmd = ["apk", "info", _pkg]
                elif fam == "solus":
                    cmd = ["eopkg", "info", _pkg]
                elif fam == "gentoo":
                    cmd = None
                elif fam == "nixos":
                    cmd = ["sh", "-c", f"nix-env -qa --available {shlex.quote(_pkg)} 2>/dev/null | grep -q ."]
                elif fam == "slackware":
                    cmd = None

                if cmd is None: return _pkg, True

                try:
                    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    return _pkg, (res.returncode == 0)
                except (OSError, subprocess.SubprocessError):
                    return _pkg, True

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(check_pkg, p) for p in names]
                for fut in concurrent.futures.as_completed(futures):
                    if self._is_cancelled: break
                    try:
                        pkg, is_valid = fut.result()
                        if is_valid:
                            valid.append(pkg)
                        else:
                            invalid.append(pkg)
                    except (concurrent.futures.CancelledError, concurrent.futures.TimeoutError):
                        pass
                    except Exception as e:
                        logger.debug("Verification error: %s", e)

                    processed += 1
                    self.progress.emit(processed, total)

        if not self._is_cancelled:
            self.result.emit(valid, invalid)
