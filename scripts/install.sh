#!/usr/bin/env bash
# scripts/install.sh — install autoduo as a systemd service.
# Idempotent: safe to re-run. Runs as root, no dedicated user.
#
# This installs *the product*, not just one unit: the legacy unit, the
# per-instance template, log rotation for both layouts, and the CLI wrapper.
# Enrollment stays a separate, deliberate step (`autoduo setup`).
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/autoduo}"
VAULT_DIR="/var/lib/autoduo"
LOG_DIR="/var/log/autoduo"

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (got uid $EUID)" >&2
    exit 1
fi

echo "==> installing OS dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip systemd-timesyncd

echo "==> enabling time-sync (signature timestamps must be accurate)"
systemctl enable --now systemd-timesyncd

echo "==> creating runtime directories"
# The vault directory holds the only copy of the device's private key and
# must not be traversable by other users. The log directory is 0750, the
# same mode both units ask for, and the files inside it are 0640.
install -d -m 0700 "$VAULT_DIR"
install -d -m 0750 "$LOG_DIR"

echo "==> creating venv"
if [[ ! -d "$REPO_DIR/.venv" ]]; then
    python3 -m venv "$REPO_DIR/.venv"
fi
"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/.venv/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

echo "==> installing systemd units"
install -m 0644 "$REPO_DIR/systemd/autoduo.service" \
    /etc/systemd/system/autoduo.service
# The template makes every named account a unit of its own. Without it,
# `autoduo setup --instance alice` enrolls fine and then cannot start.
install -m 0644 "$REPO_DIR/systemd/autoduo@.service" \
    /etc/systemd/system/autoduo@.service

echo "==> installing log rotation"
# One config covers the legacy log and the per-instance directories.
install -m 0644 "$REPO_DIR/scripts/logrotate-autoduo" \
    /etc/logrotate.d/autoduo

echo "==> installing autoduo wrapper to /usr/local/bin"
install -m 0755 "$REPO_DIR/scripts/autoduo" /usr/local/bin/autoduo

echo "==> reloading systemd"
systemctl daemon-reload

cat <<EOF

INSTALL COMPLETE. Nothing is enrolled or running yet.

  1. Enroll an account. This asks for the activation URL from
     Duo Admin -> Users -> Add Device -> Tablet -> Android, and keeps it
     out of argv and out of a predictable temp path:

         sudo autoduo setup

     For a second account, give it a name:

         sudo autoduo setup --instance alice

  2. Approve the new-device prompt on your real Duo Mobile phone (once).

  3. Watch it work:

         sudo autoduo instances
         sudo autoduo watch

  Logs:   journalctl -u autoduo -f
          /var/log/autoduo/daemon.log, /var/log/autoduo/audit.log

  To remove one account:  sudo autoduo uninstall
  To remove everything:   sudo autoduo uninstall --all
EOF
