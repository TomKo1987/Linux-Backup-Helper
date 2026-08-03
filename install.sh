#!/usr/bin/env bash
set -e

echo "=== Linux-Backup-Helper: Installation ==="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$SCRIPT_DIR/requirements.txt" ]; then
    echo "Error: requirements.txt not found."
    echo "Please run this script from the Linux-Backup-Helper project directory."
    exit 1
fi

if [ -f /etc/arch-release ]; then
    DISTRO="arch"
elif [ -f /etc/debian_version ]; then
    DISTRO="debian"
elif [ -f /etc/fedora-release ]; then
    DISTRO="fedora"
elif [ -f /etc/os-release ]; then
    . /etc/os-release
    case "${ID_LIKE:-$ID}" in
        *arch*)          DISTRO="arch"    ;;
        *debian*)        DISTRO="debian"  ;;
        *fedora*|*rhel*) DISTRO="fedora"  ;;
        *suse*)          DISTRO="suse"    ;;
        *)               DISTRO="unknown" ;;
    esac
else
    DISTRO="unknown"
fi

echo "Detected distribution family: $DISTRO"

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "Error: Python 3.10 or newer is required (found $PY_VER)."
    echo "Please upgrade Python and re-run this script."
    exit 1
fi
echo "Python $PY_VER detected — OK"

# inxi is used for system information display and is always installed.
# rsync (SSH/remote backup) and smbclient/cifs-utils (SMB share support) are
# only needed for optional backup targets, so they are offered separately below
# rather than installed unconditionally.
echo ""
echo "Installing core system dependencies (inxi)..."
case "$DISTRO" in
    arch)    sudo pacman -Syu --noconfirm --needed inxi ;;
    debian)  sudo DEBIAN_FRONTEND=noninteractive apt-get update -q && \
             sudo DEBIAN_FRONTEND=noninteractive apt-get install -y inxi ;;
    fedora)  sudo dnf install -y inxi ;;
    suse)    sudo zypper install -y inxi ;;
    *)       echo "Warning: Could not detect distro. Please install inxi manually." ;;
esac

if ! command -v rsync &> /dev/null; then
    echo ""
    echo "rsync was not found on your system."
    echo "It is only required if you want to copy files to/from a remote host via SSH."
    read -r -p "Do you want to install rsync? [y/N] " response
    case "$response" in
        [yY][eE][sS]|[yY])
            case "$DISTRO" in
                arch)    sudo pacman -Syu --noconfirm --needed rsync ;;
                debian)  sudo DEBIAN_FRONTEND=noninteractive apt-get update -q && \
                         sudo DEBIAN_FRONTEND=noninteractive apt-get install -y rsync ;;
                fedora)  sudo dnf install -y rsync ;;
                suse)    sudo zypper install -y rsync ;;
                *)       echo "Could not detect your distribution."
                         echo "Please install rsync manually if you need SSH/remote backup support." ;;
            esac
            ;;
        *)
            echo "Skipping rsync. SSH/remote backup features will not be available."
            ;;
    esac
else
    echo "rsync already installed."
fi

if ! command -v smbclient &> /dev/null; then
    echo ""
    echo "smbclient was not found on your system."
    echo "It is only required if you want to copy files to/from SMB/Samba network shares."
    read -r -p "Do you want to install smbclient? [y/N] " response
    case "$response" in
        [yY][eE][sS]|[yY])
            # On Arch, installing the samba package pulls in cifs-utils automatically
            # (cifs-utils is a hard dependency of smbclient there). On Debian/Fedora/
            # openSUSE, smbclient does NOT depend on cifs-utils, so it must be
            # installed explicitly to enable mounting SMB shares via mount.cifs.
            case "$DISTRO" in
                arch)    sudo pacman -Syu --noconfirm --needed samba ;;
                debian)  sudo DEBIAN_FRONTEND=noninteractive apt-get update -q && \
                         sudo DEBIAN_FRONTEND=noninteractive apt-get install -y smbclient cifs-utils ;;
                fedora)  sudo dnf install -y samba-client cifs-utils ;;
                suse)    sudo zypper install -y samba-client cifs-utils ;;
                *)       echo "Could not detect your distribution."
                         echo "Please install smbclient (and cifs-utils, for mounting shares) manually if you need SMB/Samba support." ;;
            esac
            ;;
        *)
            echo "Skipping smbclient/cifs-utils. SMB/Samba share features will not be available."
            ;;
    esac
else
    echo "smbclient already installed."
    if ! command -v mount.cifs &> /dev/null; then
        echo ""
        echo "cifs-utils (mount.cifs) was not found on your system."
        echo "It is only required if you want to mount SMB/Samba shares as a drive."
        read -r -p "Do you want to install cifs-utils? [y/N] " response
        case "$response" in
            [yY][eE][sS]|[yY])
                case "$DISTRO" in
                    arch)    sudo pacman -Syu --noconfirm --needed cifs-utils ;;
                    debian)  sudo DEBIAN_FRONTEND=noninteractive apt-get update -q && \
                             sudo DEBIAN_FRONTEND=noninteractive apt-get install -y cifs-utils ;;
                    fedora)  sudo dnf install -y cifs-utils ;;
                    suse)    sudo zypper install -y cifs-utils ;;
                    *)       echo "Could not detect your distribution."
                             echo "Please install cifs-utils manually if you need to mount SMB shares." ;;
                esac
                ;;
            *)
                echo "Skipping cifs-utils. Mounting SMB shares will not be available."
                ;;
        esac
    else
        echo "cifs-utils already installed."
    fi
fi

if ! command -v ufw &> /dev/null && ! command -v firewall-cmd &> /dev/null; then
    echo ""
    echo "Neither ufw nor firewalld was found on your system."
    echo "One of them is only required if you want to use the Firewall Settings feature."
    read -r -p "Do you want to install a firewall backend now? [y/N] " response
    case "$response" in
        [yY][eE][sS]|[yY])
            case "$DISTRO" in
                arch)    sudo pacman -Syu --noconfirm --needed ufw ;;
                debian)  sudo DEBIAN_FRONTEND=noninteractive apt-get update -q && \
                         sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ufw ;;
                fedora)  sudo dnf install -y firewalld ;;
                suse)    sudo zypper install -y firewalld ;;
                *)       echo "Could not detect your distribution."
                         echo "Please install ufw or firewalld manually if you need the Firewall Settings feature." ;;
            esac
            ;;
        *)
            echo "Skipping firewall backend. The Firewall Settings feature will not be available."
            ;;
    esac
else
    echo "A firewall backend (ufw or firewalld) is already installed."
fi

echo ""
echo "Installing Python dependencies..."

case "$DISTRO" in
    arch)
        echo "Arch detected — installing via pacman..."
        sudo pacman -Syu --noconfirm --needed \
            python-pyqt6 \
            python-keyring \
            python-secretstorage
        ;;
    debian)
        echo "Debian/Ubuntu detected — installing via apt..."
        sudo DEBIAN_FRONTEND=noninteractive apt-get update -q
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
            python3-pyqt6 \
            python3-keyring \
            python3-secretstorage
        ;;
    fedora)
        echo "Fedora detected — installing via dnf..."
        sudo dnf install -y \
            python3-pyqt6 \
            python3-keyring \
            python3-secretstorage
        ;;
    suse)
        echo "openSUSE detected — installing via zypper..."
        sudo zypper install -y \
            python3-PyQt6 \
            python3-keyring \
            python3-SecretStorage
        ;;
    *)
        echo "Unknown distro — attempting pip install..."
        if command -v pip3 &> /dev/null; then
            pip3 install --user --break-system-packages -r "$SCRIPT_DIR/requirements.txt" \
            || pip3 install --user -r "$SCRIPT_DIR/requirements.txt"
        elif command -v python3 &> /dev/null; then
            python3 -m pip install --user --break-system-packages -r "$SCRIPT_DIR/requirements.txt" \
            || python3 -m pip install --user -r "$SCRIPT_DIR/requirements.txt"
        else
            echo "Error: No pip3 or Python 3 found."
            echo "Please install the following packages manually:"
            echo "  PyQt6, keyring, secretstorage"
            exit 1
        fi
        ;;
esac

echo ""
echo "=== Installation complete ==="
echo "Run the application with:  python3 main.py"
