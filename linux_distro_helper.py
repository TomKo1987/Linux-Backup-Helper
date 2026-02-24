import subprocess, platform, os, re, concurrent.futures, shutil

from logging_config import setup_logger
logger = setup_logger(__name__)

PACKAGE_NAME_REGEX = re.compile(r'^[a-zA-Z0-9._+:-]+$')
MIN_PACKAGES_FOR_PARALLEL = 5

_ARCH    = {"arch", "manjaro", "garuda", "endeavouros"}
_DEBIAN  = {"debian", "ubuntu", "pop", "mint", "elementary"}
_FEDORA  = {"fedora", "rhel", "centos", "rocky", "almalinux"}
_SUSE    = {"opensuse", "opensuse-leap", "opensuse-tumbleweed", "suse"}

_PKG_CONFIGS = {
    "arch":  dict(
        check   = lambda p: ["pacman", "-Qi", p],
        install = "sudo pacman -S --noconfirm {package}",
        update  = "sudo pacman -Syu --noconfirm",
        remove  = "sudo pacman -Rns --noconfirm {package}",
        clean   = "sudo pacman -Scc --noconfirm",
        orphans = "pacman -Qdtq",
        yay     = "git clone https://aur.archlinux.org/yay.git && cd yay && makepkg -si --noconfirm",
        has_aur = True,
        kernel  = "linux-headers",
    ),
    "debian": dict(
        check   = lambda p: ["dpkg", "-s", p],
        install = "sudo apt-get install -y {package}",
        update  = "sudo apt-get update && sudo apt-get upgrade -y",
        remove  = "sudo apt-get remove -y {package}",
        clean   = "sudo apt-get clean && sudo apt-get autoremove -y",
        orphans = "apt list --installed | grep -v -e \"automatic\" | cut -d'/' -f1",
        yay     = "echo 'AUR/yay is not supported on Debian/Ubuntu'",
        has_aur = False,
        kernel  = "linux-headers-generic",
    ),
    "fedora": dict(
        check   = lambda p: ["rpm", "-q", p],
        install = "sudo dnf install -y {package}",
        update  = "sudo dnf upgrade -y",
        remove  = "sudo dnf remove -y {package}",
        clean   = "sudo dnf clean all && sudo dnf autoremove -y",
        orphans = "dnf repo-query --extras",
        yay     = "echo 'AUR/yay is not supported on Fedora/CentOS/RHEL'",
        has_aur = False,
        kernel  = "kernel-devel",
    ),
    "suse": dict(
        check   = lambda p: ["rpm", "-q", p],
        install = "sudo zypper install -y {package}",
        update  = "sudo zypper update -y",
        remove  = "sudo zypper remove -y {package}",
        clean   = "sudo zypper clean --all",
        orphans = "zypper packages --orphaned",
        yay     = "echo 'AUR/yay is not supported on openSUSE/SUSE'",
        has_aur = False,
        kernel  = "kernel-default-devel",
    ),
    "void": dict(
        check   = lambda p: ["xbps-query", p],
        install = "sudo xbps-install -y {package}",
        update  = "sudo xbps-install -Su",
        remove  = "sudo xbps-remove -y {package}",
        clean   = "sudo xbps-remove -Oo",
        orphans = "xbps-query -O",
        yay     = "echo 'AUR/yay is not supported on Void Linux'",
        has_aur = False,
        kernel  = "linux-headers",
    ),
    "gentoo": dict(
        check   = lambda p: ["qlist", "-I", p],
        install = "sudo emerge --ask=n {package}",
        update  = "sudo emerge --sync && sudo emerge -uDU @world",
        remove  = "sudo emerge --depclean {package}",
        clean   = "sudo eclean-dist --deep",
        orphans = "emerge --depclean -pv",
        yay     = "echo 'AUR/yay is not supported on Gentoo'",
        has_aur = False,
        kernel  = "sys-kernel/linux-headers",
    ),
    "nixos": dict(
        check   = lambda p: ["nix-env", "-q", p],
        install = "nix-env -iA nixpkgs.{package}",
        update  = "sudo nixos-rebuild switch --upgrade",
        remove  = "nix-env -e {package}",
        clean   = "nix-collect-garbage -d",
        orphans = "nix-store --gc --print-dead",
        yay     = "echo 'AUR/yay is not supported on NixOS'",
        has_aur = False,
        kernel  = "linuxPackages.kernel",
    ),
    "alpine": dict(
        check   = lambda p: ["apk", "info", "-e", p],
        install = "sudo apk add {package}",
        update  = "sudo apk update && sudo apk upgrade",
        remove  = "sudo apk del {package}",
        clean   = "sudo apk cache clean",
        orphans = "apk info -a",
        yay     = "echo 'AUR/yay is not supported on Alpine Linux'",
        has_aur = False,
        kernel  = "linux-headers",
    ),
    "unknown": dict(
        check   = lambda p: ["which", p],
        install = "echo 'Package manager not detected for {package}'",
        update  = "echo 'Update command not available'",
        remove  = "echo 'Remove command not available'",
        clean   = "echo 'Clean cache command not available'",
        orphans = "echo 'Find orphans command not available'",
        yay     = "echo 'AUR/yay is not supported on this distribution'",
        has_aur = False,
        kernel  = "linux-headers",
    ),
}

_SSH_PACKAGES = {
    "debian": ["openssh-server"],
    "fedora": ["openssh-server"],
    "suse":   ["openssh"],
    None:     ["openssh-server"],
}
_SSH_SERVICE = {
    "debian": "ssh",
    None:     "sshd",
}
_SAMBA_PACKAGES = {
    "debian": ["samba", "samba-common-bin"],
    "fedora": ["samba", "samba-common"],
    None:     ["samba"],
}
_CRON_PACKAGES = {
    "debian": ["cron"],
    "fedora": ["cronie", "cronie-anacron"],
    "suse":   ["cron"],
    "arch":   ["cronie"],
    None:     ["cronie"],
}
_BLUETOOTH_PACKAGES = {
    "arch": ["bluez", "bluez-utils"],
    None:   ["bluez", "bluez-tools"],
}


def _kernel_version() -> str:
    try:
        return subprocess.check_output(["uname", "-r"], text=True).strip()
    except Exception as e:
        logger.error(f"Kernel Version: {e}")
        return ""


def _distro_family(distro_id: str) -> str:
    if distro_id in _ARCH:
        return "arch"
    if distro_id in _DEBIAN:
        return "debian"
    if distro_id in _FEDORA:
        return "fedora"
    if distro_id in _SUSE:
        return "suse"
    return distro_id


def _pkg_lookup(mapping: dict, family: str):
    return mapping.get(family) or mapping.get(None, [])


class LinuxDistroHelper:

    def __init__(self) -> None:
        info = self._detect_distro_info()
        self.distro_id          = info["id"]
        self.distro_name        = info["name"]
        self.distro_pretty_name = info["pretty_name"]
        self.has_flatpak        = bool(shutil.which("flatpak"))
        self.has_snap           = bool(shutil.which("snap"))
        self._setup_commands()

    @staticmethod
    def _detect_distro_info() -> dict:
        info = {"id": "unknown", "name": "", "pretty_name": ""}
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("ID="):
                        info["id"] = line.strip().split("=", 1)[1].strip('"').lower()
                    elif line.startswith("NAME="):
                        info["name"] = line.strip().split("=", 1)[1].strip('"')
                    elif line.startswith("PRETTY_NAME="):
                        info["pretty_name"] = line.strip().split("=", 1)[1].strip('"')
        except Exception as e:
            logger.error("Error reading /etc/os-release: %s", e)
            info["id"] = platform.system().lower()
        return info

    @staticmethod
    def detect_session() -> str | None:
        from options import SESSIONS
        sessions_lower = {s.lower(): s for s in SESSIONS}
        for var in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
            val = os.getenv(var)
            if val:
                for part in val.split(":"):
                    match = sessions_lower.get(part.strip().lower())
                    if match:
                        return match
        return None

    def _setup_commands(self) -> None:
        family = _distro_family(self.distro_id)
        cfg = _PKG_CONFIGS.get(family) or _PKG_CONFIGS["unknown"]
        if family == "unknown" and self.distro_id not in _PKG_CONFIGS:
            logger.warning("Unknown distribution: %s, using generic commands", self.distro_id)

        self.pkg_check_installed = cfg["check"]
        self.pkg_install         = cfg["install"]
        self.pkg_update          = cfg["update"]
        self.pkg_remove          = cfg["remove"]
        self.pkg_clean_cache     = cfg["clean"]
        self.find_orphans        = cfg["orphans"]
        self.install_yay         = cfg["yay"]
        self.has_aur             = cfg["has_aur"]
        self.kernel_headers      = cfg["kernel"]

        if family in ("debian", "fedora"):
            kv = _kernel_version()
            if kv:
                self.kernel_headers = (
                    f"linux-headers-{kv}" if family == "debian"
                    else f"kernel-devel-{kv}"
                )
            else:
                logger.error("Could not determine kernel version for %s.", family)

    @staticmethod
    def _is_valid_package_name(package: str) -> bool:
        if not package or not isinstance(package, str):
            return False
        package = package.strip()
        if not package or len(package) > 255:
            return False
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._+-]*$', package):
            return False
        return not any(c in package for c in (';', '&', '|', '`', '$', '(', ')', '<', '>', '\n', '\r', '\t', ' '))

    def package_is_installed(self, package: str) -> bool:
        if not self._is_valid_package_name(package):
            return False
        try:
            result = subprocess.run(
                self.pkg_check_installed(package),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning("Error checking package %s: %s", package, e)
            return False

    def filter_not_installed(self, packages: list) -> list:
        if not packages or not isinstance(packages, (list, tuple)):
            return []
        valid = [p.strip() for p in packages if self._is_valid_package_name(p)]
        if not valid:
            return []
        if len(valid) <= MIN_PACKAGES_FOR_PARALLEL:
            return [p for p in valid if not self.package_is_installed(p)]
        return self._check_packages_parallel(valid)

    def _check_packages_parallel(self, packages: list) -> list:
        max_workers = min(4, max(1, len(packages) // 4))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(self.package_is_installed, p): p for p in packages}
                not_installed = []
                for future in concurrent.futures.as_completed(futures, timeout=60):
                    pkg = futures[future]
                    try:
                        if not future.result(timeout=10):
                            not_installed.append(pkg)
                    except Exception as e:
                        logger.warning("Error checking package %s: %s", pkg, e)
                        not_installed.append(pkg)
                return not_installed
        except Exception as e:
            logger.error("Error in parallel checking: %s", e)
            return [p for p in packages if not self.package_is_installed(p)]

    def get_kernel_headers_pkg(self) -> str:
        try:
            kv = os.uname().release
            family = _distro_family(self.distro_id)
            if family == "arch":
                if "lts" in kv.lower():      return "linux-lts-headers"
                if "zen" in kv.lower():      return "linux-zen-headers"
                if "hardened" in kv.lower(): return "linux-hardened-headers"
                return "linux-headers"
            if family == "debian": return f"linux-headers-{kv}"
            if family == "fedora": return f"kernel-devel-{kv}"
            if family == "suse":   return "kernel-default-devel"
        except Exception as e:
            logger.error("Error determining kernel header: %s", e)
        return self.kernel_headers

    def get_shell_package_name(self, shell_name: str) -> str:
        shell_map = {
            "bash": "bash", "zsh": "zsh", "fish": "fish", "elvish": "elvish",
            "nushell": "nushell", "xonsh": "xonsh", "ngs": "ngs",
            "pwsh": "powershell" if _distro_family(self.distro_id) == "debian" else "pwsh",
        }
        return shell_map.get(shell_name.lower(), shell_name.lower())

    def _family(self) -> str:
        return _distro_family(self.distro_id)

    @staticmethod
    def get_printer_packages() -> list:
        return ["cups", "ghostscript", "system-config-printer", "gutenprint"]

    def get_samba_packages(self) -> list:
        return _pkg_lookup(_SAMBA_PACKAGES, self._family())

    def get_bluetooth_packages(self) -> list:
        return _pkg_lookup(_BLUETOOTH_PACKAGES, self._family())

    def get_cron_packages(self) -> list:
        return _pkg_lookup(_CRON_PACKAGES, self._family())

    @staticmethod
    def get_firewall_packages() -> list:
        return ["ufw"]

    @staticmethod
    def get_at_packages() -> list:
        return ["at"]

    def get_ssh_packages(self) -> list:
        fam = self._family()
        if fam in ("void", "alpine", "arch", "suse"):
            return ["openssh"]
        return _pkg_lookup(_SSH_PACKAGES, fam)

    def get_ssh_service_name(self) -> str:
        return _pkg_lookup(_SSH_SERVICE, self._family()) if isinstance(
            _pkg_lookup(_SSH_SERVICE, self._family()), str
        ) else (_SSH_SERVICE.get(self._family()) or _SSH_SERVICE[None])

    @staticmethod
    def get_flatpak_packages() -> list:
        return ["flatpak"]

    @staticmethod
    def flatpak_add_flathub() -> str:
        return "flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo"

    @staticmethod
    def get_snap_packages() -> list:
        return ["snapd"]

    def supports_aur(self) -> bool:
        return self.has_aur

    def get_pkg_install_cmd(self, package: str) -> str:
        return self.pkg_install.format(package=package)

    def get_pkg_remove_cmd(self, package: str) -> str:
        return self.pkg_remove.format(package=package)

    def get_pkg_update_cmd(self) -> str:
        return self.pkg_update

    def get_clean_cache_cmd(self) -> str:
        return self.pkg_clean_cache

    def get_find_orphans_cmd(self) -> str:
        return self.find_orphans
