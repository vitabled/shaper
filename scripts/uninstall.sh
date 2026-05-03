#!/usr/bin/env bash
set -euo pipefail
systemctl stop remna-node-quota 2>/dev/null || true
systemctl disable remna-node-quota 2>/dev/null || true
rm -f /etc/systemd/system/remna-node-quota.service
systemctl daemon-reload
rm -rf /opt/remna-node-quota
printf 'Config and DB are kept in /etc/remna-node-quota and /var/lib/remna-node-quota\n'
