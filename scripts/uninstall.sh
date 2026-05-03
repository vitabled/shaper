#!/usr/bin/env bash
set -euo pipefail

systemctl disable --now remna-node-quota 2>/dev/null || true
rm -f /etc/systemd/system/remna-node-quota.service
systemctl daemon-reload
rm -rf /opt/remna-node-quota

echo "Removed program files. Config and DB are kept:"
echo "  /etc/remna-node-quota"
echo "  /var/lib/remna-node-quota"
echo "Remove them manually if needed."
