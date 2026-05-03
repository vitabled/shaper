# remna-node-quota

`remna-node-quota` is a Linux daemon for **per-node traffic limits** on Remnawave/Xray nodes.

It does **not** bind a quota to a client IP address. It reads Xray user statistics like:

```text
user>>>IDENTIFIER>>>traffic>>>uplink
user>>>IDENTIFIER>>>traffic>>>downlink
```

Then it compares local traffic on the current node with user limits fetched from the **Remnawave Panel API**. When the limit is exceeded, the daemon removes the user from selected Xray inbound tags on this node by using Xray `HandlerService`.

## What problem it solves

Remnawave can have a global user/subscription traffic limit. This program adds a separate local rule:

```text
One user can spend no more than N GB on one specific node.
```

Changing IP address does not reset anything, because the counter is tied to the Xray user identifier, not to the source IP.

## Requirements

On the node:

- Linux with systemd
- Python 3
- Xray binary with API command support
- Xray API enabled locally
- Remnawave Panel API token

## Required Xray config profile fragment

The node's Xray config/profile must expose `StatsService` and `HandlerService`:

```json
{
  "api": {
    "tag": "api",
    "listen": "127.0.0.1:10085",
    "services": [
      "HandlerService",
      "StatsService"
    ]
  },
  "stats": {},
  "policy": {
    "levels": {
      "0": {
        "statsUserUplink": true,
        "statsUserDownlink": true
      }
    },
    "system": {
      "statsInboundUplink": true,
      "statsInboundDownlink": true,
      "statsOutboundUplink": true,
      "statsOutboundDownlink": true
    }
  }
}
```

Check that Xray exposes user stats:

```bash
xray api statsquery --server=127.0.0.1:10085 -pattern "user>>>"
```

## Interactive installation from GitHub

Replace `YOUR_GITHUB_USER` and `YOUR_REPO` with your repository.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/YOUR_GITHUB_USER/YOUR_REPO/main/install.sh)
```

Or clone manually:

```bash
git clone https://github.com/YOUR_GITHUB_USER/YOUR_REPO.git
cd YOUR_REPO
sudo ./install.sh
```

## Configuration

The installer writes:

```text
/etc/remna-node-quota/config.json
```

Example:

```json
{
  "xray_bin": "/usr/local/bin/xray",
  "xray_api_server": "127.0.0.1:10085",
  "poll_interval_sec": 20,
  "db_path": "/var/lib/remna-node-quota/quota.db",
  "period": "month",
  "dry_run": true,
  "inbound_tags": [
    "VLESS_TCP_REALITY",
    "VLESS_XHTTP"
  ],
  "remnawave": {
    "enabled": true,
    "base_url": "https://panel.example.com",
    "token": "PASTE_REMNAWAVE_API_TOKEN_HERE",
    "users_endpoint": "/api/users",
    "page_limit": 100,
    "refresh_interval_sec": 300,
    "timeout_sec": 20,
    "verify_tls": true,
    "status_allowlist": ["ACTIVE"],
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
    "default_limit_bytes": 0,
    "fallback_to_local_users": false
  },
  "default_limit_bytes": 0,
  "users": {}
}
```

### Important fields

- `dry_run: true` — only logs actions, does not remove users.
- `dry_run: false` — actually removes exceeded users from Xray inbound tags.
- `period` — `day`, `week`, `month`, or `forever`.
- `inbound_tags` — inbound tags from which exceeded users will be removed.
- `remnawave.base_url` — your panel URL.
- `remnawave.token` — Bearer token for panel API.
- `remnawave.id_fields` — fields used to match Remnawave users with Xray stats identifiers.
- `remnawave.limit_fields` — fields used to read the traffic limit from the panel response.

## Test run

```bash
/opt/remna-node-quota/venv/bin/python -m remna_node_quota -c /etc/remna-node-quota/config.json --once
```

## Service management

```bash
systemctl status remna-node-quota
systemctl restart remna-node-quota
journalctl -u remna-node-quota -f
```

## Disable dry-run

After checking logs and matching identifiers:

```bash
nano /etc/remna-node-quota/config.json
```

Change:

```json
"dry_run": true
```

To:

```json
"dry_run": false
```

Then restart:

```bash
systemctl restart remna-node-quota
```

## How blocking works

When a user exceeds the local quota for the current period, the daemon runs commands equivalent to:

```bash
xray api rmu --server=127.0.0.1:10085 --tag=INBOUND_TAG --email=USER_IDENTIFIER
```

If Remnawave later re-syncs the user back into Xray, the daemon sees that the user is already marked as blocked in SQLite and removes the user again.

## Data storage

SQLite database:

```text
/var/lib/remna-node-quota/quota.db
```

It stores:

- current period counters
- last seen Xray totals
- blocked users for the period

## Uninstall

```bash
sudo /opt/remna-node-quota/scripts/uninstall.sh
```

The uninstall script keeps config and database by default:

```text
/etc/remna-node-quota
/var/lib/remna-node-quota
```
