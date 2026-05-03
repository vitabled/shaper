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
    echo "Run as root:"
    echo "  sudo $0"
    exit 1
fi

log() {
    echo -e "\033[1;32m[+]\033[0m $*"
}

warn() {
    echo -e "\033[1;33m[!]\033[0m $*"
}

err() {
    echo -e "\033[1;31m[-]\033[0m $*" >&2
}

ask() {
    local prompt="$1"
    local default="${2:-}"
    local value=""

    if [[ -n "$default" ]]; then
        read -r -p "$prompt [$default]: " value
        echo "${value:-$default}"
    else
        read -r -p "$prompt: " value
        echo "$value"
    fi
}

ask_secret() {
    local prompt="$1"
    local value=""
    read -r -s -p "$prompt: " value
    echo
    echo "$value"
}

ask_yes_no() {
    local prompt="$1"
    local default="${2:-y}"
    local answer=""

    while true; do
        if [[ "$default" == "y" ]]; then
            read -r -p "$prompt [Y/n]: " answer
            answer="${answer:-Y}"
        else
            read -r -p "$prompt [y/N]: " answer
            answer="${answer:-N}"
        fi

        case "$answer" in
            y|Y|yes|YES|Yes) return 0 ;;
            n|N|no|NO|No) return 1 ;;
            *) echo "Please answer y or n." ;;
        esac
    done
}

normalize_url() {
    local url="$1"
    url="${url%/}"
    echo "$url"
}

require_cmd() {
    local cmd="$1"
    local pkg="$2"

    if ! command -v "$cmd" >/dev/null 2>&1; then
        log "Installing missing package: $pkg"
        apt-get update
        apt-get install -y "$pkg"
    fi
}

detect_script_dir() {
    local source="${BASH_SOURCE[0]}"
    local dir=""

    if [[ -e "$source" ]]; then
        dir="$(cd "$(dirname "$source")" 2>/dev/null && pwd || true)"
    fi

    echo "$dir"
}

self_bootstrap_if_needed() {
    local script_dir="$1"

    if [[ -f "${script_dir}/requirements.txt" && -d "${script_dir}/remna_node_quota" ]]; then
        return 0
    fi

    warn "Installer was started without repository files."
    warn "This usually happens when using bash <(curl ...)."
    log "Cloning repository from: ${REPO_URL}"

    require_cmd git git
    require_cmd mktemp coreutils

    local tmp_dir
    tmp_dir="$(mktemp -d)"

    git clone --depth=1 "$REPO_URL" "$tmp_dir/${APP_NAME}"

    cd "$tmp_dir/${APP_NAME}"
    exec bash ./install.sh "$@"
}

install_os_dependencies() {
    log "Installing OS dependencies"

    apt-get update
    apt-get install -y \
        python3 \
        python3-venv \
        python3-pip \
        curl \
        ca-certificates \
        jq
}

write_config() {
    local config_file="${CONFIG_DIR}/config.json"

    log "Interactive configuration"

    local xray_bin
    local xray_api_server
    local panel_url
    local panel_token
    local users_endpoint
    local inbound_tags_raw
    local period
    local poll_interval
    local refresh_interval
    local dry_run
    local verify_tls
    local status_allowlist_raw
    local default_limit_gb
    local default_limit_bytes

    xray_bin="$(ask "Path to xray binary" "/usr/local/bin/xray")"
    xray_api_server="$(ask "Xray API server" "127.0.0.1:10085")"

    panel_url="$(ask "Remnawave Panel URL, for example https://panel.example.com" "")"
    panel_url="$(normalize_url "$panel_url")"

    panel_token="$(ask_secret "Remnawave API Bearer token")"

    users_endpoint="$(ask "Remnawave users endpoint" "/api/users")"
    inbound_tags_raw="$(ask "Inbound tags to block, comma-separated" "VLESS_TCP_REALITY,VLESS_XHTTP")"

    period="$(ask "Quota period: day, week, month, forever" "month")"
    poll_interval="$(ask "Xray stats polling interval in seconds" "20")"
    refresh_interval="$(ask "Remnawave users refresh interval in seconds" "300")"

    if ask_yes_no "Enable dry-run mode? Recommended for first launch" "y"; then
        dry_run="true"
    else
        dry_run="false"
    fi

    if ask_yes_no "Verify Remnawave Panel TLS certificate?" "y"; then
        verify_tls="true"
    else
        verify_tls="false"
    fi

    status_allowlist_raw="$(ask "Allowed user statuses, comma-separated" "ACTIVE")"
    default_limit_gb="$(ask "Default per-node limit in GB if user limit is missing, 0 means disabled" "0")"

    default_limit_bytes="$(python3 - <<PY
from decimal import Decimal
gb = Decimal("${default_limit_gb}")
print(int(gb * Decimal(1024) * Decimal(1024) * Decimal(1024)))
PY
)"

    mkdir -p "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR"

    python3 - "$config_file" <<PY
import json
import sys

config_file = sys.argv[1]

def split_csv(value):
    return [x.strip() for x in value.split(",") if x.strip()]

config = {
    "xray_bin": ${xray_bin@Q},
    "xray_api_server": ${xray_api_server@Q},
    "poll_interval_sec": int(${poll_interval@Q}),
    "db_path": "/var/lib/remna-node-quota/quota.db",
    "period": ${period@Q},
    "dry_run": ${dry_run},
    "inbound_tags": split_csv(${inbound_tags_raw@Q}),
    "remnawave": {
        "enabled": True,
        "base_url": ${panel_url@Q},
        "token": ${panel_token@Q},
        "users_endpoint": ${users_endpoint@Q},
        "page_limit": 100,
        "refresh_interval_sec": int(${refresh_interval@Q}),
        "timeout_sec": 20,
        "verify_tls": ${verify_tls},
        "status_allowlist": split_csv(${status_allowlist_raw@Q}),
        "id_fields": [
            "uuid",
            "shortUuid",
            "username",
            "email",
            "subscriptionUuid"
        ],
        "limit_fields": [
            "dataLimitBytes",
            "trafficLimitBytes",
            "usedTrafficBytesLimit",
            "trafficLimit",
            "dataLimit"
        ],
        "limit_multiplier": 1.0,
        "default_limit_bytes": int(${default_limit_bytes@Q}),
        "fallback_to_local_users": False
    },
    "default_limit_bytes": int(${default_limit_bytes@Q}),
    "users": {}
}

with open(config_file, "w", encoding="utf-8") as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
    f.write("\\n")
PY

    chmod 600 "$config_file"
    log "Config written to ${config_file}"
}

install_app_files() {
    local source_dir="$1"

    log "Installing application to ${APP_DIR}"

    systemctl stop "${APP_NAME}" 2>/dev/null || true

    mkdir -p "$APP_DIR"
    mkdir -p "$DATA_DIR"

    rsync -a \
        --delete \
        --exclude ".git" \
        --exclude "venv" \
        --exclude "__pycache__" \
        --exclude "*.pyc" \
        "${source_dir}/" "${APP_DIR}/"

    python3 -m venv "${APP_DIR}/venv"
    "${APP_DIR}/venv/bin/python" -m pip install --upgrade pip
    "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

    chmod +x "${APP_DIR}/install.sh" 2>/dev/null || true
    chmod +x "${APP_DIR}/scripts/uninstall.sh" 2>/dev/null || true
}

install_systemd_service() {
    log "Installing systemd service"

    if [[ -f "${APP_DIR}/systemd/${APP_NAME}.service" ]]; then
        cp "${APP_DIR}/systemd/${APP_NAME}.service" "$SERVICE_FILE"
    else
        cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Remnawave per-node quota limiter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python -m remna_node_quota -c ${CONFIG_DIR}/config.json
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
    fi

    systemctl daemon-reload
    log "Service installed: ${SERVICE_FILE}"
}

print_next_steps() {
    cat <<EOF

Installation completed.

Useful commands:

  Test run:
    ${APP_DIR}/venv/bin/python -m remna_node_quota -c ${CONFIG_DIR}/config.json

  Start service:
    systemctl enable --now ${APP_NAME}

  View logs:
    journalctl -u ${APP_NAME} -f

  Edit config:
    nano ${CONFIG_DIR}/config.json

  Restart:
    systemctl restart ${APP_NAME}

Important:
  First run is recommended with dry_run=true.
  When logs show correct users and limits, set dry_run=false and restart service.

EOF
}

main() {
    local script_dir
    script_dir="$(detect_script_dir)"

    self_bootstrap_if_needed "$script_dir" "$@"

    install_os_dependencies

    require_cmd rsync rsync

    install_app_files "$script_dir"

    if [[ ! -f "${CONFIG_DIR}/config.json" ]] || ask_yes_no "Config already exists. Recreate it?" "n"; then
        write_config
    else
        log "Keeping existing config: ${CONFIG_DIR}/config.json"
    fi

    install_systemd_service

    if ask_yes_no "Run one foreground test now?" "y"; then
        "${APP_DIR}/venv/bin/python" -m remna_node_quota -c "${CONFIG_DIR}/config.json" || true
    fi

    if ask_yes_no "Enable and start systemd service now?" "n"; then
        systemctl enable --now "${APP_NAME}"
        systemctl status "${APP_NAME}" --no-pager || true
    fi

    print_next_steps
}

main "$@"
