# remna-node-quota / shaper

Per-node traffic quota limiter for Remnawave/Remnanode.

It limits traffic by Xray user identifier, not by client IP. The tested Remnanode mode uses:

- Remnawave Panel API: `/api/users`
- Remnanode runtime Xray API: `REMNAWAVE_API_INBOUND` on `127.0.0.1:61000`
- TLS/gRPC helper inside the `remnanode` container

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/vitabled/shaper/main/install.sh)
```

The installer asks for:

- Remnanode container name, default `remnanode`
- Remnawave Panel URL and API token
- inbound tags to block, for example `VLESS_TCP_REALITY-SEL-RU-1`
- quota period
- default limit
- local HTTP API settings

## Test

```bash
/opt/remna-node-quota/venv/bin/python -m remna_node_quota \
  -c /etc/remna-node-quota/config.json \
  --once \
  --log-level DEBUG
```

Expected log:

```text
Remnawave API page=1 raw users parsed: ...
Remnawave users build result: users=... identifiers=...
Fetched Xray stats for ... users; managed identifiers=...
user=18 period=... used=... limit=...
```

## Service

```bash
systemctl enable --now remna-node-quota
journalctl -u remna-node-quota -f
```

## Local HTTP API

By default it listens on `127.0.0.1:8765`.

```bash
TOKEN='CHANGE_ME_LOCAL_API_TOKEN'

curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/status
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/users
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/counters
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/blocked
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/enforce
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/api/v1/users/18/block
curl -X POST -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8765/api/v1/users/18/reset?period_key=2026-05-03'
```

Keep the HTTP API bound to `127.0.0.1` unless you put it behind your own authenticated reverse proxy or firewall.

## Notes

The installer prepares the Alpine-based `remnanode` container with:

```bash
apk add --no-cache python3 py3-grpcio py3-protobuf
```

The systemd unit also runs `--prepare-container` before startup, so dependencies are restored after container recreation.
