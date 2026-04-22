import concurrent.futures
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

from constants import USER_SHELLS, ARCH_KERNEL_VARIANTS, PKG_NAME_RE
from state import logger

__all__ = ["LinuxDistroHelper", "SESSIONS", "USER_SHELLS", "ARCH_KERNEL_VARIANTS", "is_valid_pkg_name"]

_MIN_PARALLEL = 5

_DISTROS_ARCH = {"arch", "manjaro", "garuda", "endeavouros", "omarchy", "archman", "rebornos", "cachyos", "artix",
                 "arcolinux", "blendos", "crystal", "archcraft", "archbang", "archlabs"}

_DISTROS_DEBIAN = {"debian", "ubuntu", "pop", "pop-os", "popos", "mint", "linuxmint", "elementary", "lmde", "kali",
                   "parrot", "zorin", "zorinos", "mxlinux", "mx", "antix", "raspbian", "peppermint", "deepin", "lite",
                   "q4os", "linuxlite", "tails", "siduction", "sparky", "sparkylinux", "bodhi", "bunsenlabs", "pureos",
                   "ubuntu-budgie", "devuan", "refracta", "kubuntu", "xubuntu", "lubuntu", "ubuntu-mate"}

_DISTROS_FEDORA = {"fedora", "rhel", "centos", "rocky", "almalinux", "nobara", "ultramarine", "mageia", "openmandriva"}

_DISTROS_SUSE = {"opensuse", "opensuse-leap", "opensuse-tumbleweed", "opensuse-slowroot", "suse", "sled", "sles"}

_DISTROS_GENTOO = {"gentoo", "funtoo", "calculate", "sabayon"}

_DISTROS_SLACKWARE = {"slackware", "salix", "porteus", "slax"}


def is_valid_pkg_name(name: str) -> bool:
    return (
        isinstance(name, str)
        and bool(name.strip())
        and len(name.strip()) <= 255
        and bool(PKG_NAME_RE.match(name.strip()))
    )


_DISTRO_FAMILY_MAP: dict[str, str] = {
    distro_id: family
    for family, distro_set in [("arch", _DISTROS_ARCH), ("debian", _DISTROS_DEBIAN), ("fedora", _DISTROS_FEDORA),
                               ("suse", _DISTROS_SUSE), ("gentoo", _DISTROS_GENTOO), ("slackware", _DISTROS_SLACKWARE),
                               ("void", {"void"}), ("nixos", {"nixos"}), ("alpine", {"alpine", "postmarketos"}),
                               ("solus", {"solus"})] for distro_id in distro_set}

del _DISTROS_ARCH, _DISTROS_DEBIAN, _DISTROS_FEDORA, _DISTROS_SUSE, _DISTROS_GENTOO, _DISTROS_SLACKWARE


_SHELL_BINARIES: dict[str, str] = {"nushell": "nu", "powershell": "pwsh", "powershell-bin": "pwsh"}

_SHELL_PKG_MAP: dict[str, str] = {"bash": "bash", "zsh": "zsh", "fish": "fish", "elvish": "elvish",
                                  "nushell": "nushell", "xonsh": "xonsh", "ngs": "ngs"}

SESSIONS = ["KDE", "GNOME", "XFCE", "Cinnamon", "MATE", "LXDE", "LXQt", "Budgie", "Deepin", "Openbox", "i3", "Sway",
            "Hyprland", "bspwm", "dwm", "awesome", "qtile", "xmonad", "Wayfire", "River", "niri", "COSMIC"]

_SESSION_LOWER: dict[str, str] = {s.lower(): s for s in SESSIONS}

_PKG_MGR_NAME: dict[str, str] = {"arch": "pacman", "debian": "apt", "fedora": "dnf", "suse": "zypper", "solus": "eopkg",
                                 "void": "xbps-install", "gentoo": "emerge", "nixos": "nix-env", "alpine": "apk",
                                 "slackware": "pkgtool", "unknown": "unknown"}


def _nixos_check(p: str) -> list[str]: return ["sh", "-c", "nix-env -q --installed 2>/dev/null | grep -qF -- " + shlex.quote(p)]


def _slackware_check(p: str) -> list[str]: return ["sh", "-c", f"ls /var/log/packages/{p}-* >/dev/null 2>&1"]


_PKG: dict[str, dict[str, Any]] = {
    "arch": dict(
        check   = lambda p: ["pacman", "-Qi", p],
        install = "sudo pacman -S --needed {p}",
        update  = "sudo pacman -Syu --noconfirm",
        remove  = "sudo pacman -Rns --noconfirm {p}",
        clean   = "sudo pacman -Scc --noconfirm",
        orphans = "pacman -Qdtq",
        has_aur = True,
        kernel  = "linux-headers",
    ),
    "debian": dict(
        check   = lambda p: ["dpkg-query", "-W", "-f=${Status}", p],
        install = "sudo apt-get install -y {p}",
        update  = "sudo apt-get update && sudo apt-get upgrade -y",
        remove  = "sudo apt-get autoremove -y {p}",
        clean   = "sudo apt-get clean && sudo apt-get autoremove -y",
        orphans = "apt-get --dry-run autoremove",
        has_aur = False,
        kernel  = "linux-headers-generic",
    ),
    "fedora": dict(
        check   = lambda p: ["rpm", "-q", p],
        install = "sudo dnf install -y {p}",
        update  = "sudo dnf upgrade -y",
        remove  = "sudo dnf remove -y {p}",
        clean   = "sudo dnf clean all && sudo dnf autoremove -y",
        orphans = "sudo dnf repoquery --extras",
        has_aur = False,
        kernel  = "kernel-devel",
    ),
    "suse": dict(
        check   = lambda p: ["rpm", "-q", p],
        install = "sudo zypper install -y {p}",
        update  = "sudo zypper update -y",
        remove  = "sudo zypper remove -y {p}",
        clean   = "sudo zypper clean --all",
        orphans = "zypper --no-refresh packages --orphaned",
        has_aur = False,
        kernel  = "kernel-default-devel",
    ),
    "void": dict(
        check   = lambda p: ["xbps-query", p],
        install = "sudo xbps-install -y {p}",
        update  = "sudo xbps-install -Su",
        remove  = "sudo xbps-remove -y {p}",
        clean   = "sudo xbps-remove -Oo",
        orphans = "xbps-query -O",
        has_aur = False,
        kernel  = "linux-headers",
    ),
    "gentoo": dict(
        check   = lambda p: ["qlist", "-I", p],
        install = "sudo emerge --ask=n {p}",
        update  = "sudo emerge --sync && sudo emerge -uDU @world",
        remove  = "sudo emerge --depclean {p}",
        clean   = "sudo eclean-dist --deep",
        orphans = "",
        has_aur = False,
        kernel  = "sys-kernel/linux-headers",
    ),
    "nixos": dict(
        check   = _nixos_check,
        install = "nix-env -iA nixpkgs.{p}",
        update  = "sudo nixos-rebuild switch --upgrade",
        remove  = "nix-env -e {p}",
        clean   = "nix-collect-garbage -d",
        orphans = "",
        has_aur = False,
        kernel  = "linuxPackages.kernel",
    ),
    "alpine": dict(
        check   = lambda p: ["apk", "info", "-e", p],
        install = "sudo apk add {p}",
        update  = "sudo apk update && sudo apk upgrade",
        remove  = "sudo apk del {p}",
        clean   = "sudo apk cache clean",
        orphans = "",
        has_aur = False,
        kernel  = "linux-headers",
    ),
    "slackware": dict(
        check   = _slackware_check,
        install = "sudo installpkg {p}",
        update  = "sudo slackpkg update && sudo slackpkg upgrade-all",
        remove  = "sudo removepkg {p}",
        clean   = "sudo slackpkg clean-system",
        orphans = "",
        has_aur = False,
        kernel  = "kernel-headers",
    ),
    "solus": dict(
        check   = lambda p: ["eopkg", "info", p],
        install = "sudo eopkg install {p}",
        update  = "sudo eopkg upgrade",
        remove  = "sudo eopkg remove {p}",
        clean   = "sudo eopkg delete-cache",
        orphans = "eopkg list-orphans",
        has_aur = False,
        kernel  = "linux-headers",
    ),
    "unknown": dict(
        check   = lambda p: ["which", p],
        install = "echo 'No package manager detected: {p}'",
        update  = "echo 'Update not available'",
        remove  = "echo 'Remove not available: {p}'",
        clean   = "echo 'Clean not available'",
        orphans = "",
        has_aur = False,
        kernel  = "linux-headers",
    ),
}


_SSH_PKGS: dict = {
    "debian":    ["openssh-server"],
    "fedora":    ["openssh-server"],
    "suse":      ["openssh"],
    "void":      ["openssh"],
    "alpine":    ["openssh"],
    "arch":      ["openssh"],
    "gentoo":    ["net-misc/openssh"],
    "nixos":     ["openssh"],
    "slackware": ["openssh"],
    "solus":     ["openssh-server"],
    None:        ["openssh-server"],
}
_SSH_SVC  = {"debian": "ssh", None: "sshd"}
_SAMBA_SVC = {"debian": "smbd", None: "smb"}
_SAMBA_PKGS: dict = {
    "debian":    ["samba", "samba-common-bin"],
    "fedora":    ["samba", "samba-common"],
    "suse":      ["samba"],
    "arch":      ["samba"],
    "void":      ["samba"],
    "alpine":    ["samba"],
    "gentoo":    ["net-fs/samba"],
    "nixos":     ["samba"],
    "slackware": ["samba"],
    "solus":     ["samba"],
    None:        ["samba"],
}
_CRON_SVC = {"debian": "cron", None: "cronie"}
_CRON_PKGS: dict = {
    "debian": ["cron"],
    "fedora": ["cronie", "cronie-anacron"],
    "suse":   ["cron"],
    "arch":   ["cronie"],
    "void":   ["cronie"],
    "alpine": ["cronie"],
    "nixos":  ["cronie"],
    "solus":  ["cronie"],
    "gentoo": ["sys-process/cronie"],
    None:     ["cronie"],
}
_BT_PKGS: dict = {
    "arch":   ["bluez", "bluez-utils"],
    "debian": ["bluez", "bluez-tools"],
    "fedora": ["bluez", "bluez-tools"],
    "suse":   ["bluez"],
    "void":   ["bluez"],
    "alpine": ["bluez"],
    "gentoo": ["net-wireless/bluez"],
    "solus":  ["bluez"],
    None:     ["bluez", "bluez-tools"],
}


_UCODE_PKGS: dict[str, dict[str, str]] = {
    "intel": {
        "arch":     "intel-ucode",
        "debian":   "intel-microcode",
        "fedora":   "microcode_ctl",
        "suse":     "ucode-intel",
        "void":     "intel-ucode",
        "alpine":   "intel-ucode",
        "gentoo":   "sys-firmware/intel-microcode",
        "nixos":    "hardware.cpu.intel.updateMicrocode",
        "slackware":"","solus":"","unknown":"",
    },
    "amd": {
        "arch":     "amd-ucode",
        "debian":   "amd64-microcode",
        "fedora":   "microcode_ctl",
        "suse":     "ucode-amd",
        "void":     "linux-firmware-amd",
        "alpine":   "linux-firmware-amd",
        "gentoo":   "sys-firmware/linux-firmware",
        "nixos":    "hardware.cpu.amd.updateMicrocode",
        "slackware":"","solus":"","unknown":"",
    },
}

_WM_PROCS: dict[str, str] = {
    "kwin_wayland":   "KDE",
    "kwin_x11":       "KDE",
    "gnome-shell":    "GNOME",
    "xfce4-session":  "XFCE",
    "cinnamon":       "Cinnamon",
    "mate-session":   "MATE",
    "lxsession":      "LXDE",
    "lxqt-session":   "LXQt",
    "budgie-wm":      "Budgie",
    "deepin-session": "Deepin",
    "openbox":        "Openbox",
    "i3":             "i3",
    "sway":           "Sway",
    "hyprland":       "Hyprland",
    "wayfire":        "Wayfire",
    "river":          "River",
    "niri":           "niri",
    "cosmic-comp":    "COSMIC",
    "bspwm":          "bspwm",
    "dwm":            "dwm",
    "awesome":        "awesome",
    "qtile":          "qtile",
    "xmonad":         "xmonad",
}


def distro_family(distro_id: str) -> str: return _DISTRO_FAMILY_MAP.get(distro_id, distro_id)


def _lookup(table: dict, family: str) -> list[Any] | None | Any:
    result = table.get(family)
    if result is not None:
        return result
    return table.get(None) or []


class LinuxDistroHelper:

    def __init__(self) -> None:
        self.distro_id, self.distro_name, self.distro_pretty_name = self._read_os_release()
        self._init_pkg()

    @staticmethod
    def _read_os_release() -> tuple[str, str, str]:
        d_id = d_name = d_pretty = d_like = ""
        try:
            with open("/etc/os-release", encoding="utf-8") as fh:
                for line in fh:
                    k, _, v = line.partition("=")
                    v = v.strip().strip('"')
                    if k == "ID":
                        d_id = v.lower()
                    elif k == "NAME":
                        d_name = v
                    elif k == "PRETTY_NAME":
                        d_pretty = v
                    elif k == "ID_LIKE":
                        d_like = v.lower()
        except Exception as exc:
            logger.error("/etc/os-release: %s", exc)
            d_id = "unknown"
            d_name = "Unknown Linux Distribution"
            d_pretty = "Unknown Linux Distribution"

        resolved = d_id or "unknown"
        if resolved not in _DISTRO_FAMILY_MAP and d_like:
            for candidate in d_like.split():
                candidate = candidate.strip()
                if candidate in _DISTRO_FAMILY_MAP:
                    logger.debug("distro '%s' unknown; resolved via ID_LIKE '%s'", d_id, candidate)
                    resolved = candidate
                    break

        return resolved or "unknown", d_name, d_pretty

    def _init_pkg(self) -> None:
        self._family: str = distro_family(self.distro_id)
        cfg = _PKG.get(self._family) or _PKG["unknown"]
        if self._family == "unknown":
            logger.warning("Unknown distro '%s', using generic commands.", self.distro_id)

        self._check_fn: Callable[[str], list[str]] = cfg["check"]
        self._install: str = cfg["install"]
        self._update: str = cfg["update"]
        self._remove: str = cfg["remove"]
        self._clean: str = cfg["clean"]
        self._orphans: str = cfg["orphans"]
        self.has_aur: bool = bool(cfg["has_aur"])
        self._kernel_pkg: str = cfg["kernel"]

    def family(self) -> str: return self._family

    def pkg_manager_name(self) -> str: return _PKG_MGR_NAME.get(self.family(), "unknown")

    def supports_aur(self) -> bool: return self.has_aur

    @staticmethod
    def valid(name: str) -> bool:
        return is_valid_pkg_name(name)

    def package_is_installed(self, pkg: str) -> bool:
        if not self.valid(pkg):
            return False
        try:
            r = subprocess.run(self._check_fn(pkg.strip()), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10, check=False)
            return r.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning("pkg check '%s': %s", pkg, exc)
            return False

    def filter_not_installed(self, packages: list[str]) -> list[str]:
        valid = [p.strip() for p in packages if self.valid(p)]
        if not valid:
            return []
        if len(valid) < _MIN_PARALLEL:
            return [p for p in valid if not self.package_is_installed(p)]
        return self._parallel_check(valid)

    def _parallel_check(self, packages: list[str]) -> list[str]:
        workers = min(4, len(packages))
        adaptive_timeout = max(15, len(packages) * 2)
        results: dict[str, bool] = {pkg: False for pkg in packages}
        done: set[str] = set()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self.package_is_installed, p): p for p in packages}
                for fut in concurrent.futures.as_completed(futs, timeout=adaptive_timeout):
                    pkg = futs[fut]
                    done.add(pkg)
                    try:
                        results[pkg] = fut.result()
                    except Exception as exc:
                        logger.warning("parallel check '%s': %s", pkg, exc)
        except concurrent.futures.TimeoutError:
            remaining = [p for p in packages if p not in done]
            logger.warning("parallel check timed out; %d package(s) checked sequentially", len(remaining))
            for p in remaining:
                try:
                    results[p] = self.package_is_installed(p)
                except Exception as exc:
                    logger.warning("sequential fallback check '%s': %s", p, exc)
        except Exception as exc:
            logger.error("parallel check failed: %s", exc)
            return [p for p in packages if not self.package_is_installed(p)]
        return [pkg for pkg in packages if not results[pkg]]

    def get_pkg_install_cmd(self, package: str) -> str: return self._install.format(p=package)
    def get_pkg_remove_cmd(self,  package: str) -> str: return self._remove.format(p=package)
    def get_update_system_cmd(self)             -> str: return self._update
    def get_clean_cache_cmd(self)               -> str: return self._clean
    def get_find_orphans_cmd(self)              -> str: return self._orphans

    def get_batch_install_cmd(self, packages: list[str]) -> str:
        if not packages:
            return ""
        fam = self.family()
        if fam == "nixos":
            return "nix-env -iA " + " ".join(f"nixpkgs.{p}" for p in packages)
        if fam == "slackware":
            return "sudo slackpkg install " + " ".join(packages)
        return self._install.format(p=" ".join(packages))

    def parse_orphan_output(self, raw: str) -> list[str]:
        fam   = self.family()
        lines = raw.strip().splitlines()

        if fam == "suse":
            pkgs = []
            for line in lines:
                line = line.strip()
                if line.startswith("i ") or line.startswith("i+"):
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) >= 2:
                        name = parts[1].strip()
                        if name and self.valid(name):
                            pkgs.append(name)
            return pkgs

        if fam == "debian":
            return [parts[1] for line in lines
                    if (parts := line.strip().split()) and line.strip().startswith("Remv ")
                    and len(parts) >= 2 and self.valid(parts[1])]

        if fam == "fedora":
            pkgs = []
            for line in lines:
                line = line.strip()
                if not line or line.startswith("Last metadata") or line.startswith("Extra"):
                    continue
                name = re.sub(r"^\d+:", "", line)
                name = re.sub(r"-\d.*$", "", name)
                if name and self.valid(name):
                    pkgs.append(name)
            return pkgs

        return [name for line in lines if (name := line.strip()) and self.valid(name)]

    def get_kernel_headers_pkg(self) -> str:
        try:
            kv  = os.uname().release
            fam = self.family()
            if fam == "arch":
                for tag in ("lts", "zen", "hardened", "rt", "xanmod", "cachyos", "bore", "tkg"):
                    if tag in kv.lower():
                        return f"linux-{tag}-headers"
                return "linux-headers"
            if fam == "debian": return f"linux-headers-{kv}"
            if fam == "fedora": return f"kernel-devel-{kv}"
            if fam == "suse":   return "kernel-default-devel"
            if fam == "void":   return "linux-headers"
            if fam == "alpine": return "linux-headers"
            if fam == "gentoo": return "sys-kernel/linux-headers"
            if fam == "solus":  return "linux-headers"
        except Exception as exc:
            logger.error("kernel headers pkg: %s", exc)
        return self._kernel_pkg

    @staticmethod
    def detect_session() -> str | None:
        for var in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
            for part in os.getenv(var, "").split(":"):
                match = _SESSION_LOWER.get(part.strip().lower())
                if match:
                    return match
        try:
            _MAX_PROCS = 2000
            _count = 0
            with os.scandir("/proc") as it:
                for entry in it:
                    if not entry.name.isdigit():
                        continue
                    _count += 1
                    if _count > _MAX_PROCS:
                        break
                    try:
                        comm = Path(f"/proc/{entry.name}/comm").read_text().strip().lower()
                    except OSError:
                        continue
                    if comm in _WM_PROCS:
                        return _WM_PROCS[comm]
        except Exception as err:
            logger.error("Error detect_session: %s", err)
        return None

    def get_shell_package_name(self, shell_name: str) -> str:
        if shell_name.lower() in ("pwsh", "powershell"):
            return "powershell-bin" if self.family() == "arch" else "powershell"
        return _SHELL_PKG_MAP.get(shell_name.lower(), shell_name.lower())

    def get_shell_binary_name(self, shell_name: str) -> str:
        pkg = self.get_shell_package_name(shell_name)
        return _SHELL_BINARIES.get(pkg, pkg)

    def get_ssh_packages(self)       -> list[Any] | None | Any: return _lookup(_SSH_PKGS, self.family())
    def get_samba_packages(self)     -> list[Any] | None | Any: return _lookup(_SAMBA_PKGS, self.family())
    def get_bluetooth_packages(self) -> list[Any] | None | Any: return _lookup(_BT_PKGS, self.family())
    def get_cron_packages(self)      -> list[Any] | None | Any: return _lookup(_CRON_PKGS, self.family())

    def get_ssh_service_name(self)   -> str: return _SSH_SVC.get(self.family())   or _SSH_SVC[None]
    def get_samba_service_name(self) -> str: return _SAMBA_SVC.get(self.family()) or _SAMBA_SVC[None]
    def get_cron_service_name(self)  -> str: return _CRON_SVC.get(self.family())  or _CRON_SVC[None]

    @staticmethod
    def get_printer_packages()  -> list: return ["cups", "ghostscript", "system-config-printer", "gutenprint"]
    @staticmethod
    def get_firewall_packages() -> list: return ["ufw"]
    @staticmethod
    def get_at_packages()       -> list: return ["at"]
    @staticmethod
    def get_flatpak_packages()  -> list: return ["flatpak"]
    @staticmethod
    def get_snap_packages()     -> list: return ["snapd"]
    @staticmethod
    def flatpak_add_flathub() -> str:
        return "sudo flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo"

    @staticmethod
    def detect_bootloader() -> str:
        if Path("/boot/grub/grub.cfg").exists():
            return "grub"
        if Path("/boot/loader/loader.conf").exists() or Path("/boot/loader/entries").is_dir():
            return "systemd-boot"
        return "unknown"

    def get_ucode_package(self) -> str | None:
        cpu_vendor = self.detect_cpu_vendor()
        if not cpu_vendor:
            return None
        fam = self.family()
        pkg = _UCODE_PKGS.get(cpu_vendor, {}).get(fam, "")
        if not pkg or pkg.startswith("hardware.cpu"):
            return None
        return pkg

    @staticmethod
    def detect_cpu_vendor() -> str | None:
        try:
            text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if line.startswith("vendor_id"):
                    val = line.split(":", 1)[-1].strip().lower()
                    if "intel" in val:
                        return "intel"
                    if "amd" in val:
                        return "amd"
        except OSError as exc:
            logger.warning("CPU vendor detection: %s", exc)
        return None

    @staticmethod
    def detect_running_kernel_variant() -> str:
        try:
            release = os.uname().release.lower()
            if "hardened" in release:
                return "linux-hardened"
            if "lts" in release:
                return "linux-lts"
            if "zen" in release:
                return "linux-zen"
        except OSError:
            pass
        return "linux"

    def detect_installed_kernel_variants(self) -> set[str]:
        installed: set[str] = set()
        for pkg in ARCH_KERNEL_VARIANTS.keys():
            if self.package_is_installed(pkg):
                installed.add(pkg)
        return installed

    @staticmethod
    def detect_system_default_kernel(bootloader: str) -> str | None:
        found = None
        if bootloader == "systemd-boot":
            try:
                conf_path = Path("/boot/loader/loader.conf")
                if conf_path.exists():
                    text = conf_path.read_text(encoding="utf-8", errors="replace")
                    for line in text.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split(None, 1)
                        if len(parts) == 2 and parts[0].lower() == "default":
                            val = parts[1].strip().lower()
                            if "lts" in val:
                                found = "linux-lts"
                            elif "zen" in val:
                                found = "linux-zen"
                            elif "hardened" in val:
                                found = "linux-hardened"
                            elif "arch" in val or "linux" in val:
                                found = "linux"
                            break
            except OSError:
                pass

        elif bootloader == "grub":
            try:
                grub_def_path = Path("/etc/default/grub")
                default_val = ""
                if grub_def_path.exists():
                    text = grub_def_path.read_text(encoding="utf-8", errors="replace")
                    for line in text.splitlines():
                        if line.strip().upper().startswith("GRUB_DEFAULT="):
                            default_val = line.split("=", 1)[1].strip().strip('"\'').lower()
                            break

                if default_val == "saved":
                    try:
                        output = subprocess.check_output(["grub-editenv", "list"],
                                                         stderr=subprocess.DEVNULL, text=True)
                        for line in output.splitlines():
                            if line.startswith("saved_entry="):
                                default_val = line.split("=", 1)[1].lower()
                                break
                    except (subprocess.SubprocessError, FileNotFoundError):
                        pass

                if "lts" in default_val:
                    found = "linux-lts"
                elif "zen" in default_val:
                    found = "linux-zen"
                elif "hardened" in default_val:
                    found = "linux-hardened"
                elif default_val and default_val not in ("0", "saved"):
                    found = "linux"
            except OSError:
                pass

        return found or LinuxDistroHelper.detect_running_kernel_variant()