#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="remna-node-quota"
INSTALL_DIR="/opt/${APP_NAME}"
CONFIG_DIR="/etc/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
REPO_RAW_BASE_DEFAULT=""

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
blue() { printf '\033[34m%s\033[0m\n' "$*"; }

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    red "Run as root: sudo bash install.sh"
    exit 1
  fi
}

ask() {
  local prompt="$1" default="${2:-}" value
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " value || true
    printf '%s' "${value:-$default}"
  else
    read -r -p "$prompt: " value || true
    printf '%s' "$value"
  fi
}

ask_secret() {
  local prompt="$1" value
  read -r -s -p "$prompt: " value || true
  printf '\n' >&2
  printf '%s' "$value"
}

ask_yes_no() {
  local prompt="$1" default="${2:-y}" value
  local suffix="[Y/n]"
  [[ "$default" == "n" ]] && suffix="[y/N]"
  read -r -p "$prompt $suffix: " value || true
  value="${value:-$default}"
  [[ "$value" =~ ^[YyДд]$ ]]
}

install_deps() {
  blue "Installing dependencies..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y python3 python3-venv python3-pip curl ca-certificates jq
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip curl ca-certificates jq
    python3 -m ensurepip --upgrade || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip curl ca-certificates jq
    python3 -m ensurepip --upgrade || true
  else
    red "Unsupported package manager. Install python3, python3-venv/python3-pip, curl, jq manually."
    exit 1
  fi
}

copy_sources() {
  blue "Installing files to ${INSTALL_DIR}..."
  mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$DATA_DIR"

  local src_dir
  src_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  rsync -a --delete \
    --exclude '.git' \
    --exclude 'venv' \
    --exclude '__pycache__' \
    "$src_dir/" "$INSTALL_DIR/" 2>/dev/null || {
      cp -a "$src_dir/." "$INSTALL_DIR/"
    }

  python3 -m venv "$INSTALL_DIR/venv"
  "$INSTALL_DIR/venv/bin/python" -m pip install --upgrade pip
  "$INSTALL_DIR/venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"
}

write_config_interactive() {
  blue "Interactive configuration"

  local xray_bin xray_api panel_url token users_endpoint inbounds period dry_run verify_tls status_allowlist refresh
  xray_bin="$(ask "Path to xray binary" "/usr/local/bin/xray")"
  xray_api="$(ask "Xray API server" "127.0.0.1:10085")"
  panel_url="$(ask "Remnawave Panel URL, for example https://panel.example.com" "")"
  token="$(ask_secret "Remnawave API Bearer token")"
  users_endpoint="$(ask "Remnawave users endpoint" "/api/users")"
  inbounds="$(ask "Inbound tags for blocking, comma-separated" "VLESS_TCP_REALITY,VLESS_XHTTP")"
  period="$(ask "Quota period: day/week/month/forever" "month")"
  refresh="$(ask "Refresh users from panel every N seconds" "300")"

  if ask_yes_no "Start in dry-run mode? Recommended for first launch" "y"; then
    dry_run="true"
  else
    dry_run="false"
  fi

  if ask_yes_no "Verify Remnawave Panel TLS certificate?" "y"; then
    verify_tls="true"
  else
    verify_tls="false"
  fi

  status_allowlist="$(ask "Allowed statuses from panel, comma-separated. Empty = all" "ACTIVE")"

  python3 - "$CONFIG_DIR/config.json" <<PY
import json, sys
path = sys.argv[1]
inbounds = [x.strip() for x in '''$inbounds'''.split(',') if x.strip()]
statuses = [x.strip() for x in '''$status_allowlist'''.split(',') if x.strip()]
config = {
  "xray_bin": '''$xray_bin''',
  "xray_api_server": '''$xray_api''',
  "poll_interval_sec": 20,
  "db_path": "/var/lib/remna-node-quota/quota.db",
  "period": '''$period''',
  "dry_run": $dry_run,
  "inbound_tags": inbounds,
  "remnawave": {
    "enabled": True,
    "base_url": '''$panel_url'''.rstrip('/'),
    "token": '''$token''',
    "users_endpoint": '''$users_endpoint''',
    "page_limit": 100,
    "refresh_interval_sec": int('''$refresh''' or 300),
    "timeout_sec": 20,
    "verify_tls": $verify_tls,
    "status_allowlist": statuses,
    "id_fields": ["uuid", "shortUuid", "username", "email", "subscriptionUuid"],
    "limit_fields": ["dataLimitBytes", "trafficLimitBytes", "usedTrafficBytesLimit", "trafficLimit", "dataLimit"],
    "limit_multiplier": 1.0,
    "default_limit_bytes": 0,
    "fallback_to_local_users": False
  },
  "default_limit_bytes": 0,
  "users": {}
}
with open(path, 'w', encoding='utf-8') as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
print(path)
PY
  chmod 600 "$CONFIG_DIR/config.json"
}

install_service() {
  blue "Installing systemd service..."
  cp "$INSTALL_DIR/systemd/remna-node-quota.service" "$SERVICE_FILE"
  systemctl daemon-reload
  systemctl enable "$APP_NAME"
}

print_xray_hint() {
  cat <<'TXT'

Important: Xray config profile must expose StatsService and HandlerService, for example:

{
  "api": {
    "tag": "api",
    "listen": "127.0.0.1:10085",
    "services": ["HandlerService", "StatsService"]
  },
  "stats": {},
  "policy": {
    "levels": {
      "0": {
        "statsUserUplink": true,
        "statsUserDownlink": true
      }
    }
  }
}

Check stats manually:
  xray api statsquery --server=127.0.0.1:10085 -pattern "user>>>"
TXT
}

main() {
  need_root
  blue "=== remna-node-quota installer ==="
  install_deps
  copy_sources
  write_config_interactive
  install_service

  green "Installed."
  print_xray_hint

  if ask_yes_no "Run one test iteration now?" "y"; then
    "$INSTALL_DIR/venv/bin/python" -m remna_node_quota -c "$CONFIG_DIR/config.json" --once || true
  fi

  if ask_yes_no "Start systemd service now?" "n"; then
    systemctl restart "$APP_NAME"
    green "Service started. Logs: journalctl -u remna-node-quota -f"
  else
    yellow "Service not started. Start later: systemctl start remna-node-quota"
  fi
}

main "$@"
