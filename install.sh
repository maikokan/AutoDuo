#!/usr/bin/env bash
# AutoDuo installer — one-liner install from GitHub.
# Usage: curl -fsSL https://raw.githubusercontent.com/maikokan/AutoDuo/main/install.sh | sudo bash
set -euo pipefail

REPO_URL="https://github.com/maikokan/AutoDuo.git"
INSTALL_DIR="/opt/autoduo"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must run as root. Try: sudo bash install.sh" >&2
    exit 1
fi

echo "╔══════════════════════════════════════════════════════╗"
echo "║         AutoDuo — One-Liner Installer               ║"
echo "╚══════════════════════════════════════════════════════╝"
echo

# Show disclaimer
echo "WARNING: AutoDuo defeats Duo 2FA by design."
echo "See https://github.com/maikokan/AutoDuo#threat-model for details."
echo
read -p "Type 'I ACCEPT' to continue: " CONFIRM
if [[ "$CONFIRM" != "I ACCEPT" ]]; then
    echo "Aborted."
    exit 1
fi
echo

# Clone or update
if [[ -d "$INSTALL_DIR" ]]; then
    echo "AutoDuo already installed at $INSTALL_DIR"
    echo "Pulling latest..."
    git -C "$INSTALL_DIR" pull --ff-only || true
else
    echo "Cloning AutoDuo..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

# Install
cd "$INSTALL_DIR"
chmod +x scripts/autoduo scripts/install.sh scripts/uninstall.sh

# Install systemd service, wrapper, dependencies
bash scripts/install.sh

echo
echo "╔══════════════════════════════════════════════════════╗"
echo "║              Install Complete!                       ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Next: sudo autoduo setup                            ║"
echo "╚══════════════════════════════════════════════════════╝"