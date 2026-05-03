#!/usr/bin/env bash
set -euo pipefail

APP_NAME="remna-node-quota"
DEFAULT_REPO_URL="https://github.com/vitabled/shaper.git"
APP_DIR="/opt/${APP_NAME}"
CONFIG_DIR="/etc/${APP_NAME}"
DATA_DIR="/var/lib/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
REPO_URL="${REPO_URL:-$DEFAULT_REPO_URL}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo $0"
  exit 1
fi

log(){ echo -e "\033[1;32m[+]\033[0m $*"; }
warn(){ echo -e "\033[1;33m[!]\033[0m $*"; }
err(){ echo -e "\033[1;31m[-]\033[0m $*" >&2; }

ask(){
  local prompt="$1" default="${2:-}" value=""
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " value
    value="${value:-$default}"
  else
    read -r -p "$prompt: " value
  fi
  value="$(printf '%s' "$value" | tr -d '\r')"
  printf '%s' "$value"
}

ask_secret(){
  local prompt="$1" value=""
  read -r -s -p "$prompt: " value
  echo >&2
  value="$(printf '%s' "$value" | tr -d '\r\n')"
  printf '%s' "$value"
}

ask_yes_no(){
  local prompt="$1" default="${2:-y}" answer=""
  while true; do
    if [[ "$default" == "y" ]]; then
      read -r -p "$prompt [Y/n]: " answer; answer="${answer:-Y}"
    else
      read -r -p "$prompt [y/N]: " answer; answer="${answer:-N}"
    fi
    case "$answer" in
      y|Y|yes|YES|Yes) return 0 ;;
      n|N|no|NO|No) return 1 ;;
      *) echo "Please answer y or n." ;;
    esac
  done
}

require_cmd(){
  local cmd="$1" pkg="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log "Installing missing package: $pkg"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg"
  fi
}

detect_script_dir(){
  local source="${BASH_SOURCE[0]}" dir=""
  if [[ -e "$source" ]]; then
    dir="$(cd "$(dirname "$source")" 2>/dev/null && pwd || true)"
  fi
  echo "$dir"
}

self_bootstrap_if_needed(){
  local script_dir="$1"; shift || true
  if [[ -f "${script_dir}/requirements.txt" && -d "${script_dir}/remna_node_quota" ]]; then
    return 0
  fi
  warn "Installer was started without repository files. Cloning repository."
  require_cmd git git
  local tmp_dir; tmp_dir="$(mktemp -d)"
  git clone --depth=1 "$REPO_URL" "$tmp_dir/${APP_NAME}"
  cd "$tmp_dir/${APP_NAME}"
  exec bash ./install.sh "$@"
}

ensure_ufw_ipsets(){
  log "Checking UFW ipset dependencies"
  if [[ ! -d /etc/ufw ]]; then
    warn "/etc/ufw not found. Skipping."
    return 0
  fi
  if ! grep -Rqs -- '--match-set' /etc/ufw/*.rules 2>/dev/null; then
    log "No ipset references found in /etc/ufw/*.rules"
    return 0
  fi
  require_cmd ipset ipset
  local backup_dir="/root/${APP_NAME}-ufw-ipset-backup-$(date +%F_%H-%M-%S)"
  mkdir -p "$backup_dir"
  cp -a /etc/ufw "$backup_dir/ufw" 2>/dev/null || true
  ipset save > "$backup_dir/ipset.save" 2>/dev/null || true
  log "Backup created: $backup_dir"
  local rules_file family set_names set_name
  for rules_file in /etc/ufw/*.rules; do
    [[ -f "$rules_file" ]] || continue
    grep -qs -- '--match-set' "$rules_file" || continue
    case "$(basename "$rules_file")" in *6.rules) family="inet6" ;; *) family="inet" ;; esac
    set_names="$(grep -hoE -- '--match-set[[:space:]]+[A-Za-z0-9_.:-]+' "$rules_file" 2>/dev/null | awk '{print $2}' | sort -u)"
    [[ -n "$set_names" ]] || continue
    while IFS= read -r set_name; do
      [[ -n "$set_name" ]] || continue
      if ipset list "$set_name" >/dev/null 2>&1; then
        log "ipset exists, keeping unchanged: $set_name"
      else
        log "Creating missing ipset: $set_name, family=$family"
        ipset create "$set_name" hash:net family "$family" hashsize 1024 maxelem 100000 -exist || warn "Failed to create $set_name"
      fi
    done <<< "$set_names"
  done
  mkdir -p /etc/iptables 2>/dev/null || true
  ipset save > /etc/iptables/ipsets 2>/dev/null || true
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi "Status: active"; then
    ufw --force reload || warn "UFW reload failed. Check backup: $backup_dir"
  fi
}

install_os_dependencies(){
  log "Installing OS dependencies"
  ensure_ufw_ipsets
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip curl ca-certificates jq ipset rsync docker.io
  ensure_ufw_ipsets
}

container_exists(){ docker ps --format '{{.Names}}' | grep -Fxq "$1"; }

prepare_remnanode_container(){
  local container="$1"
  if ! container_exists "$container"; then
    warn "Container '$container' is not running. Skipping container preparation."
    return 0
  fi
  log "Preparing container $container for TLS/gRPC Xray API helper"
  docker exec "$container" sh -lc 'python3 - <<"PY"
import grpc, google.protobuf
print("grpc/protobuf OK")
PY' >/dev/null 2>&1 || docker exec "$container" sh -lc 'apk add --no-cache python3 py3-grpcio py3-protobuf'
  docker cp "${APP_DIR}/remna_node_quota/xray_grpc_helper.py" "$container:/tmp/xray_grpc_helper.py" >/dev/null 2>&1 || true
}

write_config(){
  local config_file="${CONFIG_DIR}/config.json"
  log "Interactive configuration"
  local xray_api_server panel_url panel_token users_endpoint inbound_tags_raw period poll_interval refresh_interval dry_run verify_tls status_allowlist_raw default_limit_gb default_limit_bytes
  local container api_enabled api_listen api_port api_token

  container="$(ask "Remnanode container name" "remnanode")"
  xray_api_server="$(ask "Xray API server inside container" "127.0.0.1:61000")"
  panel_url="$(ask "Remnawave Panel URL" "https://remnapanel.ordinarysiteforcoud.uk")"; panel_url="${panel_url%/}"
  panel_token="$(ask_secret "Remnawave API Bearer token")"
  users_endpoint="$(ask "Remnawave users endpoint" "/api/users")"
  inbound_tags_raw="$(ask "Inbound tags to block, comma-separated" "VLESS_TCP_REALITY-SEL-RU-1")"
  period="$(ask "Quota period: day, week, month, forever" "day")"
  poll_interval="$(ask "Xray stats polling interval in seconds" "20")"
  refresh_interval="$(ask "Remnawave users refresh interval in seconds" "300")"
  if ask_yes_no "Enable dry-run mode? Recommended for first launch" "y"; then dry_run="true"; else dry_run="false"; fi
  if ask_yes_no "Verify Remnawave Panel TLS certificate?" "y"; then verify_tls="true"; else verify_tls="false"; fi
  status_allowlist_raw="$(ask "Allowed user statuses, comma-separated; empty means no filter" "ACTIVE")"
  default_limit_gb="$(ask "Default per-node limit in GB if user limit is missing, 0 means disabled" "10")"
  if ask_yes_no "Enable local HTTP API?" "y"; then api_enabled="true"; else api_enabled="false"; fi
  api_listen="$(ask "HTTP API listen address" "127.0.0.1")"
  api_port="$(ask "HTTP API port" "8765")"
  api_token="$(ask_secret "HTTP API token, leave empty to disable auth")"

  default_limit_bytes="$(DEFAULT_LIMIT_GB="$default_limit_gb" python3 <<'PY'
from decimal import Decimal
import os
print(int(Decimal(os.environ['DEFAULT_LIMIT_GB']) * Decimal(1024) * Decimal(1024) * Decimal(1024)))
PY
)"
  mkdir -p "$CONFIG_DIR"; chmod 700 "$CONFIG_DIR"
  CONFIG_FILE="$config_file" CONTAINER="$container" XRAY_API_SERVER="$xray_api_server" POLL_INTERVAL="$poll_interval" PERIOD="$period" DRY_RUN="$dry_run" INBOUND_TAGS_RAW="$inbound_tags_raw" PANEL_URL="$panel_url" PANEL_TOKEN="$panel_token" USERS_ENDPOINT="$users_endpoint" REFRESH_INTERVAL="$refresh_interval" VERIFY_TLS="$verify_tls" STATUS_ALLOWLIST_RAW="$status_allowlist_raw" DEFAULT_LIMIT_BYTES="$default_limit_bytes" API_ENABLED="$api_enabled" API_LISTEN="$api_listen" API_PORT="$api_port" API_TOKEN="$api_token" python3 <<'PY'
import json, os

def split_csv(v): return [x.strip() for x in v.split(',') if x.strip()]
def b(v): return str(v).strip().lower() in ('1','true','yes','y','on')
config = {
  "xray_bin": "/usr/local/bin/xray",
  "xray_runner": {"mode": "docker_grpc_exec", "container": os.environ['CONTAINER'], "python": "python3", "auto_install_deps": True},
  "xray_api_server": os.environ['XRAY_API_SERVER'],
  "poll_interval_sec": int(os.environ['POLL_INTERVAL']),
  "db_path": "/var/lib/remna-node-quota/quota.db",
  "period": os.environ['PERIOD'],
  "dry_run": b(os.environ['DRY_RUN']),
  "inbound_tags": split_csv(os.environ['INBOUND_TAGS_RAW']),
  "remnawave": {
    "enabled": True,
    "base_url": os.environ['PANEL_URL'],
    "token": os.environ['PANEL_TOKEN'].strip(),
    "users_endpoint": os.environ['USERS_ENDPOINT'],
    "page_limit": 100,
    "refresh_interval_sec": int(os.environ['REFRESH_INTERVAL']),
    "timeout_sec": 20,
    "verify_tls": b(os.environ['VERIFY_TLS']),
    "status_allowlist": split_csv(os.environ['STATUS_ALLOWLIST_RAW']),
    "id_fields": ["id", "uuid", "shortUuid", "username", "email", "vlessUuid", "trojanPassword", "ssPassword"],
    "limit_fields": ["trafficLimitBytes"],
    "limit_multiplier": 1.0,
    "default_limit_bytes": int(os.environ['DEFAULT_LIMIT_BYTES']),
    "fallback_to_local_users": False
  },
  "api": {"enabled": b(os.environ['API_ENABLED']), "listen": os.environ['API_LISTEN'], "port": int(os.environ['API_PORT']), "token": os.environ['API_TOKEN'].strip()},
  "default_limit_bytes": int(os.environ['DEFAULT_LIMIT_BYTES']),
  "users": {}
}
with open(os.environ['CONFIG_FILE'], 'w', encoding='utf-8') as f:
    json.dump(config, f, ensure_ascii=False, indent=2); f.write('\n')
PY
  chmod 600 "$config_file"
  log "Config written to ${config_file}"
}

install_app_files(){
  local source_dir="$1"
  log "Installing application to ${APP_DIR}"
  systemctl stop "${APP_NAME}" 2>/dev/null || true
  mkdir -p "$APP_DIR" "$DATA_DIR"
  rsync -a --delete --exclude '.git' --exclude 'venv' --exclude '__pycache__' --exclude '*.pyc' "${source_dir}/" "${APP_DIR}/"
  python3 -m venv "${APP_DIR}/venv"
  "${APP_DIR}/venv/bin/python" -m pip install --upgrade pip
  "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
  chmod +x "${APP_DIR}/install.sh" 2>/dev/null || true
  chmod +x "${APP_DIR}/scripts/uninstall.sh" 2>/dev/null || true
}

install_systemd_service(){
  log "Installing systemd service"
  if [[ -f "${APP_DIR}/systemd/${APP_NAME}.service" ]]; then
    cp "${APP_DIR}/systemd/${APP_NAME}.service" "$SERVICE_FILE"
  else
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Remnawave per-node quota limiter
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStartPre=${APP_DIR}/venv/bin/python -m remna_node_quota -c ${CONFIG_DIR}/config.json --prepare-container
ExecStart=${APP_DIR}/venv/bin/python -m remna_node_quota -c ${CONFIG_DIR}/config.json
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
  fi
  systemctl daemon-reload
}

print_next_steps(){
  cat <<EOF

Installation completed.

Test:
  ${APP_DIR}/venv/bin/python -m remna_node_quota -c ${CONFIG_DIR}/config.json --once --log-level DEBUG

Start service:
  systemctl enable --now ${APP_NAME}

Logs:
  journalctl -u ${APP_NAME} -f

Local HTTP API examples:
  curl -H "Authorization: Bearer <API_TOKEN>" http://127.0.0.1:8765/api/v1/status
  curl -H "Authorization: Bearer <API_TOKEN>" http://127.0.0.1:8765/api/v1/users
  curl -X POST -H "Authorization: Bearer <API_TOKEN>" http://127.0.0.1:8765/api/v1/enforce

Keep dry_run=true until usage and matching are verified.
EOF
}

main(){
  local script_dir; script_dir="$(detect_script_dir)"
  self_bootstrap_if_needed "$script_dir" "$@"
  install_os_dependencies
  install_app_files "$script_dir"
  if [[ ! -f "${CONFIG_DIR}/config.json" ]] || ask_yes_no "Config already exists. Recreate it?" "n"; then
    write_config
  else
    log "Keeping existing config: ${CONFIG_DIR}/config.json"
  fi
  install_systemd_service
  local c
  c="$(python3 - <<PY
import json
c=json.load(open('${CONFIG_DIR}/config.json'))
print((c.get('xray_runner') or {}).get('container','remnanode'))
PY
)"
  prepare_remnanode_container "$c" || true
  if ask_yes_no "Run one foreground test now?" "y"; then
    "${APP_DIR}/venv/bin/python" -m remna_node_quota -c "${CONFIG_DIR}/config.json" --once --log-level DEBUG || true
  fi
  if ask_yes_no "Enable and start systemd service now?" "n"; then
    systemctl enable --now "${APP_NAME}"
    systemctl status "${APP_NAME}" --no-pager || true
  fi
  print_next_steps
}

main "$@"
