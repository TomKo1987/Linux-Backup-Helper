import hashlib
import re
import subprocess
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QTabWidget, QVBoxLayout, QWidget,
)

from drive_utils import check_drives_to_mount, mount_required_drives
from linux_distro_helper import LinuxDistroHelper, USER_SHELLS, ARCH_KERNEL_VARIANTS, SESSIONS
from state import S, save_profile, logger, all_profile_pkg_names, active_pkg_names, active_system_files, sort_pkg_list, \
    sort_specific_pkg_list
from themes import current_theme, font_sz
from ui_utils import sep, _StandardKeysMixin

_VER_RE      = re.compile(r"[-_]\d[\w.+~:-]*$")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _run(cmd: list[str], timeout: int = 25) -> list[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]
    except Exception as exc:
        logger.warning("capture_verify._run %s: %s", cmd[0] if cmd else "?", exc)
        return []


def _strip_ver(name: str) -> str: return _VER_RE.sub("", name).strip()


def _clean_title(text: str) -> str: return _HTML_TAG_RE.sub(" ", text).strip()


def _ep(path_str: str) -> Path: return Path(path_str.strip()).expanduser()


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _sha256(path: Path, limit: int = 8 * 1024 * 1024) -> Optional[str]:
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


def _surface_mtime(path: Path) -> float:
    best = _mtime(path)
    if path.is_dir():
        try:
            for child in path.iterdir():
                t = _mtime(child)
                if t > best:
                    best = t
        except OSError:
            pass
    return best


_SYSTEM_BASE_PKGS: frozenset[str] = frozenset({
    "base",
    "base-devel",
    "linux-firmware",
    "plasma-desktop",
    "gnome-shell",
    "xfce4-session",
    "cinnamon",
    "mate-session-manager",
    "lxsession",
    "lxqt-session",
    "budgie-desktop",
    "deepin-session-shell",
})


def _get_sm_managed_packages(helper: LinuxDistroHelper) -> frozenset:
    pkgs: set[str] = {"yay", "paru", "reflector"}

    for shell in USER_SHELLS:
        pkgs.add(helper.get_shell_package_name(shell))

    for get_fn in (
        helper.get_ssh_packages, helper.get_samba_packages,
        helper.get_bluetooth_packages, helper.get_cron_packages,
        helper.get_at_packages, helper.get_firewall_packages,
        helper.get_printer_packages, helper.get_flatpak_packages,
        helper.get_snap_packages,
    ):
        try:
            result = get_fn()
            if result:
                pkgs.update(result)
        except (NotImplementedError, OSError, subprocess.SubprocessError):
            pass

    for kernel_pkg, header_pkg in ARCH_KERNEL_VARIANTS.values():
        pkgs.add(kernel_pkg)
        pkgs.add(header_pkg)

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


_OP_SERVICE_ALL: dict[str, str] = {
    "enable_bluetooth_service":          "bluetooth",
    "enable_atd_service":                "atd",
    "enable_firewall":                   "ufw",
    "enable_printer_support":            "cups",
    "enable_flatpak_integration":        "",
    "install_snap":                      "snapd",
}

_OP_SERVICE_DYNAMIC_FNS: dict[str, str] = {
    "enable_ssh_service":               "get_ssh_service_name",
    "enable_samba_network_filesharing": "get_samba_service_name",
    "enable_cronie_service":            "get_cron_service_name",
}


def _resolve_all_ops(helper: LinuxDistroHelper) -> dict[str, str]:
    dynamic = {op: getattr(helper, fn)() for op, fn in _OP_SERVICE_DYNAMIC_FNS.items()}
    return {**_OP_SERVICE_ALL, **dynamic}


def _get_active_sm_services(helper: LinuxDistroHelper) -> list[tuple[str, str]]:
    all_ops = _resolve_all_ops(helper)
    return [(op, svc) for op, svc in all_ops.items()
            if op in S.system_manager_ops and svc]


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
    for sf in active_system_files():
        for key in ("source", "destination"):
            v = sf.get(key, "").strip()
            if v: paths.append(str(_ep(v)))
    for entry in S.entries:
        if isinstance(entry, dict):
            for key in ("source", "destination"):
                for p in entry.get(key, []):
                    if p:
                        paths.append(str(_ep(p)))
    return paths


class _CaptureWorker(QThread):
    progress = pyqtSignal(str)
    done     = pyqtSignal(dict)

    def __init__(self, helper: LinuxDistroHelper) -> None:
        super().__init__()
        self._h = helper

    def run(self) -> None:
        fam = self._h.family()
        res: dict = {"basic": [], "aur": [], "specific": [], "sys_files": [], "services": [], "error": ""}
        self.progress.emit("Scanning installed packages…")
        try:
            if fam == "arch":
                unrequired = set(_run(["pacman", "-Qqet"]))
                foreign    = set(_run(["pacman", "-Qqm"]))
                explicit   = set(_run(["pacman", "-Qqe"]))
                aur_explicit = foreign & explicit
                res["aur"]   = sorted(aur_explicit)
                res["basic"] = sorted(unrequired - foreign)
            elif fam == "debian":
                res["basic"] = sorted(_run(["apt-mark", "showmanual"]))
            elif fam == "fedora":
                raw = _run(["sh", "-c",
                    "dnf repoquery --userinstalled -q --qf '%{name}' 2>/dev/null | sort -u"])
                res["basic"] = sorted(set(raw))
            elif fam == "suse":
                raw = _run(["sh", "-c",
                    "zypper se --installed-only 2>/dev/null "
                    "| awk -F'|' '/^i /{gsub(/ /,\"\",$2);if($2)print $2}'"])
                res["basic"] = sorted(raw)
            elif fam == "void":
                res["basic"] = sorted(_strip_ver(l) for l in _run(["xbps-query", "-m"]) if l)
            elif fam == "alpine":
                res["basic"] = sorted(_run(["apk", "info"]))
            elif fam == "gentoo":
                res["basic"] = sorted(_run(["sh", "-c", "qlist -I -C 2>/dev/null"]))
            elif fam == "nixos":
                res["basic"] = sorted(_strip_ver(l) for l in _run(["nix-env", "-q"]) if l)
            elif fam == "slackware":
                raw = _run(["sh", "-c", "ls /var/log/packages/ 2>/dev/null"])
                res["basic"] = sorted(_strip_ver(l) for l in raw if l)
            elif fam == "solus":
                res["basic"] = sorted(_run(["eopkg", "li", "-N"]))
            else:
                res["error"] = f"Package detection not supported for distro family '{fam}'."
        except Exception as exc:
            res["error"] = str(exc)
            logger.error("CaptureWorker: %s", exc)

        self.progress.emit("Checking system files…")
        try:
            for sf in active_system_files():
                src_raw = sf.get("source", "").strip()
                dst_raw = sf.get("destination", "").strip()
                if not src_raw or not dst_raw:
                    continue
                src  = _ep(src_raw)
                dst  = _ep(dst_raw)
                name = src.name or str(src)
                if not src.exists():
                    status = "src_missing"
                elif not dst.exists():
                    status = "dst_missing"
                else:
                    h_src, h_dst = _sha256(src), _sha256(dst)
                    if h_src and h_dst:
                        status = "ok" if h_src == h_dst else "changed"
                    else:
                        status = "ok" if _mtime(dst) >= _mtime(src) - 2 else "changed"
                res["sys_files"].append({"name": name, "status": status, "src": str(src), "dst": str(dst)})
        except Exception as exc:
            logger.warning("CaptureWorker: system file check failed: %s", exc)

        self.progress.emit("Scanning active services…")
        try:
            all_services = _get_all_sm_services(self._h)
            for op, svc in all_services:
                res["services"].append({
                    "op": op,
                    "service": svc,
                    "active": _service_is_active(svc),
                    "in_profile": op in S.system_manager_ops,
                })
        except Exception as exc:
            logger.warning("CaptureWorker: service scan failed: %s", exc)

        self.progress.emit("Checking specific packages…")
        try:
            spec_entries = [p for p in S.specific_packages if isinstance(p, dict) and not p.get("disabled") and p.get("package")]
            spec_names   = [p["package"] for p in spec_entries]
            if spec_names:
                missing = set(self._h.filter_not_installed(spec_names))
                res["specific"] = [
                    {"name": p["package"], "session": p.get("session", ""), "installed": p["package"] not in missing}
                    for p in spec_entries
                ]
        except Exception as exc:
            logger.warning("CaptureWorker: specific package check failed: %s", exc)

        self.done.emit(res)


class _VerifyWorker(QThread):
    progress = pyqtSignal(str)
    done     = pyqtSignal(dict)

    def __init__(self, helper: LinuxDistroHelper) -> None:
        super().__init__()
        self._h = helper

    def run(self) -> None:
        res: dict = {"pkgs": [], "sys_files": [], "backups": [], "services": []}

        self.progress.emit("Checking packages…")
        all_pkgs = (
                [(n, "basic") for n in active_pkg_names(S.basic_packages)] +
                [(n, "aur") for n in active_pkg_names(S.aur_packages)] +
                [(n, "specific") for n in active_pkg_names(S.specific_packages, is_specific=True)]
        )
        if all_pkgs:
            names   = [n for n, _ in all_pkgs]
            missing = set(self._h.filter_not_installed(names))
            res["pkgs"] = [{"name": n, "kind": k, "installed": n not in missing} for n, k in all_pkgs]

        self.progress.emit("Checking system files…")
        for sf in S.system_files:
            if not isinstance(sf, dict) or sf.get("disabled"):
                continue
            src_raw = sf.get("source", "").strip()
            dst_raw = sf.get("destination", "").strip()
            if not src_raw or not dst_raw:
                continue
            src  = _ep(src_raw)
            dst  = _ep(dst_raw)
            name = src.name or str(src)
            if not src.exists():
                status = "src_missing"
            elif not dst.exists():
                status = "dst_missing"
            else:
                h_src, h_dst = _sha256(src), _sha256(dst)
                if h_src and h_dst:
                    status = "ok" if h_src == h_dst else "changed"
                else:
                    logger.debug("verify: hashing skipped for large file '%s', using mtime", src)
                    status = "ok" if _mtime(dst) >= _mtime(src) - 2 else "changed"
            res["sys_files"].append({"name": name, "status": status, "src": str(src), "dst": str(dst)})

        self.progress.emit("Checking backup entries…")
        for entry in S.entries:
            if not isinstance(entry, dict):
                continue
            issues: list[str] = []
            for s in entry.get("source", []):
                if not _ep(s).exists():
                    issues.append(f"Source missing: {s}")
            for d in entry.get("destination", []):
                if not _ep(d).exists():
                    issues.append(f"Destination missing: {d}")
            if not issues:
                srcs, dsts = entry.get("source", []), entry.get("destination", [])
                if srcs and dsts:
                    src_t = max((_surface_mtime(_ep(s)) for s in srcs), default=0.0)
                    dst_t = max((_surface_mtime(_ep(d)) for d in dsts), default=0.0)
                    if src_t > dst_t + 2:
                        issues.append("Backup may be outdated (source newer than destination)")
            res["backups"].append({
                "header": entry.get("header", ""),
                "title":  entry.get("title",  ""),
                "status": "issues" if issues else "ok",
                "issues": issues,
            })

        self.progress.emit("Checking services…")
        for op, svc in _get_active_sm_services(self._h):
            res["services"].append({"op": op, "service": svc, "active": _service_is_active(svc)})

        self.done.emit(res)


class _Section(QWidget):

    def __init__(self, icon: str, title: str, ok: int, total: int,
                 ok_color: str, bad_color: str, start_expanded: bool = True) -> None:
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

        self._arrow_lbl  = QLabel(arrow)
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
        tx.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        lay.addWidget(ic)
        lay.addWidget(tx)

        if detail:
            dt = QLabel(detail)
            dt.setStyleSheet(f"color:{t['muted']};border:none;font-size:{font_sz(-1)}px;font-family:monospace;")
            dt.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            dt.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            lay.addWidget(dt)

        self._content_lay.addWidget(row)

    def add_entry_group(self, label: str, issues: list[str], color: str) -> None:
        t = current_theme()

        grp = QWidget()
        grp.setStyleSheet(f"border-bottom:1px solid {t['bg3']};")
        gl  = QVBoxLayout(grp)
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
            il  = QHBoxLayout()
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


# noinspection PyUnresolvedReferences
class _CaptureTab(QWidget):
    def __init__(self, helper: LinuxDistroHelper) -> None:
        super().__init__()
        self._helper  = helper
        self._worker: Optional[_CaptureWorker] = None
        self._cbs:    list[tuple[QCheckBox, str, str]] = []
        self._svc_cbs: list[tuple[QCheckBox, str, str]] = []
        self._sm_pkgs = _get_sm_managed_packages(helper)
        self._current_session = helper.detect_session() or SESSIONS[0]
        self._build_ui()
        self._start()

    def _save_and_notify(self, ok_msg: str, on_success=None) -> None:
        if save_profile():
            if on_success:
                on_success()
            QMessageBox.information(self.window(), "Done", ok_msg)
        else:
            QMessageBox.warning(self.window(), "Save Failed", "Could not save profile.")

    def _build_ui(self) -> None:
        t   = current_theme()
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
        export_btn  = QPushButton("💾  Export Report")
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
        if self._worker and self._worker.isRunning():
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

        self._worker = _CaptureWorker(self._helper)
        self._worker.progress.connect(self._prog_label.setText)
        self._worker.done.connect(self._on_done)
        self._worker.start()

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
        _excluded   = self._sm_pkgs | _SYSTEM_BASE_PKGS
        new_basic = [p for p in res["basic"] if p not in profile_all and p not in _excluded]
        new_aur   = [p for p in res["aur"]   if p not in profile_all and p not in _excluded]

        total   = sum(1 for p in res["basic"] + res["aur"] if p not in _excluded)
        already = total - len(new_basic) - len(new_aur)

        container = QWidget()
        cl = QVBoxLayout(container)
        cl.setContentsMargins(4, 4, 4, 4)
        cl.setSpacing(6)

        summary = QLabel(f"<b>{total}</b> packages detected  •  "
                         f"<span style='color:{t['success']};'><b>{already}</b> already in profile</span>  •  "
                         f"<span style='color:{t['accent']};'><b>{len(new_basic)+len(new_aur)}</b> new</span>")
        summary.setTextFormat(Qt.TextFormat.RichText)
        cl.addWidget(summary)
        cl.addWidget(sep())

        has_new_pkgs = bool(new_basic or new_aur)
        for title, items, kind in [("Basic Packages", new_basic, "basic"),
                                   ("AUR Packages",   new_aur,   "aur")]:
            if not items:
                continue
            lbl = QLabel(f"<b>{title}</b> — <span style='color:{t['accent']};'>{len(items)} new</span>")
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setStyleSheet(f"font-size:{font_sz(1)}px;")
            cl.addWidget(lbl)

            grid = QWidget()
            gl   = QGridLayout(grid)
            gl.setContentsMargins(8, 2, 8, 2)
            gl.setSpacing(3)
            cols = 4
            for i, name in enumerate(items):
                cb = QCheckBox(name)
                cb.setChecked(False)
                gl.addWidget(cb, i // cols, i % cols)
                self._cbs.append((cb, name, kind))
            cl.addWidget(grid)
            cl.addWidget(sep())

        if not has_new_pkgs:
            ok_lbl = QLabel("✅  All packages from this system are already in the profile.")
            ok_lbl.setStyleSheet(f"color:{t['success']};font-weight:bold;")
            ok_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(ok_lbl)

        cl.addWidget(self._build_specific_section(res["basic"], _excluded, t))

        specific = res.get("specific", [])
        if specific:
            cl.addWidget(sep())
            n_ok  = sum(1 for p in specific if p["installed"])
            n_bad = len(specific) - n_ok
            color = t["error"] if n_bad else t["success"]
            hdr = QLabel(
                f"<b>Specific Packages (Profile)</b> — "
                f"<span style='color:{color};'>{n_ok}/{len(specific)} installed</span>"
            )
            hdr.setTextFormat(Qt.TextFormat.RichText)
            hdr.setStyleSheet(f"font-size:{font_sz(1)}px;color:{t['accent2']};")
            cl.addWidget(hdr)

            grid = QWidget()
            gl   = QGridLayout(grid)
            gl.setContentsMargins(8, 2, 8, 2)
            gl.setSpacing(3)
            cols = 3
            for i, p in enumerate(specific):
                session = f"[{p['session']}]" if p.get("session") else ""
                icon    = "✅" if p["installed"] else "❌"
                lbl     = QLabel(f"{icon}  {p['name']}  <span style='color:{t['muted']};font-size:{font_sz(-2)}px;'>{session}</span>")
                lbl.setTextFormat(Qt.TextFormat.RichText)
                lbl.setStyleSheet(f"color:{t['success'] if p['installed'] else t['error']};")
                gl.addWidget(lbl, i // cols, i % cols)
            cl.addWidget(grid)

        sys_files = res.get("sys_files", [])
        if sys_files:
            cl.addWidget(sep())
            n_ok  = sum(1 for f in sys_files if f["status"] == "ok")
            n_bad = len(sys_files) - n_ok
            color = t["error"] if n_bad else t["success"]
            hdr = QLabel(
                f"<b>📄  System Files</b> — "
                f"<span style='color:{color};'>{n_ok}/{len(sys_files)} up to date</span>"
            )
            hdr.setTextFormat(Qt.TextFormat.RichText)
            hdr.setStyleSheet(f"font-size:{font_sz(1)}px;")
            cl.addWidget(hdr)
            _status_map = {
                "changed":     ("⚠",  "Changed",       "warning"),
                "dst_missing": ("❌", "Not backed up",  "error"),
                "src_missing": ("❓", "Source missing", "muted"),
            }
            sf_grid = QWidget()
            sg = QGridLayout(sf_grid)
            sg.setContentsMargins(8, 2, 8, 2)
            sg.setSpacing(2)
            for i, f in enumerate(sys_files):
                if f["status"] == "ok":
                    icon, label, ck = "✅", "OK", "success"
                else:
                    icon, label, ck = _status_map.get(f["status"], ("?", f["status"], "text"))
                row_lbl = QLabel(
                    f"{icon}  <b>{f['name']}</b>  "
                    f"<span style='color:{t['muted']};font-size:{font_sz(-2)}px;'>"
                    f"{f['src']} → {f['dst']}</span>"
                    f"  <span style='color:{t[ck]};font-size:{font_sz(-1)}px;'>[{label}]</span>"
                )
                row_lbl.setTextFormat(Qt.TextFormat.RichText)
                row_lbl.setWordWrap(True)
                sg.addWidget(row_lbl, i, 0)
            cl.addWidget(sf_grid)
        elif S.system_files:
            cl.addWidget(sep())
            info = QLabel("📄  <i>No active system files configured.</i>")
            info.setTextFormat(Qt.TextFormat.RichText)
            info.setStyleSheet(f"color:{t['muted']};")
            cl.addWidget(info)

        services = res.get("services", [])
        if services:
            cl.addWidget(sep())
            cl.addWidget(self._build_service_section(services, t))

        cl.addStretch()
        self._scroll.setWidget(container)
        self._scroll.show()
        self._sel_all_btn.setVisible(has_new_pkgs)
        self._add_btn.setVisible(has_new_pkgs)
        if has_new_pkgs:
            self._btns.show()

    def _build_specific_section(self, new_basic: list[str], excluded: frozenset, t: dict) -> QWidget:
        wrapper = QWidget()
        vl = QVBoxLayout(wrapper)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(4)

        hdr = QLabel(f"<b>Mark as Specific Package</b> — install only for a certain session")
        hdr.setTextFormat(Qt.TextFormat.RichText)
        hdr.setStyleSheet(f"font-size:{font_sz(1)}px;color:{t['accent2']};")
        vl.addWidget(hdr)

        hint = QLabel(
            "Select packages below and choose a session. "
            "They will be added to <i>Specific Packages</i> and "
            "removed from Basic Packages if present."
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{t['muted']};font-size:{font_sz(-1)}px;")
        vl.addWidget(hint)

        from PyQt6.QtWidgets import QComboBox
        sess_row = QHBoxLayout()
        sess_row.addWidget(QLabel("Target session:"))
        self._spec_sess_cb = QComboBox()
        self._spec_sess_cb.addItems(SESSIONS)
        if self._current_session in SESSIONS:
            self._spec_sess_cb.setCurrentText(self._current_session)
        sess_row.addWidget(self._spec_sess_cb)
        sess_row.addStretch()
        vl.addLayout(sess_row)

        existing_specific = {p.get("package", "") for p in S.specific_packages if isinstance(p, dict)}
        eligible = [p for p in new_basic if p not in excluded and p not in existing_specific]

        if not eligible:
            no_lbl = QLabel("— No eligible packages found —")
            no_lbl.setStyleSheet(f"color:{t['muted']};")
            vl.addWidget(no_lbl)
            return wrapper

        from PyQt6.QtWidgets import QLineEdit
        search = QLineEdit()
        search.setPlaceholderText("Filter packages…")
        search.setMaximumWidth(300)
        vl.addWidget(search)

        grid_w = QWidget()
        self._spec_grid = QGridLayout(grid_w)
        self._spec_grid.setContentsMargins(8, 2, 8, 2)
        self._spec_grid.setSpacing(3)
        self._spec_cbs: list[tuple[QCheckBox, str]] = []
        cols = 4
        for i, name in enumerate(eligible):
            cb = QCheckBox(name)
            cb.setChecked(False)
            self._spec_grid.addWidget(cb, i // cols, i % cols)
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

        hint = QLabel(
            "Active services are ticked. "
            "Check services you want to add to the profile (System Manager Operations). "
            "Already-configured services are shown as disabled."
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{t['muted']};font-size:{font_sz(-1)}px;")
        vl.addWidget(hint)

        grid = QWidget()
        gl = QGridLayout(grid)
        gl.setContentsMargins(8, 4, 8, 4)
        gl.setSpacing(4)

        _OP_LABELS: dict[str, str] = {
            "enable_bluetooth_service": "Bluetooth",
            "enable_atd_service": "atd (at-daemon)",
            "enable_firewall": "Firewall (ufw)",
            "enable_printer_support": "Printer (CUPS)",
            "enable_ssh_service": "SSH server",
            "enable_samba_network_filesharing": "Samba (file sharing)",
            "enable_cronie_service": "Cron (cronie/cron)",
            "install_snap": "Snapd",
            "enable_flatpak_integration": "Flatpak",
        }

        cols = 2
        for i, svc_info in enumerate(services):
            op     = svc_info["op"]
            svc    = svc_info["service"]
            active = svc_info["active"]
            in_profile = svc_info["in_profile"]

            label = _OP_LABELS.get(op, op)
            status = "✅ active" if active else "⬜ inactive"
            cb = QCheckBox(f"{label}  [{status}]  ({svc}.service)")
            cb.setChecked(active and not in_profile)
            cb.setEnabled(not in_profile)
            if in_profile:
                cb.setStyleSheet(f"color:{t['muted']};")
                cb.setToolTip("Already in System Manager Operations profile")
            elif active:
                cb.setStyleSheet(f"color:{t['success']};")
                cb.setToolTip(f"{svc}.service is currently active — add op to profile?")
            else:
                cb.setStyleSheet(f"color:{t['muted']};")
                cb.setToolTip(f"{svc}.service is currently inactive")

            gl.addWidget(cb, i // cols, i % cols)
            self._svc_cbs.append((cb, op, svc))

        vl.addWidget(grid)

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
        from PyQt6.QtWidgets import QFileDialog
        from state import _HOME
        from datetime import datetime

        res = self._last_result
        if not res:
            QMessageBox.information(self.window(), "No Data", "Run a check first.")
            return

        lines: list[str] = [
            "Backup Helper — Capture Report",
            f"Profile : {S.profile_name or '—'}",
            f"Date    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 60,
        ]

        def _section(title: str, rows: list[tuple[str, str]]) -> None:
            lines.append(f"\n[{title}]")
            if not rows:
                lines.append("  —")
                return
            for _status, detail in rows:
                lines.append(f"  {_status}  {detail}")

        profile_all = all_profile_pkg_names()
        _excluded   = self._sm_pkgs | _SYSTEM_BASE_PKGS
        basic_pkgs  = res.get("basic", [])
        aur_pkgs    = res.get("aur",   [])
        pkg_rows: list[tuple[str, str]] = []
        for name in basic_pkgs:
            if name in _excluded:
                continue
            status = "✓" if name in profile_all else "new"
            pkg_rows.append((status, f"{name} (basic)"))
        for name in aur_pkgs:
            if name in _excluded:
                continue
            status = "✓" if name in profile_all else "new"
            pkg_rows.append((status, f"{name} (aur)"))
        for p in res.get("specific", []):
            session = p.get("session") or "—"
            pkg_rows.append(("✓" if p["installed"] else "✗", f"{p['name']} (specific / {session})"))
        _section("Packages", pkg_rows)

        _status_labels = {"ok": "OK", "changed": "Changed", "dst_missing": "Not backed up", "src_missing": "Source missing"}
        _section("System Files", [
            ("✓" if f["status"] == "ok" else "⚠",
             f"{f['name']}  [{_status_labels.get(f['status'], f['status'])}]  {f['src']} → {f['dst']}")
            for f in res.get("sys_files", [])
        ])

        services = res.get("services", [])
        _section("Services", [
            ("✓" if s["active"] else "⚠",
             f"{s['service']}.service  ({s['op']})")
            for s in services
        ])

        text = "\n".join(lines)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self.window(), "Export Capture Report",
            str(_HOME / f"capture_report_{ts}.txt"), "Text (*.txt)")
        if not path:
            return
        try:
            from pathlib import Path
            Path(path).write_text(text, encoding="utf-8")
            QMessageBox.information(self.window(), "Exported", f"Report saved to:\n{path}")
        except OSError as exc:
            QMessageBox.critical(self.window(), "Export Failed", str(exc))

    def _add_to_profile(self) -> None:
        new_basic = [n for cb, n, k in self._cbs if cb.isChecked() and cb.isEnabled() and k == "basic"]
        new_aur   = [n for cb, n, k in self._cbs if cb.isChecked() and cb.isEnabled() and k == "aur"]
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
        if not hasattr(self, "_spec_cbs") or not hasattr(self, "_spec_sess_cb"):
            return

        session = self._spec_sess_cb.currentText()
        selected = [name for cb, name in self._spec_cbs if cb.isChecked() and cb.isEnabled()]

        if not selected:
            QMessageBox.information(self.window(), "Nothing Selected",
                                    "No packages selected for Specific Packages.")
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
            QMessageBox.information(self.window(), "Nothing Selected",
                                    "No services selected.")
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


# noinspection PyUnresolvedReferences
class _VerifyTab(QWidget):
    def __init__(self, helper: LinuxDistroHelper) -> None:
        super().__init__()
        self._helper = helper
        self._worker: Optional[_VerifyWorker] = None
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
        if self._worker and self._worker.isRunning():
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

        self._worker = _VerifyWorker(self._helper)
        self._worker.progress.connect(self._prog_label.setText)
        self._worker.done.connect(self._on_done)
        self._worker.start()

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
            n_ok    = sum(1 for p in pkgs if p["installed"])
            n_bad   = len(pkgs) - n_ok
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
            n_ok  = sum(1 for f in sys_files if f["status"] == "ok")
            n_bad = len(sys_files) - n_ok
            total_issues += n_bad
            sec = _Section("📄", "System Files", n_ok, len(sys_files), t["success"], t["warning"])
            _status_map = {
                "changed":     ("⚠",  "Changed",       "warning"),
                "dst_missing": ("❌", "Not backed up",  "error"),
                "src_missing": ("❓", "Source missing", "muted"),
            }
            for f in sys_files:
                if f["status"] == "ok":
                    sec.add_row("✅", f["name"], f"OK  —  {f['src']}  →  {f['dst']}", t["success"])
                else:
                    ic, lbl, ck = _status_map.get(f["status"], ("?", f["status"], "text"))
                    sec.add_row(ic, f["name"], f"{lbl}  —  {f['src']}  →  {f['dst']}", t[ck])
            cl.addWidget(sec)
        else:
            sec = _Section("📄", "System Files", 0, 0, t["muted"], t["muted"])
            msg = "No active system files" if S.system_files else "No system files in profile"
            sec.add_row("—", msg, "", t["muted"])
            cl.addWidget(sec)

        backups = res.get("backups", [])
        if backups:
            n_ok  = sum(1 for b in backups if b["status"] == "ok")
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
            n_ok  = sum(1 for s in services if s["active"])
            n_bad = len(services) - n_ok
            total_issues += n_bad
            sec = _Section("⚙️", "Services", n_ok, len(services), t["success"], t["warning"])
            for s in services:
                if s["active"]:
                    sec.add_row("✅", f"{s['service']}.service", f"Active  —  op: {s['op']}", t["success"])
                else:
                    sec.add_row("⚠", f"{s['service']}.service", f"Inactive  —  op: {s['op']}", t["warning"])
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

        if total_issues == 0:
            self._summary.setStyleSheet(f"font-size:{font_sz(1)}px;font-weight:bold;padding:8px;"
                                        f"border-radius:4px;background:{t['bg2']};color:{t['success']};")
            self._summary.setText("✅  Everything looks good — system matches profile.")
        else:
            self._summary.setStyleSheet(f"font-size:{font_sz(1)}px;font-weight:bold;padding:8px;"
                                        f"border-radius:4px;background:{t['bg2']};color:{t['warning']};")
            self._summary.setText(f"⚠  {total_issues} issue(s) found — click section headers to expand details.")
        self._summary.show()


class CaptureVerifyDialog(_StandardKeysMixin, QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("System Capture & Verify")
        self._helper = LinuxDistroHelper()
        self._build_ui()
        self._size_to_screen()

    def _size_to_screen(self) -> None:
        scr = QApplication.primaryScreen()
        if scr:
            sg = scr.availableGeometry()
            w  = min(max(950, sg.width()  * 2 // 3), sg.width()  - 60)
            h  = min(max(620, sg.height() * 3 // 4), sg.height() - 60)
            self.resize(w, h)
        else:
            self.resize(950, 640)

    def _build_ui(self) -> None:
        t   = current_theme()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        info = QLabel(f"<b>{self._helper.distro_pretty_name}</b>  •  "
                      f"Package manager: <b>{self._helper.pkg_manager_name()}</b>  •  "
                      f"Profile: <b>{S.profile_name or 'none'}</b>")
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setStyleSheet(f"color:{t['muted']};font-size:{font_sz(-1)}px;")
        lay.addWidget(info)

        tabs = QTabWidget()
        tabs.addTab(_CaptureTab(self._helper), "🔍  System Capture")
        tabs.addTab(_VerifyTab(self._helper), "✅  Verify Profile")
        lay.addWidget(tabs, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close_btn)
        lay.addLayout(row)