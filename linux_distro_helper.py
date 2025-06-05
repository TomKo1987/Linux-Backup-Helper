import subprocess, platform, os

class LinuxDistroHelper:
    def __init__(self):
        self.distro_id = self._detect_distro_id()
        self.kernel_headers = ""
        self.pkg_check_installed = None
        self.pkg_install = ""
        self.pkg_update = ""
        self.pkg_remove = ""
        self.pkg_clean_cache = ""
        self.find_orphans = ""
        self.install_yay = ""
        self.has_aur = False
        self._setup_commands()
        info = self._detect_distro_info()
        self.distro_id = info["id"]
        self.distro_name = info["name"]
        self.distro_pretty_name = info["pretty_name"]

    @staticmethod
    def _detect_distro_id():
        distro_id = "unknown"
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("ID="):
                        distro_id = line.strip().split("=")[1].strip('"').lower()
                        break
        except Exception as e:
            print(f"Error in _detect_distro_id: {e}")
            distro_id = platform.system().lower()
        return distro_id

    @staticmethod
    def _detect_distro_info():
        distro_info = {"id": "unknown", "name": "", "pretty_name": ""}
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("ID="):
                        distro_info["id"] = line.strip().split("=")[1].strip('"').lower()
                    elif line.startswith("NAME="):
                        distro_info["name"] = line.strip().split("=")[1].strip('"')
                    elif line.startswith("PRETTY_NAME="):
                        distro_info["pretty_name"] = line.strip().split("=")[1].strip('"')
        except Exception as e:
            print(f"Error in _detect_distro_info: {e}")
            distro_info["id"] = platform.system().lower()
        return distro_info

    def _setup_commands(self):
        self.pkg_check_installed = lambda pkg: ["pacman", "-Qi", pkg]
        self.pkg_install = "sudo pacman -S --noconfirm {package}"
        self.pkg_update = "sudo pacman -Syu --noconfirm"
        self.pkg_remove = "sudo pacman -R --noconfirm {package}"
        self.pkg_clean_cache = "sudo pacman -Scc --noconfirm"
        self.find_orphans = "pacman -Qdtq"
        self.install_yay = "git clone https://aur.archlinux.org/yay.git && cd yay && makepkg -si --noconfirm"
        self.has_aur = True
        self.kernel_headers = "linux-headers"

        if self.distro_id in ["debian", "ubuntu"]:
            self.pkg_install = "sudo apt-get install -y {package}"
            self.pkg_update = "sudo apt-get update && sudo apt-get upgrade -y"
            self.pkg_remove = "sudo apt-get remove -y {package}"
            self.pkg_clean_cache = "sudo apt-get clean"
            self.find_orphans = "deborphan"
            self.install_yay = "echo 'AUR/yay is not supported on Debian/Ubuntu'"
            self.has_aur = False
            self.pkg_check_installed = lambda pkg: ["dpkg", "-s", pkg]
            try:
                kernel_version = subprocess.check_output(["uname", "-r"], text=True).strip()
                self.kernel_headers = f"linux-headers-{kernel_version}"
            except Exception as e:
                print(f"Error in _setup_commands:{e}")
                self.kernel_headers = "linux-headers-generic"

        elif self.distro_id in ["fedora", "rhel", "centos"]:
            self.pkg_install = "sudo dnf install -y {package}"
            self.pkg_update = "sudo dnf upgrade -y"
            self.pkg_remove = "sudo dnf remove -y {package}"
            self.pkg_clean_cache = "sudo dnf clean all"
            self.find_orphans = "dnf repoquery --extras"
            self.install_yay = "echo 'AUR/yay is not supported on Fedora/CentOS/RHEL'"
            self.has_aur = False
            self.pkg_check_installed = lambda pkg: ["rpm", "-q", pkg]
            self.kernel_headers = "kernel-devel"

        elif self.distro_id in ["opensuse", "suse"]:
            self.pkg_install = "sudo zypper install -y {package}"
            self.pkg_update = "sudo zypper update -y"
            self.pkg_remove = "sudo zypper remove -y {package}"
            self.pkg_clean_cache = "sudo zypper clean --all"
            self.find_orphans = "zypper packages --orphaned"
            self.install_yay = "echo 'AUR/yay is not supported on openSUSE/SUSE'"
            self.has_aur = False
            self.pkg_check_installed = lambda pkg: ["rpm", "-q", pkg]
            self.kernel_headers = "kernel-devel"

    def get_pkg_install_cmd(self, package):
        return self.pkg_install.format(package=package)

    def get_pkg_update_cmd(self):
        return self.pkg_update

    def get_pkg_remove_cmd(self, package):
        return self.pkg_remove.format(package=package)

    def get_clean_cache_cmd(self):
        return self.pkg_clean_cache

    def get_find_orphans_cmd(self):
        return self.find_orphans

    def get_install_yay_cmd(self):
        return self.install_yay

    def supports_aur(self):
        return self.has_aur

    def get_kernel_headers_pkg(self):
        try:
            kernel_version = os.uname().release
            if self.distro_id == "arch":
                if "lts" in kernel_version.lower():
                    return "linux-lts-headers"
                elif "zen" in kernel_version.lower():
                    return "linux-zen-headers"
                elif "hardened" in kernel_version.lower():
                    return "linux-hardened-headers"
                else:
                    return "linux-headers"
            elif self.distro_id in ["debian", "ubuntu"]:
                return f"linux-headers-{kernel_version}"
            elif self.distro_id in ["fedora", "rhel", "centos"]:
                return f"kernel-devel-{kernel_version}"
            elif self.distro_id in ["opensuse", "suse"]:
                return "kernel-default-devel"
            else:
                return "linux-headers"
        except (AttributeError, OSError) as e:
            print(f"Error detecting kernel headers: {e}")
            return "linux-headers"

    def get_shell_package_name(self, shell_name):
        shell_map = {"bash": "bash", "zsh": "zsh", "fish": "fish", "elvish": "elvish", "nushell": "nushell", "xonsh": "xonsh",
                     "ngs": "ngs", "pwsh": "powershell" if self.distro_id in ["debian", "ubuntu"] else "pwsh"}
        return shell_map.get(shell_name.lower(), shell_name.lower())

    def package_is_installed(self, package):
        cmd = self.pkg_check_installed(package)
        try:
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return result.returncode == 0
        except Exception as e:
            print(f"Error determining if package is installed: {e}")
            return False

    def filter_not_installed(self, packages):
        return [pkg for pkg in packages if not self.package_is_installed(pkg)]

    def get_printer_packages(self):
        base_packages = ["cups", "ghostscript"]

        if self.distro_id in ["debian", "ubuntu"]:
            return base_packages + ["system-config-printer", "printer-driver-gutenprint"]
        elif self.distro_id in ["fedora", "rhel", "centos"]:
            return base_packages + ["system-config-printer", "gutenprint"]
        elif self.distro_id in ["opensuse", "suse"]:
            return base_packages + ["system-config-printer", "gutenprint"]
        else:
            return base_packages + ["system-config-printer", "gutenprint"]

    def get_samba_packages(self):
        if self.distro_id in ["debian", "ubuntu"]:
            return ["gvfs-backends", "samba", "samba-common-bin"]
        elif self.distro_id in ["fedora", "rhel", "centos"]:
            return ["gvfs-smb", "samba", "samba-common"]
        elif self.distro_id in ["opensuse", "suse"]:
            return ["gvfs-smb", "samba"]
        else:
            return ["gvfs-smb", "samba"]

    def get_bluetooth_packages(self):
        if self.distro_id in ["debian", "ubuntu"]:
            return ["bluez", "bluez-tools"]
        elif self.distro_id in ["fedora", "rhel", "centos"]:
            return ["bluez", "bluez-tools"]
        elif self.distro_id in ["opensuse", "suse"]:
            return ["bluez", "bluez-tools"]
        else:
            return ["bluez", "bluez-utils"]

    def get_cron_packages(self):
        if self.distro_id in ["debian", "ubuntu"]:
            return ["cron"]
        elif self.distro_id in ["fedora", "rhel", "centos"]:
            return ["cronie", "cronie-anacron"]
        elif self.distro_id in ["opensuse", "suse"]:
            return ["cron"]
        else:
            return ["cronie"]

    @staticmethod
    def get_firewall_packages():
        return ["ufw"]

    @staticmethod
    def get_at_packages():
        return ["at"]