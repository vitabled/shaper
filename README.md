# remna-node-quota / shaper

Per-node overlay quota for Remnawave nodes.

The program uses Remnawave Panel API only to receive the list of users and their identifiers. The actual quota is local and applies only on the node where this program is installed.

Example:

- Remnawave global limit: 500 GB/day for every user.
- This program on a selected node: 10 GiB/day.
- Result: users keep their 500 GB/day globally, but on this node they are limited to 10 GiB/day.

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/vitabled/shaper/main/install.sh)
```

## Key config options

```json
"period": "day",
"per_node_limit_bytes": 10737418240,
"dry_run": true,
"remnawave": {
  "use_panel_traffic_limit": false,
  "id_fields": ["id", "uuid", "shortUuid", "username", "email", "vlessUuid"]
}
```

`use_panel_traffic_limit` is intentionally `false` by default. This makes the program enforce the same local node quota for every active user from Remnawave.

## Remnanode support

Default mode is TLS/gRPC through the internal Remnawave API inbound:

```json
"xray_runner": {
  "mode": "docker_grpc_exec",
  "container": "remnanode",
  "python": "python3"
},
"xray_api_server": "127.0.0.1:61000"
```

The helper reads the internal Remnanode runtime config and connects to `REMNAWAVE_API_INBOUND` using the generated TLS material and SNI.

## Local API

The API listens on `127.0.0.1:8765` by default.

```bash
TOKEN=$(python3 - <<'PY'
import json
c=json.load(open('/etc/remna-node-quota/config.json'))
print(c.get('api',{}).get('token',''))
PY
)

curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/status
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/users
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/counters
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/blocked
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/enforce
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/users/18/block
curl -X POST -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8765/api/v1/users/18/reset?period_key=2026-05-03'
```

## Service

```bash
systemctl enable --now remna-node-quota
journalctl -u remna-node-quota -f
```

Keep `dry_run=true` until you verify that `used` grows and the limit is the local per-node limit, for example `10.00 GiB`, not the Remnawave global limit.
