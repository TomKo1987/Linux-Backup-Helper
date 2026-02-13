from pathlib import Path
import json, os, tempfile, functools, pwd
from linux_distro_helper import LinuxDistroHelper
from PyQt6.QtCore import QObject, pyqtSignal, QMutex, QMutexLocker, QUuid

user = pwd.getpwuid(os.getuid()).pw_name
home_user = Path.home()

MAX_REPLACEMENT_ITERATIONS = 10 

from logging_config import setup_logger
logger = setup_logger(__name__)
MAX_MOUNT_OPTIONS = 3
SESSIONS = [
    "GNOME", "KDE", "XFCE", "LXQt", "LXDE", "Cinnamon", "Mate", "Deepin", "Budgie", "Enlightenment",
    "Hyprland", "sway", "i3", "bspwm", "openbox", "awesome", "herbstluftwm", "icewm", "fluxbox",
    "xmonad", "spectrwm", "qtile", "pekwm", "wmii", "dwm"
]
USER_SHELL = ["Bash", "Fish", "Zsh", "Elvish", "Nushell", "Powershell", "Xonsh", "Ngs"]

DETAIL_KEYS = (
    'no_backup', 'no_restore',
    'sublayout_games_1', 'sublayout_games_2',
    'sublayout_games_3', 'sublayout_games_4'
)

def _new_uuid():
    return QUuid.createUuid().toString(QUuid.StringFormat.WithoutBraces)

def _to_list_str(x):
    return [str(i) for i in (x if isinstance(x, list) else [x])]

def _normalize_newlines(item):
    return item.replace('\\n', '\n') if isinstance(item, str) else item

def _process_path_list(raw_data):
    if isinstance(raw_data, list):
        return [_normalize_newlines(i) for i in raw_data if i]
    return [_normalize_newlines(raw_data)] if raw_data else ['']


class Options(QObject):
    settings_changed = pyqtSignal()
    config_file_path = Path(home_user).joinpath(".config", "Backup Helper", "config.json")
    main_window = None
    run_mount_command_on_launch = False
    user_shell = USER_SHELL[0]
    entries_mutex = QMutex()

    all_entries, entries_sorted = [], []
    mount_options, headers, header_order, header_inactive = [], [], [], []
    header_colors, system_manager_operations = {}, []
    system_files, essential_packages, additional_packages, specific_packages = [], [], [], []
    sublayout_names = {f'sublayout_games_{i}': '' for i in range(1, 5)}
    ui_settings = {
        "backup_window_columns": 2,
        "restore_window_columns": 2,
        "settings_window_columns": 2,
        "theme": "Tokyo Night",
        "font_family": "DejaVu Sans",
        "font_size": 14
    }

    text_replacements = [
        (home_user.as_posix(), '~'),
        (f"/run/media/{user}/", ''),
        ("[1m", ""), ("[0m", "")
    ]

    system_manager_tooltips = {}

    def __init__(self, header, title, source, destination, details=None):
        super().__init__()
        self.header = str(header or "")
        self.title = str(title or "")
        self.source = source
        self.destination = destination
        self.details = dict.fromkeys(DETAIL_KEYS, False)
        self.details['unique_id'] = _new_uuid()  # type: ignore

        if details:
            self.details.update({k: bool(v) for k, v in details.items() if k in self.details})
            if details.get('unique_id'):
                self.details['unique_id'] = details['unique_id']

    @staticmethod
    def set_main_window(main_window): Options.main_window = main_window

    @staticmethod
    def mount_drives_on_startup():
        if Options.run_mount_command_on_launch:
            from drive_manager import DriveManager
            DriveManager().mount_drives_at_launch()

    @staticmethod
    def _ensure_unique_id(entry):
        if not hasattr(entry, 'details') or not isinstance(entry.details, dict):
            return False
        if not entry.details.get('unique_id'):
            entry.details['unique_id'] = _new_uuid()
        return True

    @staticmethod
    def sort_entries():
        try:
            with QMutexLocker(Options.entries_mutex):
                if not Options.all_entries:
                    Options.entries_sorted = []
                    return []

                header_order_map = {h: i for i, h in enumerate(Options.header_order)}
                sorted_entries = []
                for entry in Options.all_entries:
                    if not all(hasattr(entry, a) for a in ('header', 'title', 'details')):
                        continue
                    entry_dict = {
                        'header': entry.header,
                        'title': entry.title,
                        'source': entry.source,
                        'destination': entry.destination,
                        'unique_id': entry.details.get('unique_id', _new_uuid())
                    }
                    entry_dict.update({k: entry.details.get(k, False) for k in DETAIL_KEYS})
                    sorted_entries.append(entry_dict)

                sorted_entries.sort(key=lambda x: (header_order_map.get(x['header'], 999), x['title'].lower()))
                Options.entries_sorted = sorted_entries
                return sorted_entries
        except Exception as e:
            logger.error(f"Error in sort_entries: {e}")
            Options.entries_sorted = []
            return []

    @staticmethod
    def format_package_list(pkgs):
        pkgs = [str(p) for p in pkgs or []]
        if len(pkgs) == 1:
            return pkgs[0]
        if len(pkgs) == 2:
            return f"{pkgs[0]} and {pkgs[1]}"
        return ", ".join(pkgs[:-1]) + f" and {pkgs[-1]}"

    @staticmethod
    def get_system_manager_operation_text(distro_helper):
        pkg_install_cmd = distro_helper.pkg_install.replace("{package}", "PACKAGE")
        pkg_update_cmd = distro_helper.pkg_update

        if "pacman" in pkg_install_cmd:
            pkg_manager = "pacman"
        elif "apt" in pkg_install_cmd:
            pkg_manager = "apt"
        elif "dnf" in pkg_install_cmd:
            pkg_manager = "dnf"
        else:
            pkg_manager = "zypper"

        session = distro_helper.detect_session()

        def format_pkgs(get_func):
            return Options.format_package_list(get_func())

        printer_pkgs = format_pkgs(distro_helper.get_printer_packages)
        samba_pkgs = format_pkgs(distro_helper.get_samba_packages)
        bluetooth_pkgs = format_pkgs(distro_helper.get_bluetooth_packages)
        cron_pkgs = format_pkgs(distro_helper.get_cron_packages)
        firewall_pkgs = format_pkgs(distro_helper.get_firewall_packages)
        at_pkgs = format_pkgs(distro_helper.get_at_packages)

        cron_service = "cronie" if pkg_manager == "pacman" or distro_helper.distro_id in ["fedora", "rhel",
                                                                                          "centos"] else "cron"

        return {
            "copy_system_files": "Copy 'System Files' (Using 'sudo cp'.)",
            "update_mirrors": "Mirror update<br>(Install 'reflector' and get the 10 fastest servers in your country, or worldwide if not detected.)",
            "set_user_shell": "Change shell for current user<br>(Install corresponding package for selected shell and change it for the current user.)",
            "update_system": f"System update<br>(Using '{'yay --noconfirm' if distro_helper.package_is_installed('yay') else pkg_update_cmd}'.)",
            "install_kernel_header": f"Check kernel version and install corresponding headers ({distro_helper.get_kernel_headers_pkg()})",
            "install_essential_packages": f"Install 'Essential Packages' (Using '{pkg_install_cmd}'.)",
            "install_yay": "Install 'yay' (Necessary for 'Additional Packages'.)",
            "install_additional_packages": "Install 'Additional Packages' ('yay' needed.)",
            "install_specific_packages": f"Install 'Specific Packages' for {session}<br>(Using '{pkg_install_cmd}'.)",
            "enable_printer_support": f"Initialize printer support<br>(Install '{printer_pkgs}'.<br>Enable && start 'cups.service'.)",
            "enable_samba_network_filesharing": f"Initialize samba (Network filesharing via samba)<br>(Install '{samba_pkgs}'. Enable && start 'smb.service'.)",
            "enable_bluetooth_service": f"Initialize bluetooth<br>(Install '{bluetooth_pkgs}'. Enable && start 'bluetooth.service'.)",
            "enable_atd_service": f"Initialize atd<br>(Install '{at_pkgs}'. Enable && start 'atd.service'.)",
            "enable_cronie_service": f"Initialize {cron_service}<br>(Install '{cron_pkgs}'. Enable && start '{cron_service}.service'.)",
            "enable_firewall": f"Initialize firewall<br>(Install '{firewall_pkgs}'. Enable && start 'ufw.service' and set to 'deny all by default'.)",
            "remove_orphaned_packages": "Remove orphaned package(s)",
            "clean_cache": f"Clean cache (For '{pkg_manager}'{' and \'yay\'' if distro_helper.has_aur else ''}.)"
        }

    @staticmethod
    def _prepare_config_data():
        with QMutexLocker(Options.entries_mutex):
            Options.all_entries = [e for e in Options.all_entries if Options._ensure_unique_id(e)]
            for e in Options.all_entries:
                if e.header not in Options.header_order:
                    Options.header_order.append(e.header)

        seen_headers = list(dict.fromkeys(Options.header_order + Options.header_inactive))
        header_data = {
            h: {"inactive": h in Options.header_inactive,
                "header_color": Options.header_colors.get(h, '#ffffff')}
            for h in seen_headers
        }

        def sort_by_name(items):
            return sorted(items, key=lambda x: x.get('name', '').lower() if isinstance(x, dict) else str(x).lower())

        mount_options = sorted(
            [o for o in Options.mount_options if isinstance(o, dict) and o.get("drive_name")],
            key=lambda x: x.get("drive_name", "")
        )

        essential_packages = sort_by_name(Options.essential_packages.copy())
        additional_packages = sort_by_name(Options.additional_packages.copy())

        specific_packages = Options.specific_packages.copy()
        if isinstance(specific_packages, list) and all(isinstance(i, dict) for i in specific_packages):
            specific_packages.sort(key=lambda x: (x.get('package', '').lower(), x.get('session', '').lower()))
        else:
            specific_packages = []

        system_files = Options.system_files.copy()
        if isinstance(system_files, list) and all(isinstance(i, dict) for i in system_files):
            system_files.sort(key=lambda x: x.get('source', '').lower())
        else:
            system_files = []

        return {
            "mount_options": mount_options,
            "run_mount_command_on_launch": Options.run_mount_command_on_launch,
            "header": header_data,
            "sublayout_names": Options.sublayout_names,
            "system_manager_operations": Options.system_manager_operations,
            "system_files": system_files,
            "essential_packages": essential_packages,
            "additional_packages": additional_packages,
            "specific_packages": specific_packages,
            "ui_settings": Options.ui_settings,
            "user_shell": Options.user_shell,
            "entries": []
        }

    @staticmethod
    def save_config():
        config_dir = Path(Options.config_file_path).parent
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Error creating config directory: {e}")
            return False

        try:
            entries_data = Options._prepare_config_data()
            with QMutexLocker(Options.entries_mutex):
                for e in Options.all_entries:
                    entries_data["entries"].append({
                        "header": e.header,
                        "title": e.title,
                        "source": _to_list_str(e.source),
                        "destination": _to_list_str(e.destination),
                        "details": {**{k: e.details.get(k, False) for k in DETAIL_KEYS},
                                    "unique_id": e.details.get('unique_id', _new_uuid())}
                    })

            with tempfile.NamedTemporaryFile(dir=config_dir, delete=False, mode='w', encoding='utf-8') as temp_file:
                temp_path = temp_file.name
                json.dump(entries_data, temp_file, indent=4, ensure_ascii=False)
                temp_file.flush()
                os.fsync(temp_file.fileno())

            os.replace(temp_path, Options.config_file_path)
            Options.sort_entries()

            if Options.main_window:
                try:
                    Options.main_window.settings_changed.emit()
                except Exception as e:
                    logger.error(f"Error emitting settings_changed signal: {e}")

            return True

        except Exception as e:
            logger.error(f"Error saving config: {e}")
            temp_path = locals().get('temp_path')
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return False

    @staticmethod
    def _normalize_package_list(pkg_list):
        updated = []
        for pkg in pkg_list:
            if isinstance(pkg, str):
                updated.append({"name": pkg, "disabled": False})
            elif isinstance(pkg, dict):
                pkg.setdefault('disabled', False)
                updated.append(pkg)
        return sorted(updated, key=lambda x: x.get('name', '').lower())

    @staticmethod
    def load_config(file_path):
        if not os.path.exists(file_path):
            logger.warning(f"Config file not found: {file_path}. Creating default config.")
            Options.save_config()
            return
        try:
            with open(file_path, encoding='utf-8') as f:
                entries_data = json.load(f)
            if not isinstance(entries_data, dict):
                logger.warning("Invalid config format: expected dictionary")
                return

            header_data = entries_data.get('header', {})
            Options.header_order = list(header_data.keys())
            Options.headers = Options.header_order.copy()
            Options.header_colors = {h: d.get('header_color', '#ffffff') for h, d in header_data.items()}
            Options.header_inactive = [h for h, d in header_data.items() if d.get('inactive', False)]

            Options.sublayout_names = entries_data.get("sublayout_names", Options.sublayout_names)
            Options.system_manager_operations = entries_data.get("system_manager_operations", [])
            Options.user_shell = entries_data.get("user_shell", USER_SHELL[0])
            Options.mount_options = entries_data.get("mount_options", [])
            loaded_ui = entries_data.get("ui_settings", {})
            Options.ui_settings = {**Options.ui_settings, **loaded_ui}

            Options.run_mount_command_on_launch = entries_data.get("run_mount_command_on_launch", False)
            config_changed = not Options.mount_options and Options.run_mount_command_on_launch
            if config_changed:
                Options.run_mount_command_on_launch = False

            raw_sys_files = entries_data.get("system_files", [])
            Options.system_files = sorted(raw_sys_files,
                                          key=lambda x: x.get('source', '').lower() if isinstance(x, dict) else '')
            for f in Options.system_files:
                if isinstance(f, dict): f.setdefault('disabled', False)

            Options.essential_packages = Options._normalize_package_list(entries_data.get("essential_packages", []))
            Options.additional_packages = Options._normalize_package_list(entries_data.get("additional_packages", []))

            raw_spec_pkgs = entries_data.get("specific_packages", [])
            if isinstance(raw_spec_pkgs, list):
                Options.specific_packages = sorted(raw_spec_pkgs, key=lambda x: (x.get('package', '').lower(),
                                                                                 x.get('session',
                                                                                       '').lower()) if isinstance(x,
                                                                                                                  dict) else '')
                for pkg in Options.specific_packages:
                    if isinstance(pkg, dict): pkg.setdefault('disabled', False)
            else:
                Options.specific_packages = []

            with QMutexLocker(Options.entries_mutex):
                Options.all_entries = []
                for e_data in entries_data.get('entries', []):
                    header = e_data.get('header', '')
                    if header and header not in Options.header_order:
                        Options.header_order.append(header)
                    title = _normalize_newlines(e_data.get('title', ''))
                    src = _process_path_list(e_data.get('source', []))
                    dest = _process_path_list(e_data.get('destination', []))
                    new_entry = Options(header, title, src, dest)
                    details = e_data.get('details', {})
                    for k in DETAIL_KEYS:
                        new_entry.details[k] = details.get(k, False)
                    new_entry.details['unique_id'] = details.get('unique_id', _new_uuid())
                    Options.all_entries.append(new_entry)

            if config_changed:
                Options.save_config()

        except (IOError, json.JSONDecodeError) as e:
            error_type = "JSON decoding" if isinstance(e, json.JSONDecodeError) else "loading"
            logger.error(f"Error {error_type} entries from {file_path}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error loading config: {e}")

    @staticmethod
    def generate_tooltip():
        def apply_replacements(text, max_iter=10):
            for _ in range(max_iter):
                original = text
                text = functools.reduce(lambda t, r: t.replace(*r), Options.text_replacements, text)
                if text == original: break
            return text

        def format_html(title_item, src_text_item, dest_text_item):
            return f"""<table style='border-collapse: collapse; width: 100%; font-family: FiraCode Nerd Font Mono;'>
    <tr style='background-color: #121212;'>
    <td colspan='2' style='font-size: 13px;color: #ffc1c2;text-align: center;padding: 5px 5px;white-space: nowrap;'>{title_item}</td></tr>
    <tr style='background-color: #2a2a2a;'>
    <td colspan='2' style='font-size: 12px;color: #00fa9a;text-align: left;padding: 6px;white-space: nowrap;'>
    Source:<br><br>{src_text_item}</td></tr>
    <tr style='background-color: #1e1e1e;'>
    <td colspan='2' style='font-size: 12px;color: #00fa9a;text-align: left;padding: 6px;white-space: nowrap;'>
    Destination:<br><br>{dest_text_item}</td></tr></table>"""

        backup_tooltips, restore_tooltips = {}, {}
        with QMutexLocker(Options.entries_mutex):
            entries_snapshot = Options.entries_sorted.copy()
        for e in entries_snapshot:
            title = e["title"]
            tooltip_key = f"{title}_tooltip"
            src = e.get('source', []) if isinstance(e.get('source'), list) else [e.get('source', '')]
            dest = e.get('destination', []) if isinstance(e.get('destination'), list) else [e.get('destination', '')]
            src_text, dest_text = "<br/>".join(map(str, src)), "<br/>".join(map(str, dest))
            backup_tooltips[tooltip_key] = apply_replacements(format_html(title, src_text, dest_text))
            restore_tooltips[tooltip_key] = apply_replacements(format_html(title, dest_text, src_text))

        system_manager_tooltips = {}
        distro_helper = LinuxDistroHelper()
        op_text = Options.get_system_manager_operation_text(distro_helper)
        operation_keys = {
            "copy_system_files": "system_files",
            "install_essential_packages": "essential_packages",
            "install_additional_packages": "additional_packages",
            "install_specific_packages": "specific_packages",
            "set_user_shell": "user_shell"
        }

        label_maps = {
            "system_files": {"source": "Source:<br>", "destination": "<br>Destination:<br>"},
            "specific_packages": {"package": lambda v: f"{v}", "session": lambda v: f"<br>({v})"},
            "user_shell": lambda v: f"Selected shell: {v}"
        }

        for op, key in operation_keys.items():
            if op not in op_text: continue
            raw_items = getattr(Options, key, None)
            if not raw_items: continue
            if key in ["system_files", "essential_packages", "additional_packages", "specific_packages"]:
                items = [i for i in raw_items if
                         (isinstance(i, dict) and not i.get('disabled')) or not isinstance(i, dict)]
            else:
                items = raw_items

            column_width = 1 if key == "system_files" else 4
            mapped = label_maps.get(key)

            if key == "user_shell":
                items = [mapped(items)]
            elif mapped:
                def format_val(m, k, v):
                    val = m.get(k)
                    return val(v) if callable(val) else f"{val}{v}" if val else f"{k}: {v}"

                items = [{k: format_val(mapped, k, v) for k, v in i.items() if k != 'disabled'}
                         for i in items]
            elif key in ["essential_packages", "additional_packages"]:
                items = [i.get('name', str(i)) if isinstance(i, dict) else str(i) for i in items]

            item_format = (lambda l: "".join(l)) if key == "specific_packages" else (lambda l: "<br>".join(l))
            item_strings = [
                item_format([str(v) for v in i.values()]) if isinstance(i, dict) else str(i)
                for i in items
            ]

            rows = []
            for idx in range(0, len(item_strings), column_width):
                bg = "#2a2a2a" if (idx // column_width) % 2 == 0 else "#1e1e1e"
                cells = ''.join(
                    f'<td style="padding: 5px 5px; border: 1px solid #444; color: #00fa9a; font-family: FiraCode Nerd Font Mono;">{item}</td>'
                    for item in item_strings[idx:idx + column_width]
                )
                rows.append(f'<tr style="background-color: {bg};">{cells}</tr>')

            tooltip = (
                f"<div style='white-space: nowrap; font-size: 14px; color: #00fa9a; font-family: FiraCode Nerd Font Mono; background-color: #121212; padding: 5px 5px; border: 1px solid #444;'>"
                f"<table style='border-collapse: collapse; table-layout: auto;'>{''.join(rows)}</table></div>"
            )
            system_manager_tooltips[op] = apply_replacements(tooltip)

        Options.system_manager_tooltips = system_manager_tooltips
        return backup_tooltips, restore_tooltips, system_manager_tooltips
