from typing import Callable
import concurrent.futures, os, re, shlex, subprocess

from state import logger

__all__ = ["LinuxDistroHelper", "distro_family", "USER_SHELLS", "SESSIONS"]

_MIN_PARALLEL = 5

_ARCH   = {"arch", "manjaro", "garuda", "endeavouros", "omarchy", "archman", "rebornos", "cachyos", "artix",
           "arcolinux", "blendos", "crystal", "archcraft", "archbang", "archlabs"}

_DEBIAN = {"debian", "ubuntu", "pop", "popos", "mint", "linuxmint", "elementary", "lmde", "kali", "parrot", "zorin",
           "zorinos", "mxlinux", "mx", "antix", "raspbian", "peppermint", "deepin", "lite", "linuxlite", "tails",
           "siduction", "sparky", "sparkylinux", "bodhi", "bunsenlabs", "pureos", "devuan", "q4os", "refracta",
           "kubuntu", "xubuntu", "lubuntu", "ubuntu-mate", "ubuntu-budgie"}

_FEDORA = {"fedora", "rhel", "centos", "rocky", "almalinux", "nobara", "ultramarine", "mageia", "openmandriva", "pclinuxos"}

_SUSE   = {"opensuse", "opensuse-leap", "opensuse-tumbleweed", "opensuse-slowroot", "suse", "sled", "sles"}

_VOID      = {"void"}
_GENTOO    = {"gentoo", "funtoo", "calculate", "sabayon"}
_NIXOS     = {"nixos"}
_ALPINE    = {"alpine", "postmarketos"}
_SLACKWARE = {"slackware", "salix", "porteus", "slax"}
_SOLUS     = {"solus"}

USER_SHELLS = ["bash", "fish", "zsh", "elvish", "nushell", "powershell", "xonsh", "ngs"]

_SHELL_BINARIES: dict[str, str] = {
    "nushell":      "nu",
    "powershell":   "pwsh",
    "powershell-bin": "pwsh",
}

_SHELL_PKG_MAP: dict[str, str] = {
    "bash":    "bash",
    "zsh":     "zsh",
    "fish":    "fish",
    "elvish":  "elvish",
    "nushell": "nushell",
    "xonsh":   "xonsh",
    "ngs":     "ngs",
}

SESSIONS = ["KDE", "GNOME", "XFCE", "Cinnamon", "MATE", "LXDE", "LXQt", "Budgie", "Deepin", "Openbox", "i3", "Sway",
            "Hyprland", "bspwm", "dwm", "awesome", "qtile", "xmonad", "Wayfire", "River", "niri", "COSMIC"]

_PKG_MGR_NAME: dict[str, str] = {"arch": "pacman", "debian": "apt", "fedora": "dnf", "suse": "zypper", "void": "xbps-install",
                                 "gentoo": "emerge", "nixos": "nix-env", "alpine": "apk", "slackware": "pkgtool",
                                 "solus": "eopkg", "unknown": "unknown"}


def _nixos_check(p: str) -> list[str]:
    return ["sh", "-c", "nix-env -q --installed 2>/dev/null | grep -qF -- " + shlex.quote(p)]


def _slackware_check(p: str) -> list[str]:
    return ["sh", "-c", f"test -n \"$(ls /var/log/packages/{p}-* 2>/dev/null)\""]


_PKG: dict[str, dict] = {
    "arch": dict(
        check   = lambda p: ["pacman", "-Qi", p],
        install = "sudo pacman -S --noconfirm {p}",
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
_SSH_SVC    = {"debian": "ssh", None: "sshd"}
_SAMBA_SVC  = {"debian": "smbd", None: "smb"}
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
_CRON_SVC   = {"debian": "cron", None: "cronie"}
_CRON_PKGS: dict = {
    "debian":    ["cron"],
    "fedora":    ["cronie", "cronie-anacron"],
    "suse":      ["cron"],
    "arch":      ["cronie"],
    "void":      ["cronie"],
    "alpine":    ["cronie"],
    "nixos":     ["cronie"],
    "solus":     ["cronie"],
    "gentoo":    ["sys-process/cronie"],
    None:        ["cronie"],
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

_PKG_RE  = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._+-]*$")

_WM_PROCS: dict[str, str] = {
    "kwin_wayland":  "KDE",
    "kwin_x11":      "KDE",
    "gnome-shell":   "GNOME",
    "xfce4-session": "XFCE",
    "cinnamon":      "Cinnamon",
    "mate-session":  "MATE",
    "lxsession":     "LXDE",
    "lxqt-session":  "LXQt",
    "budgie-wm":     "Budgie",
    "deepin-session":"Deepin",
    "openbox":       "Openbox",
    "i3":            "i3",
    "sway":          "Sway",
    "hyprland":      "Hyprland",
    "wayfire":       "Wayfire",
    "river":         "River",
    "niri":          "niri",
    "cosmic-comp":   "COSMIC",
    "bspwm":         "bspwm",
    "dwm":           "dwm",
    "awesome":       "awesome",
    "qtile":         "qtile",
    "xmonad":        "xmonad",
}


def distro_family(distro_id: str) -> str:
    if distro_id in _ARCH:      return "arch"
    if distro_id in _DEBIAN:    return "debian"
    if distro_id in _FEDORA:    return "fedora"
    if distro_id in _SUSE:      return "suse"
    if distro_id in _VOID:      return "void"
    if distro_id in _GENTOO:    return "gentoo"
    if distro_id in _NIXOS:     return "nixos"
    if distro_id in _ALPINE:    return "alpine"
    if distro_id in _SLACKWARE: return "slackware"
    if distro_id in _SOLUS:     return "solus"
    return distro_id


def _lookup(table: dict, family: str) -> list:
    return table.get(family) or table.get(None, [])


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
            d_id = os.uname().sysname.lower()

        resolved = d_id or "unknown"
        if distro_family(resolved) == resolved and d_like:
            for candidate in d_like.split():
                candidate = candidate.strip()
                if distro_family(candidate) != candidate:
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
        self._install  = cfg["install"]
        self._update   = cfg["update"]
        self._remove   = cfg["remove"]
        self._clean    = cfg["clean"]
        self._orphans  = cfg["orphans"]
        self.has_aur: bool = cfg["has_aur"]
        self._kernel_pkg   = cfg["kernel"]

    def family(self) -> str:
        return self._family

    def pkg_manager_name(self) -> str:
        return _PKG_MGR_NAME.get(self.family(), "unknown")

    def supports_aur(self) -> bool:
        return self.has_aur

    @staticmethod
    def _valid(name: str) -> bool:
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            return False
        return bool(_PKG_RE.match(name.strip()))

    def package_is_installed(self, pkg: str) -> bool:
        if not self._valid(pkg):
            return False
        try:
            r = subprocess.run(self._check_fn(pkg.strip()), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            return r.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning("pkg check '%s': %s", pkg, exc)
            return False

    def filter_not_installed(self, packages: list[str]) -> list[str]:
        valid = [p.strip() for p in packages if self._valid(p)]
        if not valid:
            return []
        if len(valid) < _MIN_PARALLEL:
            return [p for p in valid if not self.package_is_installed(p)]
        return self._parallel_check(valid)

    def _parallel_check(self, packages: list[str]) -> list[str]:
        workers = min(4, len(packages))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futs    = {pool.submit(self.package_is_installed, p): p for p in packages}
                missing: list[str] = []
                for fut in concurrent.futures.as_completed(futs, timeout=60):
                    pkg = futs[fut]
                    try:
                        if not fut.result():
                            missing.append(pkg)
                    except Exception as exc:
                        logger.warning("parallel check '%s': %s", pkg, exc)
                        missing.append(pkg)
                return missing
        except Exception as exc:
            logger.error("parallel check failed: %s", exc)
            return [p for p in packages if not self.package_is_installed(p)]

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
            attrs = " ".join(f"nixpkgs.{p}" for p in packages)
            return f"nix-env -iA {attrs}"
        if fam == "slackware":
            names = " ".join(packages)
            return f"sudo slackpkg install {names}"
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
                        if name and self._valid(name):
                            pkgs.append(name)
            return pkgs

        if fam == "debian":
            pkgs = []
            for line in lines:
                line = line.strip()
                if line.startswith("Remv "):
                    parts = line.split()
                    if len(parts) >= 2 and self._valid(parts[1]):
                        pkgs.append(parts[1])
            return pkgs

        if fam == "fedora":
            pkgs = []
            for line in lines:
                line = line.strip()
                if not line or line.startswith("Last metadata") or line.startswith("Extra"):
                    continue
                name = re.sub(r"^\d+:", "", line)
                name = re.sub(r"-\d.*$", "", name)
                if name and self._valid(name):
                    pkgs.append(name)
            return pkgs

        pkgs = []
        for line in lines:
            name = line.strip()
            if name and self._valid(name):
                pkgs.append(name)
        return pkgs

    def get_kernel_headers_pkg(self) -> str:
        try:
            kv  = os.uname().release
            fam = self.family()
            if fam == "arch":
                for tag in ("lts", "zen", "hardened", "rt", "xanmod", "cachyos", "bore", "tkg"):
                    if tag in kv.lower():
                        return f"linux-{tag}-headers"
                return "linux-headers"
            if fam == "debian":  return f"linux-headers-{kv}"
            if fam == "fedora":  return f"kernel-devel-{kv}"
            if fam == "suse":    return "kernel-default-devel"
            if fam == "void":    return "linux-headers"
            if fam == "alpine":  return "linux-headers"
            if fam == "gentoo":  return "sys-kernel/linux-headers"
            if fam == "solus":   return "linux-headers"
        except Exception as exc:
            logger.error("kernel headers pkg: %s", exc)
        return self._kernel_pkg

    @staticmethod
    def detect_session() -> str | None:
        lc: dict[str, str] = {s.lower(): s for s in SESSIONS}
        for var in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
            for part in os.getenv(var, "").split(":"):
                match = lc.get(part.strip().lower())
                if match:
                    return match
        try:
            procs = subprocess.check_output(["ps", "-e", "-o", "cmd="], text=True, timeout=3).splitlines()
            proc_set = {os.path.basename(p.split()[0]).strip().lower() for p in procs if p.strip()}
            for proc_name, session in _WM_PROCS.items():
                if proc_name in proc_set:
                    return session
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

    def get_ssh_packages(self)       -> list: return _lookup(_SSH_PKGS,   self.family())
    def get_samba_packages(self)     -> list: return _lookup(_SAMBA_PKGS, self.family())
    def get_bluetooth_packages(self) -> list: return _lookup(_BT_PKGS,    self.family())
    def get_cron_packages(self)      -> list: return _lookup(_CRON_PKGS,  self.family())

    def get_ssh_service_name(self) -> str:
        return _SSH_SVC.get(self.family()) or _SSH_SVC[None]

    def get_samba_service_name(self) -> str:
        return _SAMBA_SVC.get(self.family()) or _SAMBA_SVC[None]

    def get_cron_service_name(self) -> str:
        return _CRON_SVC.get(self.family()) or _CRON_SVC[None]

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
    def flatpak_add_flathub()   -> str:
        return "flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo"
