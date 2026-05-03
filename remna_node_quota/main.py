#!/usr/bin/env python3
"""
remna-node-quota
Per-node traffic quota enforcer for Remnawave nodes using Xray Stats API + Remnawave Panel API.

It does not bind limits to client IP. It binds limits to the Xray user identifier,
for example the value inside:
  user>>>IDENTIFIER>>>traffic>>>uplink
  user>>>IDENTIFIER>>>traffic>>>downlink
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

LOG = logging.getLogger("remna-node-quota")
STAT_RE = re.compile(r"^user>>>(?P<user>.+?)>>>traffic>>>(?P<dir>uplink|downlink)$")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def period_key(period: str, now: Optional[dt.datetime] = None) -> str:
    n = now or now_utc()
    if period == "day":
        return n.strftime("%Y-%m-%d")
    if period == "week":
        iso = n.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if period == "month":
        return n.strftime("%Y-%m")
    if period == "forever":
        return "forever"
    raise ValueError(f"Unsupported period: {period}")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def bytes_human(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    v = float(value)
    for u in units:
        if abs(v) < 1024.0 or u == units[-1]:
            return f"{v:.2f} {u}"
        v /= 1024.0
    return f"{value} B"


def parse_limit_to_bytes(value: Any, multiplier: float = 1.0) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(float(value) * multiplier)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0
        if s.isdigit():
            return int(float(s) * multiplier)
        m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([kmgtp]?i?b?|[kmgtp])$", s, re.I)
        if not m:
            return 0
        num = float(m.group(1))
        unit = m.group(2).lower()
        factors = {
            "b": 1,
            "": 1,
            "k": 1000,
            "kb": 1000,
            "m": 1000**2,
            "mb": 1000**2,
            "g": 1000**3,
            "gb": 1000**3,
            "t": 1000**4,
            "tb": 1000**4,
            "p": 1000**5,
            "pb": 1000**5,
            "kib": 1024,
            "mib": 1024**2,
            "gib": 1024**3,
            "tib": 1024**4,
            "pib": 1024**5,
        }
        return int(num * factors.get(unit, 1) * multiplier)
    return 0


def deep_get(obj: dict, dotted: str) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


@dataclasses.dataclass
class ManagedUser:
    identifiers: List[str]
    limit_bytes: int
    raw: dict


class QuotaDB:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.init()

    def init(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS counters (
                user_id TEXT NOT NULL,
                period_key TEXT NOT NULL,
                used_bytes INTEGER NOT NULL DEFAULT 0,
                last_xray_total INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, period_key)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id TEXT NOT NULL,
                period_key TEXT NOT NULL,
                limit_bytes INTEGER NOT NULL,
                used_bytes INTEGER NOT NULL,
                blocked_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY (user_id, period_key)
            )
            """
        )
        self.conn.commit()

    def update_usage_from_xray_total(self, user_id: str, pkey: str, xray_total: int) -> int:
        row = self.conn.execute(
            "SELECT used_bytes,last_xray_total FROM counters WHERE user_id=? AND period_key=?",
            (user_id, pkey),
        ).fetchone()
        now = now_utc().isoformat()
        if row is None:
            used = max(0, int(xray_total))
            self.conn.execute(
                "INSERT INTO counters(user_id,period_key,used_bytes,last_xray_total,updated_at) VALUES(?,?,?,?,?)",
                (user_id, pkey, used, int(xray_total), now),
            )
        else:
            old_used = int(row["used_bytes"])
            old_total = int(row["last_xray_total"])
            # Xray stats can reset on service restart. If it goes down, count the new value as additional usage.
            delta = int(xray_total) - old_total
            if delta < 0:
                delta = int(xray_total)
            used = old_used + max(0, delta)
            self.conn.execute(
                "UPDATE counters SET used_bytes=?, last_xray_total=?, updated_at=? WHERE user_id=? AND period_key=?",
                (used, int(xray_total), now, user_id, pkey),
            )
        self.conn.commit()
        return int(used)

    def is_blocked(self, user_id: str, pkey: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM blocked_users WHERE user_id=? AND period_key=?",
            (user_id, pkey),
        ).fetchone()
        return row is not None

    def mark_blocked(self, user_id: str, pkey: str, limit_bytes: int, used_bytes: int, reason: str) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO blocked_users(user_id,period_key,limit_bytes,used_bytes,blocked_at,reason)
            VALUES(?,?,?,?,?,?)
            """,
            (user_id, pkey, int(limit_bytes), int(used_bytes), now_utc().isoformat(), reason),
        )
        self.conn.commit()


class XrayApi:
    def __init__(self, xray_bin: str, server: str, dry_run: bool):
        self.xray_bin = xray_bin
        self.server = server
        self.dry_run = dry_run

    def _run(self, args: List[str]) -> subprocess.CompletedProcess:
        cmd = [self.xray_bin, "api", *args, f"--server={self.server}"]
        LOG.debug("Run command: %s", " ".join(cmd))
        return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def stats(self) -> Dict[str, int]:
        proc = self._run(["statsquery", "-pattern", "user>>>"])
        if proc.returncode != 0:
            raise RuntimeError(f"xray statsquery failed: {proc.stderr.strip() or proc.stdout.strip()}")
        text = proc.stdout.strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"xray statsquery returned non-json output: {text[:300]}") from exc

        totals: Dict[str, int] = {}
        for item in payload.get("stat", []) or payload.get("stats", []):
            name = item.get("name") or item.get("Name")
            value = item.get("value") or item.get("Value") or 0
            if not name:
                continue
            m = STAT_RE.match(name)
            if not m:
                continue
            user = m.group("user")
            totals[user] = totals.get(user, 0) + int(value)
        return totals

    def remove_user(self, inbound_tag: str, user_id: str) -> bool:
        # xray api rmu --tag=<inbound> --email=<email> --server=...
        if self.dry_run:
            LOG.warning("DRY-RUN: would remove user=%s from inbound=%s", user_id, inbound_tag)
            return True
        proc = self._run(["rmu", f"--tag={inbound_tag}", f"--email={user_id}"])
        if proc.returncode == 0:
            LOG.warning("Removed user=%s from inbound=%s", user_id, inbound_tag)
            return True
        # If user is absent in one inbound, it is not fatal for the global decision.
        LOG.error("Failed to remove user=%s from inbound=%s: %s %s", user_id, inbound_tag, proc.stdout.strip(), proc.stderr.strip())
        return False


class RemnawaveClient:
    def __init__(self, cfg: dict):
        if requests is None:
            raise RuntimeError("Python package 'requests' is required for Remnawave API mode")
        self.cfg = cfg
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        self.token = str(cfg.get("token", ""))
        self.users_endpoint = str(cfg.get("users_endpoint", "/api/users"))
        self.page_limit = int(cfg.get("page_limit", 100))
        self.timeout = int(cfg.get("timeout_sec", 20))
        self.verify_tls = bool(cfg.get("verify_tls", True))
        self.id_fields = list(cfg.get("id_fields", ["uuid", "shortUuid", "username", "email"]))
        self.limit_fields = list(cfg.get("limit_fields", ["dataLimitBytes", "trafficLimitBytes", "dataLimit"]))
        self.status_allowlist = set(str(x).upper() for x in cfg.get("status_allowlist", []))
        self.limit_multiplier = float(cfg.get("limit_multiplier", 1.0))
        self.default_limit_bytes = int(cfg.get("default_limit_bytes", 0))

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "remna-node-quota/0.2.0",
        }

    def _extract_items(self, payload: Any) -> List[dict]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("users", "items", "data", "records", "result"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            if isinstance(val, dict):
                nested = self._extract_items(val)
                if nested:
                    return nested
        return []

    def _has_next(self, payload: Any, page: int, items_count: int) -> bool:
        if not isinstance(payload, dict):
            return False
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else payload
        for key in ("hasNextPage", "has_next", "hasMore"):
            if key in meta:
                return bool(meta[key])
        total_pages = meta.get("totalPages") or meta.get("total_pages")
        if total_pages:
            return page < int(total_pages)
        total = meta.get("total") or meta.get("totalCount") or meta.get("total_count")
        if total:
            return page * self.page_limit < int(total)
        return items_count >= self.page_limit

    def fetch_users_raw(self) -> List[dict]:
        if not self.base_url or not self.token:
            raise RuntimeError("Remnawave base_url/token are not configured")
        all_items: List[dict] = []
        for page in range(1, 10000):
            url = self.base_url + self.users_endpoint
            params = {"page": page, "limit": self.page_limit, "size": self.page_limit}
            resp = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout, verify=self.verify_tls)
            if resp.status_code >= 400:
                raise RuntimeError(f"Remnawave API error {resp.status_code}: {resp.text[:500]}")
            payload = resp.json()
            items = self._extract_items(payload)
            all_items.extend(items)
            if not self._has_next(payload, page, len(items)):
                break
        return all_items

    def build_managed_users(self) -> Dict[str, ManagedUser]:
        users = self.fetch_users_raw()
        result: Dict[str, ManagedUser] = {}
        for u in users:
            status = str(deep_get(u, "status") or deep_get(u, "state") or "").upper()
            if self.status_allowlist and status and status not in self.status_allowlist:
                continue
            identifiers: List[str] = []
            for field in self.id_fields:
                val = deep_get(u, field)
                if val is None:
                    continue
                if isinstance(val, list):
                    identifiers.extend(str(x) for x in val if x)
                else:
                    identifiers.append(str(val))
            identifiers = sorted(set(x.strip() for x in identifiers if str(x).strip()))
            if not identifiers:
                continue
            limit = 0
            for field in self.limit_fields:
                limit = parse_limit_to_bytes(deep_get(u, field), self.limit_multiplier)
                if limit > 0:
                    break
            if limit <= 0:
                limit = self.default_limit_bytes
            if limit <= 0:
                continue
            mu = ManagedUser(identifiers=identifiers, limit_bytes=limit, raw=u)
            for ident in identifiers:
                result[ident] = mu
        return result


class QuotaDaemon:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.db = QuotaDB(cfg.get("db_path", "/var/lib/remna-node-quota/quota.db"))
        self.xray = XrayApi(
            xray_bin=cfg.get("xray_bin", "/usr/local/bin/xray"),
            server=cfg.get("xray_api_server", "127.0.0.1:10085"),
            dry_run=bool(cfg.get("dry_run", True)),
        )
        self.period = cfg.get("period", "month")
        self.inbound_tags = list(cfg.get("inbound_tags", []))
        self.poll_interval = int(cfg.get("poll_interval_sec", 20))
        self.default_limit_bytes = int(cfg.get("default_limit_bytes", 0))
        self.local_users = cfg.get("users", {}) or {}
        self.remna_cfg = cfg.get("remnawave", {}) or {}
        self.remna_enabled = bool(self.remna_cfg.get("enabled", False))
        self.remna_client: Optional[RemnawaveClient] = RemnawaveClient(self.remna_cfg) if self.remna_enabled else None
        self.remna_cache: Dict[str, ManagedUser] = {}
        self.remna_last_refresh = 0.0
        self.remna_refresh_interval = int(self.remna_cfg.get("refresh_interval_sec", 300))

    def refresh_managed_users_if_needed(self, force: bool = False) -> Dict[str, ManagedUser]:
        managed: Dict[str, ManagedUser] = {}

        for user_id, data in self.local_users.items():
            limit = parse_limit_to_bytes(data.get("limit_bytes", 0) if isinstance(data, dict) else data)
            if limit > 0:
                managed[str(user_id)] = ManagedUser([str(user_id)], limit, {"source": "local"})

        if self.remna_client:
            now = time.time()
            if force or not self.remna_cache or now - self.remna_last_refresh >= self.remna_refresh_interval:
                try:
                    self.remna_cache = self.remna_client.build_managed_users()
                    self.remna_last_refresh = now
                    LOG.info("Remnawave users cache refreshed: %d identifiers", len(self.remna_cache))
                except Exception as exc:
                    LOG.exception("Failed to refresh Remnawave users: %s", exc)
                    if not bool(self.remna_cfg.get("fallback_to_local_users", False)):
                        raise
            managed.update(self.remna_cache)
        return managed

    def enforce_once(self) -> None:
        pkey = period_key(self.period)
        managed = self.refresh_managed_users_if_needed()
        if not managed:
            LOG.warning("No managed users with positive limits. Nothing to enforce.")
            return
        stats = self.xray.stats()
        LOG.info("Fetched Xray stats for %d users; managed identifiers=%d", len(stats), len(managed))
        for user_id, xray_total in sorted(stats.items()):
            mu = managed.get(user_id)
            if not mu:
                continue
            used = self.db.update_usage_from_xray_total(user_id, pkey, xray_total)
            LOG.info("user=%s period=%s used=%s limit=%s", user_id, pkey, bytes_human(used), bytes_human(mu.limit_bytes))
            if used < mu.limit_bytes:
                continue
            if self.db.is_blocked(user_id, pkey):
                # Remnawave can re-sync users. Keep removing to make block persistent on this node.
                LOG.warning("user=%s already marked blocked for period=%s; enforcing again", user_id, pkey)
            else:
                LOG.warning("quota exceeded: user=%s period=%s used=%s limit=%s", user_id, pkey, used, mu.limit_bytes)
                self.db.mark_blocked(user_id, pkey, mu.limit_bytes, used, "quota_exceeded")
            for tag in self.inbound_tags:
                self.xray.remove_user(tag, user_id)

    def run_forever(self) -> None:
        self.refresh_managed_users_if_needed(force=True)
        while True:
            try:
                self.enforce_once()
            except Exception as exc:
                LOG.exception("Iteration failed: %s", exc)
            time.sleep(self.poll_interval)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Per-node Remnawave/Xray traffic quota enforcer")
    parser.add_argument("-c", "--config", default="/etc/remna-node-quota/config.json", help="Path to config.json")
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    setup_logging(args.log_level)
    cfg = load_config(args.config)
    daemon = QuotaDaemon(cfg)
    if args.once:
        daemon.enforce_once()
        return 0
    daemon.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
