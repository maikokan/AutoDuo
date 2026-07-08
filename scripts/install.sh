#!/usr/bin/env bash
# scripts/install.sh — install autoduo as a systemd service.
# Idempotent: safe to re-run. Runs as root, no dedicated user.
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
install -d -m 0755 "$VAULT_DIR" "$LOG_DIR"

echo "==> creating venv"
if [[ ! -d "$REPO_DIR/.venv" ]]; then
    python3 -m venv "$REPO_DIR/.venv"
fi
"$REPO_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$REPO_DIR/.venv/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

echo "==> installing systemd unit"
install -m 0644 "$REPO_DIR/systemd/autoduo.service" /etc/systemd/system/autoduo.service

echo "==> installing autoduo wrapper to /usr/local/bin"
install -m 0755 "$REPO_DIR/scripts/autoduo" /usr/local/bin/autoduo

echo "==> reloading systemd"
systemctl daemon-reload

cat <<EOF

INSTALL COMPLETE. Next steps:

  1. Save your Duo activation URL to a file:
       # Use the URL from Duo Admin → Users → Add Device → Tablet → Android
       echo -n 'https://m-XXXXXXXX.duosecurity.com/activate/CODE' > /tmp/act.txt
       chmod 0600 /tmp/act.txt

  2. Enroll (generates RSA keypair, calls Duo API, writes vault):
       python3 -m autoduo.enroll \\
         --vault /var/lib/autoduo/vault.enc \\
         --activation-file /tmp/act.txt \\
         --no-passphrase

  3. Approve the new-device prompt on your real Duo Mobile phone (once).

  4. Enable and start the daemon:
       systemctl enable --now autoduo
       journalctl -u autoduo -f

  To uninstall: ./scripts/uninstall.sh
EOF