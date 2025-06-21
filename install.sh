#!/bin/bash

set -e

echo "=== Linux-Backup-Helper: Installation Routine ==="

# Install inxi if not already present
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
        echo "Please install inxi manually for your distribution!"
        exit 1
    fi
else
    echo "inxi is already installed."
fi

# Install Python dependencies
if [ -f requirements.txt ]; then
    echo "Installing Python dependencies..."
    pip install -r requirements.txt
fi

echo "=== Installation complete ==="