import subprocess, platform, os, logging.handlers, re, concurrent.futures

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

PACKAGE_NAME_REGEX = re.compile(r'^[a-zA-Z0-9._+:-]+$')

class LinuxDistroHelper:
    def __init__(self):
        info = self._detect_distro_info()
        self.distro_id = info["id"]
        self.distro_name = info["name"]
        self.distro_pretty_name = info["pretty_name"]

        self.pkg_check_installed = None
        self.pkg_install = ""
        self.pkg_update = ""
        self.pkg_remove = ""
        self.pkg_clean_cache = ""
        self.find_orphans = ""
        self.install_yay = ""
        self.has_aur = False
        self.kernel_headers = ""

        self._setup_commands()

    @staticmethod
    def _detect_distro_info():
        distro_info = {"id": "unknown", "name": "", "pretty_name": ""}
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("ID="):
                        distro_info["id"] = line.strip().split("=", 1)[1].strip('"').lower()
                    elif line.startswith("NAME="):
                        distro_info["name"] = line.strip().split("=", 1)[1].strip('"')
                    elif line.startswith("PRETTY_NAME="):
                        distro_info["pretty_name"] = line.strip().split("=", 1)[1].strip('"')
        except Exception as e:
            logger.error(f"Error when reading /etc/os-release: {e}")
            distro_info["id"] = platform.system().lower()
        return distro_info

    @staticmethod
    def detect_session():
        from options import SESSIONS
        for var in ['XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP', 'DESKTOP_SESSION']:
            val = os.getenv(var)
            if val:
                for part in val.split(':'):
                    match = next((env for env in SESSIONS if part.strip().lower() == env.lower()), None)
                    if match:
                        return match
        return None

    def _setup_commands(self):
        distro = self.distro_id

        if distro == "arch" or distro == "manjaro":
            self.pkg_check_installed = lambda pkg: ["pacman", "-Qi", pkg]
            self.pkg_install = "sudo pacman -S --noconfirm {package}"
            self.pkg_update = "sudo pacman -Syu --noconfirm"
            self.pkg_remove = "sudo pacman -Rns --noconfirm {package}"
            self.pkg_clean_cache = "sudo pacman -Scc --noconfirm"
            self.find_orphans = "pacman -Qdtq"
            self.install_yay = "git clone https://aur.archlinux.org/yay.git && cd yay && makepkg -si --noconfirm"
            self.has_aur = True
            self.kernel_headers = "linux-headers"

        elif distro in ["debian", "ubuntu", "pop", "mint", "elementary"]:
            self.pkg_check_installed = lambda pkg: ["dpkg", "-s", pkg]
            self.pkg_install = "sudo apt-get install -y {package}"
            self.pkg_update = "sudo apt-get update && sudo apt-get upgrade -y"
            self.pkg_remove = "sudo apt-get remove -y {package}"
            self.pkg_clean_cache = "sudo apt-get clean && sudo apt-get autoremove -y"
            self.find_orphans = "apt list --installed | grep -v -e \"automatic\" | cut -d'/' -f1"
            self.install_yay = "echo 'AUR/yay is not supported on Debian/Ubuntu'"
            self.has_aur = False
            try:
                kernel_version = subprocess.check_output(["uname", "-r"], text=True).strip()
                self.kernel_headers = f"linux-headers-{kernel_version}"
            except Exception as e:
                logger.error(f"Error when determining the kernel version: {e}")
                self.kernel_headers = "linux-headers-generic"

        elif distro in ["fedora", "rhel", "centos", "rocky", "almalinux"]:
            self.pkg_check_installed = lambda pkg: ["rpm", "-q", pkg]
            self.pkg_install = "sudo dnf install -y {package}"
            self.pkg_update = "sudo dnf upgrade -y"
            self.pkg_remove = "sudo dnf remove -y {package}"
            self.pkg_clean_cache = "sudo dnf clean all && sudo dnf autoremove -y"
            self.find_orphans = "dnf repoquery --extras"
            self.install_yay = "echo 'AUR/yay is not supported on Fedora/CentOS/RHEL'"
            self.has_aur = False
            try:
                kernel_version = subprocess.check_output(["uname", "-r"], text=True).strip()
                self.kernel_headers = f"kernel-devel-{kernel_version}"
            except Exception as e:
                logger.error(f"Error when determining the kernel version: {e}")
                self.kernel_headers = "kernel-devel"

        elif distro in ["opensuse", "opensuse-leap", "opensuse-tumbleweed", "suse"]:
            self.pkg_check_installed = lambda pkg: ["rpm", "-q", pkg]
            self.pkg_install = "sudo zypper install -y {package}"
            self.pkg_update = "sudo zypper update -y"
            self.pkg_remove = "sudo zypper remove -y {package}"
            self.pkg_clean_cache = "sudo zypper clean --all"
            self.find_orphans = "zypper packages --orphaned"
            self.install_yay = "echo 'AUR/yay is not supported on openSUSE/SUSE'"
            self.has_aur = False
            self.kernel_headers = "kernel-default-devel"

        else:
            logger.warning(f"Unknown distribution: {distro}, using generic commands")
            self.pkg_check_installed = lambda pkg: ["which", pkg]
            self.pkg_install = "echo 'Package manager not detected for {package}'"
            self.pkg_update = "echo 'Update command not available'"
            self.pkg_remove = "echo 'Remove command not available'"
            self.pkg_clean_cache = "echo 'Clean cache command not available'"
            self.find_orphans = "echo 'Find orphans command not available'"
            self.install_yay = "echo 'AUR/yay is not supported on this distribution'"
            self.has_aur = False
            self.kernel_headers = "linux-headers"

    def package_is_installed(self, package):
        if not self._is_valid_package_name(package):
            return False

        try:
            result = subprocess.run(
                self.pkg_check_installed(package),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning(f"Error checking package {package}: {e}")
            return False

    @staticmethod
    def _is_valid_package_name(package):
        if not package or not isinstance(package, str):
            return False

        package = package.strip()
        if not package or len(package) > 255:
            return False

        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._+-]*$', package):
            return False

        dangerous_chars = [';', '&', '|', '`', '$', '(', ')', '<', '>', '\n', '\r', '\t', ' ']
        return not any(char in package for char in dangerous_chars)

    def filter_not_installed(self, packages):
        if not packages or not isinstance(packages, (list, tuple)):
            return []

        valid_packages = [
            pkg.strip() for pkg in packages
            if self._is_valid_package_name(pkg)
        ]

        if not valid_packages:
            return []

        if len(valid_packages) <= 5:
            return [pkg for pkg in valid_packages if not self.package_is_installed(pkg)]

        return self._check_packages_parallel(valid_packages)

    def _check_packages_parallel(self, packages):
        max_workers = min(4, max(1, len(packages) // 4))

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_pkg = {
                    executor.submit(self.package_is_installed, pkg): pkg
                    for pkg in packages
                }

                not_installed = []
                for future in concurrent.futures.as_completed(future_to_pkg, timeout=60):
                    pkg = future_to_pkg[future]
                    try:
                        if not future.result(timeout=10):
                            not_installed.append(pkg)
                    except Exception as e:
                        logger.warning(f"Error checking package {pkg}: {e}")
                        not_installed.append(pkg)

                return not_installed
        except Exception as e:
            logger.error(f"Error in parallel checking: {e}")
            return [pkg for pkg in packages if not self.package_is_installed(pkg)]

    def get_kernel_headers_pkg(self):
        try:
            kernel_version = os.uname().release
            if self.distro_id in ["arch", "manjaro"]:
                if any(variant in kernel_version.lower() for variant in ["lts"]):
                    return "linux-lts-headers"
                elif any(variant in kernel_version.lower() for variant in ["zen"]):
                    return "linux-zen-headers"
                elif any(variant in kernel_version.lower() for variant in ["hardened"]):
                    return "linux-hardened-headers"
                else:
                    return "linux-headers"
            elif self.distro_id in ["debian", "ubuntu", "pop", "mint", "elementary"]:
                return f"linux-headers-{kernel_version}"
            elif self.distro_id in ["fedora", "rhel", "centos", "rocky", "almalinux"]:
                return f"kernel-devel-{kernel_version}"
            elif self.distro_id in ["opensuse", "opensuse-leap", "opensuse-tumbleweed", "suse"]:
                return "kernel-default-devel"
        except Exception as e:
            logger.error(f"Error when determining the kernel header: {e}")
        return self.kernel_headers

    def get_shell_package_name(self, shell_name):
        shell_map = {
            "bash": "bash", "zsh": "zsh", "fish": "fish", "elvish": "elvish",
            "nushell": "nushell", "xonsh": "xonsh", "ngs": "ngs",
            "pwsh": "powershell" if self.distro_id in ["debian", "ubuntu", "pop", "mint", "elementary"] else "pwsh"
        }
        return shell_map.get(shell_name.lower(), shell_name.lower())

    def get_printer_packages(self):
        base_packages = ["cups", "ghostscript"]

        if self.distro_id in ["debian", "ubuntu", "pop", "mint", "elementary"]:
            return base_packages + ["system-config-printer", "printer-driver-gutenprint"]
        elif self.distro_id in ["fedora", "rhel", "centos", "rocky", "almalinux"]:
            return base_packages + ["system-config-printer", "gutenprint"]
        elif self.distro_id in ["opensuse", "opensuse-leap", "opensuse-tumbleweed", "suse"]:
            return base_packages + ["system-config-printer", "gutenprint"]
        elif self.distro_id in ["arch", "manjaro"]:
            return base_packages + ["system-config-printer", "gutenprint"]
        else:
            return base_packages + ["system-config-printer", "gutenprint"]

    def get_samba_packages(self):
        if self.distro_id in ["debian", "ubuntu", "pop", "mint", "elementary"]:
            return ["gvfs-backends", "samba", "samba-common-bin"]
        elif self.distro_id in ["fedora", "rhel", "centos", "rocky", "almalinux"]:
            return ["gvfs-smb", "samba", "samba-common"]
        elif self.distro_id in ["opensuse", "opensuse-leap", "opensuse-tumbleweed", "suse"]:
            return ["gvfs-smb", "samba"]
        elif self.distro_id in ["arch", "manjaro"]:
            return ["gvfs-smb", "samba"]
        else:
            return ["gvfs-smb", "samba"]

    def get_bluetooth_packages(self):
        if self.distro_id in ["arch", "manjaro"]:
            return ["bluez", "bluez-utils"]
        else:
            return ["bluez", "bluez-tools"]

    def get_cron_packages(self):
        if self.distro_id in ["debian", "ubuntu", "pop", "mint", "elementary"]:
            return ["cron"]
        elif self.distro_id in ["fedora", "rhel", "centos", "rocky", "almalinux"]:
            return ["cronie", "cronie-anacron"]
        elif self.distro_id in ["opensuse", "opensuse-leap", "opensuse-tumbleweed", "suse"]:
            return ["cron"]
        elif self.distro_id in ["arch", "manjaro"]:
            return ["cronie"]
        else:
            return ["cronie"]

    @staticmethod
    def get_firewall_packages():
        return ["ufw"]

    @staticmethod
    def get_at_packages():
        return ["at"]

    def get_pkg_install_cmd(self, package):
        return self.pkg_install.format(package=package)

    def get_pkg_remove_cmd(self, package):
        return self.pkg_remove.format(package=package)

    def get_pkg_update_cmd(self):
        return self.pkg_update

    def get_clean_cache_cmd(self):
        return self.pkg_clean_cache

    def get_find_orphans_cmd(self):
        return self.find_orphans

    def supports_aur(self):
        return self.has_aur
