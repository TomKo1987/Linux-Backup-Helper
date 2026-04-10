import json
import logging
import os
import pwd
import re
from dataclasses import dataclass, field, fields as _dc_fields
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

_USER = pwd.getpwuid(os.getuid()).pw_name
_HOME = Path.home()
_CONFIG_DIR   = _HOME / ".config" / "Backup Helper"
_PROFILES_DIR = _CONFIG_DIR / "profiles"
_LOG_DIR      = _CONFIG_DIR / "logs"
_LOG_FILE     = _LOG_DIR / "backup_helper.log"
_PROFILE_RE   = re.compile(r"^[^\s._][\w\-. ]*\S$|^[^\s._]$")

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


_ANSI_RE       = re.compile(r"\x1b\[[0-9;]*[mKHJA-Z]")
_NORM_PATHS_RE = re.compile(r"(?<=[^\s/]) (?=/|smb://|cifs://)")


def apply_replacements(text: str) -> str:
    for old, new in _path_replacements:
        text = text.replace(old, new)
    if "\x1b" in text:
        text = _ANSI_RE.sub("", text)
    return text


_invalidate_hooks: list = []

def register_invalidate_hook(hook) -> None:
    if hook not in _invalidate_hooks:
        _invalidate_hooks.append(hook)

def invalidate_tooltip_cache() -> None:
    for hook in _invalidate_hooks:
        try:
            hook()
        except Exception as exc:
            logger.warning("Tooltip invalidation hook failed: %s", exc)


@dataclass
class State:
    profile_name:       str             = ""
    entries:            list[dict]      = field(default_factory=list)
    headers:            dict[str, dict] = field(default_factory=dict)
    mount_options:      list[dict]      = field(default_factory=list)
    system_manager_ops: list[str]       = field(default_factory=list)
    system_files:       list[dict]      = field(default_factory=list)
    basic_packages:     list[dict]      = field(default_factory=list)
    aur_packages:       list[dict]      = field(default_factory=list)
    specific_packages:  list[dict]      = field(default_factory=list)
    user_shell:         str             = "bash"
    ui: dict = field(default_factory=lambda: {"theme": "Tokyo Night", "font_family": "", "font_size": 14,
                                              "backup_window_columns": 2, "restore_window_columns": 2,
                                              "settings_window_columns": 2})

    def reset_to_fresh(self) -> None:
        fresh = State()
        for f in _dc_fields(fresh):
            setattr(self, f.name, getattr(fresh, f.name))


S = State()


_KNOWN_UI_KEYS = frozenset(S.ui.keys())


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except (OSError, TypeError, ValueError):
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
    if isinstance(raw, list):
        return [p.strip() for p in (str(x) for x in raw) if p.strip()]
    s = str(raw).strip()
    if not s:
        return []
    result = []
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
    return {"header": header, "title":  title, "source": source, "destination": dest, "details": details
    if isinstance(details, dict) else {}}


def load_profile(path: Path) -> bool:
    try:
        return _load_profile_from_data(path, json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        logger.error("load_profile failed: %s", exc)
        return False


def _load_profile_from_data(path: Path, data: dict) -> bool:
    try:
        new_name = path.stem

        new_headers = {k: {"inactive": bool(v.get("inactive")), "color": (v.get("header_color", "#ffffff")
        if _valid_hex_color(v.get("header_color")) else "#ffffff")} for k, v in data.get("header", {}).items()
                       if isinstance(v, dict)}

        new_entries = [e for raw in data.get("entries", []) if (e := _parse_entry(raw)) is not None]
        new_mount = [o for o in data.get("mount_options", []) if isinstance(o, dict)]

        def _as_list(key: str) -> list:
            v = data.get(key)
            return v if isinstance(v, list) else []

        new_sm_ops    = _as_list("system_manager_operations")
        new_sys_files = _as_list("system_files")
        new_basic     = _norm_pkgs(_as_list("basic_packages"))
        new_aur       = _norm_pkgs(_as_list("aur_packages"))

        def _norm_specific(raw: list) -> list[dict]:
            result = []
            for p in raw:
                if isinstance(p, str):
                    result.append({"package": p, "session": "", "disabled": False})
                elif isinstance(p, dict):
                    result.append({**p, "disabled": p.get("disabled", False)})
            return sorted(result, key=lambda x: (x.get("session", ""), x.get("package", "")))
        new_specific = _norm_specific(_as_list("specific_packages"))

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
            new_ui.update({k: v for k, v in raw_ui.items() if k in _KNOWN_UI_KEYS})

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
    data = {"is_default": True,
            "mount_options": S.mount_options,
            "header": {k: {"inactive": v.get("inactive", False), "header_color": v.get("color", "#ffffff")}
                       for k, v in S.headers.items()},
            "system_manager_operations": S.system_manager_ops,
            "system_files":   S.system_files,
            "basic_packages": S.basic_packages,
            "aur_packages":   S.aur_packages,
            "specific_packages": sorted(
                S.specific_packages, key=lambda x: str(x.get("package", "") if isinstance(x, dict) else x).lower()),
            "ui_settings": S.ui,
            "user_shell":  S.user_shell,
            "entries": sorted(S.entries, key=lambda e: (e.get("header", "").lower(), e.get("title", "").lower()))}
    try:
        _atomic_write(path, data)
        invalidate_tooltip_cache()
        return True
    except Exception as exc:
        logger.error("save_profile failed: %s", exc)
        return False


def list_profiles() -> list[str]:
    if not _PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in _PROFILES_DIR.glob("*.json") if _PROFILE_RE.match(p.stem))


def startup_load() -> bool:
    default_path = None
    default_data = None
    other_profiles = []

    for name in list_profiles():
        p = _PROFILES_DIR / f"{name}.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("is_default"):
                if default_path is None:
                    default_path = p
                    default_data = data
                else:
                    data.pop("is_default", None)
                    try:
                        _atomic_write(p, data)
                        logger.warning("Cleared duplicate is_default flag in '%s'", p.stem)
                    except OSError as exc:
                        logger.error("startup_load: could not clear duplicate in '%s': %s", p.stem, exc)
                    other_profiles.append(p)
            else:
                other_profiles.append(p)
        except Exception as exc:
            logger.error("startup_load: Error reading '%s': %s", p.stem, exc)

    if default_path and isinstance(default_data, dict):
        if _load_profile_from_data(default_path, default_data):
            return True
        logger.warning("startup_load: default profile '%s' failed to load, trying others", default_path.stem)

    for p in other_profiles:
        if load_profile(p):
            save_profile()
            return True
    return False
