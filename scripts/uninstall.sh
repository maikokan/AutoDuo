#!/usr/bin/env bash
# scripts/uninstall.sh — undo install.sh.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (got uid $EUID)" >&2
    exit 1
fi

if systemctl list-unit-files autoduo.service >/dev/null 2>&1; then
    echo "==> disabling autoduo service"
    systemctl disable --now autoduo || true
fi

echo "==> removing systemd unit"
rm -f /etc/systemd/system/autoduo.service
systemctl daemon-reload

echo "==> removing logrotate config"
rm -f /etc/logrotate.d/autoduo

echo "==> removing /var/lib/autoduo (vault, state) and /var/log/autoduo"
rm -rf /var/lib/autoduo /var/log/autoduo

cat <<EOF

UNINSTALL COMPLETE. /opt/autoduo (source tree) was left in place.
Delete it manually if you want a full removal:
    rm -rf /opt/autoduo
EOF