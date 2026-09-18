import concurrent.futures
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from constants import USER_SHELLS, ARCH_KERNEL_VARIANTS, PKG_NAME_RE
from state import logger
from translations import tr

__all__ = ["ARCH_KERNEL_VARIANTS", "SESSIONS", "USER_SHELLS", "LinuxDistroHelper", "distro_family", "is_valid_pkg_name"]

_MIN_PARALLEL = 5
_INSTALLED_TTL = 30.0

_DISTROS_ARCH = {"arch", "manjaro", "garuda", "endeavouros", "omarchy", "archman", "rebornos", "cachyos", "artix",
                 "arcolinux", "blendos", "crystal", "archcraft", "archbang", "archlabs", "steamos", "parabola",
                 "hyperbola", "obarun", "arch32", "archarm", "archlinux", "arch-linux", "athena", "biglinux",
                 "bluestar", "chakra", "ctlos", "instantos", "kaos", "mabox", "msys2", "puppyarch", "snal",
                 "tearch", "ninjaos"}

_DISTROS_DEBIAN = {"debian", "ubuntu", "pop", "pop-os", "popos", "mint", "linuxmint", "elementary", "lmde", "kali",
                   "parrot", "zorin", "zorinos", "mxlinux", "mx", "antix", "raspbian", "raspios", "peppermint",
                   "deepin", "lite", "q4os", "linuxlite", "tails", "siduction", "sparky", "sparkylinux", "bodhi",
                   "bunsenlabs", "pureos", "ubuntu-budgie", "devuan", "refracta", "kubuntu", "xubuntu", "lubuntu",
                   "ubuntu-mate", "neon", "tuxedo", "trisquel", "pika", "pikaos", "vanilla", "vanillaos", "nitrux",
                   "endless", "endlessos", "kdeneon", "crunchbang", "handylinux", "armbian", "proxmox", "pve",
                   "univention", "astra", "rosa-debian", "wattos", "voyager", "spiral", "spirallinux"}

_VER_PKG = re.compile(r"[-_]\d[\w.+~:-]*$")

_DISTROS_FEDORA = {"fedora", "rhel", "centos", "rocky", "almalinux", "nobara", "ultramarine", "mageia",
                   "openmandriva", "amzn", "ol", "oracle", "scientific", "springdale", "eurolinux", "navylinux",
                   "circle", "miraclelinux", "openela", "risios", "qubes", "berry", "korora", "rhel-ha"}

_DISTROS_FEDORA_ATOMIC = {"fedora-silverblue", "fedora-kinoite", "fedora-sericea", "fedora-onyx", "silverblue",
                          "kinoite", "bazzite", "bluefin", "aurora", "ucore", "ublue", "nobara-atomic",
                          "fedora-iot", "fedora-coreos"}

_DISTROS_SUSE = {"opensuse", "opensuse-leap", "opensuse-tumbleweed", "opensuse-slowroll", "suse", "sled", "sles",
                 "geckolinux", "gecko"}

_DISTROS_SUSE_IMMUTABLE = {"opensuse-microos", "opensuse-aeon", "opensuse-kalpa", "opensuse-leap-micro",
                           "sle-micro", "opensuse-microos-desktop"}

_DISTROS_GENTOO = {"gentoo", "funtoo", "calculate", "sabayon", "redcore", "pentoo"}

_DISTROS_SLACKWARE = {"slackware", "salix", "porteus", "slax", "absolute", "zenwalk"}

_DISTROS_ALPINE = {"alpine", "postmarketos"}

_DISTROS_VOID = {"void"}

_DISTROS_NIXOS = {"nixos"}

_DISTROS_SOLUS = {"solus"}


def _run_capture(cmd: list[str], timeout: int = 25) -> list[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]
    except Exception as exc:
        logger.warning("_run_capture %s: %s", cmd[0] if cmd else "?", exc)
        return []


def is_valid_pkg_name(name: str) -> bool:
    if not isinstance(name, str):
        return False
    stripped = name.strip()
    return bool(stripped) and len(stripped) <= 255 and bool(PKG_NAME_RE.match(stripped))


_DISTRO_FAMILY_MAP: dict[str, str] = {
    distro_id: family
    for family, distro_set in [("arch", _DISTROS_ARCH), ("debian", _DISTROS_DEBIAN), ("fedora", _DISTROS_FEDORA),
                               ("fedora-atomic", _DISTROS_FEDORA_ATOMIC),
                               ("suse", _DISTROS_SUSE), ("suse-immutable", _DISTROS_SUSE_IMMUTABLE),
                               ("gentoo", _DISTROS_GENTOO), ("slackware", _DISTROS_SLACKWARE),
                               ("void", _DISTROS_VOID), ("nixos", _DISTROS_NIXOS), ("alpine", _DISTROS_ALPINE),
                               ("solus", _DISTROS_SOLUS)] for distro_id in distro_set}

del (_DISTROS_ARCH, _DISTROS_DEBIAN, _DISTROS_FEDORA, _DISTROS_FEDORA_ATOMIC, _DISTROS_SUSE,
     _DISTROS_SUSE_IMMUTABLE, _DISTROS_GENTOO, _DISTROS_SLACKWARE, _DISTROS_ALPINE, _DISTROS_VOID,
     _DISTROS_NIXOS, _DISTROS_SOLUS)


_FAMILY_ALIASES: dict[str, str] = {
    "arch": "arch", "debian": "debian", "ubuntu": "debian", "fedora": "fedora", "rhel": "fedora",
    "centos": "fedora", "suse": "suse", "opensuse": "suse", "gentoo": "gentoo", "alpine": "alpine",
    "void": "void", "nixos": "nixos", "slackware": "slackware", "solus": "solus",
}

_BINARY_FAMILY: tuple[tuple[str, str], ...] = (
    ("transactional-update", "suse-immutable"),
    ("rpm-ostree", "fedora-atomic"),
    ("pacman", "arch"),
    ("apt-get", "debian"),
    ("dnf", "fedora"),
    ("zypper", "suse"),
    ("xbps-install", "void"),
    ("emerge", "gentoo"),
    ("eopkg", "solus"),
    ("apk", "alpine"),
    ("slackpkg", "slackware"),
    ("nix-env", "nixos"),
    ("yum", "amazon2"),
)


_SHELL_BINARIES: dict[str, str] = {"nushell": "nu", "powershell": "pwsh", "powershell-bin": "pwsh"}

_SHELL_PKG_MAP: dict[str, str] = {"bash": "bash", "zsh": "zsh", "fish": "fish", "elvish": "elvish",
                                  "nushell": "nushell", "xonsh": "xonsh", "ngs": "ngs"}

SESSIONS = ["KDE", "GNOME", "XFCE", "Cinnamon", "MATE", "LXDE", "LXQt", "Budgie", "Deepin", "Openbox", "i3", "Sway",
            "Hyprland", "bspwm", "dwm", "awesome", "qtile", "xmonad", "Wayfire", "River", "niri", "COSMIC"]

_SESSION_LOWER: dict[str, str] = {s.lower(): s for s in SESSIONS}

_PKG_MGR_NAME: dict[str, str] = {"arch": "pacman", "debian": "apt", "fedora": "dnf", "amazon2": "yum",
                                 "suse": "zypper", "solus": "eopkg", "void": "xbps", "gentoo": "emerge",
                                 "nixos": "nix-env", "alpine": "apk", "slackware": "slackpkg",
                                 "suse-immutable": "transactional-update", "fedora-atomic": "rpm-ostree",
                                 "unknown": "unknown"}


def _nixos_check(p: str) -> list[str]:
    pattern = f"^{re.escape(p)}-[0-9]"
    return ["sh", "-c", "nix-env -q --installed 2>/dev/null | grep -qE -- " + shlex.quote(pattern)]


def _slackware_check(p: str) -> list[str]:
    return ["sh", "-c", f"ls /var/log/packages/{shlex.quote(p)}-[0-9]* >/dev/null 2>&1"]


def _solus_check(p: str) -> list[str]:
    pattern = f"^{re.escape(p)}[[:space:]]*-[[:space:]]"
    return ["sh", "-c", "eopkg list-installed -N 2>/dev/null | grep -qE -- " + shlex.quote(pattern)]


def _gentoo_check(p: str) -> list[str]:
    return ["sh", "-c", f"qlist -Ie -- {shlex.quote(p)} 2>/dev/null | grep -q ."]


def _alpine_check(p: str) -> list[str]:
    return ["sh", "-c", f"apk info -e -- {shlex.quote(p)} 2>/dev/null | grep -q ."]


_PKG: dict[str, dict[str, Any]] = {
    "arch": {
        "check": lambda p: ["pacman", "-Qi", p],
        "install": "sudo pacman -S --needed --noconfirm {p}",
        "update": "sudo pacman -Syu --noconfirm",
        "remove": "sudo pacman -Rns --noconfirm {p}",
        "clean": "sudo pacman -Scc --noconfirm",
        "orphans": "pacman -Qdtq",
        "has_aur": True,
        "kernel": "linux-headers",
    },
    "debian": {
        "check": lambda p: ["sh", "-c", f"dpkg-query -W -f='${{Status}}' {shlex.quote(p)} 2>/dev/null | grep -q '^install ok installed$'"],
        "install": "sudo env DEBIAN_FRONTEND=noninteractive apt-get install -yq -o Dpkg::Options::='--force-confdef' -o Dpkg::Options::='--force-confold' {p}",
        "update": "sudo env DEBIAN_FRONTEND=noninteractive apt-get update && sudo env DEBIAN_FRONTEND=noninteractive apt-get upgrade -yq -o Dpkg::Options::='--force-confdef' -o Dpkg::Options::='--force-confold'",
        "remove": "sudo env DEBIAN_FRONTEND=noninteractive apt-get autoremove -yq {p}",
        "clean": "sudo apt-get clean && sudo apt-get autoremove -y",
        "orphans": "apt-get --dry-run autoremove",
        "has_aur": False,
        "kernel": "linux-headers-generic",
    },
    "fedora": {
        "check": lambda p: ["rpm", "-q", p],
        "install": "sudo dnf install -y {p}",
        "update": "sudo dnf upgrade -y",
        "remove": "sudo dnf remove -y {p}",
        "clean": "sudo dnf clean all && sudo dnf autoremove -y",
        "orphans": "dnf repoquery --unneeded",
        "has_aur": False,
        "kernel": "kernel-devel",
    },
    "amazon2": {
        "check": lambda p: ["rpm", "-q", p],
        "install": "sudo yum install -y {p}",
        "update": "sudo yum update -y",
        "remove": "sudo yum remove -y {p}",
        "clean": "sudo yum clean all",
        "orphans": "",
        "has_aur": False,
        "kernel": "kernel-devel",
    },
    "suse": {
        "check": lambda p: ["rpm", "-q", p],
        "install": "sudo zypper --non-interactive install {p}",
        "update": "sudo zypper --non-interactive update",
        "remove": "sudo zypper --non-interactive remove {p}",
        "clean": "sudo zypper clean --all",
        "orphans": "zypper --no-refresh packages --unneeded",
        "has_aur": False,
        "kernel": "kernel-default-devel",
    },
    "void": {
        "check": lambda p: ["xbps-query", p],
        "install": "sudo xbps-install -y {p}",
        "update": "sudo xbps-install -Su",
        "remove": "sudo xbps-remove -y {p}",
        "clean": "sudo xbps-remove -Oo",
        "orphans": "xbps-query -O",
        "has_aur": False,
        "kernel": "linux-headers",
    },
    "gentoo": {
        "check": _gentoo_check,
        "install": "sudo emerge --ask=n {p}",
        "update": "sudo emerge --sync && sudo emerge -uDU @world",
        "remove": "sudo emerge --depclean {p}",
        "clean": "sudo eclean-dist --deep",
        "orphans": "",
        "has_aur": False,
        "kernel": "sys-kernel/linux-headers",
    },
    "nixos": {
        "check": _nixos_check,
        "install": "nix-env -iA nixpkgs.{p}",
        "update": "sudo nixos-rebuild switch --upgrade",
        "remove": "nix-env -e {p}",
        "clean": "sudo nix-collect-garbage -d",
        "orphans": "",
        "has_aur": False,
        "kernel": "linuxPackages.kernel",
    },
    "alpine": {
        "check": _alpine_check,
        "install": "sudo apk add {p}",
        "update": "sudo apk update && sudo apk upgrade",
        "remove": "sudo apk del {p}",
        "clean": "sudo apk cache clean",
        "orphans": "apk info --orphaned",
        "has_aur": False,
        "kernel": "linux-headers",
    },
    "slackware": {
        "check": _slackware_check,
        "install": "sudo slackpkg -batch=on -default_answer=y install {p}",
        "update": "sudo slackpkg update && sudo slackpkg install-new && sudo slackpkg upgrade-all",
        "remove": "sudo removepkg {p}",
        "clean": "sudo mkdir -p /var/cache/packages && sudo find /var/cache/packages -mindepth 1 -delete",
        "orphans": "",
        "has_aur": False,
        "kernel": "kernel-headers",
    },
    "solus": {
        "check": _solus_check,
        "install": "sudo eopkg install -y {p}",
        "update": "sudo eopkg upgrade -y",
        "remove": "sudo eopkg remove -y {p}",
        "clean": "sudo eopkg delete-cache",
        "orphans": "",
        "has_aur": False,
        "kernel": "linux-headers",
    },
    "suse-immutable": {
        "check": lambda p: ["rpm", "-q", p],
        "install": "echo 'This is a transactional (read-only) openSUSE system. "
                   "Install packages with: sudo transactional-update pkg install {p} (a reboot is required "
                   "afterwards). System Manager package installation is not supported here.'",
        "update": "echo 'This is a transactional (read-only) openSUSE system. "
                  "Update with: sudo transactional-update up / dup (a reboot is required afterwards). "
                  "System Manager updates are not supported here.'",
        "remove": "echo 'This is a transactional (read-only) openSUSE system. "
                  "Remove packages with: sudo transactional-update pkg remove {p} (a reboot is required "
                  "afterwards). System Manager package removal is not supported here.'",
        "clean": "echo 'Cache cleaning is not applicable on a transactional (read-only) openSUSE system.'",
        "orphans": "",
        "has_aur": False,
        "kernel": "kernel-default-devel",
    },
    "fedora-atomic": {
        "check": lambda p: ["rpm", "-q", p],
        "install": "echo 'This is an image based (rpm-ostree) system. "
                   "Layer packages with: sudo rpm-ostree install {p} (a reboot is required afterwards). "
                   "System Manager package installation is not supported here.'",
        "update": "echo 'This is an image based (rpm-ostree) system. "
                  "Update with: sudo rpm-ostree upgrade (a reboot is required afterwards). "
                  "System Manager updates are not supported here.'",
        "remove": "echo 'This is an image based (rpm-ostree) system. "
                  "Remove layered packages with: sudo rpm-ostree uninstall {p} (a reboot is required "
                  "afterwards). System Manager package removal is not supported here.'",
        "clean": "echo 'Cache cleaning is not applicable on an image based (rpm-ostree) system. "
                 "Use: sudo rpm-ostree cleanup -m'",
        "orphans": "",
        "has_aur": False,
        "kernel": "kernel-devel",
    },
    "unknown": {
        "check": lambda p: ["which", p],
        "install": "echo 'No package manager detected: {p}'",
        "update": "echo 'Update not available'",
        "remove": "echo 'Remove not available: {p}'",
        "clean": "echo 'Clean not available'",
        "orphans": "",
        "has_aur": False,
        "kernel": "linux-headers",
    },
}


_RPM_LIST = ["rpm", "-qa", "--qf", "%{NAME}\n"]

_INSTALLED_LIST_CMD: dict[str, list[str]] = {
    "arch": ["pacman", "-Qq"],
    "debian": ["sh", "-c", "dpkg-query -W -f='${Status}|${Package}\\n' 2>/dev/null "
                           "| awk -F'|' '$1==\"install ok installed\"{print $2}'"],
    "fedora": _RPM_LIST,
    "fedora-atomic": _RPM_LIST,
    "amazon2": _RPM_LIST,
    "suse": _RPM_LIST,
    "suse-immutable": _RPM_LIST,
    "void": ["sh", "-c", "xbps-query -l 2>/dev/null | awk '{print $2}'"],
    "alpine": ["sh", "-c", "apk info 2>/dev/null"],
    "gentoo": ["sh", "-c", "qlist -I -C 2>/dev/null"],
    "nixos": ["sh", "-c", "nix-env -q 2>/dev/null"],
    "slackware": ["sh", "-c", "ls -1 /var/log/packages 2>/dev/null"],
    "solus": ["sh", "-c", "eopkg list-installed -N 2>/dev/null"],
}

_STRIP_VERSION_FAMILIES = frozenset({"void", "nixos", "slackware", "gentoo"})


_SSH_PKGS: dict = {
    "debian":    ["openssh-server"],
    "fedora":    ["openssh-server"],
    "fedora-atomic": ["openssh-server"],
    "amazon2":   ["openssh-server"],
    "suse":      ["openssh"],
    "suse-immutable": ["openssh"],
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
    "fedora-atomic": ["samba", "samba-common"],
    "amazon2":   ["samba", "samba-common"],
    "suse":      ["samba"],
    "suse-immutable": ["samba"],
    "arch":      ["samba"],
    "void":      ["samba"],
    "alpine":    ["samba"],
    "gentoo":    ["net-fs/samba"],
    "nixos":     ["samba"],
    "slackware": ["samba"],
    "solus":     ["samba"],
    None:        ["samba"],
}
_CRON_SVC = {"debian": "cron", "fedora": "crond", "fedora-atomic": "crond", "amazon2": "crond", "suse": "cron",
             "suse-immutable": "cron", "alpine": "crond", None: "cronie"}
_CRON_PKGS: dict = {
    "debian": ["cron"],
    "fedora": ["cronie", "cronie-anacron"],
    "fedora-atomic": ["cronie"],
    "amazon2": ["cronie"],
    "suse":   ["cron"],
    "suse-immutable": ["cron"],
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
    "fedora-atomic": ["bluez", "bluez-tools"],
    "amazon2": ["bluez"],
    "suse":   ["bluez"],
    "suse-immutable": ["bluez"],
    "void":   ["bluez"],
    "alpine": ["bluez"],
    "gentoo": ["net-wireless/bluez"],
    "solus":  ["bluez"],
    None:     ["bluez", "bluez-tools"],
}


_PRINTER_PKGS: dict = {
    "gentoo": ["net-print/cups", "app-text/ghostscript-gpl", "app-admin/system-config-printer", "net-print/gutenprint"],
    "nixos":  ["cups", "ghostscript", "system-config-printer", "gutenprint"],
    "alpine": ["cups", "ghostscript"],
    None:     ["cups", "ghostscript", "system-config-printer", "gutenprint"],
}
_AT_PKGS: dict = {
    "gentoo": ["sys-process/at"],
    None:     ["at"],
}
_FLATPAK_PKGS: dict = {
    "gentoo": ["sys-apps/flatpak"],
    "fedora-atomic": [],
    None:     ["flatpak"],
}
_SNAP_PKGS: dict = {
    "gentoo": ["app-containers/snapd"],
    "nixos":  [],
    "slackware": [],
    "fedora-atomic": [],
    "suse-immutable": [],
    None:     ["snapd"],
}


_UCODE_PKGS: dict[str, dict[str, str]] = {
    "intel": {
        "arch":     "intel-ucode",
        "debian":   "intel-microcode",
        "fedora":   "microcode_ctl",
        "amazon2":  "microcode_ctl",
        "suse":     "ucode-intel",
        "suse-immutable": "ucode-intel",
        "void":     "intel-ucode",
        "alpine":   "intel-ucode",
        "gentoo":   "sys-firmware/intel-microcode",
        "nixos":    "hardware.cpu.intel.updateMicrocode",
        "fedora-atomic": "", "slackware": "", "solus": "", "unknown": "",
    },
    "amd": {
        "arch":     "amd-ucode",
        "debian":   "amd64-microcode",
        "fedora":   "microcode_ctl",
        "amazon2":  "microcode_ctl",
        "suse":     "ucode-amd",
        "suse-immutable": "ucode-amd",
        "void":     "linux-firmware-amd",
        "alpine":   "linux-firmware-amd",
        "gentoo":   "sys-firmware/linux-firmware",
        "nixos":    "hardware.cpu.amd.updateMicrocode",
        "fedora-atomic": "", "slackware": "", "solus": "", "unknown": "",
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

_KERNEL_TAGS: tuple[str, ...] = ("lts", "zen", "hardened", "rt", "xanmod", "cachyos", "bore", "tkg")
_KERNEL_TOKEN_RE = re.compile(r"[-._+]")


def _kernel_tag(release: str) -> str | None:
    tokens = set(_KERNEL_TOKEN_RE.split(release.lower()))
    for tag in _KERNEL_TAGS:
        if tag in tokens:
            return tag
    return None


def distro_family(distro_id: str) -> str: return _DISTRO_FAMILY_MAP.get(distro_id, distro_id)


def _lookup(table: dict[Any, list[Any]], family: str) -> list[Any]:
    result: list[Any] | None = table.get(family)
    if result is not None:
        return result
    return table.get(None) or []


_session_cache: list = []
_bootloader_cache: list = []
_esp_cache: list = []
_cpu_vendor_cache: list = []


class LinuxDistroHelper:

    def __init__(self) -> None:
        self.distro_id_raw, self.distro_pretty_name, self.distro_version_id, self._id_like = self._read_os_release()
        self.distro_id = self.distro_id_raw
        self._installed_names: frozenset[str] | None = None
        self._installed_ts: float = 0.0
        self._installed_lock = threading.Lock()
        self._init_pkg()

    @staticmethod
    def _read_os_release() -> tuple[str, str, str, str]:
        d_id = d_pretty = d_like = d_version_id = ""
        parsed = False
        last_exc: Exception | None

        for path in ("/etc/os-release", "/usr/lib/os-release"):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        k, sep, v = line.partition("=")
                        if not sep:
                            continue
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k == "ID":
                            d_id = v.lower()
                        elif k == "PRETTY_NAME":
                            d_pretty = v
                        elif k == "ID_LIKE":
                            d_like = v.lower()
                        elif k == "VERSION_ID":
                            d_version_id = v
                parsed = True
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                continue

        if not parsed:
            logger.error("os-release: %s", last_exc)

        if not d_pretty:
            d_pretty = d_id.capitalize() if d_id else tr("Unknown Linux Distribution")

        return (d_id or "unknown"), d_pretty, d_version_id, d_like

    def _resolve_family(self) -> str:
        raw_id = self.distro_id_raw
        family = _DISTRO_FAMILY_MAP.get(raw_id)
        if family:
            return family

        for candidate in self._id_like.split():
            candidate = candidate.strip()
            family = _DISTRO_FAMILY_MAP.get(candidate) or _FAMILY_ALIASES.get(candidate)
            if family:
                logger.info("distro '%s' unknown; resolved family '%s' via ID_LIKE '%s'", raw_id, family, candidate)
                return family

        probed = self._probe_family_by_binaries()
        if probed:
            logger.info("distro '%s' unknown; resolved family '%s' by package manager probe", raw_id, probed)
            return probed

        logger.warning("Unknown distro '%s', using generic commands.", raw_id)
        return "unknown"

    @staticmethod
    def _probe_family_by_binaries() -> str | None:
        for binary, family in _BINARY_FAMILY:
            if shutil.which(binary):
                return family
        return None

    def _init_pkg(self) -> None:
        raw_family = self._resolve_family()

        if raw_family == "fedora":
            if self.distro_id_raw == "amzn":
                major = self.distro_version_id.split(".", 1)[0]
                if major.isdigit() and int(major) < 2023:
                    raw_family = "amazon2"
            elif Path("/run/ostree-booted").exists() or (
                    shutil.which("rpm-ostree") and not shutil.which("dnf")):
                raw_family = "fedora-atomic"

        if raw_family == "suse" and shutil.which("transactional-update") and not shutil.which("zypper"):
            raw_family = "suse-immutable"

        cfg = _PKG.get(raw_family)
        if cfg is None:
            self._family: str = "unknown"
            cfg = _PKG["unknown"]
            logger.warning("No package profile for family '%s', using generic commands.", raw_family)
        else:
            self._family = raw_family

        self.distro_id = self.distro_id_raw
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

    _UNSUPPORTED_FAMILIES = frozenset({"unknown", "suse-immutable", "fedora-atomic"})

    def pkg_mgmt_supported(self) -> bool:
        return self.family() not in self._UNSUPPORTED_FAMILIES

    @staticmethod
    def valid(name: str) -> bool:
        return is_valid_pkg_name(name)

    def invalidate_installed_cache(self) -> None:
        with self._installed_lock:
            self._installed_names = None
            self._installed_ts = 0.0

    def installed_names(self, *, refresh: bool = False) -> frozenset[str] | None:
        now = time.monotonic()
        with self._installed_lock:
            if not refresh and self._installed_names is not None and now - self._installed_ts < _INSTALLED_TTL:
                return self._installed_names

        cmd = _INSTALLED_LIST_CMD.get(self.family())
        if not cmd:
            return None

        raw = _run_capture(cmd, timeout=40)
        if not raw:
            return None

        fam = self.family()
        names: set[str] = set()
        for line in raw:
            name = line.split(" - ", 1)[0].strip() if fam == "solus" else line
            if not name:
                continue
            if fam in _STRIP_VERSION_FAMILIES:
                name = _VER_PKG.sub("", name).strip()
            if not name:
                continue
            names.add(name)
            if "/" in name:
                names.add(name.rsplit("/", 1)[1])

        result = frozenset(names)
        with self._installed_lock:
            self._installed_names = result
            self._installed_ts = time.monotonic()
        return result

    @staticmethod
    def _in_set(pkg: str, names: frozenset[str]) -> bool:
        if pkg in names:
            return True
        return "/" in pkg and pkg.rsplit("/", 1)[1] in names

    def package_is_installed(self, pkg: str, *, allow_bulk: bool = True) -> bool:
        if not self.valid(pkg):
            return False
        pkg = pkg.strip()
        if allow_bulk:
            names = self.installed_names()
            if names is not None:
                return self._in_set(pkg, names)
        try:
            r = subprocess.run(self._check_fn(pkg), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL, timeout=10, check=False)
            return r.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning("pkg check '%s': %s", pkg, exc)
            return False

    def filter_not_installed(self, packages: list[str], *, refresh: bool = False) -> list[str]:
        valid = [p.strip() for p in packages if self.valid(p)]
        if not valid:
            return []

        names = self.installed_names(refresh=refresh)
        if names is not None:
            return [p for p in valid if not self._in_set(p, names)]

        if len(valid) < _MIN_PARALLEL:
            return [p for p in valid if not self.package_is_installed(p, allow_bulk=False)]
        return self._parallel_check(valid)

    def _parallel_check(self, packages: list[str]) -> list[str]:
        workers = min(8, len(packages))
        adaptive_timeout = max(15.0, len(packages) * 2.0)
        results: dict[str, bool] = dict.fromkeys(packages, False)
        pending: dict[concurrent.futures.Future, str]
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        try:
            pending = {pool.submit(self.package_is_installed, p, allow_bulk=False): p for p in packages}
            try:
                for fut in concurrent.futures.as_completed(pending, timeout=adaptive_timeout):
                    try:
                        results[pending[fut]] = fut.result()
                    except Exception as exc:
                        logger.warning("parallel check '%s': %s", pending[fut], exc)
            except concurrent.futures.TimeoutError:
                logger.warning("parallel package check timed out after %.0fs", adaptive_timeout)
                for fut, pkg in pending.items():
                    if fut.done():
                        try:
                            results[pkg] = fut.result()
                        except Exception as exc:
                            logger.warning("parallel check timeout '%s': %s", pkg, exc)
        except Exception as exc:
            logger.error("parallel check failed: %s", exc)
            return [p for p in packages if not self.package_is_installed(p, allow_bulk=False)]
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        return [pkg for pkg in packages if not results[pkg]]

    def get_pkg_install_cmd(self, package: str) -> str: return self._install.format(p=shlex.quote(package))
    def get_update_system_cmd(self)             -> str: return self._update
    def get_clean_cache_cmd(self)               -> str: return self._clean
    def get_find_orphans_cmd(self)              -> str: return self._orphans

    _DIRECT_ORPHAN_REMOVAL: dict[str, str] = {
        "solus": "sudo eopkg remove-orphans -y",
    }

    def get_direct_orphan_removal_cmd(self) -> str:
        return self._DIRECT_ORPHAN_REMOVAL.get(self.family(), "")

    def get_batch_install_cmd(self, packages: list[str]) -> str:
        if not packages:
            return ""
        if self.family() == "nixos":
            return "nix-env -iA " + " ".join("nixpkgs." + shlex.quote(p) for p in packages)
        return self._install.format(p=" ".join(shlex.quote(p) for p in packages))

    def get_batch_remove_cmd(self, packages: list[str]) -> str:
        if not packages:
            return ""
        safe_pkgs = " ".join(shlex.quote(p) for p in packages)
        if self.family() == "nixos":
            return "nix-env -e " + safe_pkgs
        return self._remove.format(p=safe_pkgs)

    def parse_orphan_output(self, raw: str) -> list[str]:
        fam   = self.family()
        lines = raw.strip().splitlines()

        if fam == "suse":
            pkgs = []
            for line in lines:
                line = line.strip()
                if line.startswith(("i ", "i+")):
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) >= 3:
                        name = parts[2].strip()
                        if name and self.valid(name):
                            pkgs.append(name)
            return pkgs

        if fam == "debian":
            return [parts[1] for line in lines
                    if (parts := line.strip().split()) and line.strip().startswith("Remv ")
                    and len(parts) >= 2 and self.valid(parts[1])]

        if fam in ("fedora", "amazon2"):
            pkgs = []
            for line in lines:
                line = line.strip()
                if not line or line.startswith(("Last metadata", "Extra")):
                    continue
                name = re.sub(r"^\d+:", "", line)
                name = re.sub(r"-\d.*$", "", name)
                if name and self.valid(name):
                    pkgs.append(name)
            return pkgs

        if fam == "void":
            pkgs = []
            for line in lines:
                name = _VER_PKG.sub("", line.strip()).strip()
                if name and self.valid(name):
                    pkgs.append(name)
            return pkgs

        return [name for line in lines if (name := line.strip()) and self.valid(name)]

    def get_kernel_headers_pkg(self) -> str:
        try:
            kv  = os.uname().release
            fam = self.family()
            if fam == "arch":
                tag = _kernel_tag(kv)
                return f"linux-{tag}-headers" if tag else "linux-headers"
            if fam == "debian":  return f"linux-headers-{kv}"
            if fam in ("fedora", "amazon2", "fedora-atomic"): return f"kernel-devel-{kv}"
            if fam in ("suse", "suse-immutable"): return "kernel-default-devel"
            if fam in ("void", "alpine", "solus"): return "linux-headers"
            if fam == "gentoo":  return "sys-kernel/linux-headers"
        except Exception as exc:
            logger.error("kernel headers pkg: %s", exc)
        return self._kernel_pkg

    def get_explicitly_installed_packages(self) -> tuple[list[str], list[str]]:
        fam = self.family()

        if fam == "arch":
            foreign = set(_run_capture(["pacman", "-Qqm"]))
            explicit = set(_run_capture(["pacman", "-Qqe"]))
            aur = sorted(foreign & explicit)
            basic = sorted(explicit - foreign)
            return basic, aur

        if fam == "debian":
            return sorted(_run_capture(["apt-mark", "showmanual"])), []

        if fam in ("fedora", "fedora-atomic"):
            raw = _run_capture(["sh", "-c",
                                "dnf repoquery --userinstalled -q --qf '%{name}' 2>/dev/null"])
            if not raw:
                raw = _run_capture(["sh", "-c", "rpm -qa --qf '%{NAME}\\n' 2>/dev/null"])
            return sorted(set(raw)), []

        if fam == "amazon2":
            raw = _run_capture(["sh", "-c", "yumdb search reason user 2>/dev/null"])
            names = []
            for entry in raw:
                entry = entry.strip()
                if not entry or ":" not in entry and "-" not in entry:
                    continue
                entry = entry.split(":", 1)[1] if re.match(r"^\d+:", entry) else entry
                name = _VER_PKG.sub("", entry).strip()
                if name:
                    names.append(name)
            return sorted({n for n in names if self.valid(n)}), []

        if fam in ("suse", "suse-immutable"):
            raw = _run_capture(["sh", "-c",
                                "zypper se --installed-only 2>/dev/null "
                                "| awk -F'|' '/^i /{gsub(/ /,\"\",$2);if($2)print $2}'"])
            return sorted(raw), []

        if fam == "void":
            raw = _run_capture(["xbps-query", "-m"])
            return sorted(_VER_PKG.sub("", entry).strip() for entry in raw if entry), []

        if fam == "alpine":
            return sorted(_run_capture(["apk", "info"])), []

        if fam == "gentoo":
            return sorted(_run_capture(["sh", "-c", "qlist -I -C 2>/dev/null"])), []

        if fam == "nixos":
            raw = _run_capture(["nix-env", "-q"])
            return sorted(_VER_PKG.sub("", entry).strip() for entry in raw if entry), []

        if fam == "slackware":
            raw = _run_capture(["sh", "-c", "ls /var/log/packages/ 2>/dev/null"])
            return sorted(_VER_PKG.sub("", entry).strip() for entry in raw if entry), []

        if fam == "solus":
            raw = _run_capture(["eopkg", "list-installed", "-N"])
            names = [line.split(" - ", 1)[0].strip() for line in raw]
            return sorted(n for n in names if n), []

        raise RuntimeError(f"Package detection not supported for distro family '{fam}'.")

    @staticmethod
    def detect_session() -> str | None:
        if _session_cache:
            return _session_cache[0]

        result: str | None = None
        for var in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
            for part in os.getenv(var, "").split(":"):
                token = part.strip().lower()
                if token.startswith("x-"):
                    token = token[2:]
                match = _SESSION_LOWER.get(token)
                if match:
                    result = match
                    break
            if result:
                break

        if result is None:
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
                            with open(f"/proc/{entry.name}/comm", encoding="utf-8", errors="replace") as fh:
                                comm = fh.read().strip().lower()
                        except OSError:
                            continue
                        if comm in _WM_PROCS:
                            result = _WM_PROCS[comm]
                            break
            except Exception as err:
                logger.error("Error detect_session: %s", err)

        _session_cache.append(result)
        return result

    @staticmethod
    def systemd_unit_exists(unit: str) -> bool:
        if not shutil.which("systemctl"):
            return False
        try:
            r = subprocess.run(["systemctl", "cat", "--no-pager", "--", unit],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL, timeout=10, check=False)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def get_shell_package_name(self, shell_name: str) -> str:
        if shell_name.lower() in ("pwsh", "powershell"):
            return "powershell-bin" if self.family() == "arch" else "powershell"
        return _SHELL_PKG_MAP.get(shell_name.lower(), shell_name.lower())

    def get_shell_binary_name(self, shell_name: str) -> str:
        pkg = self.get_shell_package_name(shell_name)
        return _SHELL_BINARIES.get(pkg, pkg)

    def get_ssh_packages(self)       -> list[Any]: return _lookup(_SSH_PKGS, self.family())
    def get_samba_packages(self)     -> list[Any]: return _lookup(_SAMBA_PKGS, self.family())
    def get_bluetooth_packages(self) -> list[Any]: return _lookup(_BT_PKGS, self.family())
    def get_cron_packages(self)      -> list[Any]: return _lookup(_CRON_PKGS, self.family())

    def get_ssh_service_name(self)   -> str: return _SSH_SVC.get(self.family())   or _SSH_SVC[None]
    def get_samba_service_name(self) -> str: return _SAMBA_SVC.get(self.family()) or _SAMBA_SVC[None]
    def get_cron_service_name(self)  -> str: return _CRON_SVC.get(self.family())  or _CRON_SVC[None]

    _FIREWALL_PKGS: dict = {
        "debian": ["ufw"], "arch": ["ufw"], "void": ["ufw"], "gentoo": ["net-firewall/ufw"],
        "fedora": ["firewalld"], "fedora-atomic": ["firewalld"], "amazon2": ["firewalld"],
        "suse": ["firewalld"], "suse-immutable": ["firewalld"],
        "alpine": ["ufw"], "solus": ["ufw"], "nixos": ["ufw"], "slackware": [], None: ["ufw"],
    }
    _FIREWALL_SVC: dict = {"fedora": "firewalld", "fedora-atomic": "firewalld", "amazon2": "firewalld",
                           "suse": "firewalld", "suse-immutable": "firewalld", None: "ufw"}

    _FIREWALL_PKGS_BY_BACKEND: dict[str, dict] = {
        "ufw":       {"gentoo": ["net-firewall/ufw"], "slackware": [], None: ["ufw"]},
        "firewalld": {"gentoo": ["net-firewall/firewalld"], "slackware": [], None: ["firewalld"]},
    }

    def get_firewall_packages(self) -> list[Any]: return _lookup(self._FIREWALL_PKGS, self.family())
    def get_firewall_service_name(self) -> str: return self._FIREWALL_SVC.get(self.family()) or self._FIREWALL_SVC[None]
    def firewall_supported(self) -> bool: return bool(self.get_firewall_packages())

    def get_firewall_packages_for(self, backend: str) -> list[Any]:
        table = self._FIREWALL_PKGS_BY_BACKEND.get((backend or "").strip().lower())
        if table is None:
            return self.get_firewall_packages()
        return _lookup(table, self.family())

    _NTP_PKGS: dict = {
        "debian": ["systemd-timesyncd"], "arch": [], "fedora": ["chrony"], "fedora-atomic": [], "amazon2": ["chrony"],
        "suse": ["chrony"], "suse-immutable": ["chrony"], "void": ["chrony"], "alpine": ["chrony"],
        "gentoo": ["net-misc/chrony"], "solus": ["chrony"], "nixos": [], "slackware": [], None: [],
    }

    _NTP_SVC: dict[str, str] = {
        "debian": "systemd-timesyncd", "arch": "systemd-timesyncd", "nixos": "systemd-timesyncd",
        "fedora": "chronyd", "fedora-atomic": "chronyd", "amazon2": "chronyd", "suse": "chronyd",
        "suse-immutable": "chronyd", "solus": "chronyd", "gentoo": "chronyd", "void": "chronyd",
        "alpine": "chronyd",
    }

    def get_ntp_packages(self) -> list[Any]: return _lookup(self._NTP_PKGS, self.family())

    def get_ntp_service_name(self) -> str:
        svc = self._NTP_SVC.get(self.family())
        if svc:
            return svc
        if shutil.which("timedatectl"):
            return "systemd-timesyncd"
        return "chronyd"

    def ntp_supported(self) -> bool:
        if self.family() in self._NTP_SVC:
            return True
        return bool(shutil.which("timedatectl") or shutil.which("chronyd") or shutil.which("chronyc"))

    def get_printer_packages(self) -> list[Any]: return _lookup(_PRINTER_PKGS, self.family())
    def get_at_packages(self)      -> list[Any]: return _lookup(_AT_PKGS, self.family())
    def get_flatpak_packages(self) -> list[Any]: return _lookup(_FLATPAK_PKGS, self.family())
    def get_snap_packages(self)    -> list[Any]: return _lookup(_SNAP_PKGS, self.family())

    @staticmethod
    def flatpak_add_flathub() -> str:
        return "sudo flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo"

    @staticmethod
    def detect_esp() -> Path:
        if _esp_cache:
            return _esp_cache[0]
        result = LinuxDistroHelper._detect_esp_uncached()
        _esp_cache.append(result)
        return result

    @staticmethod
    def _detect_esp_uncached() -> Path:
        try:
            out = subprocess.check_output(
                ["bootctl", "--print-esp-path"], stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, text=True, timeout=10
            ).strip()
            if out:
                p = Path(out)
                if p.is_dir():
                    return p
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            pass

        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 3 and parts[2].lower() in ("vfat", "fat", "msdos"):
                        mp = Path(parts[1])
                        if (mp / "EFI").is_dir() or (mp / "loader" / "loader.conf").exists():
                            return mp
        except OSError:
            pass

        for candidate in (Path("/efi"), Path("/boot/efi"), Path("/boot")):
            if (candidate / "loader" / "loader.conf").exists() or (candidate / "EFI").is_dir():
                return candidate
        return Path("/boot")

    @staticmethod
    def detect_uki_mode(esp: Path | None = None) -> bool:
        resolved_esp: Path = esp if esp is not None else LinuxDistroHelper.detect_esp()
        efi_linux = resolved_esp / "EFI" / "Linux"
        try:
            return efi_linux.is_dir() and any(efi_linux.glob("*.efi"))
        except OSError:
            return False

    @staticmethod
    def _iter_esp_candidates() -> list[Path]:
        seen: set[Path] = set()
        candidates: list[Path] = []

        def _add(_p: Path) -> None:
            try:
                r = _p.resolve()
            except OSError:
                r = _p
            if r not in seen and _p.exists():
                seen.add(r)
                candidates.append(_p)

        for cmd in (["bootctl", "--print-esp-path"],
                    ["sudo", "-n", "bootctl", "--print-esp-path"]):
            try:
                out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                              text=True, timeout=10).strip()
                if out:
                    _add(Path(out))
                    break
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                pass

        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    mp = Path(parts[1])
                    fstype = parts[2].lower() if len(parts) >= 3 else ""
                    if fstype in ("vfat", "fat", "fat32", "msdos", "exfat"):
                        _add(mp)
                    elif mp.name in ("efi", "esp", "boot") or str(mp) in ("/efi", "/boot/efi", "/esp"):
                        _add(mp)
        except OSError:
            pass

        for p in (Path("/efi"), Path("/esp"), Path("/boot/efi"), Path("/boot")):
            _add(p)

        return candidates

    @staticmethod
    def _path_has_systemd_boot(esp: Path) -> bool:
        try:
            if (esp / "loader" / "loader.conf").exists():
                return True
            if (esp / "loader" / "entries").is_dir():
                return True
            efi_linux = esp / "EFI" / "Linux"
            if efi_linux.is_dir() and any(efi_linux.glob("*.efi")):
                return True
            for sd_efi in (
                esp / "EFI" / "systemd" / "systemd-bootx64.efi",
                esp / "EFI" / "systemd" / "systemd-bootaa64.efi",
                esp / "EFI" / "BOOT" / "BOOTX64.EFI",
            ):
                if sd_efi.exists():
                    if "BOOT" in str(sd_efi):
                        if not (sd_efi.parent / "grubx64.efi").exists():
                            return True
                    else:
                        return True
        except OSError:
            pass
        return False

    @staticmethod
    def detect_bootloader() -> str:
        if _bootloader_cache:
            return _bootloader_cache[0]
        result = LinuxDistroHelper._detect_bootloader_uncached()
        _bootloader_cache.append(result)
        return result

    @staticmethod
    def _detect_bootloader_uncached() -> str:
        for cmd in (["bootctl", "is-installed"],
                    ["sudo", "-n", "bootctl", "is-installed"]):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
                if r.returncode == 0:
                    answer = r.stdout.strip().lower()
                    if answer == "yes":
                        return "systemd-boot"
                    if answer == "no":
                        break
            except FileNotFoundError:
                break
            except (OSError, subprocess.SubprocessError):
                pass

        for esp in LinuxDistroHelper._iter_esp_candidates():
            if LinuxDistroHelper._path_has_systemd_boot(esp):
                return "systemd-boot"

        if (Path("/sys/firmware/efi/efivars").exists()
                and not Path("/boot/grub/grub.cfg").exists()):
            try:
                out = subprocess.check_output(["efibootmgr"], stderr=subprocess.DEVNULL,
                                              stdin=subprocess.DEVNULL, text=True, timeout=10)
                if "Linux Boot Manager" in out or "systemd" in out.lower():
                    return "systemd-boot"
            except (FileNotFoundError, subprocess.SubprocessError, OSError):
                pass

        for grub_cfg in (Path("/boot/grub/grub.cfg"), Path("/boot/grub2/grub.cfg"),
                         Path("/boot/efi/EFI/grub/grub.cfg")):
            if grub_cfg.exists():
                return "grub"

        return "unknown"

    def get_ucode_package(self) -> str | None:
        cpu_vendor = self.detect_cpu_vendor()
        if not cpu_vendor:
            return None
        pkg = _UCODE_PKGS.get(cpu_vendor, {}).get(self.family(), "")
        if not pkg or pkg.startswith("hardware.cpu"):
            return None
        return pkg

    @staticmethod
    def detect_cpu_vendor() -> str | None:
        if _cpu_vendor_cache:
            return _cpu_vendor_cache[0]
        result: str | None = None
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith(("vendor_id", "CPU implementer")):
                        val = line.split(":", 1)[-1].strip().lower()
                        if "intel" in val:
                            result = "intel"
                        elif "amd" in val:
                            result = "amd"
                        if result:
                            break
        except OSError as exc:
            logger.warning("CPU vendor detection: %s", exc)
        _cpu_vendor_cache.append(result)
        return result

    @staticmethod
    def detect_running_kernel_variant() -> str:
        try:
            tag = _kernel_tag(os.uname().release)
        except OSError:
            tag = None
        if tag in ("hardened", "lts", "zen"):
            return f"linux-{tag}"
        return "linux"

    def detect_installed_kernel_variants(self) -> set[str]:
        if self.family() != "arch":
            return set()
        names = self.installed_names()
        if names is not None:
            return {v for v in ARCH_KERNEL_VARIANTS if v in names}
        return {v for v in ARCH_KERNEL_VARIANTS if self.package_is_installed(v, allow_bulk=False)}

    @staticmethod
    def _variant_from_text(value: str) -> str | None:
        val = value.lower()
        if "lts" in val:
            return "linux-lts"
        if "zen" in val:
            return "linux-zen"
        if "hardened" in val:
            return "linux-hardened"
        return None

    @staticmethod
    def detect_system_default_kernel(bootloader: str) -> str | None:
        found = None
        if bootloader == "systemd-boot":
            try:
                esp = LinuxDistroHelper.detect_esp()
                conf_path = esp / "loader" / "loader.conf"
                if conf_path.exists():
                    text = conf_path.read_text(encoding="utf-8", errors="replace")
                    for line in text.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split(None, 1)
                        if len(parts) == 2 and parts[0].lower() == "default":
                            val = parts[1].strip().lower()
                            if val in ("@saved", "@current"):
                                break
                            found = LinuxDistroHelper._variant_from_text(val)
                            if not found and ("arch" in val or "linux" in val):
                                found = "linux"
                            break
            except OSError:
                pass

        elif bootloader == "grub":
            try:
                grub_def_path = Path("/etc/default/grub")
                default_val = ""
                if grub_def_path.exists():
                    try:
                        with open(grub_def_path, "r", encoding="utf-8", errors="replace") as f:
                            for line in f:
                                if line.strip().upper().startswith("GRUB_DEFAULT="):
                                    default_val = line.split("=", 1)[1].strip().strip('"\'').lower()
                                    break
                    except OSError:
                        default_val = ""

                if default_val == "saved":
                    try:
                        output = subprocess.check_output(["grub-editenv", "list"], stderr=subprocess.DEVNULL,
                                                         stdin=subprocess.DEVNULL, text=True, timeout=10)
                        for line in output.splitlines():
                            if line.startswith("saved_entry="):
                                default_val = line.split("=", 1)[1].lower()
                                break
                    except (subprocess.SubprocessError, FileNotFoundError, OSError):
                        default_val = ""

                found = LinuxDistroHelper._variant_from_text(default_val)
                if not found and default_val and default_val not in ("0", "saved"):
                    found = "linux"
            except OSError:
                pass

        return found or LinuxDistroHelper.detect_running_kernel_variant()
