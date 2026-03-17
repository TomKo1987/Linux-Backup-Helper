from pathlib import Path
import html as _html_mod
from typing import Any, Optional
from logging.handlers import RotatingFileHandler
import json, logging, os, pwd, re, tempfile, threading

_USER         = pwd.getpwuid(os.getuid()).pw_name
_HOME         = Path.home()
_CONFIG_DIR   = _HOME / ".config" / "Backup Helper"
_PROFILES_DIR = _CONFIG_DIR / "profiles"
_LOG_DIR      = _CONFIG_DIR / "logs"
_LOG_FILE     = _LOG_DIR / "backup_helper.log"
_PROFILE_RE   = re.compile(r"^[^\s._][\w\-. ]*$")

_COLS_NARROW = 2
_COLS_WIDE   = 4


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

text_replacements: list = [(_HOME.as_posix(), "~"), (f"/run/media/{_USER}/", ""), ("\x1b[1m", ""), ("\x1b[0m", "")]


def apply_replacements(text: str) -> str:
    for old, new in text_replacements:
        if old:
            text = text.replace(old, new)
    return text


def _safe_path_for_html(path: str) -> str:
    return _html_mod.escape(apply_replacements(path))


def block_set(cb, checked: bool) -> None:
    cb.blockSignals(True)
    cb.setChecked(checked)
    cb.blockSignals(False)


class State:
    def __init__(self):
        self.profile_name:       str             = ""
        self.entries:            list[dict]      = []
        self.headers:            dict[str, dict] = {}
        self.mount_options:      list[dict]      = []
        self.system_manager_ops: list[str]       = []
        self.system_files:       list[dict]      = []
        self.basic_packages:     list[dict]      = []
        self.aur_packages:       list[dict]      = []
        self.specific_packages:  list[dict]      = []
        self.user_shell: str = "bash"
        self.ui: dict = {"theme": "Tokyo Night", "font_family": "", "font_size": 14, "backup_window_columns": 2,
                         "restore_window_columns":   2, "settings_window_columns":  2}


S = State()


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _norm_pkgs(raw: list) -> list[dict]:
    result = []
    for p in raw:
        if isinstance(p, str):
            result.append({"name": p, "disabled": False})
        elif isinstance(p, dict):
            entry = dict(p)
            entry.setdefault("disabled", False)
            result.append(entry)
    return sorted(result, key=lambda x: x.get("name", x.get("package", "")).lower())


def _norm_paths(raw: Any) -> list[str]:
    if not raw:
        return []

    def _smart_split(s: str) -> list[str]:
        if " /" in s or " smb://" in s:
            return [p.strip() for p in re.split(r" (?=/|smb://)", s.strip()) if p.strip()]
        return [s.strip()]

    if isinstance(raw, str):
        return _smart_split(raw)

    result = []
    if isinstance(raw, list):
        for item in raw:
            val = str(item).strip()
            if val:
                result.extend(_smart_split(val))
    return result


def load_profile(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        S.profile_name = path.stem

        hdr = data.get("header", {})
        validated_headers: dict[str, dict[str, Any]] = {}
        for k, v in hdr.items():
            if not isinstance(v, dict):
                v = {}
            color = v.get("header_color", "#ffffff")
            if not (isinstance(color, str) and re.match(r"^#[0-9a-fA-F]{6}$", color)):
                color = "#ffffff"
            validated_headers[k] = {"inactive": bool(v.get("inactive", False)), "color": color}

        S.headers = validated_headers
        S.entries = []

        for e in data.get("entries", []):
            if not isinstance(e, dict):
                continue
            header = e.get("header", "").strip()
            title  = e.get("title",  "").strip()
            src    = _norm_paths(e.get("source", []))
            dst    = _norm_paths(e.get("destination", []))
            if header and title and src and dst:
                raw_details = e.get("details", {})
                details = raw_details if isinstance(raw_details, dict) else {}
                S.entries.append({"header": header, "title": title, "source": src, "destination": dst, "details": details})

        S.mount_options      = [o for o in data.get("mount_options", []) if isinstance(o, dict)]
        S.system_manager_ops = data.get("system_manager_operations", [])
        S.system_files       = data.get("system_files", [])
        S.basic_packages     = _norm_pkgs(data.get("basic_packages", []))
        S.aur_packages       = _norm_pkgs(data.get("aur_packages", []))
        S.specific_packages  = data.get("specific_packages", [])
        S.user_shell         = data.get("user_shell", "bash")
        S.ui.update(data.get("ui_settings", {}))

        logger.info("Loaded profile '%s'", S.profile_name)
        return True
    except Exception as exc:
        logger.error("load_profile failed: %s", exc)
        return False


def save_profile(path: Optional[Path] = None) -> bool:
    if path is None:
        if not S.profile_name:
            return False
        path = _PROFILES_DIR / f"{S.profile_name}.json"

    sorted_entries = sorted(S.entries, key=lambda e: (e.get("header", "").lower(), e.get("title", "").lower()))
    data = {
        "is_default": True,
        "mount_options": S.mount_options,
        "header": {k: {"inactive": v.get("inactive", False), "header_color": v.get("color", "#ffffff")} for k, v in S.headers.items()},
        "system_manager_operations": S.system_manager_ops,
        "system_files":      S.system_files,
        "basic_packages":    S.basic_packages,
        "aur_packages":      S.aur_packages,
        "specific_packages": sorted(S.specific_packages, key=lambda x: (x.get("package", "") if isinstance(x, dict) else str(x)).lower()),
        "ui_settings": S.ui,
        "user_shell":  S.user_shell,
        "entries":     sorted_entries,
    }
    try:
        _atomic_write(path, data)
        logger.info("Saved profile '%s'", path.stem)
        return True
    except Exception as exc:
        logger.error("save_profile failed: %s", exc)
        return False


def list_profiles() -> list[str]:
    if not _PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in _PROFILES_DIR.glob("*.json") if _PROFILE_RE.match(p.stem))


def startup_load() -> bool:
    profiles = list_profiles()
    if not profiles:
        return False

    valid_paths = [_PROFILES_DIR / f"{name}.json" for name in profiles]
    default_path = None

    for p in valid_paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("is_default"):
                if default_path is None:
                    default_path = p
                else:
                    data.pop("is_default")
                    try:
                        _atomic_write(p, data)
                    except OSError as exc:
                        logger.error("startup_load: could not clear duplicate is_default in '%s': %s", p.stem, exc)
                    logger.warning("Cleared duplicate is_default flag in '%s'", p.stem)
        except Exception as exc:
            logger.error("startup_load: %s", exc)

    if default_path:
        return load_profile(default_path)

    for p in valid_paths:
        if load_profile(p):
            return True
    return False


_session_lock     = threading.Lock()
_cached_session:  str  = ""
_session_detected: bool = False


def generate_tooltip() -> tuple[dict, dict, dict]:
    global _cached_session, _session_detected
    from themes import current_theme
    from linux_distro_helper import LinuxDistroHelper

    with _session_lock:
        if not _session_detected:
            try: _cached_session = LinuxDistroHelper().detect_session() or ""
            except Exception as e: logger.warning("Session detect failed: %s", e)
            _session_detected = True
        session = _cached_session or None

    t = current_theme()
    _bg, _bg2, _bg3, _c_t, _c_d, _c_b = t["bg"], t["bg2"], t["bg3"], t["accent2"], t["success"], t["header_sep"]

    def _entry_html(_title: str, src_lines: list, dst_lines: list) -> str:
        s_html, d_html = ["<br/>".join(_safe_path_for_html(str(p)) for p in lines) for lines in (src_lines, dst_lines)]
        safe_title = _html_mod.escape(_title).replace("&lt;br&gt;", "<br/>")
        return (f"<table style='border-collapse:collapse;width:100%;font-family:monospace;white-space:nowrap'>"
                f"<tr style='background-color:{_bg};'><td style='font-size:16px;color:{_c_t};text-align:center;padding:5px;'>{safe_title}</td></tr>"
                f"<tr style='background-color:{_bg2};'><td style='font-size:14px;color:{_c_d};padding:6px;'>Source:<br>{s_html}</td></tr>"
                f"<tr style='background-color:{_bg3};'><td style='font-size:14px;color:{_c_d};padding:6px;'>Destination:<br>{d_html}</td></tr></table>")

    backup_tips  = {e["title"]: _entry_html(e["title"], e.get("source", []), e.get("destination", [])) for e in S.entries}
    restore_tips = {e["title"]: _entry_html(e["title"], e.get("destination", []), e.get("source", [])) for e in S.entries}
    sm_tips: dict = {}

    sys_files = [f for f in (S.system_files or []) if isinstance(f, dict) and not f.get("disabled")]
    if sys_files:
        sf_cols = 2 if len(sys_files) > 8 else 1
        header = (f"<tr><td colspan='{sf_cols}' style='padding:4px 5px 2px;font-size:14px;"
                  f"font-weight:bold;color:{_c_t};border-bottom:1px solid {_c_b};'>"
                  f"System Files ({len(sys_files)})</td></tr>")
        cells = [(f"<td style='padding:4px 6px;border:1px solid {_c_b};white-space:nowrap;vertical-align:top;'>"
                  f"<span style='color:{_c_t};font-weight:bold;'>{_html_mod.escape(Path(f.get('source', '')).name)}</span>"
                  f"<br><span style='font-size:11px;color:{_c_d};'>{_html_mod.escape(apply_replacements(f.get('source', '')))}"
                  f"<br>⤵<br>{_html_mod.escape(apply_replacements(f.get('destination', '')))}</span></td>")
                 for f in sys_files]
        rows = []
        for i in range(0, len(cells), sf_cols):
            bg_ = _bg2 if (i // sf_cols) % 2 == 0 else _bg3
            row_cells = "".join(cells[i:i + sf_cols])
            rows.append(f"<tr style='background-color:{bg_};'>{row_cells}</tr>")
        sm_tips["copy_system_files"] = (f"<table style='border-collapse:collapse;font-family:monospace;font-size:12px;'>"
                                        f"{header}{''.join(rows)}</table>")

    for key, pkgs, label in [("install_basic_packages", S.basic_packages, "Basic Packages"),
                             ("install_aur_packages", S.aur_packages, "AUR Packages")]:
        active_list = [_html_mod.escape(p["name"]) for p in pkgs if not p.get("disabled") and "name" in p]
        if active_list:
            cols = 8 if len(active_list) > 25 else 5
            header_row = (f"<tr><td colspan='{cols}' style='padding:4px 5px 2px;font-size:14px;"
                          f"font-weight:bold;color:{_c_t};border-bottom:1px solid {_c_b};'>"
                          f"{label} ({len(active_list)})</td></tr>")
            sm_tips[key] = (f"<table style='border-collapse:collapse;font-family:monospace;font-size:12px;'>"
                            f"{header_row}" + "".join(f"<tr style='background-color:{_bg2 if (i // cols) % 2 == 0 else _bg3};'>"
            + "".join(f"<td style='padding:5px;border:1px solid {_c_b};color:{_c_d};white-space:nowrap;'>{active_list[j]}</td>"
                      for j in range(i, min(i + cols, len(active_list)))) + "</tr>" for i in range(0, len(active_list), cols)) + "</table>")

    sp_active = [p for p in S.specific_packages if not p.get("disabled") and (not session or p.get("session") == session)]
    if sp_active:
        from collections import defaultdict
        sp_groups: dict = defaultdict(list)
        for p in sp_active:
            sp_groups[p.get("session", "?")].append(_html_mod.escape(p.get("package", "")))
        cols = 5
        rows = []
        show_sess_header = len(sp_groups) > 1
        for i, sess in enumerate(sorted(sp_groups)):
            hdr_bg = _bg2 if i % 2 == 0 else _bg3
            if show_sess_header:
                rows.append(f"<tr style='background-color:{hdr_bg};'>"
                            f"<td colspan='{cols}' style='padding:3px 5px;font-size:12px;"
                            f"font-weight:bold;color:{_c_t};border-bottom:1px solid {_c_b};'>"
                            f"{_html_mod.escape(sess)}</td></tr>")
            pkgs_in_sess = sorted(sp_groups[sess])
            for j in range(0, len(pkgs_in_sess), cols):
                bg_ = _bg2 if (j // cols) % 2 == 0 else _bg3
                cells = "".join(f"<td style='padding:5px;border:1px solid {_c_b};color:{_c_d};white-space:nowrap;'>{pkgs_in_sess[k]}</td>"
                                for k in range(j, min(j + cols, len(pkgs_in_sess))))
                rows.append(f"<tr style='background-color:{bg_};'>{cells}</tr>")
        detected = _html_mod.escape(session or "current session")

        outer_header = (f"<tr><td colspan='{cols}' style='padding:4px 5px 2px;font-size:14px;"
                        f"font-weight:bold;color:{_c_t};border-bottom:1px solid {_c_b};'>"
                        f"Specific Packages for {detected} ({len(sp_active)})</td></tr>")

        sm_tips["install_specific_packages"] = (f"<table style='border-collapse:collapse;font-family:monospace;font-size:14px;'>"
                                                f"{outer_header}{''.join(rows)}</table>")

    if S.user_shell:
        header = (f"<tr><td style='padding:4px 5px 2px;font-size:14px;"
                  f"font-weight:bold;color:{_c_t};border-bottom:1px solid {_c_b};'>"
                  f"Selected Shell</td></tr>")

        shell_display = _html_mod.escape(S.user_shell)

        cell_content = (f"<td style='padding:8px 6px;border:1px solid {_c_b};white-space:nowrap;'>"
                        f"<span style='font-size:13px;color:{_c_d};'>{shell_display}</span></td>")

        sm_tips["set_user_shell"] = (f"<table style='border-collapse:collapse;font-family:monospace;'>"
                                     f"{header}<tr style='background-color:{_bg2};'>{cell_content}</tr></table>")

    return backup_tips, restore_tips, sm_tips