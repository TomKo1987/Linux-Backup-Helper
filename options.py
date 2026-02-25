from __future__ import annotations
from pathlib import Path
from linux_distro_helper import LinuxDistroHelper
import functools, json, os, pwd, re, shutil, tempfile, zipfile
from PyQt6.QtCore import QMutex, QMutexLocker, QObject, QUuid, pyqtSignal

from logging_config import setup_logger
logger = setup_logger(__name__)

_USER = pwd.getpwuid(os.getuid()).pw_name
_HOME = Path.home()

MAX_MOUNT_OPTIONS          = 3
MAX_REPLACEMENT_ITERATIONS = 10

SESSIONS = [
    "GNOME", "KDE", "XFCE", "LXQt", "LXDE", "Cinnamon", "Mate", "Deepin",
    "Budgie", "Enlightenment", "Hyprland", "sway", "i3", "bspwm", "openbox",
    "awesome", "herbstluftwm", "icewm", "fluxbox", "xmonad", "spectrwm",
    "qtile", "pekwm", "wmii", "dwm",
]

USER_SHELL = ["Bash", "Fish", "Zsh", "Elvish", "Nushell", "Powershell", "Xonsh", "Ngs"]

DETAIL_KEYS = (
    "no_backup", "no_restore",
    "sublayout_games_1", "sublayout_games_2",
    "sublayout_games_3", "sublayout_games_4",
)

_PROFILE_NAME_RE = re.compile(r"^[\w\-. ]+$")


def _new_uuid() -> str:
    return QUuid.createUuid().toString(QUuid.StringFormat.WithoutBraces)


def _to_str_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _normalise_newlines(value):
    return value.replace("\\n", "\n") if isinstance(value, str) else value


def _load_path_list(raw) -> list[str]:
    if isinstance(raw, list):
        return [_normalise_newlines(item) for item in raw if item]
    return [_normalise_newlines(raw)] if raw else [""]


def _atomic_json_write(path: Path, data: dict) -> None:
    with tempfile.NamedTemporaryFile(
        dir=path.parent, delete=False, mode="w", encoding="utf-8"
    ) as tmp:
        tmp_path = tmp.name
        json.dump(data, tmp, indent=4, ensure_ascii=False)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp_path, path)


class Options(QObject):
    settings_changed = pyqtSignal()

    _config_dir:  Path = _HOME / ".config" / "Backup Helper"
    profiles_dir: Path = _config_dir / "profiles"

    _active_profile: str = ""

    main_window = None
    run_mount_command_on_launch: bool = False
    user_shell: str = USER_SHELL[0]

    entries_mutex  = QMutex()
    all_entries:   list = []
    entries_sorted: list = []

    mount_options:             list = []
    headers:                   list = []
    header_order:              list = []
    header_inactive:           list = []
    header_colors:             dict = {}
    system_manager_operations: list = []
    system_files:              list = []
    basic_packages:            list = []
    aur_packages:              list = []
    specific_packages:         list = []
    sublayout_names:           dict = {f"sublayout_games_{i}": "" for i in range(1, 5)}
    system_manager_tooltips:   dict = {}

    ui_settings: dict = {
        "backup_window_columns":   2,
        "restore_window_columns":  2,
        "settings_window_columns": 2,
        "theme":       "Tokyo Night",
        "font_family": "DejaVu Sans",
        "font_size":   14,
    }

    text_replacements: list = [
        (_HOME.as_posix(), "~"),
        (f"/run/media/{_USER}/", ""),
        ("\x1b[1m", ""),
        ("\x1b[0m", ""),
        ("\x1b", ""),
    ]

    class _ProfilePathDescriptor:
        def __get__(self, obj, objtype=None) -> Path | None:
            path = Options.active_profile_path()
            if path is None:
                profiles = Options.list_profiles()
                if profiles:
                    Options._active_profile = profiles[0]
                    Options._persist_active_profile()
                    return Options.profiles_dir / f"{profiles[0]}.json"
            return path

    config_file_path = _ProfilePathDescriptor()

    def __init__(self, header: str, title: str, source, destination, details=None) -> None:
        super().__init__()
        self.header      = str(header or "")
        self.title       = str(title  or "")
        self.source      = source
        self.destination = destination

        self.details: dict = dict.fromkeys(DETAIL_KEYS, False)
        self.details["unique_id"] = _new_uuid()

        if details:
            for key in DETAIL_KEYS:
                if key in details:
                    self.details[key] = bool(details[key])
            if details.get("unique_id"):
                self.details["unique_id"] = details["unique_id"]

    @classmethod
    def active_profile_path(cls) -> Path | None:
        if not cls._active_profile:
            return None
        return cls.profiles_dir / f"{cls._active_profile}.json"

    @staticmethod
    def set_main_window(window) -> None:
        Options.main_window = window

    @staticmethod
    def _persist_active_profile() -> None:
        legacy = Options._config_dir / "last_profile"
        if legacy.exists():
            try:
                legacy.unlink()
                logger.info("Removed legacy 'last_profile' file.")
            except OSError as exc:
                logger.warning("Could not remove legacy file: %s", exc)

        if not Options._active_profile or not Options.profiles_dir.exists():
            return

        active_path = Options.profiles_dir / f"{Options._active_profile}.json"
        for profile_path in Options.profiles_dir.glob("*.json"):
            is_active = profile_path == active_path
            try:
                with open(profile_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if data.get("is_default") == is_active:
                    continue
                data["is_default"] = is_active
                _atomic_json_write(profile_path, data)
            except Exception as exc:
                logger.warning("Could not update is_default in '%s': %s", profile_path.name, exc)

    @staticmethod
    def startup_load() -> bool:
        profiles = Options.list_profiles()

        for name in profiles:
            path = Options.profiles_dir / f"{name}.json"
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if data.get("is_default", False):
                    Options._active_profile = name
                    Options.load_config(path)
                    Options._persist_active_profile()
                    logger.info("Startup: loaded default profile '%s'.", name)
                    return True
            except Exception as exc:
                logger.error("Error reading profile '%s': %s", name, exc)

        legacy = Options._config_dir / "last_profile"
        if legacy.exists():
            try:
                name = legacy.read_text(encoding="utf-8").strip()
                if name and (Options.profiles_dir / f"{name}.json").exists():
                    Options._active_profile = name
                    Options.load_config(Options.profiles_dir / f"{name}.json")
                    Options._persist_active_profile()
                    logger.info("Startup: migrated legacy profile '%s'.", name)
                    return True
            except OSError:
                pass

        if profiles:
            name = profiles[0]
            Options._active_profile = name
            Options.load_config(Options.profiles_dir / f"{name}.json")
            Options._persist_active_profile()
            logger.info("Startup: no default set — using '%s'.", name)
            return True

        logger.info("No profiles found — first run.")
        return False

    @staticmethod
    def _ensure_unique_id(entry) -> bool:
        if not hasattr(entry, "details") or not isinstance(entry.details, dict):
            return False
        if not entry.details.get("unique_id"):
            entry.details["unique_id"] = _new_uuid()
        return True

    @staticmethod
    def sort_entries() -> list:
        try:
            with QMutexLocker(Options.entries_mutex):
                if not Options.all_entries:
                    Options.entries_sorted = []
                    return []
                rank = {h: i for i, h in enumerate(Options.header_order)}
                result = []
                for entry in Options.all_entries:
                    if not all(hasattr(entry, a) for a in ("header", "title", "details")):
                        continue
                    row = {
                        "header":      entry.header,
                        "title":       entry.title,
                        "source":      entry.source,
                        "destination": entry.destination,
                        "unique_id":   entry.details.get("unique_id", _new_uuid()),
                    }
                    row.update({k: entry.details.get(k, False) for k in DETAIL_KEYS})
                    result.append(row)
                result.sort(key=lambda x: (rank.get(x["header"], 999), x["title"].lower()))
                Options.entries_sorted = result
                return result
        except Exception as exc:
            logger.error("sort_entries: %s", exc)
            Options.entries_sorted = []
            return []

    @staticmethod
    def _serialise_entries() -> list[dict]:
        result = []
        for entry in Options.all_entries:
            result.append({
                "header":      entry.header,
                "title":       entry.title,
                "source":      _to_str_list(entry.source),
                "destination": _to_str_list(entry.destination),
                "details": {
                    **{k: entry.details.get(k, False) for k in DETAIL_KEYS},
                    "unique_id": entry.details.get("unique_id", _new_uuid()),
                },
            })
        return result

    @staticmethod
    def _build_config_data() -> dict:
        with QMutexLocker(Options.entries_mutex):
            Options.all_entries = [e for e in Options.all_entries if Options._ensure_unique_id(e)]
            for entry in Options.all_entries:
                if entry.header not in Options.header_order:
                    Options.header_order.append(entry.header)

        seen = list(dict.fromkeys(Options.header_order + Options.header_inactive))
        header_data = {
            h: {
                "inactive":     h in Options.header_inactive,
                "header_color": Options.header_colors.get(h, "#ffffff"),
            }
            for h in seen
        }

        def _sort_by_name(items):
            return sorted(
                items,
                key=lambda x: (x.get("name", "") if isinstance(x, dict) else str(x)).lower(),
            )

        mount_opts = sorted(
            [o for o in Options.mount_options if isinstance(o, dict) and o.get("drive_name")],
            key=lambda x: x.get("drive_name", ""),
        )

        spec_pkgs = Options.specific_packages
        if isinstance(spec_pkgs, list) and all(isinstance(i, dict) for i in spec_pkgs):
            spec_pkgs = sorted(
                spec_pkgs,
                key=lambda x: (x.get("package", "").lower(), x.get("session", "").lower()),
            )
        else:
            spec_pkgs = []

        sys_files = Options.system_files
        if isinstance(sys_files, list) and all(isinstance(i, dict) for i in sys_files):
            sys_files = sorted(sys_files, key=lambda x: x.get("source", "").lower())
        else:
            sys_files = []

        return {
            "is_default":                False,
            "mount_options":             mount_opts,
            "run_mount_command_on_launch": Options.run_mount_command_on_launch,
            "header":                    header_data,
            "sublayout_names":           Options.sublayout_names,
            "system_manager_operations": Options.system_manager_operations,
            "system_files":              sys_files,
            "basic_packages":            _sort_by_name(Options.basic_packages),
            "aur_packages":              _sort_by_name(Options.aur_packages),
            "specific_packages":         spec_pkgs,
            "ui_settings":               Options.ui_settings,
            "user_shell":                Options.user_shell,
            "entries":                   [],
        }

    @staticmethod
    def save_config() -> bool:
        target = Options.active_profile_path()
        if not target:
            logger.error("save_config: no active profile — cannot save.")
            return False
        try:
            Options.profiles_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("save_config: cannot create profiles directory: %s", exc)
            return False
        try:
            data = Options._build_config_data()
            with QMutexLocker(Options.entries_mutex):
                data["entries"] = Options._serialise_entries()
            _atomic_json_write(target, data)
            Options.sort_entries()
            Options._persist_active_profile()
            if Options.main_window:
                try:
                    Options.main_window.settings_changed.emit()
                except Exception as exc:
                    logger.error("save_config: error emitting settings_changed: %s", exc)
            return True
        except Exception as exc:
            logger.error("save_config: %s", exc)
            return False

    @staticmethod
    def load_config(file_path=None) -> None:
        if file_path is None:
            file_path = Options.active_profile_path()
        if file_path is None:
            logger.warning("load_config: no active profile — nothing to load.")
            return

        path = Path(file_path)
        if not path.exists():
            logger.warning("load_config: file not found: %s", path)
            return

        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            if not isinstance(data, dict):
                logger.warning("load_config: unexpected format in %s", path)
                return

            header_data = data.get("header", {})
            Options.header_order    = list(header_data.keys())
            Options.headers         = Options.header_order.copy()
            Options.header_colors   = {h: d.get("header_color", "#ffffff") for h, d in header_data.items()}
            Options.header_inactive = [h for h, d in header_data.items() if d.get("inactive", False)]

            Options.sublayout_names           = data.get("sublayout_names", Options.sublayout_names)
            Options.system_manager_operations = data.get("system_manager_operations", [])
            Options.user_shell                = data.get("user_shell", USER_SHELL[0])
            Options.mount_options             = data.get("mount_options", [])
            Options.run_mount_command_on_launch = data.get("run_mount_command_on_launch", False)

            _save_needed = False
            if not Options.mount_options and Options.run_mount_command_on_launch:
                Options.run_mount_command_on_launch = False
                _save_needed = True

            Options.ui_settings = {**Options.ui_settings, **data.get("ui_settings", {})}

            raw_sys_files = data.get("system_files", [])
            Options.system_files = sorted(
                [f for f in raw_sys_files if isinstance(f, dict)],
                key=lambda x: x.get("source", "").lower(),
            )
            for f in Options.system_files:
                f.setdefault("disabled", False)

            Options.basic_packages = Options._normalise_packages(data.get("basic_packages", []))
            Options.aur_packages   = Options._normalise_packages(data.get("aur_packages", []))

            raw_spec = data.get("specific_packages", [])
            Options.specific_packages = sorted(
                [p for p in raw_spec if isinstance(p, dict)],
                key=lambda x: (x.get("package", "").lower(), x.get("session", "").lower()),
            )
            for p in Options.specific_packages:
                p.setdefault("disabled", False)

            with QMutexLocker(Options.entries_mutex):
                Options.all_entries = []
                for edata in data.get("entries", []):
                    header = edata.get("header", "")
                    if header and header not in Options.header_order:
                        Options.header_order.append(header)
                    entry = Options(
                        header,
                        _normalise_newlines(edata.get("title", "")),
                        _load_path_list(edata.get("source", [])),
                        _load_path_list(edata.get("destination", [])),
                    )
                    details = edata.get("details", {})
                    for k in DETAIL_KEYS:
                        entry.details[k] = details.get(k, False)
                    entry.details["unique_id"] = details.get("unique_id", _new_uuid())
                    Options.all_entries.append(entry)

            if _save_needed and Options._active_profile:
                Options.save_config()

        except (json.JSONDecodeError, IOError) as exc:
            logger.error("load_config: error reading '%s': %s", path, exc)
        except Exception as exc:
            logger.error("load_config: unexpected error: %s", exc)

    @staticmethod
    def _normalise_packages(raw: list) -> list[dict]:
        result = []
        for item in raw:
            if isinstance(item, str):
                result.append({"name": item, "disabled": False})
            elif isinstance(item, dict):
                item.setdefault("disabled", False)
                result.append(item)
        return sorted(result, key=lambda x: x.get("name", "").lower())

    @staticmethod
    def list_profiles() -> list[str]:
        if not Options.profiles_dir.exists():
            return []
        return sorted(p.stem for p in Options.profiles_dir.glob("*.json") if p.is_file())

    @staticmethod
    def get_active_profile() -> str:
        return Options._active_profile

    @staticmethod
    def get_default_profile() -> str:
        if not Options.profiles_dir.exists():
            return Options._active_profile
        for p in Options.profiles_dir.glob("*.json"):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if data.get("is_default", False):
                    return p.stem
            except Exception as exc:
                logger.error("get_default_profile: '%s': %s", p.name, exc)
        return Options._active_profile

    @staticmethod
    def save_profile(name: str) -> bool:
        if not name or not _PROFILE_NAME_RE.match(name):
            logger.error("save_profile: invalid name '%s'.", name)
            return False
        try:
            Options.profiles_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("save_profile: cannot create profiles dir: %s", exc)
            return False
        target = Options.profiles_dir / f"{name}.json"
        try:
            data = Options._build_config_data()
            data["is_default"] = (name == Options._active_profile)
            with QMutexLocker(Options.entries_mutex):
                data["entries"] = Options._serialise_entries()
            _atomic_json_write(target, data)
            logger.info("Profile '%s' saved.", name)
            return True
        except Exception as exc:
            logger.error("save_profile: '%s': %s", name, exc)
            return False

    @staticmethod
    def load_profile(name: str) -> bool:
        path = Options.profiles_dir / f"{name}.json"
        if not path.exists():
            logger.error("load_profile: '%s' not found.", name)
            return False
        try:
            Options._active_profile = name
            Options._persist_active_profile()
            Options.load_config(path)
            logger.info("Switched to profile '%s'.", name)
            if Options.main_window:
                try:
                    Options.main_window.settings_changed.emit()
                except Exception as exc:
                    logger.error("load_profile: error emitting settings_changed: %s", exc)
            return True
        except Exception as exc:
            logger.error("load_profile: '%s': %s", name, exc)
            return False

    @staticmethod
    def delete_profile(name: str) -> bool:
        if name == Options._active_profile:
            logger.error("delete_profile: cannot delete the active profile.")
            return False
        try:
            (Options.profiles_dir / f"{name}.json").unlink()
            logger.info("Profile '%s' deleted.", name)
            return True
        except OSError as exc:
            logger.error("delete_profile: '%s': %s", name, exc)
            return False

    @staticmethod
    def set_default_profile(name: str) -> bool:
        target = Options.profiles_dir / f"{name}.json"
        if not target.exists():
            logger.error("set_default_profile: '%s' not found.", name)
            return False
        for path in Options.profiles_dir.glob("*.json"):
            is_default = path == target
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if data.get("is_default") == is_default:
                    continue
                data["is_default"] = is_default
                _atomic_json_write(path, data)
            except Exception as exc:
                logger.warning("set_default_profile: '%s': %s", path.name, exc)
        logger.info("Default profile set to '%s'.", name)
        return True

    @staticmethod
    def export_all_profiles(dest_zip: str) -> bool:
        if not Options.profiles_dir.exists() or not any(Options.profiles_dir.glob("*.json")):
            logger.warning("export_all_profiles: no profiles to export.")
            return False
        try:
            Options.save_config()
            with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(Options.profiles_dir.glob("*.json")):
                    zf.write(p, p.name)
            logger.info("Exported all profiles to '%s'.", dest_zip)
            return True
        except Exception as exc:
            logger.error("export_all_profiles: %s", exc)
            return False

    @staticmethod
    def import_profiles_from_zip(src_zip: str, overwrite: bool = False) -> tuple[list, list]:
        try:
            Options.profiles_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("import_profiles_from_zip: cannot create dir: %s", exc)
            return [], []

        imported, skipped = [], []
        try:
            with zipfile.ZipFile(src_zip, "r") as zf:
                for member in zf.namelist():
                    if not member.endswith(".json"):
                        continue
                    name = Path(member).stem
                    if not _PROFILE_NAME_RE.match(name):
                        skipped.append(member)
                        continue
                    dest = Options.profiles_dir / f"{name}.json"
                    if dest.exists() and not overwrite:
                        skipped.append(name)
                        continue
                    try:
                        raw  = zf.read(member)
                        data = json.loads(raw)
                        if not isinstance(data, dict):
                            raise ValueError("not a JSON object")
                    except Exception as exc:
                        logger.warning("Skipping invalid JSON '%s': %s", member, exc)
                        skipped.append(name)
                        continue
                    dest.write_bytes(raw)
                    imported.append(name)
            logger.info("Imported %d profile(s).", len(imported))
            return imported, skipped
        except Exception as exc:
            logger.error("import_profiles_from_zip: %s", exc)
            return [], []

    @staticmethod
    def import_single_profile(src_json: str, name: str) -> bool:
        if not name or not _PROFILE_NAME_RE.match(name):
            logger.error("import_single_profile: invalid name '%s'.", name)
            return False
        try:
            with open(src_json, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                logger.error("import_single_profile: not a valid profile JSON.")
                return False
        except Exception as exc:
            logger.error("import_single_profile: cannot read file: %s", exc)
            return False
        try:
            Options.profiles_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_json, Options.profiles_dir / f"{name}.json")
            logger.info("Profile '%s' imported.", name)
            return True
        except Exception as exc:
            logger.error("import_single_profile: %s", exc)
            return False

    @staticmethod
    def generate_tooltip() -> tuple[dict, dict, dict]:
        def apply_replacements(text: str) -> str:
            for _ in range(MAX_REPLACEMENT_ITERATIONS):
                original = text
                text = functools.reduce(lambda t, r: t.replace(*r), Options.text_replacements, text)
                if text == original:
                    break
            return text

        def entry_tooltip_html(_title: str, src_lines: list, dst_lines: list) -> str:
            src_html = "<br/>".join(map(str, src_lines))
            dst_html = "<br/>".join(map(str, dst_lines))
            return (
                "<table style='border-collapse:collapse;width:100%;"
                "font-family:FiraCode Nerd Font Mono;'>"
                f"<tr style='background-color:#121212;'>"
                f"<td colspan='2' style='font-size:13px;color:#ffc1c2;"
                f"text-align:center;padding:5px;white-space:nowrap;'>{_title}</td></tr>"
                f"<tr style='background-color:#2a2a2a;'>"
                f"<td colspan='2' style='font-size:12px;color:#00fa9a;"
                f"text-align:left;padding:6px;white-space:nowrap;'>"
                f"Source:<br><br>{src_html}</td></tr>"
                f"<tr style='background-color:#1e1e1e;'>"
                f"<td colspan='2' style='font-size:12px;color:#00fa9a;"
                f"text-align:left;padding:6px;white-space:nowrap;'>"
                f"Destination:<br><br>{dst_html}</td></tr>"
                "</table>"
            )

        backup_tips:  dict = {}
        restore_tips: dict = {}

        with QMutexLocker(Options.entries_mutex):
            snapshot = Options.entries_sorted.copy()

        for entry in snapshot:
            title = entry["title"]
            key   = f"{title}_tooltip"
            src   = entry.get("source", [])
            dst   = entry.get("destination", [])
            src   = src if isinstance(src, list) else [src]
            dst   = dst if isinstance(dst, list) else [dst]
            backup_tips[key]  = apply_replacements(entry_tooltip_html(title, src, dst))
            restore_tips[key] = apply_replacements(entry_tooltip_html(title, dst, src))

        sm_tips: dict = {}
        distro = LinuxDistroHelper()
        op_texts = Options.get_system_manager_operation_text(distro)

        op_data_keys = {
            "copy_system_files":          "system_files",
            "install_basic_packages":     "basic_packages",
            "install_aur_packages":       "aur_packages",
            "install_specific_packages":  "specific_packages",
            "set_user_shell":             "user_shell",
        }
        label_maps = {
            "system_files": {"source": "Source:<br>", "destination": "<br>Destination:<br>"},
            "specific_packages": {
                "package": lambda v: str(v),
                "session": lambda v: f"<br>({v})",
            },
            "user_shell": lambda v: f"Selected shell: {v}",
        }

        for op, data_key in op_data_keys.items():
            if op not in op_texts:
                continue
            raw = getattr(Options, data_key, None)
            if not raw:
                continue

            if data_key in ("system_files", "basic_packages", "aur_packages", "specific_packages"):
                items = [i for i in raw if not (isinstance(i, dict) and i.get("disabled"))]
            else:
                items = raw

            col_width = 1 if data_key == "system_files" else 4
            mapped    = label_maps.get(data_key)

            if data_key == "user_shell":
                item_strings = [mapped(items)]
            elif data_key in ("basic_packages", "aur_packages"):
                item_strings = [i.get("name", str(i)) if isinstance(i, dict) else str(i) for i in items]
            elif mapped and data_key in ("system_files", "specific_packages"):
                formatted = []
                for item in items:
                    if isinstance(item, dict):
                        parts = []
                        for field_key, field_val in item.items():
                            if field_key == "disabled":
                                continue
                            m = mapped.get(field_key)
                            parts.append(
                                m(field_val) if callable(m)
                                else f"{m}{field_val}" if m
                                else f"{field_key}: {field_val}"
                            )
                        formatted.append("".join(parts))
                    else:
                        formatted.append(str(item))
                item_strings = formatted
            else:
                item_strings = [str(i) for i in items]

            rows = []
            for idx in range(0, len(item_strings), col_width):
                bg    = "#2a2a2a" if (idx // col_width) % 2 == 0 else "#1e1e1e"
                cells = "".join(
                    f"<td style='padding:5px;border:1px solid #444;"
                    f"color:#00fa9a;font-family:FiraCode Nerd Font Mono;'>{cell}</td>"
                    for cell in item_strings[idx:idx + col_width]
                )
                rows.append(f"<tr style='background-color:{bg};'>{cells}</tr>")

            sm_tips[op] = apply_replacements(
                "<div style='white-space:nowrap;font-size:14px;color:#00fa9a;"
                "font-family:FiraCode Nerd Font Mono;background-color:#121212;"
                "padding:5px;border:1px solid #444;'>"
                f"<table style='border-collapse:collapse;table-layout:auto;'>{''.join(rows)}</table></div>"
            )

        Options.system_manager_tooltips = sm_tips
        return backup_tips, restore_tips, sm_tips

    @staticmethod
    def format_package_list(pkgs: list) -> str:
        if not pkgs:
            return ""
        pkgs = [str(p) for p in pkgs]
        if len(pkgs) == 1:
            return pkgs[0]
        if len(pkgs) == 2:
            return f"{pkgs[0]} and {pkgs[1]}"
        return ", ".join(pkgs[:-1]) + f" and {pkgs[-1]}"

    @staticmethod
    def get_system_manager_operation_text(distro: LinuxDistroHelper) -> dict:
        install_cmd = distro.pkg_install.replace("{package}", "PACKAGE")
        update_cmd  = distro.pkg_update
        pkg_mgr = (
            "pacman" if "pacman" in install_cmd else
            "apt"    if "apt"    in install_cmd else
            "dnf"    if "dnf"    in install_cmd else
            "zypper"
        )

        has_yay  = distro.package_is_installed("yay")
        session  = distro.detect_session() or "Unknown"

        def pkglist(fn): return Options.format_package_list(fn())

        cron_svc = (
            "cronie"
            if pkg_mgr == "pacman" or distro.distro_id in ("fedora", "rhel", "centos")
            else "cron"
        )

        return {
            "copy_system_files":
                "Copy 'System Files' (using 'sudo cp'.)",
            "update_mirrors":
                "Mirror update<br>(Installs 'reflector' and fetches the 10 fastest servers in your country, "
                "or worldwide if location is not detected.)",
            "set_user_shell":
                "Change shell for current user<br>"
                "(Installs the package for the selected shell and sets it as the default.)",
            "update_system":
                f"System update<br>(Using '{'yay --noconfirm' if has_yay else update_cmd}'.)",
            "install_kernel_header":
                f"Check kernel version and install corresponding headers ({distro.get_kernel_headers_pkg()})",
            "install_basic_packages":
                f"Install 'Basic Packages' (using '{install_cmd}'.)",
            "install_yay":
                "Install 'yay' (required for 'AUR Packages'.)",
            "install_aur_packages":
                "Install 'AUR Packages' ('yay' required.)",
            "install_specific_packages":
                f"Install 'Specific Packages' for {session}<br>(using '{install_cmd}'.)",
            "enable_printer_support":
                f"Initialise printer support<br>"
                f"(Install '{pkglist(distro.get_printer_packages)}'. Enable & start 'cups.service'.)",
            "enable_ssh_service":
                f"Initialise SSH server<br>"
                f"(Install '{pkglist(distro.get_ssh_packages)}'. "
                f"Enable & start '{distro.get_ssh_service_name()}.service'.)",
            "enable_samba_network_filesharing":
                f"Initialise Samba (network file-sharing)<br>"
                f"(Install '{pkglist(distro.get_samba_packages)}'. Enable & start 'smb.service'.)",
            "enable_bluetooth_service":
                f"Initialise Bluetooth<br>"
                f"(Install '{pkglist(distro.get_bluetooth_packages)}'. Enable & start 'bluetooth.service'.)",
            "enable_atd_service":
                f"Initialise atd<br>"
                f"(Install '{pkglist(distro.get_at_packages)}'. Enable & start 'atd.service'.)",
            "enable_cronie_service":
                f"Initialise {cron_svc}<br>"
                f"(Install '{pkglist(distro.get_cron_packages)}'. Enable & start '{cron_svc}.service'.)",
            "enable_firewall":
                f"Initialise firewall<br>"
                f"(Install '{pkglist(distro.get_firewall_packages)}'. "
                f"Enable & start 'ufw.service', set to 'deny all by default'.)",
            "install_flatpak":
                f"Install Flatpak<br>"
                f"(Install '{pkglist(distro.get_flatpak_packages)}' and add Flathub remote.)",
            "install_snap":
                f"Install Snap<br>"
                f"(Install '{pkglist(distro.get_snap_packages)}' and enable snapd.service.)",
            "remove_orphaned_packages":
                "Remove orphaned package(s).",
            "clean_cache":
                "Clean cache (for '{mgr}'{extra}.)".format(
                    mgr=pkg_mgr,
                    extra=" and 'yay'" if distro.has_aur else "",
                ),
        }
