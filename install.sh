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
  echo "Run as root" >&2
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
  printf '%s' "$(printf '%s' "$value" | tr -d '\r')"
}

ask_secret(){
  local prompt="$1" value=""
  read -r -s -p "$prompt: " value
  echo >&2
  printf '%s' "$(printf '%s' "$value" | tr -d '\r\n')"
}

ask_yes_no(){
  local prompt="$1" default="${2:-y}" answer=""
  while true; do
    if [[ "$default" == "y" ]]; then
      read -r -p "$prompt [Y/n]: " answer; answer="${answer:-Y}"
    else
      read -r -p "$prompt [y/N]: " answer; answer="${answer:-N}"
    fi
    case "$answer" in y|Y|yes|YES|Yes) return 0;; n|N|no|NO|No) return 1;; *) echo "Please answer y or n.";; esac
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
  [[ -d /etc/ufw ]] || { warn "/etc/ufw not found. Skipping."; return 0; }
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
    case "$(basename "$rules_file")" in *6.rules) family="inet6";; *) family="inet";; esac
    set_names="$(grep -hoE -- '--match-set[[:space:]]+[A-Za-z0-9_.:-]+' "$rules_file" 2>/dev/null | awk '{print $2}' | sort -u)"
    [[ -n "$set_names" ]] || continue
    while IFS= read -r set_name; do
      [[ -n "$set_name" ]] || continue
      if ipset list "$set_name" >/dev/null 2>&1; then
        log "ipset exists, keeping unchanged: $set_name"
      else
        log "Creating missing ipset: $set_name, family=$family"
        ipset create "$set_name" hash:net family "$family" hashsize 1024 maxelem 100000 -exist || warn "Failed to create ipset $set_name"
      fi
    done <<< "$set_names"
  done
  mkdir -p /etc/iptables 2>/dev/null || true
  ipset save > /etc/iptables/ipsets 2>/dev/null || true
}

ensure_docker(){
  if command -v docker >/dev/null 2>&1; then
    log "Docker already installed: $(docker --version 2>/dev/null || true)"
    return 0
  fi
  log "Docker is not installed. Trying to install Docker."
  apt-get update
  if apt-cache policy docker-ce 2>/dev/null | grep -q "Candidate:" && ! apt-cache policy docker-ce 2>/dev/null | grep -q "Candidate: (none)"; then
    log "Installing Docker from Docker CE repository"
    DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    return 0
  fi
  warn "Docker CE repository is not available. Falling back to Ubuntu docker.io."
  if dpkg -l | awk '{print $2}' | grep -qx 'containerd.io'; then
    err "containerd.io is installed, but Docker CE repository is not available. Refusing to install conflicting docker.io."
    return 1
  fi
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-plugin
}

install_os_dependencies(){
  log "Installing OS dependencies"
  ensure_ufw_ipsets
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip curl ca-certificates jq ipset rsync
  ensure_docker
  ensure_ufw_ipsets
}

random_token(){
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

write_config(){
  local config_file="${CONFIG_DIR}/config.json"
  log "Interactive configuration"
  local xray_api_server panel_url panel_token users_endpoint inbound_tags_raw period poll_interval refresh_interval dry_run verify_tls status_allowlist_raw per_node_limit_gb per_node_limit_bytes api_enabled api_token api_listen container_name inbound_limits_raw same_limit

  container_name="$(ask "Remnanode container name" "remnanode")"
  xray_api_server="$(ask "Remnanode internal Xray API server" "127.0.0.1:61000")"
  panel_url="$(ask "Remnawave Panel URL" "https://panel.example.com")"; panel_url="${panel_url%/}"
  panel_token="$(ask_secret "Remnawave API Bearer token")"
  users_endpoint="$(ask "Remnawave users endpoint" "/api/users")"
  inbound_tags_raw="$(ask "Inbound tags to enforce, comma-separated" "VLESS_TCP_REALITY-SEL-RU-1")"
  period="$(ask "Quota period: day, week, month, forever" "day")"
  per_node_limit_gb="$(ask "Default per-node limit in GiB for every active Remnawave user" "10")"
  poll_interval="$(ask "Xray stats polling interval in seconds" "20")"
  refresh_interval="$(ask "Remnawave users refresh interval in seconds" "300")"

  if ask_yes_no "Enable dry-run mode? Recommended for first launch" "y"; then dry_run="true"; else dry_run="false"; fi
  if ask_yes_no "Verify Remnawave Panel TLS certificate?" "y"; then verify_tls="true"; else verify_tls="false"; fi
  status_allowlist_raw="$(ask "Allowed user statuses, comma-separated. Empty disables status filter" "ACTIVE")"

  if ask_yes_no "Use the same global limit for all configured inbounds?" "y"; then same_limit="true"; else same_limit="false"; fi

  per_node_limit_bytes="$(PER_NODE_LIMIT_GB="$per_node_limit_gb" python3 - <<'PY'
from decimal import Decimal
import os
print(int(Decimal(os.environ['PER_NODE_LIMIT_GB']) * Decimal(1024) * Decimal(1024) * Decimal(1024)))
PY
)"

  inbound_limits_raw=""
  IFS=',' read -ra _inbound_arr <<< "$inbound_tags_raw"
  for _tag in "${_inbound_arr[@]}"; do
    _tag="$(printf '%s' "$_tag" | xargs)"
    [[ -n "$_tag" ]] || continue
    if [[ "$same_limit" == "true" ]]; then
      _limit_bytes="$per_node_limit_bytes"
    else
      _limit_gb="$(ask "Limit in GiB for inbound ${_tag}" "$per_node_limit_gb")"
      _limit_bytes="$(PER_NODE_LIMIT_GB="$_limit_gb" python3 - <<'PY'
from decimal import Decimal
import os
print(int(Decimal(os.environ['PER_NODE_LIMIT_GB']) * Decimal(1024) * Decimal(1024) * Decimal(1024)))
PY
)"
    fi
    inbound_limits_raw+="${_tag}=${_limit_bytes}"$'\n'
  done

  if ask_yes_no "Enable local HTTP API?" "y"; then api_enabled="true"; else api_enabled="false"; fi
  if [[ "$api_enabled" == "true" ]]; then
    if ask_yes_no "Expose API to external network? Use only with firewall/VPN/HTTPS proxy" "n"; then api_listen="0.0.0.0"; else api_listen="127.0.0.1"; fi
  else
    api_listen="127.0.0.1"
  fi
  api_token="$(ask_secret "Local API bearer token. Leave empty to auto-generate")"
  [[ -n "$api_token" ]] || api_token="$(random_token)"

  mkdir -p "$CONFIG_DIR"; chmod 700 "$CONFIG_DIR"
  CONFIG_FILE="$config_file" XRAY_API_SERVER="$xray_api_server" CONTAINER_NAME="$container_name" POLL_INTERVAL="$poll_interval" PERIOD="$period" DRY_RUN="$dry_run" INBOUND_TAGS_RAW="$inbound_tags_raw" INBOUND_LIMITS_RAW="$inbound_limits_raw" PANEL_URL="$panel_url" PANEL_TOKEN="$panel_token" USERS_ENDPOINT="$users_endpoint" REFRESH_INTERVAL="$refresh_interval" VERIFY_TLS="$verify_tls" STATUS_ALLOWLIST_RAW="$status_allowlist_raw" PER_NODE_LIMIT_BYTES="$per_node_limit_bytes" API_ENABLED="$api_enabled" API_LISTEN="$api_listen" API_TOKEN="$api_token" python3 <<'PY'
import json, os

def split_csv(v):
    return [x.strip() for x in v.split(',') if x.strip()]

def parse_bool(v):
    return str(v).lower() in ('1','true','yes','y','on')

def parse_inbounds(raw, default_limit):
    result = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or '=' not in line:
            continue
        tag, limit = line.split('=', 1)
        tag = tag.strip()
        if tag:
            result[tag] = {"limit_bytes": int(limit.strip()), "enabled": True}
    if not result:
        result = {tag: {"limit_bytes": default_limit, "enabled": True} for tag in split_csv(os.environ["INBOUND_TAGS_RAW"])}
    return result

config = {
  "xray_bin": "/usr/local/bin/xray",
  "xray_runner": {"mode": "docker_grpc_exec", "container": os.environ["CONTAINER_NAME"], "python": "python3"},
  "xray_api_server": os.environ["XRAY_API_SERVER"],
  "poll_interval_sec": int(os.environ["POLL_INTERVAL"]),
  "db_path": "/var/lib/remna-node-quota/quota.db",
  "period": os.environ["PERIOD"],
  "dry_run": parse_bool(os.environ["DRY_RUN"]),
  "usage_scope": "user_total_shared_across_inbounds",
  "per_node_limit_bytes": int(os.environ["PER_NODE_LIMIT_BYTES"]),
  "inbounds": parse_inbounds(os.environ.get("INBOUND_LIMITS_RAW", ""), int(os.environ["PER_NODE_LIMIT_BYTES"])),
  "remnawave": {
    "enabled": True,
    "base_url": os.environ["PANEL_URL"],
    "token": os.environ["PANEL_TOKEN"].strip(),
    "users_endpoint": os.environ["USERS_ENDPOINT"],
    "page_limit": 100,
    "refresh_interval_sec": int(os.environ["REFRESH_INTERVAL"]),
    "timeout_sec": 20,
    "verify_tls": parse_bool(os.environ["VERIFY_TLS"]),
    "status_allowlist": split_csv(os.environ["STATUS_ALLOWLIST_RAW"]),
    "id_fields": ["id", "uuid", "shortUuid", "username", "email", "vlessUuid", "trojanPassword", "ssPassword"],
    "use_panel_traffic_limit": False,
    "per_node_limit_bytes": int(os.environ["PER_NODE_LIMIT_BYTES"]),
    "fallback_to_local_users": False
  },
  "api": {
    "enabled": parse_bool(os.environ["API_ENABLED"]),
    "listen": os.environ.get("API_LISTEN", "127.0.0.1"),
    "port": 8765,
    "token": os.environ["API_TOKEN"].strip()
  },
  "users": {}
}
with open(os.environ["CONFIG_FILE"], "w", encoding="utf-8") as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  chmod 600 "$config_file"
  log "Config written to $config_file"
  log "Local API token saved in config: ${CONFIG_DIR}/config.json"
}

install_app_files(){
  local source_dir="$1"
  log "Installing application to ${APP_DIR}"
  systemctl stop "$APP_NAME" 2>/dev/null || true
  mkdir -p "$APP_DIR" "$DATA_DIR"
  rsync -a --delete --exclude ".git" --exclude "venv" --exclude "__pycache__" --exclude "*.pyc" "$source_dir/" "$APP_DIR/"
  python3 -m venv "${APP_DIR}/venv"
  "${APP_DIR}/venv/bin/python" -m pip install --upgrade pip
  "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
  chmod +x "${APP_DIR}/install.sh" 2>/dev/null || true
  chmod +x "${APP_DIR}/scripts/uninstall.sh" 2>/dev/null || true
}

install_systemd_service(){
  log "Installing systemd service"
  cp "${APP_DIR}/systemd/${APP_NAME}.service" "$SERVICE_FILE"
  systemctl daemon-reload
}

print_next_steps(){
  cat <<EOF

Installation completed.

Test run:
  ${APP_DIR}/venv/bin/python -m remna_node_quota -c ${CONFIG_DIR}/config.json --once --log-level DEBUG

Start service:
  systemctl enable --now ${APP_NAME}

Logs:
  journalctl -u ${APP_NAME} -f

Local API examples:
  TOKEN=\$(python3 - <<'PY'
import json
c=json.load(open('${CONFIG_DIR}/config.json'))
print(c.get('api',{}).get('token',''))
PY
)
  curl -H "Authorization: Bearer \$TOKEN" http://127.0.0.1:8765/api/v1/status
  curl -H "Authorization: Bearer \$TOKEN" http://127.0.0.1:8765/api/v1/users
  curl -X POST -H "Authorization: Bearer \$TOKEN" http://127.0.0.1:8765/api/v1/enforce

Important: first run is recommended with dry_run=true. Switch to false after validation.
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
  log "Preparing remnanode container dependencies"
  "${APP_DIR}/venv/bin/python" -m remna_node_quota -c "${CONFIG_DIR}/config.json" --prepare-container || warn "Container preparation failed. You may need: docker exec remnanode apk add --no-cache py3-grpcio py3-protobuf"
  if ask_yes_no "Run one foreground test now?" "y"; then
    "${APP_DIR}/venv/bin/python" -m remna_node_quota -c "${CONFIG_DIR}/config.json" --once --log-level DEBUG || true
  fi
  if ask_yes_no "Enable and start systemd service now?" "n"; then
    systemctl enable --now "$APP_NAME"
    systemctl status "$APP_NAME" --no-pager || true
  fi
  print_next_steps
}

main "$@"
