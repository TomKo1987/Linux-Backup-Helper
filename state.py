from pathlib import Path
from typing import Any, Optional
import json, logging, os, pwd, re, tempfile
from logging.handlers import RotatingFileHandler

_USER         = pwd.getpwuid(os.getuid()).pw_name
_HOME         = Path.home()
_CONFIG_DIR   = _HOME / ".config" / "Backup Helper"
_PROFILES_DIR = _CONFIG_DIR / "profiles"
_LOG_DIR      = _CONFIG_DIR / "logs"
_LOG_FILE     = _LOG_DIR / "backup_helper.log"
_PROFILE_RE   = re.compile(r"^[\w\-. ]+$")

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
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
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
                S.entries.append({"header": header, "title": title, "source": src, "destination": dst, "details": e.get("details", {})})

        S.mount_options      = data.get("mount_options", [])
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
        "header": {k: {"inactive": v["inactive"], "header_color": v["color"]} for k, v in S.headers.items()},
        "system_manager_operations": S.system_manager_ops,
        "system_files":      S.system_files,
        "basic_packages":    S.basic_packages,
        "aur_packages":      S.aur_packages,
        "specific_packages": sorted(S.specific_packages, key=lambda x: (x.get("package", "")
                                                                        if isinstance(x, dict) else str(x)).lower()),
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
    return sorted(p.stem for p in _PROFILES_DIR.glob("*.json"))


def startup_load() -> bool:
    profiles = list_profiles()
    if not profiles:
        return False

    valid_paths   = [_PROFILES_DIR / f"{name}.json" for name in profiles]
    default_found = False

    for p in valid_paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("is_default"):
                if default_found:
                    data.pop("is_default")
                    _atomic_write(p, data)
                    logger.warning("Cleared duplicate is_default flag in '%s'", p.stem)
                    continue
                default_found = True
                return load_profile(p)
        except (FileNotFoundError, json.JSONDecodeError, PermissionError) as exc:
            logger.error("startup_load: %s", exc)

    for p in valid_paths:
        if load_profile(p):
            return True
    return False


_cached_session: str = ""
_session_detected: bool = False


def generate_tooltip() -> tuple[dict, dict, dict]:
    global _cached_session, _session_detected
    from themes import current_theme
    from linux_distro_helper import LinuxDistroHelper
    if not _session_detected:
        try:
            _cached_session = LinuxDistroHelper().detect_session() or ""
        except Exception as e:
            logger.warning(f"Error in LinuxDistroHelper detect_session: {e}")
            _cached_session = ""
        _session_detected = True
    session = _cached_session or None

    t = current_theme()

    _bg_title = t["bg"]
    _bg_row0  = t["bg2"]
    _bg_row1  = t["bg3"]
    _c_title  = t["accent2"]
    _c_data   = t["success"]
    _c_border = t["header_sep"]

    def _entry_html(_title: str, src_lines: list, dst_lines: list) -> str:
        src_html = "<br/>".join(map(str, src_lines))
        dst_html = "<br/>".join(map(str, dst_lines))
        return ("<table style='border-collapse:collapse;width:100%;font-family:monospace;'>"
                f"<tr style='background-color:{_bg_title};'>"
                f"<td colspan='2' style='font-size:16px;color:{_c_title};"
                f"text-align:center;padding:5px;white-space:nowrap;'>{_title}</td></tr>"
                f"<tr style='background-color:{_bg_row0};'>"
                f"<td colspan='2' style='font-size:14px;color:{_c_data};"
                f"text-align:left;padding:6px;white-space:nowrap;'>"
                f"Source:<br>{src_html}</td></tr>"
                f"<tr style='background-color:{_bg_row1};'>"
                f"<td colspan='2' style='font-size:14px;color:{_c_data};"
                f"text-align:left;padding:6px;white-space:nowrap;'>"
                f"Destination:<br>{dst_html}</td></tr>"
                "</table>")

    def _sm_table(item_strings: list, col_width: int) -> str:
        rows = []
        for idx in range(0, len(item_strings), col_width):
            bg    = _bg_row0 if (idx // col_width) % 2 == 0 else _bg_row1
            cells = "".join(f"<td style='padding:5px;border:1px solid {_c_border};"
                            f"color:{_c_data};font-family:monospace;'>{c}</td>"
                            for c in item_strings[idx:idx + col_width])
            rows.append(f"<tr style='background-color:{bg};'>{cells}</tr>")

        return apply_replacements(f"<div style='white-space:nowrap;font-size:14px;color:{_c_data};"
                                  f"font-family:monospace;background-color:{_bg_title};"
                                  f"padding:5px;border:1px solid {_c_border};'>"
                                  f"<table style='border-collapse:collapse;table-layout:auto;'>"
                                  f"{''.join(rows)}</table></div>")

    backup_tips:  dict = {}
    restore_tips: dict = {}
    for entry in S.entries:
        title = entry["title"]
        src   = entry.get("source", [])
        dst   = entry.get("destination", [])
        backup_tips[title]  = apply_replacements(_entry_html(title, src, dst))
        restore_tips[title] = apply_replacements(_entry_html(title, dst, src))

    sm_tips: dict = {}
    files = [f for f in (S.system_files or []) if isinstance(f, dict) and not f.get("disabled")]
    if files:
        sm_tips["copy_system_files"] = _sm_table([f"Src: {f.get('source', '')}<br>Dst: {f.get('destination', '')}"
                                                  for f in files], col_width=2)

    for op_key, pkg_list in (("install_basic_packages", S.basic_packages), ("install_aur_packages",   S.aur_packages)):
        active = [p["name"] for p in pkg_list if not p.get("disabled")]
        if active:
            sm_tips[op_key] = _sm_table(active, col_width=5)

    sp_active = [f"{p.get('package', '')} ({p.get('session', '?')})" for p in S.specific_packages
                 if isinstance(p, dict) and not p.get("disabled") and (not session or p.get("session") == session)]
    if sp_active:
        sm_tips["install_specific_packages"] = _sm_table(sp_active, col_width=5)

    if S.user_shell:
        sm_tips["set_user_shell"] = _sm_table([f"Shell: {S.user_shell}"], col_width=1)

    return backup_tips, restore_tips, sm_tips