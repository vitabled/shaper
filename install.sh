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
        value="${value:-$default}"
    else
        read -r -p "$prompt: " value
    fi

    value="$(printf '%s' "$value" | tr -d '\r')"
    printf '%s' "$value"
}

ask_secret() {
    local prompt="$1"
    local value=""
    read -r -s -p "$prompt: " value
    echo >&2
    value="$(printf '%s' "$value" | tr -d '\r\n')"
    printf '%s' "$value"
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
        DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg"
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
    shift || true

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

ensure_ufw_ipsets() {
    log "Checking UFW ipset dependencies"

    if [[ ! -d /etc/ufw ]]; then
        warn "/etc/ufw not found. Skipping UFW ipset check."
        return 0
    fi

    if ! grep -Rqs -- '--match-set' /etc/ufw/*.rules 2>/dev/null; then
        log "No ipset references found in /etc/ufw/*.rules"
        return 0
    fi

    require_cmd ipset ipset

    local backup_dir
    backup_dir="/root/${APP_NAME}-ufw-ipset-backup-$(date +%F_%H-%M-%S)"
    mkdir -p "$backup_dir"

    cp -a /etc/ufw "$backup_dir/ufw" 2>/dev/null || true
    ipset save > "$backup_dir/ipset.save" 2>/dev/null || true

    log "Backup created: $backup_dir"

    local rules_file
    local family
    local set_names
    local set_name

    for rules_file in /etc/ufw/*.rules; do
        [[ -f "$rules_file" ]] || continue

        if ! grep -qs -- '--match-set' "$rules_file"; then
            continue
        fi

        case "$(basename "$rules_file")" in
            *6.rules)
                family="inet6"
                ;;
            *)
                family="inet"
                ;;
        esac

        set_names="$(
            grep -hoE -- '--match-set[[:space:]]+[A-Za-z0-9_.:-]+' "$rules_file" 2>/dev/null \
            | awk '{print $2}' \
            | sort -u
        )"

        [[ -n "$set_names" ]] || continue

        while IFS= read -r set_name; do
            [[ -n "$set_name" ]] || continue

            if ipset list "$set_name" >/dev/null 2>&1; then
                log "ipset exists, keeping unchanged: $set_name"
                continue
            fi

            log "Creating missing ipset: $set_name, family=$family"

            if ! ipset create "$set_name" hash:net family "$family" hashsize 1024 maxelem 100000 -exist; then
                warn "Failed to create ipset $set_name with family=$family"
                warn "Continuing without deleting or modifying existing firewall rules."
            fi
        done <<< "$set_names"
    done

    mkdir -p /etc/iptables 2>/dev/null || true
    ipset save > /etc/iptables/ipsets 2>/dev/null || true

    if command -v ufw >/dev/null 2>&1; then
        log "Testing UFW rules with existing configuration"

        if ufw status 2>/dev/null | grep -qi "Status: active"; then
            if ufw --force reload; then
                log "UFW reload successful"
            else
                warn "UFW reload failed even after creating missing ipsets."
                warn "Check backup: $backup_dir"
                warn "Run manually: grep -Rni -- '--match-set' /etc/ufw"
            fi
        else
            log "UFW is not active. Skipping reload."
        fi
    fi

    log "UFW ipset check completed. Existing sets were not deleted."
}

install_os_dependencies() {
    log "Installing OS dependencies"

    ensure_ufw_ipsets

    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        python3 \
        python3-venv \
        python3-pip \
        curl \
        ca-certificates \
        jq \
        ipset \
        rsync

    ensure_ufw_ipsets
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

    default_limit_bytes="$(
        DEFAULT_LIMIT_GB="$default_limit_gb" python3 <<'PY'
from decimal import Decimal
import os

gb = Decimal(os.environ["DEFAULT_LIMIT_GB"])
print(int(gb * Decimal(1024) * Decimal(1024) * Decimal(1024)))
PY
    )"

    mkdir -p "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR"

    CONFIG_FILE="$config_file" \
    XRAY_BIN="$xray_bin" \
    XRAY_API_SERVER="$xray_api_server" \
    POLL_INTERVAL="$poll_interval" \
    PERIOD="$period" \
    DRY_RUN="$dry_run" \
    INBOUND_TAGS_RAW="$inbound_tags_raw" \
    PANEL_URL="$panel_url" \
    PANEL_TOKEN="$panel_token" \
    USERS_ENDPOINT="$users_endpoint" \
    REFRESH_INTERVAL="$refresh_interval" \
    VERIFY_TLS="$verify_tls" \
    STATUS_ALLOWLIST_RAW="$status_allowlist_raw" \
    DEFAULT_LIMIT_BYTES="$default_limit_bytes" \
    python3 <<'PY'
import json
import os

config_file = os.environ["CONFIG_FILE"]

def split_csv(value):
    return [x.strip() for x in value.split(",") if x.strip()]

def parse_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")

config = {
    "xray_bin": os.environ["XRAY_BIN"],
    "xray_api_server": os.environ["XRAY_API_SERVER"],
    "poll_interval_sec": int(os.environ["POLL_INTERVAL"]),
    "db_path": "/var/lib/remna-node-quota/quota.db",
    "period": os.environ["PERIOD"],
    "dry_run": parse_bool(os.environ["DRY_RUN"]),
    "inbound_tags": split_csv(os.environ["INBOUND_TAGS_RAW"]),
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
        "default_limit_bytes": int(os.environ["DEFAULT_LIMIT_BYTES"]),
        "fallback_to_local_users": False
    },
    "default_limit_bytes": int(os.environ["DEFAULT_LIMIT_BYTES"]),
    "users": {}
}

with open(config_file, "w", encoding="utf-8") as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
    f.write("\n")
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

    require_cmd rsync rsync

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

  Check config JSON:
    python3 -m json.tool ${CONFIG_DIR}/config.json >/dev/null && echo OK

  Check token without printing it:
    python3 - <<'PY'
import json
c=json.load(open("${CONFIG_DIR}/config.json"))
t=c["remnawave"]["token"]
print("token length:", len(t))
print("starts with:", t[:10])
print("contains newline:", "\\n" in t)
PY

  Check UFW ipsets:
    ipset list -name
    grep -Rni -- '--match-set' /etc/ufw

Important:
  First run is recommended with dry_run=true.
  When logs show correct users and limits, set dry_run=false and restart service.

EOF
}

main() {
    local script_dir
    script_dir="$(detect_script_dir)"

    self_bootstrap_if_needed "$script_dir" "$@"

    ensure_ufw_ipsets

    install_os_dependencies

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
