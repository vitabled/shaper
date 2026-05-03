#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

try:
    import requests
except ImportError:
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


def save_config(path: str, cfg: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def bytes_human(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    v = float(value)
    for u in units:
        if abs(v) < 1024.0 or u == units[-1]:
            return f"{v:.2f} {u}"
        v /= 1024.0
    return f"{value} B"


def parse_limit_to_bytes(value: Any, multiplier: float = 1.0) -> int:
    if value is None or isinstance(value, bool):
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
            "": 1, "b": 1,
            "k": 1000, "kb": 1000, "m": 1000**2, "mb": 1000**2,
            "g": 1000**3, "gb": 1000**3, "t": 1000**4, "tb": 1000**4,
            "p": 1000**5, "pb": 1000**5,
            "kib": 1024, "mib": 1024**2, "gib": 1024**3,
            "tib": 1024**4, "pib": 1024**5,
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


def deep_find_user_list(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("users"), list):
        return [x for x in response["users"] if isinstance(x, dict)]
    for key in ("users", "items", "data", "records", "result", "response", "rows"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = deep_find_user_list(value)
            if nested:
                return nested
    return []


def read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def limit_from_payload(payload: dict) -> int:
    for key in ("limit_bytes", "limitBytes", "bytes"):
        if key in payload:
            return parse_limit_to_bytes(payload[key])
    for key in ("limit_gib", "limitGiB", "gib"):
        if key in payload:
            return int(float(payload[key]) * 1024**3)
    for key in ("limit_gb", "limitGB", "gb"):
        if key in payload:
            return int(float(payload[key]) * 1000**3)
    for key in ("limit", "value"):
        if key in payload:
            return parse_limit_to_bytes(payload[key])
    return 0


@dataclasses.dataclass
class ManagedUser:
    identifiers: List[str]
    limit_bytes: int
    raw: dict


@dataclasses.dataclass
class InboundLimit:
    tag: str
    limit_bytes: int
    enabled: bool = True
    source: str = "config"


class QuotaDB:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.init()

    def init(self) -> None:
        with self.lock:
            # Legacy tables are intentionally kept for compatibility.
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS counters (
                    user_id TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    used_bytes INTEGER NOT NULL DEFAULT 0,
                    last_xray_total INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, period_key)
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    limit_bytes INTEGER NOT NULL,
                    used_bytes INTEGER NOT NULL,
                    blocked_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY (user_id, period_key)
                )
            """)
            # New per-inbound tables.
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS inbound_counters (
                    user_id TEXT NOT NULL,
                    inbound_tag TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    used_bytes INTEGER NOT NULL DEFAULT 0,
                    last_xray_total INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, inbound_tag, period_key)
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS inbound_blocked_users (
                    user_id TEXT NOT NULL,
                    inbound_tag TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    limit_bytes INTEGER NOT NULL,
                    used_bytes INTEGER NOT NULL,
                    blocked_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY (user_id, inbound_tag, period_key)
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS user_inbound_limits (
                    user_id TEXT NOT NULL,
                    inbound_tag TEXT NOT NULL,
                    limit_bytes INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, inbound_tag)
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS user_node_limits (
                    user_id TEXT NOT NULL PRIMARY KEY,
                    limit_bytes INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            self.conn.commit()

    def update_inbound_usage_from_xray_total(self, user_id: str, inbound_tag: str, pkey: str, xray_total: int) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT used_bytes,last_xray_total FROM inbound_counters WHERE user_id=? AND inbound_tag=? AND period_key=?",
                (user_id, inbound_tag, pkey),
            ).fetchone()
            now = now_utc().isoformat()
            if row is None:
                used = max(0, int(xray_total))
                self.conn.execute(
                    "INSERT INTO inbound_counters(user_id,inbound_tag,period_key,used_bytes,last_xray_total,updated_at) VALUES(?,?,?,?,?,?)",
                    (user_id, inbound_tag, pkey, used, int(xray_total), now),
                )
            else:
                old_used = int(row["used_bytes"])
                old_total = int(row["last_xray_total"])
                delta = int(xray_total) - old_total
                if delta < 0:
                    delta = int(xray_total)
                used = old_used + max(0, delta)
                self.conn.execute(
                    "UPDATE inbound_counters SET used_bytes=?, last_xray_total=?, updated_at=? WHERE user_id=? AND inbound_tag=? AND period_key=?",
                    (used, int(xray_total), now, user_id, inbound_tag, pkey),
                )
            self.conn.commit()
            return int(used)

    def is_inbound_blocked(self, user_id: str, inbound_tag: str, pkey: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM inbound_blocked_users WHERE user_id=? AND inbound_tag=? AND period_key=?",
                (user_id, inbound_tag, pkey),
            ).fetchone()
            return row is not None

    def mark_inbound_blocked(self, user_id: str, inbound_tag: str, pkey: str, limit_bytes: int, used_bytes: int, reason: str) -> None:
        with self.lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO inbound_blocked_users(user_id,inbound_tag,period_key,limit_bytes,used_bytes,blocked_at,reason)
                VALUES(?,?,?,?,?,?,?)
            """, (user_id, inbound_tag, pkey, int(limit_bytes), int(used_bytes), now_utc().isoformat(), reason))
            self.conn.commit()

    def list_counters(self, limit: int = 500) -> List[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM inbound_counters ORDER BY updated_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
            return [dict(r) for r in rows]

    def list_blocked(self, limit: int = 500) -> List[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM inbound_blocked_users ORDER BY blocked_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
            return [dict(r) for r in rows]

    def reset_user(self, user_id: str, pkey: str, inbound_tag: Optional[str] = None) -> None:
        with self.lock:
            if inbound_tag:
                self.conn.execute("DELETE FROM inbound_counters WHERE user_id=? AND inbound_tag=? AND period_key=?", (user_id, inbound_tag, pkey))
                self.conn.execute("DELETE FROM inbound_blocked_users WHERE user_id=? AND inbound_tag=? AND period_key=?", (user_id, inbound_tag, pkey))
            else:
                self.conn.execute("DELETE FROM inbound_counters WHERE user_id=? AND period_key=?", (user_id, pkey))
                self.conn.execute("DELETE FROM inbound_blocked_users WHERE user_id=? AND period_key=?", (user_id, pkey))
            self.conn.commit()

    def set_user_inbound_limit(self, user_id: str, inbound_tag: str, limit_bytes: int) -> None:
        with self.lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO user_inbound_limits(user_id,inbound_tag,limit_bytes,updated_at)
                VALUES(?,?,?,?)
            """, (str(user_id), str(inbound_tag), int(limit_bytes), now_utc().isoformat()))
            self.conn.commit()

    def delete_user_inbound_limit(self, user_id: str, inbound_tag: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM user_inbound_limits WHERE user_id=? AND inbound_tag=?", (str(user_id), str(inbound_tag)))
            self.conn.commit()

    def get_user_inbound_limit(self, user_id: str, inbound_tag: str) -> Optional[int]:
        with self.lock:
            row = self.conn.execute(
                "SELECT limit_bytes FROM user_inbound_limits WHERE user_id=? AND inbound_tag=?",
                (str(user_id), str(inbound_tag)),
            ).fetchone()
            return int(row["limit_bytes"]) if row else None

    def list_user_inbound_limits(self) -> List[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM user_inbound_limits ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]

    def set_user_node_limit(self, user_id: str, limit_bytes: int) -> None:
        with self.lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO user_node_limits(user_id,limit_bytes,updated_at)
                VALUES(?,?,?)
            """, (str(user_id), int(limit_bytes), now_utc().isoformat()))
            self.conn.commit()

    def delete_user_node_limit(self, user_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM user_node_limits WHERE user_id=?", (str(user_id),))
            self.conn.commit()

    def get_user_node_limit(self, user_id: str) -> Optional[int]:
        with self.lock:
            row = self.conn.execute("SELECT limit_bytes FROM user_node_limits WHERE user_id=?", (str(user_id),)).fetchone()
            return int(row["limit_bytes"]) if row else None

    def list_user_node_limits(self) -> List[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM user_node_limits ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]


class XrayApi:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.xray_bin = cfg.get("xray_bin", "/usr/local/bin/xray")
        self.server = cfg.get("xray_api_server", "127.0.0.1:61000")
        self.dry_run = bool(cfg.get("dry_run", True))
        self.runner = cfg.get("xray_runner", {}) or {}

    def _helper_path(self) -> str:
        return str(Path(__file__).with_name("xray_grpc_helper.py"))

    def _run_grpc_helper(self, args: List[str]) -> subprocess.CompletedProcess:
        mode = self.runner.get("mode", "local")
        helper = self._helper_path()
        if mode == "docker_grpc_exec":
            container = self.runner.get("container", "remnanode")
            py = self.runner.get("python", "python3")
            cp = subprocess.run(["docker", "cp", helper, f"{container}:/tmp/xray_grpc_helper.py"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if cp.returncode != 0:
                return cp
            cmd = ["docker", "exec", container, py, "/tmp/xray_grpc_helper.py", *args]
        else:
            cmd = [sys.executable, helper, *args]
        LOG.debug("Run gRPC helper: %s", " ".join(cmd))
        return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def _run_cli(self, args: List[str]) -> subprocess.CompletedProcess:
        mode = self.runner.get("mode", "local")
        if mode == "docker_exec":
            container = self.runner.get("container", "remnanode")
            binary = self.runner.get("bin", "rw-core")
            cmd = ["docker", "exec", container, binary, "api", *args, f"--server={self.server}"]
        else:
            cmd = [self.xray_bin, "api", *args, f"--server={self.server}"]
        LOG.debug("Run command: %s", " ".join(cmd))
        return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def stats(self) -> Dict[str, int]:
        if self.runner.get("mode") == "docker_grpc_exec":
            proc = self._run_grpc_helper(["stats", self.server, "user>>>"])
        else:
            proc = self._run_cli(["statsquery", "-pattern", "user>>>"])
        if proc.returncode != 0:
            raise RuntimeError(f"xray statsquery failed: {proc.stderr.strip() or proc.stdout.strip()}")
        text = proc.stdout.strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"xray statsquery returned non-json output: {text[:500]}") from exc
        totals: Dict[str, int] = {}
        for item in payload.get("stat", []) or payload.get("stats", []):
            name = item.get("name") or item.get("Name")
            value = item.get("value") or item.get("Value") or 0
            if not name:
                continue
            match = STAT_RE.match(name)
            if not match:
                continue
            user = match.group("user")
            totals[user] = totals.get(user, 0) + int(value)
        return totals

    def remove_user(self, inbound_tag: str, user_id: str) -> bool:
        if self.dry_run:
            LOG.warning("DRY-RUN: would remove user=%s from inbound=%s", user_id, inbound_tag)
            return True
        if self.runner.get("mode") == "docker_grpc_exec":
            proc = self._run_grpc_helper(["rmu", self.server, inbound_tag, user_id])
        else:
            proc = self._run_cli(["rmu", f"--tag={inbound_tag}", f"--email={user_id}"])
        if proc.returncode == 0:
            LOG.warning("Removed user=%s from inbound=%s", user_id, inbound_tag)
            return True
        LOG.error("Failed to remove user=%s from inbound=%s: stdout=%s stderr=%s", user_id, inbound_tag, proc.stdout.strip(), proc.stderr.strip())
        return False


class RemnawaveClient:
    def __init__(self, cfg: dict):
        if requests is None:
            raise RuntimeError("Python package 'requests' is required for Remnawave API mode")
        self.cfg = cfg
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        self.token = str(cfg.get("token", "")).strip()
        self.users_endpoint = str(cfg.get("users_endpoint", "/api/users"))
        self.page_limit = int(cfg.get("page_limit", 100))
        self.timeout = int(cfg.get("timeout_sec", 20))
        self.verify_tls = bool(cfg.get("verify_tls", True))
        self.id_fields = list(cfg.get("id_fields", ["id", "uuid", "shortUuid", "username", "email", "vlessUuid", "trojanPassword", "ssPassword"]))
        self.status_allowlist = {str(x).upper() for x in cfg.get("status_allowlist", [])}
        self.use_panel_traffic_limit = bool(cfg.get("use_panel_traffic_limit", False))
        self.limit_fields = list(cfg.get("limit_fields", ["trafficLimitBytes"]))
        self.limit_multiplier = float(cfg.get("limit_multiplier", 1.0))
        self.per_node_limit_bytes = int(cfg.get("per_node_limit_bytes", 0) or 0)
        self.default_limit_bytes = int(cfg.get("default_limit_bytes", 0) or 0)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "User-Agent": "remna-node-quota/0.5.0"}

    def _has_next(self, payload: Any, page: int, items_count: int) -> bool:
        if not isinstance(payload, dict):
            return False
        response = payload.get("response")
        if isinstance(response, dict):
            total = response.get("total")
            if total is not None:
                return page * self.page_limit < int(total)
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
            items = deep_find_user_list(payload)
            LOG.info("Remnawave API page=%s raw users parsed: %s", page, len(items))
            all_items.extend(items)
            if not self._has_next(payload, page, len(items)):
                break
        return all_items

    def build_managed_users(self) -> Dict[str, ManagedUser]:
        users = self.fetch_users_raw()
        result: Dict[str, ManagedUser] = {}
        skipped_by_status = skipped_no_identifier = skipped_no_limit = 0
        for user in users:
            if not isinstance(user, dict):
                continue
            status = str(deep_get(user, "status") or deep_get(user, "state") or deep_get(user, "userStatus") or deep_get(user, "subscriptionStatus") or "").upper()
            if self.status_allowlist and status and status not in self.status_allowlist:
                skipped_by_status += 1
                continue
            identifiers: List[str] = []
            for field in self.id_fields:
                value = deep_get(user, field)
                if value is None:
                    continue
                if isinstance(value, list):
                    identifiers.extend(str(x) for x in value if x)
                else:
                    identifiers.append(str(value))
            identifiers = sorted(set(x.strip() for x in identifiers if str(x).strip()))
            if not identifiers:
                skipped_no_identifier += 1
                continue
            if self.use_panel_traffic_limit:
                limit = 0
                for field in self.limit_fields:
                    limit = parse_limit_to_bytes(deep_get(user, field), self.limit_multiplier)
                    if limit > 0:
                        break
                if limit <= 0:
                    limit = self.default_limit_bytes
            else:
                limit = self.per_node_limit_bytes
            if limit <= 0:
                skipped_no_limit += 1
                continue
            mu = ManagedUser(identifiers=identifiers, limit_bytes=limit, raw=user)
            for ident in identifiers:
                result[ident] = mu
        LOG.info(
            "Remnawave users build result: users=%s identifiers=%s skipped_by_status=%s skipped_no_identifier=%s skipped_no_limit=%s limit_source=%s per_node_limit=%s",
            len(users), len(result), skipped_by_status, skipped_no_identifier, skipped_no_limit,
            "panel" if self.use_panel_traffic_limit else "local_per_node", bytes_human(self.per_node_limit_bytes),
        )
        return result


class QuotaDaemon:
    def __init__(self, cfg: dict, config_path: str = "/etc/remna-node-quota/config.json"):
        self.cfg = cfg
        self.config_path = config_path
        self.db = QuotaDB(cfg.get("db_path", "/var/lib/remna-node-quota/quota.db"))
        self.xray = XrayApi(cfg)
        self.period = cfg.get("period", "day")
        self.poll_interval = int(cfg.get("poll_interval_sec", 20))
        self.per_node_limit_bytes = int(cfg.get("per_node_limit_bytes", 0) or 0)
        self.usage_scope = str(cfg.get("usage_scope", "user_total_shared_across_inbounds"))
        self.local_users = cfg.get("users", {}) or {}
        self.remna_cfg = dict(cfg.get("remnawave", {}) or {})
        if "per_node_limit_bytes" not in self.remna_cfg:
            self.remna_cfg["per_node_limit_bytes"] = self.per_node_limit_bytes
        self.remna_enabled = bool(self.remna_cfg.get("enabled", False))
        self.remna_client: Optional[RemnawaveClient] = RemnawaveClient(self.remna_cfg) if self.remna_enabled else None
        self.remna_cache: Dict[str, ManagedUser] = {}
        self.remna_last_refresh = 0.0
        self.remna_refresh_interval = int(self.remna_cfg.get("refresh_interval_sec", 300))
        self.last_enforce: Dict[str, Any] = {"ok": None, "at": None, "error": None}

    def inbound_limits(self) -> Dict[str, InboundLimit]:
        result: Dict[str, InboundLimit] = {}
        inbounds = self.cfg.get("inbounds")
        if isinstance(inbounds, dict):
            for tag, item in inbounds.items():
                if isinstance(item, dict):
                    limit = parse_limit_to_bytes(item.get("limit_bytes", item.get("limit", self.per_node_limit_bytes)))
                    enabled = bool(item.get("enabled", True))
                else:
                    limit = parse_limit_to_bytes(item)
                    enabled = True
                result[str(tag)] = InboundLimit(tag=str(tag), limit_bytes=limit, enabled=enabled, source="config.inbounds")
        else:
            for tag in self.cfg.get("inbound_tags", []) or []:
                result[str(tag)] = InboundLimit(tag=str(tag), limit_bytes=self.per_node_limit_bytes, enabled=True, source="legacy.inbound_tags")
        return result

    def set_global_inbound_limit(self, inbound_tag: str, limit_bytes: int, enabled: bool = True) -> None:
        if "inbounds" not in self.cfg or not isinstance(self.cfg.get("inbounds"), dict):
            self.cfg["inbounds"] = {tag: {"limit_bytes": self.per_node_limit_bytes, "enabled": True} for tag in self.cfg.get("inbound_tags", []) or []}
        self.cfg["inbounds"][inbound_tag] = {"limit_bytes": int(limit_bytes), "enabled": bool(enabled)}
        save_config(self.config_path, self.cfg)

    def effective_limit(self, user_id: str, inbound_tag: str) -> int:
        inbound_override = self.db.get_user_inbound_limit(user_id, inbound_tag)
        if inbound_override is not None:
            return int(inbound_override)
        node_override = self.db.get_user_node_limit(user_id)
        if node_override is not None:
            return int(node_override)
        inbound = self.inbound_limits().get(inbound_tag)
        if inbound:
            return int(inbound.limit_bytes)
        return int(self.per_node_limit_bytes)

    def effective_limit_source(self, user_id: str, inbound_tag: str) -> str:
        if self.db.get_user_inbound_limit(user_id, inbound_tag) is not None:
            return "user_inbound_override"
        if self.db.get_user_node_limit(user_id) is not None:
            return "user_node_override"
        if inbound_tag in self.inbound_limits():
            return "inbound_global"
        return "node_default"

    def refresh_managed_users_if_needed(self, force: bool = False) -> Dict[str, ManagedUser]:
        managed: Dict[str, ManagedUser] = {}
        for user_id, data in self.local_users.items():
            limit = parse_limit_to_bytes(data.get("limit_bytes", 0) if isinstance(data, dict) else data)
            if limit <= 0:
                limit = self.per_node_limit_bytes
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

    def users_with_limits(self, refresh: bool = False, include_raw: bool = False) -> List[dict]:
        users = self.refresh_managed_users_if_needed(force=refresh)
        inbounds = self.inbound_limits()
        out = []
        for identifier, user in sorted(users.items()):
            node_override = self.db.get_user_node_limit(identifier)
            item = {
                "identifier": identifier,
                "node_limit_override_bytes": node_override,
                "node_limit_override": bytes_human(node_override) if node_override is not None else None,
                "inbounds": {
                    tag: {
                        "limit_bytes": self.effective_limit(identifier, tag),
                        "limit": bytes_human(self.effective_limit(identifier, tag)),
                        "global_limit_bytes": inbound.limit_bytes,
                        "global_limit": bytes_human(inbound.limit_bytes),
                        "user_inbound_override_bytes": self.db.get_user_inbound_limit(identifier, tag),
                        "override": self.db.get_user_inbound_limit(identifier, tag) is not None,
                        "limit_source": self.effective_limit_source(identifier, tag),
                        "enabled": inbound.enabled,
                    }
                    for tag, inbound in inbounds.items()
                },
            }
            if include_raw:
                item["raw"] = user.raw
            out.append(item)
        return out

    def enforce_once(self) -> Dict[str, Any]:
        pkey = period_key(self.period)
        result: Dict[str, Any] = {"period_key": pkey, "matched": 0, "blocked": 0, "stats_users": 0, "managed_identifiers": 0, "inbounds": {}, "usage_scope": self.usage_scope}
        try:
            managed = self.refresh_managed_users_if_needed()
            inbounds = self.inbound_limits()
            result["managed_identifiers"] = len(managed)
            result["inbounds"] = {tag: dataclasses.asdict(v) for tag, v in inbounds.items()}
            if not managed:
                LOG.warning("No managed users with positive limits. Nothing to enforce.")
                result.update({"ok": True, "warning": "no_managed_users"})
                return result
            if not inbounds:
                LOG.warning("No configured inbounds. Nothing to enforce.")
                result.update({"ok": True, "warning": "no_inbounds"})
                return result
            stats = self.xray.stats()
            result["stats_users"] = len(stats)
            LOG.info("Fetched Xray stats for %d users; managed identifiers=%d", len(stats), len(managed))
            if not stats:
                LOG.warning("Xray stats returned zero users. Check active traffic and stats policy.")
                result.update({"ok": True, "warning": "no_xray_stats"})
                return result
            for user_id, xray_total in sorted(stats.items()):
                mu = managed.get(user_id)
                if not mu:
                    LOG.debug("Unmanaged Xray stats user: %s", user_id)
                    continue
                result["matched"] += 1
                for tag, inbound in inbounds.items():
                    if not inbound.enabled:
                        continue
                    limit = self.effective_limit(user_id, tag)
                    if limit <= 0:
                        continue
                    used = self.db.update_inbound_usage_from_xray_total(user_id, tag, pkey, xray_total)
                    LOG.info("user=%s inbound=%s period=%s used=%s limit=%s", user_id, tag, pkey, bytes_human(used), bytes_human(limit))
                    if used < limit:
                        continue
                    if not self.db.is_inbound_blocked(user_id, tag, pkey):
                        LOG.warning("quota exceeded: user=%s inbound=%s period=%s used=%s limit=%s", user_id, tag, pkey, used, limit)
                        self.db.mark_inbound_blocked(user_id, tag, pkey, limit, used, "quota_exceeded")
                    else:
                        LOG.warning("user=%s already marked blocked for inbound=%s period=%s; enforcing again", user_id, tag, pkey)
                    if self.xray.remove_user(tag, user_id):
                        result["blocked"] += 1
            if result["matched"] == 0:
                LOG.warning("No Xray stats users matched Remnawave identifiers. Check id_fields.")
            result["ok"] = True
            return result
        except Exception as exc:
            result.update({"ok": False, "error": str(exc)})
            raise
        finally:
            result["at"] = now_utc().isoformat()
            self.last_enforce = result

    def block_user(self, user_id: str, inbound_tag: Optional[str] = None) -> Dict[str, Any]:
        tags = [inbound_tag] if inbound_tag else list(self.inbound_limits().keys())
        ok = []
        for tag in tags:
            ok.append({"tag": tag, "ok": self.xray.remove_user(tag, user_id)})
        return {"user_id": user_id, "results": ok}

    def run_forever(self) -> None:
        self.refresh_managed_users_if_needed(force=True)
        while True:
            try:
                self.enforce_once()
            except Exception as exc:
                LOG.exception("Iteration failed: %s", exc)
            time.sleep(self.poll_interval)


class ApiHandler(BaseHTTPRequestHandler):
    daemon_ref: QuotaDaemon = None  # type: ignore
    token: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("api: " + fmt, *args)

    def _auth_ok(self) -> bool:
        if not self.token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}"

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self) -> bool:
        if not self._auth_ok():
            self._send(401, {"ok": False, "error": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._require_auth():
            return
        d = self.daemon_ref
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/v1/status":
                api_cfg = d.cfg.get("api", {}) or {}
                self._send(200, {"ok": True, "dry_run": d.xray.dry_run, "period": d.period, "usage_scope": d.usage_scope, "per_node_limit_bytes": d.per_node_limit_bytes, "per_node_limit": bytes_human(d.per_node_limit_bytes), "inbounds": {k: dataclasses.asdict(v) for k, v in d.inbound_limits().items()}, "api": {"enabled": bool(api_cfg.get("enabled", False)), "listen": api_cfg.get("listen", api_cfg.get("host", "127.0.0.1")), "port": int(api_cfg.get("port", 8765)), "token_set": bool(api_cfg.get("token"))}, "remnawave_cache_identifiers": len(d.remna_cache), "last_enforce": d.last_enforce})
            elif parsed.path in ("/api/v1/inbounds", "/api/v1/limits", "/api/v1/node/limits"):
                inbound_overrides = d.db.list_user_inbound_limits()
                node_overrides = d.db.list_user_node_limits()
                self._send(200, {"ok": True, "period": d.period, "usage_scope": d.usage_scope, "global_inbound_limits": {k: {**dataclasses.asdict(v), "limit": bytes_human(v.limit_bytes)} for k, v in d.inbound_limits().items()}, "user_node_overrides": node_overrides, "user_inbound_overrides": inbound_overrides})
            elif parsed.path == "/api/v1/users":
                refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
                include_raw = qs.get("raw", ["0"])[0] in ("1", "true", "yes")
                users = d.users_with_limits(refresh=refresh, include_raw=include_raw)
                self._send(200, {"ok": True, "count": len(users), "users": users})
            else:
                m = re.match(r"^/api/v1/users/([^/]+)/limits$", parsed.path)
                if m:
                    user_id = unquote(m.group(1))
                    users = {u["identifier"]: u for u in d.users_with_limits(refresh=False)}
                    self._send(200, {"ok": True, "user_id": user_id, "limits": users.get(user_id, {"identifier": user_id, "inbounds": {}}).get("inbounds", {})})
                    return
                if parsed.path == "/api/v1/counters":
                    self._send(200, {"ok": True, "counters": d.db.list_counters(int(qs.get("limit", [500])[0]))})
                elif parsed.path == "/api/v1/blocked":
                    self._send(200, {"ok": True, "blocked": d.db.list_blocked(int(qs.get("limit", [500])[0]))})
                else:
                    self._send(404, {"ok": False, "error": "not_found"})
        except Exception as exc:
            LOG.exception("API GET failed: %s", exc)
            self._send(500, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        d = self.daemon_ref
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/v1/enforce":
                self._send(200, d.enforce_once())
                return
            m = re.match(r"^/api/v1/users/([^/]+)/block$", parsed.path)
            if m:
                inbound_tag = qs.get("inbound_tag", [None])[0]
                self._send(200, {"ok": True, **d.block_user(unquote(m.group(1)), inbound_tag)})
                return
            m = re.match(r"^/api/v1/users/([^/]+)/reset$", parsed.path)
            if m:
                pkey = qs.get("period_key", [period_key(d.period)])[0]
                inbound_tag = qs.get("inbound_tag", [None])[0]
                d.db.reset_user(unquote(m.group(1)), pkey, inbound_tag)
                self._send(200, {"ok": True, "user_id": unquote(m.group(1)), "inbound_tag": inbound_tag, "period_key": pkey})
                return
            m = re.match(r"^/api/v1/users/([^/]+)/limit$", parsed.path)
            if m:
                user_id = unquote(m.group(1))
                payload = read_json_body(self)
                limit = limit_from_payload(payload)
                if limit <= 0:
                    self._send(400, {"ok": False, "error": "limit must be positive", "accepted_fields": ["limit_bytes", "limit_gib", "limit_gb", "limit"]})
                    return
                d.db.set_user_node_limit(user_id, limit)
                self._send(200, {"ok": True, "user_id": user_id, "limit_bytes": limit, "limit": bytes_human(limit), "scope": "user_node"})
                return
            m = re.match(r"^/api/v1/users/([^/]+)/inbounds/([^/]+)/limit$", parsed.path)
            if m:
                user_id = unquote(m.group(1))
                inbound_tag = unquote(m.group(2))
                payload = read_json_body(self)
                limit = limit_from_payload(payload)
                if limit <= 0:
                    self._send(400, {"ok": False, "error": "limit must be positive", "accepted_fields": ["limit_bytes", "limit_gib", "limit_gb", "limit"]})
                    return
                d.db.set_user_inbound_limit(user_id, inbound_tag, limit)
                self._send(200, {"ok": True, "user_id": user_id, "inbound_tag": inbound_tag, "limit_bytes": limit, "limit": bytes_human(limit), "scope": "user_inbound"})
                return
            if parsed.path == "/api/v1/api-token":
                payload = read_json_body(self)
                new_token = str(payload.get("token", "")).strip()
                if not new_token and bool(payload.get("generate", False)):
                    new_token = secrets.token_urlsafe(32)
                if len(new_token) < 16:
                    self._send(400, {"ok": False, "error": "token must be at least 16 characters or use {\"generate\": true}"})
                    return
                d.cfg.setdefault("api", {})["token"] = new_token
                save_config(d.config_path, d.cfg)
                ApiHandler.token = new_token
                self._send(200, {"ok": True, "token_changed": True, "token": new_token if bool(payload.get("return_token", False)) else None})
                return
            m = re.match(r"^/api/v1/inbounds/([^/]+)/limit$", parsed.path)
            if m:
                inbound_tag = unquote(m.group(1))
                payload = read_json_body(self)
                limit = limit_from_payload(payload)
                if limit <= 0:
                    self._send(400, {"ok": False, "error": "limit must be positive"})
                    return
                enabled = bool(payload.get("enabled", True))
                d.set_global_inbound_limit(inbound_tag, limit, enabled=enabled)
                self._send(200, {"ok": True, "inbound_tag": inbound_tag, "limit_bytes": limit, "limit": bytes_human(limit), "enabled": enabled})
                return
            self._send(404, {"ok": False, "error": "not_found"})
        except Exception as exc:
            LOG.exception("API POST failed: %s", exc)
            self._send(500, {"ok": False, "error": str(exc)})

    def do_DELETE(self) -> None:
        if not self._require_auth():
            return
        d = self.daemon_ref
        parsed = urlparse(self.path)
        try:
            m = re.match(r"^/api/v1/users/([^/]+)/limit$", parsed.path)
            if m:
                user_id = unquote(m.group(1))
                d.db.delete_user_node_limit(user_id)
                self._send(200, {"ok": True, "user_id": user_id, "deleted": True, "scope": "user_node"})
                return
            m = re.match(r"^/api/v1/users/([^/]+)/inbounds/([^/]+)/limit$", parsed.path)
            if m:
                user_id = unquote(m.group(1))
                inbound_tag = unquote(m.group(2))
                d.db.delete_user_inbound_limit(user_id, inbound_tag)
                self._send(200, {"ok": True, "user_id": user_id, "inbound_tag": inbound_tag, "deleted": True, "scope": "user_inbound"})
                return
            self._send(404, {"ok": False, "error": "not_found"})
        except Exception as exc:
            LOG.exception("API DELETE failed: %s", exc)
            self._send(500, {"ok": False, "error": str(exc)})


def run_api_server(daemon: QuotaDaemon, cfg: dict) -> Optional[ThreadingHTTPServer]:
    api_cfg = cfg.get("api", {}) or {}
    if not bool(api_cfg.get("enabled", False)):
        return None
    listen = str(api_cfg.get("listen", api_cfg.get("host", "127.0.0.1")))
    port = int(api_cfg.get("port", 8765))
    ApiHandler.daemon_ref = daemon
    ApiHandler.token = str(api_cfg.get("token", ""))
    server = ThreadingHTTPServer((listen, port), ApiHandler)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    LOG.info("Local API listening on http://%s:%s", listen, port)
    return server


def setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def prepare_container(cfg: dict) -> int:
    runner = cfg.get("xray_runner", {}) or {}
    if runner.get("mode") != "docker_grpc_exec":
        LOG.info("xray_runner mode is not docker_grpc_exec; nothing to prepare")
        return 0
    container = runner.get("container", "remnanode")
    cmd = ["docker", "exec", container, "sh", "-lc", "python3 - <<'PY'\nimport grpc, google.protobuf\nprint('grpc/protobuf OK')\nPY"]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode == 0:
        LOG.info("Container %s already has grpc/protobuf", container)
        return 0
    LOG.info("Installing grpc/protobuf into container %s", container)
    proc = subprocess.run(["docker", "exec", container, "sh", "-lc", "apk add --no-cache py3-grpcio py3-protobuf"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        LOG.error("Failed to prepare container: stdout=%s stderr=%s", proc.stdout, proc.stderr)
    return proc.returncode


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Per-node Remnawave/Xray traffic quota enforcer")
    parser.add_argument("-c", "--config", default="/etc/remna-node-quota/config.json")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--prepare-container", action="store_true")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    setup_logging(args.log_level)
    cfg = load_config(args.config)
    if args.prepare_container:
        return prepare_container(cfg)
    daemon = QuotaDaemon(cfg, config_path=args.config)
    if args.once:
        daemon.enforce_once()
        return 0
    run_api_server(daemon, cfg)
    daemon.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
