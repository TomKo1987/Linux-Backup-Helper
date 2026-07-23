import hashlib
import os
import re
import subprocess
from functools import lru_cache as _lru_cache
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QVBoxLayout, QWidget
)

from dotfiles_manager import first_path
from linux_distro_helper import LinuxDistroHelper, USER_SHELLS, ARCH_KERNEL_VARIANTS
from state import (
    S, logger, active_pkg_names, active_dotfiles
)
from themes import current_theme, font_sz

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_title(text: str) -> str: return _HTML_TAG_RE.sub(" ", text).strip()


def _ep(path_str: str) -> Path: return Path(path_str.strip()).expanduser()


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _sha256(path: Path, limit: int = 8 * 1024 * 1024) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


@_lru_cache(maxsize=1024)
def _sha256_cached(path_str: str, _mtime_ns: int, _size: int) -> str | None:
    return _sha256(Path(path_str))


def _hash_v(p: Path) -> str | None:
    try:
        st = p.stat()
        return _sha256_cached(str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _surface_mtime(path: Path) -> float:
    best = _mtime(path)
    if path.is_dir():
        try:
            for child in path.iterdir():
                t = _mtime(child)
                best = max(best, t)
        except OSError:
            pass
    return best


_IGNORE_EXACT: frozenset[str] = frozenset({
    "base", "base-devel", "linux", "linux-firmware", "grub",
    "efibootmgr", "systemd", "glibc", "sudo",
    "networkmanager", "wpa_supplicant", "iwd", "dhcpcd", "dhclient",
    "pipewire", "pipewire-audio", "pipewire-alsa", "pipewire-jack",
    "pipewire-pulse", "pipewire-v4l2", "pipewire-zeroconf",
    "wireplumber", "gst-plugin-pipewire", "libpipewire",
    "pulseaudio", "pulseaudio-alsa", "pulseaudio-bluetooth",
    "pulseaudio-jack", "pulseaudio-zeroconf",
    "libpulse", "alsa-utils", "alsa-lib", "alsa-firmware",
    "plasma-desktop", "plasma-workspace",
    "gnome-shell", "gnome-session", "gnome-settings-daemon",
    "xfce4-session", "xfce4-panel", "xfdesktop", "xfwm4",
    "lxqt-session", "lxsession",
    "cinnamon", "mate-session-manager",
    "budgie-desktop", "deepin-session-shell",
    "sddm", "gdm", "lightdm", "lxdm",
    "xorg-server", "xwayland",
    "libappindicator-gtk3", "libappindicator-gtk2",
    "btrfs-progs", "e2fsprogs", "xfsprogs", "f2fs-tools",
    "dosfstools", "exfatprogs", "ntfs-3g", "jfsutils",
    "nilfs-utils", "bcachefs-tools", "reiserfsprogs",
})

_IGNORE_PREFIXES: tuple[str, ...] = (
    "plasma-",
    "kf5-", "kf6-",
    "pipewire-",
    "pulseaudio-",
    "xfce4-",
    "gnome-",
    "libappindicator",
)


def _get_sm_managed_packages(helper: LinuxDistroHelper) -> frozenset:
    pkgs: set[str] = {"yay", "paru", "reflector"}

    for shell in USER_SHELLS:
        pkgs.add(helper.get_shell_package_name(shell))

    for get_fn in (helper.get_ssh_packages, helper.get_samba_packages,
                   helper.get_bluetooth_packages, helper.get_cron_packages,
                   helper.get_at_packages, helper.get_firewall_packages,
                   helper.get_printer_packages, helper.get_flatpak_packages,
                   helper.get_snap_packages):
        try:
            result = get_fn()
            if result:
                pkgs.update(result)
        except (NotImplementedError, OSError, subprocess.SubprocessError):
            pass

    for val in ARCH_KERNEL_VARIANTS.values():
        if val:
            pkgs.add(val[0])
            if len(val) >= 2:
                pkgs.add(val[1])

    try:
        hdr_pkg = helper.get_kernel_headers_pkg()
        if hdr_pkg:
            pkgs.add(hdr_pkg)
    except (NotImplementedError, OSError, subprocess.SubprocessError):
        pass

    pkgs.update({"intel-ucode", "intel-microcode", "microcode_ctl", "ucode-intel", "amd-ucode", "amd64-microcode"})

    try:
        ucode = helper.get_ucode_package()
        if ucode:
            pkgs.add(ucode)
    except (NotImplementedError, OSError, subprocess.SubprocessError):
        pass

    return frozenset(pkgs)


_DE_META_PKGS: tuple[str, ...] = (
    "plasma-desktop", "plasma-meta", "plasma-workspace", "gnome-shell",
    "gnome-session", "xfce4-session", "lxqt-session", "lxsession",
    "cinnamon", "mate-session-manager"
)
_DEP_VER_RE = re.compile(r"[>=<]\S*")


def _get_arch_de_deps(helper: LinuxDistroHelper) -> frozenset[str]:
    if helper.family() != "arch":
        return frozenset()

    result: set[str] = set()
    for meta in _DE_META_PKGS:
        try:
            r = subprocess.run(["pacman", "-Qi", meta],
                               capture_output=True, text=True, timeout=15,
                               env={**os.environ, "LC_ALL": "C"})
            lines = r.stdout.splitlines()
        except (RuntimeError, subprocess.SubprocessError, OSError):
            continue

        if not lines:
            continue

        in_depends = False
        for line in lines:
            if line.startswith("Depends On"):
                in_depends = True
                _, _, val = line.partition(":")
                for tok in val.split():
                    name = _DEP_VER_RE.sub("", tok).strip()
                    if name and name != "None":
                        result.add(name)
            elif in_depends:
                if not line.startswith((" ", "\t")):
                    break

                for tok in line.split():
                    name = _DEP_VER_RE.sub("", tok).strip()
                    if name and name != "None":
                        result.add(name)

    return frozenset(result)


_OP_SERVICE_ALL: dict[str, str] = {"enable_bluetooth_service": "bluetooth",
                                   "enable_atd_service": "atd",
                                   "enable_printer_support": "cups",
                                   "enable_flatpak_integration": "",
                                   "install_snap": "snapd"}

_OP_SERVICE_DYNAMIC_FNS: dict[str, str] = {"enable_ssh_service": "get_ssh_service_name",
                                           "enable_samba_network_filesharing": "get_samba_service_name",
                                           "enable_cronie_service": "get_cron_service_name",
                                           "enable_firewall": "get_firewall_service_name"}


def _resolve_all_ops(helper: LinuxDistroHelper) -> dict[str, str]:
    dynamic = {}
    for op, fn in _OP_SERVICE_DYNAMIC_FNS.items():
        try:
            res = getattr(helper, fn)()
            if res:
                dynamic[op] = res
        except Exception as exc:
            logger.debug("Could not resolve dynamic service for %s: %s", op, exc)
    return {**_OP_SERVICE_ALL, **dynamic}


def _get_active_sm_services(helper: LinuxDistroHelper) -> list[tuple[str, str]]:
    all_ops = _resolve_all_ops(helper)
    return [(op, svc) for op, svc in all_ops.items() if op in S.system_manager_ops and svc]


def _get_all_sm_services(helper: LinuxDistroHelper) -> list[tuple[str, str]]:
    return [(op, svc) for op, svc in _resolve_all_ops(helper).items() if svc]


def _service_is_active(name: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", f"{name}.service"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _collect_verify_paths() -> list[str]:
    paths: list[str] = []
    for sf in active_dotfiles():
        for key in ("source", "destination"):
            v = first_path(sf.get(key, "")).strip()
            if v: paths.append(str(_ep(v)))
    for entry in S.entries:
        if isinstance(entry, dict):
            for key in ("source", "destination"):
                for p in entry.get(key, []):
                    if p:
                        paths.append(str(_ep(p)))
    return paths


def _check_dotfile_status(sf: dict) -> "dict | None":
    src_raw = first_path(sf.get("source", "")).strip()
    dst_raw = first_path(sf.get("destination", "")).strip()
    if not src_raw or not dst_raw:
        return None
    src = _ep(src_raw)
    dst = _ep(dst_raw)
    name = src.name or str(src)
    if not src.exists():
        status = "src_missing"
    elif not dst.exists():
        status = "dst_missing"
    else:
        h_src, h_dst = _hash_v(src), _hash_v(dst)
        if h_src and h_dst:
            status = "ok" if h_src == h_dst else "changed"
        else:
            status = "ok" if _mtime(dst) >= _mtime(src) - 2 else "changed"
    return {"name": name, "status": status, "src": str(src), "dst": str(dst)}


class _CaptureWorker(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(dict)

    def __init__(self, helper: LinuxDistroHelper) -> None:
        super().__init__()
        self._h = helper

    def run(self) -> None:
        res: dict = {"basic": [], "aur": [], "specific": [], "sys_files": [], "services": [], "error": ""}
        self.progress.emit("Scanning installed packages…")
        try:
            res["basic"], res["aur"] = self._h.get_explicitly_installed_packages()
        except Exception as exc:
            res["error"] = str(exc)
            logger.error("CaptureWorker: %s", exc)

        self.progress.emit("Checking dotfiles…")
        try:
            for sf in active_dotfiles():
                entry = _check_dotfile_status(sf)
                if entry is not None:
                    res["sys_files"].append(entry)
        except Exception as exc:
            logger.warning("CaptureWorker: dotfile check failed: %s", exc)

        self.progress.emit("Scanning active services…")
        try:
            all_services = _get_all_sm_services(self._h)
            for op, svc in all_services:
                res["services"].append({"op": op, "service": svc, "active": _service_is_active(svc),
                                        "in_profile": op in S.system_manager_ops})
        except Exception as exc:
            logger.warning("CaptureWorker: service scan failed: %s", exc)

        self.progress.emit("Checking specific packages…")
        try:
            spec_entries = [p for p in S.specific_packages if
                            isinstance(p, dict) and not p.get("disabled") and p.get("package")]
            spec_names = [p["package"] for p in spec_entries]
            if spec_names:
                missing = set(self._h.filter_not_installed(spec_names))
                res["specific"] = [
                    {"name": p["package"], "session": p.get("session", ""), "installed": p["package"] not in missing}
                    for p in spec_entries]
        except Exception as exc:
            logger.warning("CaptureWorker: specific package check failed: %s", exc)

        self.progress.emit("Checking profile package status…")
        try:
            profile_basic_names = active_pkg_names(S.basic_packages)
            profile_aur_names = active_pkg_names(S.aur_packages)
            all_prof = profile_basic_names + profile_aur_names
            if all_prof:
                missing_prof = set(self._h.filter_not_installed(all_prof))
                res["profile_installed_basic"] = {n for n in profile_basic_names if n not in missing_prof}
                res["profile_installed_aur"] = {n for n in profile_aur_names if n not in missing_prof}
            else:
                res["profile_installed_basic"] = set()
                res["profile_installed_aur"] = set()
        except Exception as exc:
            logger.warning("CaptureWorker: profile package install check failed: %s", exc)
            res["profile_installed_basic"] = set()
            res["profile_installed_aur"] = set()

        self.done.emit(res)


class _VerifyWorker(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(dict)

    def __init__(self, helper: LinuxDistroHelper) -> None:
        super().__init__()
        self._h = helper

    def run(self) -> None:
        res: dict = {"pkgs": [], "sys_files": [], "backups": [], "services": []}

        self.progress.emit("Checking packages…")
        all_pkgs = ([(n, "basic") for n in active_pkg_names(S.basic_packages)] +
                    [(n, "aur") for n in active_pkg_names(S.aur_packages)] +
                    [(n, "specific") for n in active_pkg_names(S.specific_packages, is_specific=True)])
        if all_pkgs:
            names = [n for n, _ in all_pkgs]
            missing = set(self._h.filter_not_installed(names))
            res["pkgs"] = [{"name": n, "kind": k, "installed": n not in missing} for n, k in all_pkgs]

        self.progress.emit("Checking dotfiles…")
        for sf in S.dotfiles:
            if not isinstance(sf, dict) or sf.get("disabled"):
                continue
            entry = _check_dotfile_status(sf)
            if entry is not None:
                res["sys_files"].append(entry)

        self.progress.emit("Checking backup entries…")
        for entry in S.entries:
            if not isinstance(entry, dict):
                continue
            issues: list[str] = []
            from drive_utils import is_smb, is_ssh

            for s in entry.get("source", []):
                if not is_smb(s) and not is_ssh(s) and not _ep(s).exists():
                    issues.append(f"Source missing: {s}")
            for d in entry.get("destination", []):
                if not is_smb(d) and not is_ssh(d) and not _ep(d).exists():
                    issues.append(f"Destination missing: {d}")
            if not issues:
                srcs, dsts = entry.get("source", []), entry.get("destination", [])
                if srcs and dsts:
                    src_t = max((_surface_mtime(_ep(s)) for s in srcs), default=0.0)
                    dst_t = max((_surface_mtime(_ep(d)) for d in dsts), default=0.0)
                    if src_t > dst_t + 2:
                        issues.append("Backup may be outdated (source newer than destination)")
            res["backups"].append({"header": entry.get("header", ""), "title": entry.get("title", ""),
                                   "status": "issues" if issues else "ok", "issues": issues})

        self.progress.emit("Checking services…")
        for op, svc in _get_active_sm_services(self._h):
            res["services"].append({"op": op, "service": svc, "active": _service_is_active(svc)})

        self.done.emit(res)


class _Section(QWidget):

    def __init__(self, icon: str, title: str, ok: int, total: int, ok_color: str, bad_color: str, start_expanded: bool = True) -> None:
        super().__init__()
        t = current_theme()
        self._expanded = start_expanded
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 6)
        outer.setSpacing(0)

        n_bad = total - ok
        arrow = "▼" if start_expanded else "▶"
        color = bad_color if n_bad else ok_color
        if n_bad:
            status_txt = f"⚠  {n_bad} issue{'s' if n_bad > 1 else ''}   {ok} / {total}"
        else:
            status_txt = f"✅  {ok} / {total}"

        self._arrow_lbl = QLabel(arrow)
        self._arrow_lbl.setFixedWidth(18)
        self._arrow_lbl.setStyleSheet(f"color:{color};font-weight:bold;")

        icon_lbl = QLabel(icon)
        icon_lbl.setStyleSheet(f"color:{color};font-weight:bold;font-size:{font_sz(1)}px;")

        title_lbl = QLabel(f"<b>{title}</b>")
        title_lbl.setTextFormat(Qt.TextFormat.RichText)
        title_lbl.setStyleSheet(f"color:{color};font-size:{font_sz(1)}px;")
        title_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        status_lbl = QLabel(status_txt)
        status_lbl.setStyleSheet(f"color:{color};font-size:{font_sz(-1)}px;")

        hdr = QWidget()
        hdr.setStyleSheet(f"background:{t['bg2']};border:1px solid {t['header_sep']};border-radius:4px;padding:2px;")
        hdr.setCursor(Qt.CursorShape.PointingHandCursor)
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(8, 6, 8, 6)
        hdr_lay.setSpacing(6)
        hdr_lay.addWidget(self._arrow_lbl)
        hdr_lay.addWidget(icon_lbl)
        hdr_lay.addWidget(title_lbl)
        hdr_lay.addWidget(status_lbl)
        outer.addWidget(hdr)

        self._content = QWidget()
        self._content.setStyleSheet(f"background:{t['bg']};border:1px solid {t['header_sep']};"
                                    f"border-top:none;border-radius:0 0 4px 4px;")
        self._content_lay = QVBoxLayout(self._content)
        self._content_lay.setContentsMargins(0, 0, 0, 0)
        self._content_lay.setSpacing(0)
        self._content.setVisible(start_expanded)
        outer.addWidget(self._content)

        hdr.mousePressEvent = lambda _e: self._toggle()

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._arrow_lbl.setText("▼" if self._expanded else "▶")
        self._content.setVisible(self._expanded)

    def add_row(self, icon: str, text: str, detail: str, color: str) -> None:
        t = current_theme()
        row = QWidget()
        row.setStyleSheet(f"border-bottom:1px solid {t['bg3']};")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(28, 5, 12, 5)
        lay.setSpacing(8)

        ic = QLabel(icon)
        ic.setFixedWidth(20)
        ic.setStyleSheet(f"color:{color};border:none;")

        tx = QLabel(text)
        tx.setStyleSheet(f"color:{color};border:none;font-family:monospace;")
        tx.setMinimumWidth(120)
        tx.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        lay.addWidget(ic)
        lay.addWidget(tx)

        if detail:
            dt = QLabel(detail)
            dt.setStyleSheet(f"color:{t['muted']};border:none;font-size:{font_sz(-1)}px;font-family:monospace;")
            dt.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            dt.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            lay.addWidget(dt)

        self._content_lay.addWidget(row)

    def add_entry_group(self, label: str, issues: list[str], color: str) -> None:
        t = current_theme()

        grp = QWidget()
        grp.setStyleSheet(f"border-bottom:1px solid {t['bg3']};")
        gl = QVBoxLayout(grp)
        gl.setContentsMargins(28, 4, 12, 4)
        gl.setSpacing(2)

        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(6)
        ic = QLabel("⚠")
        ic.setFixedWidth(20)
        ic.setStyleSheet(f"color:{color};border:none;")
        lbl = QLabel(label)
        lbl.setStyleSheet(f"color:{color};font-family:monospace;border:none;")
        hdr_row.addWidget(ic)
        hdr_row.addWidget(lbl)
        hdr_row.addStretch()
        gl.addLayout(hdr_row)

        for issue in issues:
            il = QHBoxLayout()
            il.setContentsMargins(26, 0, 0, 0)
            dot = QLabel("→")
            dot.setFixedWidth(16)
            dot.setStyleSheet(f"color:{t['muted']};border:none;")
            txt = QLabel(issue)
            txt.setStyleSheet(f"color:{t['muted']};font-size:{font_sz(-1)}px;font-family:monospace;border:none;")
            txt.setWordWrap(False)
            il.addWidget(dot)
            il.addWidget(txt)
            il.addStretch()
            gl.addLayout(il)

        self._content_lay.addWidget(grp)


def _make_progress_widget(initial_text: str = "Scanning…") -> tuple[QWidget, QLabel, QProgressBar]:
    widget = QWidget()
    pw = QVBoxLayout(widget)
    pw.setContentsMargins(0, 0, 0, 0)
    label = QLabel(initial_text)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    bar = QProgressBar()
    bar.setRange(0, 0)
    pw.addStretch()
    pw.addWidget(label)
    pw.addWidget(bar)
    pw.addStretch()
    return widget, label, bar
