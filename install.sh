#!/bin/bash

set -e

echo "=== Linux-Backup-Helper: Installation ==="

# ── inxi ──────────────────────────────────────────────────────────────────────
if ! command -v inxi &> /dev/null; then
    echo "Installing inxi..."
    if [ -f /etc/debian_version ]; then
        sudo apt-get update
        sudo apt-get install -y inxi
    elif [ -f /etc/fedora-release ]; then
        sudo dnf install -y inxi
    elif [ -f /etc/arch-release ]; then
        sudo pacman -Sy --noconfirm inxi
    else
        echo "Warning: Could not detect distro. Please install inxi manually."
    fi
else
    echo "inxi already installed."
fi

# ── smbclient (optional – only required for SMB/Samba share support) ──────────
# On Arch-based systems, smbclient is a hard dependency of the samba package
# and is already present when samba is installed. On Debian/Ubuntu and Fedora
# it must be installed separately.
if ! command -v smbclient &> /dev/null; then
    echo ""
    echo "smbclient was not found on your system."
    echo "It is only required if you want to copy files to/from SMB/Samba network shares."
    read -r -p "Do you want to install smbclient? [y/N] " response
    case "$response" in
        [yY][eE][sS]|[yY])
            if [ -f /etc/debian_version ]; then
                sudo apt-get update
                sudo apt-get install -y smbclient
            elif [ -f /etc/fedora-release ]; then
                sudo dnf install -y samba-client
            elif [ -f /etc/arch-release ]; then
                sudo pacman -Sy --noconfirm samba
            else
                echo "Could not detect your distribution."
                echo "Please install smbclient manually if you need SMB/Samba support."
            fi
            ;;
        *)
            echo "Skipping smbclient. SMB/Samba share features will not be available."
            ;;
    esac
else
    echo "smbclient already installed."
fi

# ── Python dependencies ───────────────────────────────────────────────────────
if [ -f requirements.txt ]; then
    echo "Installing Python dependencies..."
    pip install -r requirements.txt
fi

echo ""
echo "=== Installation complete ==="
echo "Run the application with:  python main.py"
