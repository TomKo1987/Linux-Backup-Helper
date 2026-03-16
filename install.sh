#!/bin/bash

set -e

echo "=== Linux-Backup-Helper: Installation ==="

# ── Detect distro ─────────────────────────────────────────────────────────────
if [ -f /etc/arch-release ]; then
    DISTRO="arch"
elif [ -f /etc/debian_version ]; then
    DISTRO="debian"
elif [ -f /etc/fedora-release ]; then
    DISTRO="fedora"
elif [ -f /etc/os-release ]; then
    . /etc/os-release
    case "${ID_LIKE:-$ID}" in
        *arch*)   DISTRO="arch"   ;;
        *debian*) DISTRO="debian" ;;
        *fedora*|*rhel*) DISTRO="fedora" ;;
        *suse*)   DISTRO="suse"   ;;
        *)        DISTRO="unknown" ;;
    esac
else
    DISTRO="unknown"
fi

echo "Detected distribution family: $DISTRO"
# ── Python version check ──────────────────────────────────────────────────────
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "Error: Python 3.10 or newer is required (found $PY_VER)."
    echo "Please upgrade Python and re-run this script."
    exit 1
fi
echo "Python $PY_VER detected — OK"



# ── inxi ──────────────────────────────────────────────────────────────────────
if ! command -v inxi &> /dev/null; then
    echo "Installing inxi..."
    case "$DISTRO" in
        arch)    sudo pacman -Sy --noconfirm inxi ;;
        debian)  sudo apt-get update && sudo apt-get install -y inxi ;;
        fedora)  sudo dnf install -y inxi ;;
        suse)    sudo zypper install -y inxi ;;
        *)       echo "Warning: Could not detect distro. Please install inxi manually." ;;
    esac
else
    echo "inxi already installed."
fi

# ── smbclient (optional – only required for SMB/Samba share support) ──────────
if ! command -v smbclient &> /dev/null; then
    echo ""
    echo "smbclient was not found on your system."
    echo "It is only required if you want to copy files to/from SMB/Samba network shares."
    read -r -p "Do you want to install smbclient? [y/N] " response
    case "$response" in
        [yY][eE][sS]|[yY])
            case "$DISTRO" in
                arch)   sudo pacman -Sy --noconfirm samba ;;
                debian) sudo apt-get update && sudo apt-get install -y smbclient ;;
                fedora) sudo dnf install -y samba-client ;;
                suse)   sudo zypper install -y samba-client ;;
                *)      echo "Could not detect your distribution."
                        echo "Please install smbclient manually if you need SMB/Samba support." ;;
            esac
            ;;
        *)
            echo "Skipping smbclient. SMB/Samba share features will not be available."
            ;;
    esac
else
    echo "smbclient already installed."
fi

# ── Python dependencies ───────────────────────────────────────────────────────

echo "Installing Python dependencies..."

case "$DISTRO" in
    arch)
        echo "Arch detected — installing via pacman..."
        sudo pacman -Sy --noconfirm --needed \
            python-pyqt6 \
            python-keyring \
            python-secretstorage
        ;;
    debian)
        echo "Debian/Ubuntu detected — installing via apt..."
        sudo apt-get update
        sudo apt-get install -y \
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
            pip3 install --user -r requirements.txt
        elif command -v python3 &> /dev/null; then
            python3 -m pip install --user -r requirements.txt
        else
            echo "Error: No pip or Python 3 found."
            echo "Please install the following packages manually:"
            echo "  PyQt6, keyring, secretstorage"
            exit 1
        fi
        ;;
esac

echo ""
echo "=== Installation complete ==="
echo "Run the application with:  python3 main.py"