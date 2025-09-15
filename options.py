from pathlib import Path
from linux_distro_helper import LinuxDistroHelper
import json, os, tempfile, functools, pwd, logging.handlers
from PyQt6.QtCore import QObject, pyqtSignal, QMutex, QMutexLocker, QUuid

user = pwd.getpwuid(os.getuid()).pw_name
home_user = os.getenv("HOME")

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

MAX_MOUNT_OPTIONS = 3
SESSIONS = ["GNOME", "KDE", "XFCE", "LXQt", "LXDE", "Cinnamon", "Mate", "Deepin", "Budgie", "Enlightenment",
            "Hyprland", "sway", "i3", "bspwm", "openbox", "awesome", "herbstluftwm", "icewm", "fluxbox",
            "xmonad", "spectrwm", "qtile", "pekwm", "wmii", "dwm"]
USER_SHELL = ["Bash", "Fish", "Zsh", "Elvish", "Nushell", "Powershell", "Xonsh", "Ngs"]


class Options(QObject):
    settings_changed = pyqtSignal()
    config_file_path = Path(home_user).joinpath(".config") / 'Backup Helper' / 'config.json'
    main_window = None
    run_mount_command_on_launch = False
    user_shell = USER_SHELL[0]
    _entries_mutex = QMutex()
    all_entries = []
    entries_sorted = []
    mount_options = []
    headers = []
    header_order = []
    header_inactive = []
    header_colors = {}
    system_manager_operations = []
    system_files = []
    essential_packages = []
    additional_packages = []
    specific_packages = []
    sublayout_names = {'sublayout_games_1': '', 'sublayout_games_2': '', 'sublayout_games_3': '', 'sublayout_games_4': ''}
    ui_settings = {
        "backup_window_columns": 2,
        "restore_window_columns": 2,
        "settings_window_columns": 2,
        "theme": "Tokyo Night",
        "font_family": "DejaVu Sans",
        "font_size": 14
    }
    text_replacements = [(home_user, '~'), (f"/run/media/{user}/", ''), ("[1m", ""), ("[0m", ""), ("", "")]
    text_replacements.extend([(env, env) for env in SESSIONS])
    system_manager_tooltips = {}

    def __init__(self, header, title, source, destination, details=None):
        super().__init__()
        self.header = str(header) if header else ""
        self.title = str(title) if title else ""
        self.source = source
        self.destination = destination
        if details is None:
            details = {}
        self.details = {
            'no_backup': bool(details.get('no_backup', False)),
            'no_restore': bool(details.get('no_restore', False)),
            'sublayout_games_1': bool(details.get('sublayout_games_1', False)),
            'sublayout_games_2': bool(details.get('sublayout_games_2', False)),
            'sublayout_games_3': bool(details.get('sublayout_games_3', False)),
            'sublayout_games_4': bool(details.get('sublayout_games_4', False)),
            'unique_id': details.get('unique_id') or QUuid.createUuid().toString(QUuid.StringFormat.WithoutBraces)
        }

    @staticmethod
    def set_main_window(main_window):
        Options.main_window = main_window

    @staticmethod
    def mount_drives_on_startup():
        if Options.run_mount_command_on_launch:
            from drive_manager import DriveManager
            DriveManager().mount_drives_at_launch()

    @staticmethod
    def sort_entries():
        try:
            with QMutexLocker(Options._entries_mutex):
                if not Options.all_entries:
                    Options.entries_sorted = []
                    return []

                header_order_map = {h: i for i, h in enumerate(Options.header_order)}
                sorted_entries = sorted([
                    {
                        'header': entry.header,
                        'title': entry.title,
                        'source': entry.source,
                        'destination': entry.destination,
                        **{k: entry.details.get(k, False) for k in
                           ('no_backup', 'no_restore', 'sublayout_games_1', 'sublayout_games_2', 'sublayout_games_3',
                            'sublayout_games_4')},
                        'unique_id': entry.details.get('unique_id', QUuid.createUuid().toString(QUuid.StringFormat.WithoutBraces))
                    }
                    for entry in Options.all_entries
                    if hasattr(entry, 'header') and hasattr(entry, 'title') and hasattr(entry, 'details')
                ], key=lambda x: (header_order_map.get(x['header'], 999), x['title'].lower()))

                Options.entries_sorted = sorted_entries
                return sorted_entries
        except Exception as e:
            logger.error(f"Error in sort_entries: {e}")
            with QMutexLocker(Options._entries_mutex):
                Options.entries_sorted = []
            return []

    @staticmethod
    def delete_entry(entry):
        try:
            with QMutexLocker(Options._entries_mutex):
                Options.all_entries.remove(entry)
            Options.save_config()
        except ValueError:
            pass

    @staticmethod
    def get_system_manager_operation_text(distro_helper):
        pkg_install_cmd = distro_helper.pkg_install.replace("{package}", "PACKAGE")
        pkg_update_cmd = distro_helper.pkg_update
        pkg_manager = "pacman" if "pacman" in pkg_install_cmd else (
            "apt" if "apt" in pkg_install_cmd else ("dnf" if "dnf" in pkg_install_cmd else "zypper"))

        session = distro_helper.detect_session()
        printer_pkgs = Options.format_package_list(distro_helper.get_printer_packages())
        samba_pkgs = Options.format_package_list(distro_helper.get_samba_packages())
        bluetooth_pkgs = Options.format_package_list(distro_helper.get_bluetooth_packages())
        cron_pkgs = Options.format_package_list(distro_helper.get_cron_packages())
        firewall_pkgs = Options.format_package_list(distro_helper.get_firewall_packages())
        at_pkgs = Options.format_package_list(distro_helper.get_at_packages())

        cron_service = "cronie" if pkg_manager == "pacman" or distro_helper.distro_id in ["fedora", "rhel", "centos"] else "cron"

        return {
            "copy_system_files": "Copy 'System Files' (Using 'sudo cp'.)",
            "update_mirrors": f"Mirror update<br>(Install 'reflector' and get the 10 fastest servers in your country, or worldwide if not detected.)",
            "set_user_shell": "Change shell for current user<br>(Install corresponding package for selected shell and change it for the current user.)",
            "update_system": f"System update<br>(Using 'yay --noconfirm'.)" if distro_helper.package_is_installed('yay') else f"(Using '{pkg_update_cmd}'.)",
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
    def format_package_list(pkgs):
        if not pkgs:
            return ""

        pkgs_quoted = [str(pkg) for pkg in pkgs]

        if len(pkgs_quoted) == 1:
            return pkgs_quoted[0]
        elif len(pkgs_quoted) == 2:
            return f"{pkgs_quoted[0]} and {pkgs_quoted[1]}"
        else:
            return ", ".join(pkgs_quoted[:-1]) + f" and {pkgs_quoted[-1]}"

    @staticmethod
    def save_config():
        config_dir = Path(Options.config_file_path).parent

        try:
            config_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"Error creating config directory: {e}")
            return False

        try:
            with QMutexLocker(Options._entries_mutex):
                for entry in Options.all_entries:
                    if not hasattr(entry, 'details') or not isinstance(entry.details, dict):
                        logger.warning(f"Invalid entry detected: {entry}")
                        continue

                    if not entry.details.get('unique_id'):
                        entry.details['unique_id'] = QUuid.createUuid().toString(QUuid.StringFormat.WithoutBraces)

                    if entry.header not in Options.header_order:
                        Options.header_order.append(entry.header)

            header_data = {header: {"inactive": header in Options.header_inactive,
                                    "header_color": Options.header_colors.get(header, '#ffffff')}
                           for header in Options.header_order + Options.header_inactive}

            mount_options = [opt for opt in Options.mount_options if isinstance(opt, dict) and opt.get("drive_name")]
            mount_options.sort(key=lambda x: x.get("drive_name", ""))

            # Sort essential packages alphabetically by name
            essential_packages = Options.essential_packages.copy() if Options.essential_packages else []
            if essential_packages:
                essential_packages.sort(
                    key=lambda x: x.get('name', '').lower() if isinstance(x, dict) else str(x).lower())

            # Sort additional packages alphabetically by name
            additional_packages = Options.additional_packages.copy() if Options.additional_packages else []
            if additional_packages:
                additional_packages.sort(
                    key=lambda x: x.get('name', '').lower() if isinstance(x, dict) else str(x).lower())

            # Sort specific packages alphabetically by package name, then by session
            specific_packages = Options.specific_packages.copy() if Options.specific_packages else []
            if (isinstance(specific_packages, list) and
                    all(isinstance(item, dict) for item in specific_packages)):
                specific_packages.sort(key=lambda x: (x.get('package', '').lower(), x.get('session', '').lower()))
            else:
                specific_packages = []

            # Sort system files alphabetically by source path
            system_files = Options.system_files.copy() if Options.system_files else []
            if (isinstance(system_files, list) and
                    all(isinstance(item, dict) for item in system_files)):
                system_files.sort(key=lambda x: x.get('source', '').lower())
            else:
                system_files = []

            entries_data = {
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

            for e in Options.all_entries:
                entry = {
                    "header": e.header,
                    "title": e.title,
                    "source": [str(src) for src in (e.source if isinstance(e.source, list) else [e.source])],
                    "destination": [str(dest) for dest in
                                    (e.destination if isinstance(e.destination, list) else [e.destination])],
                    "details": {**{k: e.details.get(k, False) for k in
                                   ('no_backup', 'no_restore', 'sublayout_games_1', 'sublayout_games_2',
                                    'sublayout_games_3', 'sublayout_games_4')},
                                "unique_id": e.details.get('unique_id', QUuid.createUuid().toString(
                                    QUuid.StringFormat.WithoutBraces))}
                }
                entries_data["entries"].append(entry)

            try:
                with tempfile.NamedTemporaryFile(dir=config_dir, delete=False, mode='w', encoding='utf-8') as temp_file:
                    temp_path = temp_file.name
                    json_data = json.dumps(entries_data, indent=4, ensure_ascii=False)
                    temp_file.write(json_data)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())

                os.replace(temp_path, Options.config_file_path)
                Options.sort_entries()

                if Options.main_window is not None:
                    try:
                        Options.main_window.settings_changed.emit()
                    except Exception as e:
                        logger.error(f"Error emitting settings_changed signal: {e}")
                return True
            except Exception as e:
                logger.error(f"Error writing or replacing config file: {e}")
                try:
                    if 'temp_path' in locals() and os.path.exists(temp_path):
                        os.unlink(temp_path)
                except OSError:
                    pass
                return False
        except Exception as e:
            logger.error(f"Unexpected error while saving config: {e}")
        return False

    @staticmethod
    def load_config(file_path):
        try:
            if not os.path.exists(file_path):
                logger.warning(f"Config file not found: {file_path}")
                return

            with open(file_path, encoding='utf-8') as file:
                entries_data = json.load(file)

            if not isinstance(entries_data, dict):
                logger.warning("Invalid config format: expected dictionary")
                return

            header_data = entries_data.get('header', {})
            Options.headers = [h for h in Options.header_order]
            Options.header_order = list(header_data.keys())
            Options.header_colors = {}
            Options.header_inactive = []

            for header, data in header_data.items():
                Options.header_colors[header] = data.get('header_color', '#ffffff')
                if data.get('inactive', False):
                    Options.header_inactive.append(header)

            Options.sublayout_names = entries_data.get("sublayout_names", Options.sublayout_names)
            Options.system_manager_operations = entries_data.get("system_manager_operations", [])

            # Load and sort system files alphabetically by source
            system_files_raw = entries_data.get("system_files")
            if isinstance(system_files_raw, list):
                Options.system_files = sorted(system_files_raw,
                                              key=lambda x: x.get('source', '').lower() if isinstance(x, dict) else '')
                # Ensure disabled field exists for each system file
                for file_item in Options.system_files:
                    if isinstance(file_item, dict) and 'disabled' not in file_item:
                        file_item['disabled'] = False
            else:
                Options.system_files = []

            # Load essential and additional packages
            Options.essential_packages = entries_data.get("essential_packages", [])
            Options.additional_packages = entries_data.get("additional_packages", [])

            # Process and sort essential and additional packages alphabetically
            for pkg_list_name in ["essential_packages", "additional_packages"]:
                pkg_list = getattr(Options, pkg_list_name, [])
                updated_list = []
                for pkg in pkg_list:
                    if isinstance(pkg, str):
                        updated_list.append({"name": pkg, "disabled": False})
                    elif isinstance(pkg, dict):
                        if 'disabled' not in pkg:
                            pkg['disabled'] = False
                        updated_list.append(pkg)
                # Sort alphabetically by name (case-insensitive)
                updated_list.sort(key=lambda x: x.get('name', '').lower() if isinstance(x, dict) else str(x).lower())
                setattr(Options, pkg_list_name, updated_list)

            # Load and sort specific packages alphabetically by package name, then by session
            specific_packages_raw = entries_data.get("specific_packages")
            if isinstance(specific_packages_raw, list):
                Options.specific_packages = sorted(specific_packages_raw,
                                                   key=lambda x: (x.get('package', '').lower(),
                                                                  x.get('session', '').lower()) if isinstance(x,
                                                                                                              dict) else '')
                for pkg_item in Options.specific_packages:
                    if isinstance(pkg_item, dict) and 'disabled' not in pkg_item:
                        pkg_item['disabled'] = False
            else:
                Options.specific_packages = []

            Options.user_shell = entries_data.get("user_shell", USER_SHELL[0])
            Options.mount_options = entries_data.get("mount_options", [])

            config_changed = False
            if not Options.mount_options and entries_data.get("run_mount_command_on_launch", False):
                config_changed = True
            else:
                Options.run_mount_command_on_launch = entries_data.get("run_mount_command_on_launch", False)

            Options.ui_settings = entries_data.get("ui_settings", Options.ui_settings)

            with QMutexLocker(Options._entries_mutex):
                Options.all_entries = []

                for entry_data in entries_data.get('entries', []):
                    header = entry_data.get('header', '')
                    if header and header not in Options.header_order:
                        Options.header_order.append(header)

                    def normalize_newlines(item):
                        return item.replace('\\n', '\n') if isinstance(item, str) else item

                    title = normalize_newlines(entry_data.get('title', ''))
                    source_raw = entry_data.get('source', [])
                    destination_raw = entry_data.get('destination', [])
                    source_list = source_raw if isinstance(source_raw, list) else [source_raw] if source_raw else ['']
                    source = [normalize_newlines(src) for src in source_list if src]
                    destination_list = destination_raw if isinstance(destination_raw, list) else [
                        destination_raw] if destination_raw else ['']
                    destination = [normalize_newlines(dest) for dest in destination_list if dest]

                    new_entry = Options(header, title, source, destination)
                    details = entry_data.get('details', {})
                    for key in ('no_backup', 'no_restore', 'sublayout_games_1', 'sublayout_games_2',
                                'sublayout_games_3',
                                'sublayout_games_4'):
                        new_entry.details[key] = details.get(key, False)
                    new_entry.details['unique_id'] = details.get('unique_id', QUuid.createUuid().toString(
                        QUuid.StringFormat.WithoutBraces))
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
        text_replacements = Options.text_replacements

        def format_html(entry_title, entry_source_text, entry_dest_text):
            template = """<table style='border-collapse: collapse; width: 100%; font-family: FiraCode Nerd Font Mono;'>
                    <tr style='background-color: #121212;'><td colspan='2' style='font-size: 13px; 
                    color: #ffc1c2; text-align: center; padding: 5px 5px; white-space: nowrap;'>{title}</td>
                    </tr><tr style='background-color: #2a2a2a;'><td colspan='2' style='font-size: 12px; 
                    color: #00fa9a; text-align: left; padding: 6px; font-family: FiraCode Nerd Font Mono; white-space: nowrap;'>
                    Source:<br><br>{source}</td></tr><tr style='background-color: #1e1e1e;'><td colspan='2' style=
                    'font-size: 12px; color: #00fa9a; text-align: left; padding: 6px; font-family: 
                    FiraCode Nerd Font Mono; white-space: nowrap;'>Destination:<br><br>{dest}</td></tr></table>"""
            return template.format(title=entry_title, source=entry_source_text, dest=entry_dest_text)

        def apply_replacements(text, max_iterations=10):
            for _ in range(max_iterations):
                original = text
                text = functools.reduce(lambda t, repl: t.replace(*repl), text_replacements, text)
                if text == original:
                    break
            return text

        backup_tooltips = {}
        restore_tooltips = {}
        for entry in Options.entries_sorted:
            title = entry["title"]
            tooltip_key = f"{title}_tooltip"
            source = entry['source'] if isinstance(entry['source'], list) else [entry['source']]
            destination = entry['destination'] if isinstance(entry['destination'], list) else [entry['destination']]
            source_text = "<br/>".join(map(str, source))
            destination_text = "<br/>".join(map(str, destination))
            backup_tooltips[tooltip_key] = apply_replacements(format_html(title, source_text, destination_text))
            restore_tooltips[tooltip_key] = apply_replacements(format_html(title, destination_text, source_text))

        system_manager_tooltips = {}
        operation_keys = {"copy_system_files": "system_files", "install_essential_packages": "essential_packages",
                          "install_additional_packages": "additional_packages",
                          "install_specific_packages": "specific_packages", "set_user_shell": "user_shell"}

        distro_helper = LinuxDistroHelper()
        system_manager_operation_text = Options.get_system_manager_operation_text(distro_helper)

        for operation, config_key in operation_keys.items():
            if operation not in system_manager_operation_text:
                continue

            raw_items = getattr(Options, config_key, None)
            if not raw_items:
                continue

            if config_key in ["system_files", "essential_packages", "additional_packages", "specific_packages"]:
                items = []
                for item in raw_items:
                    if isinstance(item, dict):
                        if not item.get('disabled', False):
                            items.append(item)
                    else:
                        items.append(item)
            else:
                items = raw_items

            if not items:
                continue

            column_width = 1 if config_key == "system_files" else 4
            label_maps = {"system_files": {"source": "Source:<br>", "destination": "<br>Destination:<br>"},
                          "specific_packages": {"package": lambda v: f"{v}", "session": lambda v: f"<br>({v})"},
                          "user_shell": lambda v: f"Selected shell: {v}"}
            if config_key in label_maps:
                mapped = label_maps[config_key]

                def apply_map(k, v):
                    if config_key == "user_shell":
                        return mapped(v)
                    value = mapped.get(k)
                    if callable(value):
                        return value(v)
                    elif value is not None:
                        return f"{value}{v}"
                    return f"{k}: {v}"

                if config_key == "user_shell":
                    items = [mapped(items)]
                else:
                    items = [{k: apply_map(k, v) for k, v in item.items() if k != 'disabled'} for item in items]
            else:
                if config_key in ["essential_packages", "additional_packages"]:
                    items = [item.get('name', str(item)) if isinstance(item, dict) else str(item) for item in items]
            item_format = "".join if config_key == "specific_packages" else lambda l: "<br>".join(l)
            item_strings = [item_format([str(v) for v in item.values()]) if isinstance(item, dict) else str(item) for
                            item in items]
            rows = []
            column_width = max(1, column_width)
            for i in range(0, len(item_strings), column_width):
                bg_color = "#2a2a2a" if (i // column_width) % 2 == 0 else "#1e1e1e"
                cells = ''.join(
                    f'<td style="padding: 5px 5px; border: 1px solid #444; color: #00fa9a; font-family: FiraCode Nerd Font Mono;">{item}</td>'
                    for item in item_strings[i:i + column_width])
                rows.append(f'<tr style="background-color: {bg_color};">{cells}</tr>')
            tooltip = (
                "<div style='white-space: nowrap; font-size: 14px; color: #00fa9a; font-family: FiraCode Nerd Font Mono; "
                f"background-color: #121212; padding: 5px 5px; border: 1px solid #444;'>"
                f"<table style='border-collapse: collapse; table-layout: auto;'>{''.join(rows)}</table></div>")
            system_manager_tooltips[operation] = apply_replacements(tooltip)
        Options.system_manager_tooltips = system_manager_tooltips
        return backup_tooltips, restore_tooltips, system_manager_tooltips
