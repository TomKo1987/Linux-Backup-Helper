import html as _html_mod
import json
import logging
import os
import pwd
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

_USER = pwd.getpwuid(os.getuid()).pw_name
_HOME = Path.home()
_CONFIG_DIR  = _HOME / ".config" / "Backup Helper"
_PROFILES_DIR = _CONFIG_DIR / "profiles"
_LOG_DIR  = _CONFIG_DIR / "logs"
_LOG_FILE = _LOG_DIR / "backup_helper.log"
_PROFILE_RE = re.compile(r"^[^\s._][\w\-. ]*\S$|^[^\s._]$")

RESTART_DIALOG: int = 2
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _make_logger(name: str) -> logging.Logger:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s — %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)
    try:
        fh = RotatingFileHandler(_LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except (OSError, PermissionError):
        log.warning("Could not create log file at %s", _LOG_FILE)
    log.propagate = False
    return log


logger = _make_logger("backup_helper")

_path_replacements: tuple[tuple[str, str], ...] = ((_HOME.as_posix(), "~"), (f"/run/media/{_USER}/", ""))

_tooltip_cache: Optional[tuple[dict, dict, dict]] = None
_tooltip_lock     = threading.Lock()
_session_lock     = threading.Lock()
_cached_session   = ""
_session_detected = False


def invalidate_tooltip_cache() -> None:
    global _tooltip_cache
    with _tooltip_lock:
        _tooltip_cache = None


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mKHJA-Z]")
_NORM_PATHS_RE = re.compile(r" (?=/|smb://|cifs://)")


def apply_replacements(text: str) -> str:
    for old, new in _path_replacements:
        if old:
            text = text.replace(old, new)
    if "\x1b" in text:
        text = _ANSI_RE.sub("", text)
    return text


def block_set(cb, checked: bool) -> None:
    cb.blockSignals(True)
    cb.setChecked(checked)
    cb.blockSignals(False)


@dataclass
class State:
    profile_name: str = ""
    entries: list[dict]           = field(default_factory=list)
    headers: dict[str, dict]      = field(default_factory=dict)
    mount_options: list[dict]     = field(default_factory=list)
    system_manager_ops: list[str] = field(default_factory=list)
    system_files: list[dict]      = field(default_factory=list)
    basic_packages: list[dict]    = field(default_factory=list)
    aur_packages: list[dict]      = field(default_factory=list)
    specific_packages: list[dict] = field(default_factory=list)
    user_shell: str = "bash"
    ui: dict = field(
        default_factory=lambda: {"theme": "Tokyo Night", "font_family": "", "font_size": 14,
                                 "backup_window_columns": 2, "restore_window_columns": 2, "settings_window_columns": 2})


S = State()


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _normalise_pkg(p) -> dict:
    if isinstance(p, str):
        return {"name": p, "disabled": False}
    return {**p, "disabled": p.get("disabled", False)}

def _norm_pkgs(raw: list) -> list[dict]:
    return sorted((_normalise_pkg(p) for p in raw), key=lambda x: x.get("name", x.get("package", "")).lower())


def _norm_paths(raw: Any) -> list[str]:
    if not raw:
        return []
    items = [raw] if isinstance(raw, str) else raw
    result = []
    for item in items:
        s = str(item).strip()
        if s:
            for p in _NORM_PATHS_RE.split(s):
                p = p.strip()
                if p:
                    result.append(p)
    return result


def _valid_hex_color(value: Any) -> bool: return isinstance(value, str) and bool(_HEX_COLOR_RE.match(value))


def _parse_entry(raw: dict) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    header = raw.get("header", "").strip()
    title  = raw.get("title",  "").strip()
    source = _norm_paths(raw.get("source"))
    dest   = _norm_paths(raw.get("destination"))
    if not (header and title and source and dest):
        return None
    details = raw.get("details", {})
    return {"header": header, "title": title, "source": source, "destination": dest, "details": details if isinstance(details, dict) else {}}


def load_profile(path: Path) -> bool:
    try:
        return _load_profile_from_data(path, json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        logger.error("load_profile failed: %s", exc)
        return False


def _load_profile_from_data(path: Path, data: dict) -> bool:
    try:
        new_name = path.stem

        new_headers = {k: {"inactive": bool(v.get("inactive")), "color": v.get("header_color", "#ffffff")
        if _valid_hex_color(v.get("header_color")) else "#ffffff"} for k, v in data.get("header", {}).items() if isinstance(v, dict)}

        new_entries = [e for raw in data.get("entries", []) if (e := _parse_entry(raw)) is not None]
        new_mount   = [o for o in data.get("mount_options", []) if isinstance(o, dict)]
        def _as_list(key: str) -> list:
            v = data.get(key)
            return v if isinstance(v, list) else []
        new_sm_ops    = _as_list("system_manager_operations")
        new_sys_files = _as_list("system_files")
        new_basic     = _norm_pkgs(_as_list("basic_packages"))
        new_aur       = _norm_pkgs(_as_list("aur_packages"))
        new_specific  = _as_list("specific_packages")
        raw_shell     = data.get("user_shell", "bash")
        new_shell     = raw_shell if isinstance(raw_shell, str) and raw_shell.strip() else "bash"
        new_ui = dict(S.ui)
        raw_ui = data.get("ui_settings", {})
        if isinstance(raw_ui, dict):
            if "font_size" in raw_ui:
                try:
                    raw_ui = {**raw_ui, "font_size": max(8, min(48, int(raw_ui["font_size"])))}
                except (ValueError, TypeError):
                    raw_ui = {k: v for k, v in raw_ui.items() if k != "font_size"}
            new_ui.update(raw_ui)

        S.profile_name       = new_name
        S.headers            = new_headers
        S.entries            = new_entries
        S.mount_options      = new_mount
        S.system_manager_ops = new_sm_ops
        S.system_files       = new_sys_files
        S.basic_packages     = new_basic
        S.aur_packages       = new_aur
        S.specific_packages  = new_specific
        S.user_shell         = new_shell
        S.ui                 = new_ui

        invalidate_tooltip_cache()
        logger.info("Loaded profile '%s'", S.profile_name)
        return True
    except Exception as exc:
        logger.error("load_profile failed: %s", exc)
        return False


def save_profile(path: Optional[Path] = None) -> bool:
    path = path or (_PROFILES_DIR / f"{S.profile_name}.json" if S.profile_name else None)
    if not path:
        return False
    data = {
        "is_default": True,
        "mount_options": S.mount_options,
        "header": {k: {"inactive": v.get("inactive", False), "header_color": v.get("color", "#ffffff")}
                   for k, v in S.headers.items()},
        "system_manager_operations": S.system_manager_ops,
        "system_files":    S.system_files,
        "basic_packages":  S.basic_packages,
        "aur_packages":    S.aur_packages,
        "specific_packages": sorted(
            S.specific_packages, key=lambda x: str(x.get("package", "") if isinstance(x, dict) else x).lower()),
        "ui_settings": S.ui,
        "user_shell":  S.user_shell,
        "entries": sorted(S.entries, key=lambda e: (e.get("header", "").lower(), e.get("title", "").lower()))}
    try:
        _atomic_write(path, data)
        invalidate_tooltip_cache()
        from drive_utils import invalidate_mount_cache
        invalidate_mount_cache()
        return True
    except Exception as exc:
        logger.error("save_profile failed: %s", exc)
        return False


def list_profiles() -> list[str]:
    if not _PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in _PROFILES_DIR.glob("*.json") if _PROFILE_RE.match(p.stem))


def startup_load() -> bool:
    parsed: list[tuple[Path, dict]] = []
    for name in list_profiles():
        p = _PROFILES_DIR / f"{name}.json"
        try:
            parsed.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except Exception as exc:
            logger.error("startup_load: %s", exc)
    default_path = default_data = None
    for p, data in parsed:
        if data.get("is_default"):
            if default_path is None:
                default_path, default_data = p, data
            else:
                data = {k: v for k, v in data.items() if k != "is_default"}
                try:
                    _atomic_write(p, data)
                except OSError as exc:
                    logger.error("startup_load: could not clear duplicate in '%s': %s", p.stem, exc)
                logger.warning("Cleared duplicate is_default flag in '%s'", p.stem)
    if default_path and default_data:
        if _load_profile_from_data(default_path, default_data):
            return True
        logger.warning("startup_load: default profile '%s' failed to load, trying others", default_path.stem)
    return any(_load_profile_from_data(p, data) for p, data in parsed if p != default_path)


def _entry_tooltip_html(title, src_lines, dst_lines, bg, bg2, bg3, c_title, c_data, font_sz_fn) -> str:
    s_html, d_html = ("<br/>".join(_html_mod.escape(apply_replacements(str(p))) for p in lines) for lines in (src_lines, dst_lines))
    safe_title = _html_mod.escape(title).replace("&lt;br&gt;", "<br/>")
    label_style = f"color:{c_title}; font-weight: bold; border: 5px solid {c_title}; margin-bottom: 5px;"
    cell_padding = "padding:6px;"

    return (f"<table style='width: 100%; font-family: monospace; white-space: nowrap; border: 5px solid {bg};'>"
            f"<tr style='background-color: {bg};'>"
            f"<td colspan='2' style='font-size: {font_sz_fn(-2)}px; color: {c_title}; text-align: center'>"
            f"<b>{safe_title}</b></td></tr><tr>"
            f"<td style='background-color: {bg2}; font-size: {font_sz_fn(-3)}px; color: {c_data}; line-height: 1.4; "
            f"{cell_padding} vertical-align: top; white-space: nowrap'>"
            f"<span style='{label_style};'>Source:</span><br>{s_html}</td>"
            f"<td style='background-color: {bg3}; font-size: {font_sz_fn(-3)}px; color: {c_data}; line-height: 1.4; "
            f"{cell_padding} vertical-align: top; white-space: nowrap'>"
            f"<span style='{label_style}'>Destination:</span><br>{d_html}</td>"
            f"</tr></table>")


def _sysfiles_tooltip_html(sys_files, t, font_sz_fn) -> str:
    cols   = 2 if len(sys_files) > 8 else 1
    header = (f"<tr><td colspan='{cols}' style='padding:4px 5px 2px;font-size:{font_sz_fn(-1)}px;"
              f"font-weight:bold;white-space:nowrap;color:{t['accent2']};border-bottom:1px solid {t['header_sep']}'>"
              f"System Files ({len(sys_files)})</td></tr>")
    cells = []
    for sf in sys_files:
        src = sf.get('source', '')
        dst = sf.get('destination', '')
        cells.append(f"<td style='padding:4px 6px;border:1px solid {t['header_sep']};white-space:nowrap;vertical-align:top;'>"
                     f"<span style='color:{t['accent2']};font-weight:bold;'>{_html_mod.escape(Path(src).name)}</span><br>"
                     f"<span style='font-size:{font_sz_fn(-3)}px;color:{t['success']};'>"
                     f"{_html_mod.escape(apply_replacements(src))}<br>⤵<br>"
                     f"{_html_mod.escape(apply_replacements(dst))}</span></td>")

    rows = [f"<tr style='background-color:{t['bg2'] if (i // cols) % 2 == 0 else t['bg3']};'>{''.join(cells[i:i + cols])}</tr>"
            for i in range(0, len(cells), cols)]
    return f"<table style='white-space:nowrap; font-family:monospace;font-size:{font_sz_fn(-2)}px;'>{header}{''.join(rows)}</table>"


def _packages_tooltip_html(label, pkg_names, t, font_sz_fn) -> str:
    cols   = 8 if len(pkg_names) > 25 else 5
    header = (f"<tr><td colspan='{cols}' style="
              f"'padding:4px 5px 2px;font-size:{font_sz_fn(-1)}px;font-weight:bold;color:{t['accent2']};"
              f"border-bottom:1px solid {t['header_sep']};'>{label} ({len(pkg_names)})</td></tr>")
    rows = []
    for i in range(0, len(pkg_names), cols):
        cells = "".join(f"<td style='padding:5px;border:1px solid {t['header_sep']};color:{t['success']};white-space:nowrap'>{p}</td>"
                        for p in pkg_names[i:i + cols])
        rows.append(f"<tr style='background-color:{t['bg2'] if (i // cols) % 2 == 0 else t['bg3']};'>{cells}</tr>")
    return f"<table style='white-space:nowrap; font-family:monospace;font-size:{font_sz_fn(-2)}px;'>{header}{''.join(rows)}</table>"


def _specific_pkgs_tooltip_html(sp_active, session, t, font_sz_fn) -> str:
    sp_groups: dict[str, list[str]] = defaultdict(list)
    for p in sp_active:
        sp_groups[p.get("session", "?")].append(_html_mod.escape(p.get("package", "")))

    rows, cols, show_sess_hdr = [], 5, len(sp_groups) > 1
    for i, sess in enumerate(sorted(sp_groups)):
        if show_sess_hdr:
            rows.append(f"<tr style='background-color:{t['bg'] if i % 2 == 0 else t['bg2']};'><td colspan='{cols}' style="
                        f"'padding:3px 5px;font-size:{font_sz_fn(-2)}px;font-weight:bold;color:{t['accent2']};"
                        f"white-space:nowrap;border-bottom:1px solid {t['header_sep']};'>{_html_mod.escape(sess)}</td></tr>")

        for j in range(0, len(sp_groups[sess]), cols):
            cells = "".join(f"<td style='padding:5px;border:1px solid {t['header_sep']};color:{t['success']};'>{p}</td>"
                            for p in sp_groups[sess][j:j + cols])
            rows.append(f"<tr style='background-color:{t['bg2'] if (j // cols) % 2 == 0 else t['bg3']};'>{cells}</tr>")

    header = (f"<tr><td colspan='{cols}' style='padding:4px 5px 2px;font-size:{font_sz_fn(-1)}px;font-weight:bold;"
              f"color:{t['accent2']};border-bottom:1px solid {t['header_sep']};'>Specific Packages "
              f"for {_html_mod.escape(session or 'current session')} ({len(sp_active)})</td></tr>")

    return f"<table style='font-family:monospace;font-size:{font_sz_fn(-2)}px; white-space:nowrap'>{header}{''.join(rows)}</table>"


def generate_tooltip() -> tuple[dict, dict, dict]:
    global _cached_session, _session_detected, _tooltip_cache
    from themes import current_theme, font_sz
    from linux_distro_helper import LinuxDistroHelper

    with _tooltip_lock:
        cached = _tooltip_cache
        if cached is not None:
            return cached

    _local_session = ""
    with _session_lock:
        already_detected = _session_detected
    if not already_detected:
        try:
            _local_session = LinuxDistroHelper.detect_session() or ""
        except Exception as e:
            logger.warning("Session detect failed: %s", e)

    with _session_lock:
        if not _session_detected:
            _cached_session   = _local_session
            _session_detected = True
        session = _cached_session if _cached_session else None

    t = current_theme()
    backup_tips = {e["title"]: _entry_tooltip_html(e["title"], e.get("source", []), e.get("destination", []),
                                                   t["bg"], t["bg2"], t["bg3"], t["accent2"], t["success"], font_sz) for e in S.entries}
    restore_tips = {e["title"]: _entry_tooltip_html(e["title"], e.get("destination", []), e.get("source", []),
                                                    t["bg"], t["bg2"], t["bg3"], t["accent2"], t["success"], font_sz) for e in S.entries}
    sm_tips: dict = {}

    active_sys_files = [f for f in (S.system_files or []) if isinstance(f, dict) and not f.get("disabled")]
    if active_sys_files:
        sm_tips["copy_system_files"] = _sysfiles_tooltip_html(active_sys_files, t, font_sz)

    for key, pkgs, label in [("install_basic_packages", S.basic_packages, "Basic Packages"),
                             ("install_aur_packages", S.aur_packages, "AUR Packages")]:
        active_names = [_html_mod.escape(p["name"]) for p in pkgs if not p.get("disabled") and "name" in p]

        if active_names:
            sm_tips[key] = _packages_tooltip_html(label, active_names, t, font_sz)

    sp_active = [p for p in S.specific_packages if not p.get("disabled") and (not session or p.get("session") == session)]
    if sp_active:
        sm_tips["install_specific_packages"] = _specific_pkgs_tooltip_html(sp_active, session, t, font_sz)

    if S.user_shell:
        sm_tips["set_user_shell"] = (f"<table style='white-space:nowrap; font-family:monospace;'>"
                                     f"<tr><td style='padding:4px 5px 2px;font-size:{font_sz(-1)}px;font-weight:bold;"
                                     f"color:{t['accent2']};border-bottom:1px solid {t['header_sep']};'>Selected Shell</td></tr>"
                                     f"<tr style='background-color:{t['bg2']};'><td style='padding:8px 6px;border:1px solid "
                                     f"{t['header_sep']};color:{t['success']};'>{_html_mod.escape(S.user_shell)}</td></tr></table>")

    result = (backup_tips, restore_tips, sm_tips)
    with _tooltip_lock:
        if _tooltip_cache is None:
            _tooltip_cache = result
        else:
            result = _tooltip_cache
    return result
