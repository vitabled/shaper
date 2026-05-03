#!/usr/bin/env python3
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
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

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
            "k": 1000, "kb": 1000,
            "m": 1000**2, "mb": 1000**2,
            "g": 1000**3, "gb": 1000**3,
            "t": 1000**4, "tb": 1000**4,
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


@dataclasses.dataclass
class ManagedUser:
    identifiers: List[str]
    limit_bytes: int
    raw: dict


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
        with self.lock:
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
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM blocked_users WHERE user_id=? AND period_key=?",
                (user_id, pkey),
            ).fetchone()
            return row is not None

    def mark_blocked(self, user_id: str, pkey: str, limit_bytes: int, used_bytes: int, reason: str) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO blocked_users(user_id,period_key,limit_bytes,used_bytes,blocked_at,reason)
                VALUES(?,?,?,?,?,?)
                """,
                (user_id, pkey, int(limit_bytes), int(used_bytes), now_utc().isoformat(), reason),
            )
            self.conn.commit()

    def list_counters(self, pkey: Optional[str] = None, limit: int = 200) -> List[dict]:
        with self.lock:
            if pkey:
                rows = self.conn.execute(
                    "SELECT * FROM counters WHERE period_key=? ORDER BY used_bytes DESC LIMIT ?",
                    (pkey, int(limit)),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM counters ORDER BY updated_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [dict(r) for r in rows]

    def list_blocked(self, pkey: Optional[str] = None, limit: int = 200) -> List[dict]:
        with self.lock:
            if pkey:
                rows = self.conn.execute(
                    "SELECT * FROM blocked_users WHERE period_key=? ORDER BY blocked_at DESC LIMIT ?",
                    (pkey, int(limit)),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM blocked_users ORDER BY blocked_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [dict(r) for r in rows]

    def reset_counter(self, user_id: str, pkey: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM counters WHERE user_id=? AND period_key=?", (user_id, pkey))
            self.conn.execute("DELETE FROM blocked_users WHERE user_id=? AND period_key=?", (user_id, pkey))
            self.conn.commit()


class XrayApi:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.xray_bin = cfg.get("xray_bin", "/usr/local/bin/xray")
        self.server = cfg.get("xray_api_server", "127.0.0.1:61000")
        self.dry_run = bool(cfg.get("dry_run", True))
        self.runner = cfg.get("xray_runner", {}) or {}
        self._prepared = False
        self._prepare_lock = threading.Lock()

    def _helper_path(self) -> str:
        return str(Path(__file__).with_name("xray_grpc_helper.py"))

    def prepare_container(self) -> None:
        mode = self.runner.get("mode", "local")
        if mode != "docker_grpc_exec":
            return
        with self._prepare_lock:
            if self._prepared:
                return
            container = self.runner.get("container", "remnanode")
            helper = self._helper_path()
            py = self.runner.get("python", "python3")
            auto_install = bool(self.runner.get("auto_install_deps", True))
            if auto_install:
                check = (
                    f"{py} - <<'PY'\n"
                    "import grpc, google.protobuf\n"
                    "print('grpc/protobuf OK')\n"
                    "PY"
                )
                proc = subprocess.run(["docker", "exec", "-i", container, "sh", "-lc", check], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                if proc.returncode != 0:
                    LOG.warning("grpc/protobuf are missing in container %s; installing via apk", container)
                    install = subprocess.run(
                        ["docker", "exec", container, "sh", "-lc", "apk add --no-cache python3 py3-grpcio py3-protobuf"],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    )
                    if install.returncode != 0:
                        raise RuntimeError(f"failed to install grpc deps in container: {install.stderr.strip() or install.stdout.strip()}")
            cp = subprocess.run(["docker", "cp", helper, f"{container}:/tmp/xray_grpc_helper.py"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if cp.returncode != 0:
                raise RuntimeError(f"failed to copy grpc helper into container: {cp.stderr.strip() or cp.stdout.strip()}")
            self._prepared = True

    def _run_grpc_helper(self, args: List[str]) -> subprocess.CompletedProcess:
        mode = self.runner.get("mode", "local")
        helper = self._helper_path()
        if mode == "docker_grpc_exec":
            self.prepare_container()
            container = self.runner.get("container", "remnanode")
            py = self.runner.get("python", "python3")
            cmd = ["docker", "exec", container, py, "/tmp/xray_grpc_helper.py", *args]
        else:
            cmd = [sys.executable, helper, *args]
        LOG.debug("Run gRPC helper: %s", " ".join(cmd))
        return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def _build_cli_cmd(self, args: List[str]) -> List[str]:
        mode = self.runner.get("mode", "local")
        if mode == "docker_exec":
            container = self.runner.get("container", "remnanode")
            binary = self.runner.get("bin", "rw-core")
            return ["docker", "exec", container, binary, "api", *args, f"--server={self.server}"]
        return [self.xray_bin, "api", *args, f"--server={self.server}"]

    def _run_cli(self, args: List[str]) -> subprocess.CompletedProcess:
        cmd = self._build_cli_cmd(args)
        LOG.debug("Run command: %s", " ".join(cmd))
        return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def stats(self) -> Dict[str, int]:
        mode = self.runner.get("mode", "local")
        if mode == "docker_grpc_exec":
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
        mode = self.runner.get("mode", "local")
        if mode == "docker_grpc_exec":
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
        self.limit_fields = list(cfg.get("limit_fields", ["trafficLimitBytes"]))
        self.status_allowlist = {str(x).upper() for x in cfg.get("status_allowlist", [])}
        self.limit_multiplier = float(cfg.get("limit_multiplier", 1.0))
        self.default_limit_bytes = int(cfg.get("default_limit_bytes", 0) or 0)
        self.last_raw_users: List[dict] = []

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "User-Agent": "remna-node-quota/0.4.0"}

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
        self.last_raw_users = all_items
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
            limit = 0
            for field in self.limit_fields:
                limit = parse_limit_to_bytes(deep_get(user, field), self.limit_multiplier)
                if limit > 0:
                    break
            if limit <= 0:
                limit = self.default_limit_bytes
            if limit <= 0:
                skipped_no_limit += 1
                continue
            mu = ManagedUser(identifiers=identifiers, limit_bytes=limit, raw=user)
            for identifier in identifiers:
                result[identifier] = mu
        LOG.info(
            "Remnawave users build result: users=%s identifiers=%s skipped_by_status=%s skipped_no_identifier=%s skipped_no_limit=%s",
            len(users), len(result), skipped_by_status, skipped_no_identifier, skipped_no_limit,
        )
        return result


class QuotaDaemon:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.db = QuotaDB(cfg.get("db_path", "/var/lib/remna-node-quota/quota.db"))
        self.xray = XrayApi(cfg)
        self.period = cfg.get("period", "month")
        self.inbound_tags = list(cfg.get("inbound_tags", []))
        self.poll_interval = int(cfg.get("poll_interval_sec", 20))
        self.local_users = cfg.get("users", {}) or {}
        self.remna_cfg = cfg.get("remnawave", {}) or {}
        self.remna_client: Optional[RemnawaveClient] = RemnawaveClient(self.remna_cfg) if bool(self.remna_cfg.get("enabled", False)) else None
        self.remna_cache: Dict[str, ManagedUser] = {}
        self.remna_last_refresh = 0.0
        self.remna_refresh_interval = int(self.remna_cfg.get("refresh_interval_sec", 300))
        self.state_lock = threading.Lock()
        self.last_enforce_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_stats_count = 0
        self.last_managed_count = 0
        self.last_matched_count = 0

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

    def enforce_once(self) -> dict:
        pkey = period_key(self.period)
        managed = self.refresh_managed_users_if_needed()
        if not managed:
            LOG.warning("No managed users with positive limits. Nothing to enforce.")
            with self.state_lock:
                self.last_enforce_at = now_utc().isoformat()
                self.last_managed_count = 0
                self.last_stats_count = 0
                self.last_matched_count = 0
            return {"period_key": pkey, "managed": 0, "stats": 0, "matched": 0, "blocked": 0}
        stats = self.xray.stats()
        LOG.info("Fetched Xray stats for %d users; managed identifiers=%d", len(stats), len(managed))
        blocked = 0
        matched = 0
        for user_id, xray_total in sorted(stats.items()):
            mu = managed.get(user_id)
            if not mu:
                LOG.debug("Unmanaged Xray stats user: %s", user_id)
                continue
            matched += 1
            used = self.db.update_usage_from_xray_total(user_id, pkey, xray_total)
            LOG.info("user=%s period=%s used=%s limit=%s", user_id, pkey, bytes_human(used), bytes_human(mu.limit_bytes))
            if used < mu.limit_bytes:
                continue
            if self.db.is_blocked(user_id, pkey):
                LOG.warning("user=%s already marked blocked for period=%s; enforcing again", user_id, pkey)
            else:
                LOG.warning("quota exceeded: user=%s period=%s used=%s limit=%s", user_id, pkey, used, mu.limit_bytes)
                self.db.mark_blocked(user_id, pkey, mu.limit_bytes, used, "quota_exceeded")
                blocked += 1
            for tag in self.inbound_tags:
                self.xray.remove_user(tag, user_id)
        if stats and matched == 0:
            LOG.warning("No Xray stats users matched Remnawave identifiers. Check id_fields.")
        with self.state_lock:
            self.last_enforce_at = now_utc().isoformat()
            self.last_error = None
            self.last_stats_count = len(stats)
            self.last_managed_count = len(managed)
            self.last_matched_count = matched
        return {"period_key": pkey, "managed": len(managed), "stats": len(stats), "matched": matched, "blocked": blocked}

    def get_status(self) -> dict:
        with self.state_lock:
            return {
                "ok": self.last_error is None,
                "dry_run": bool(self.cfg.get("dry_run", True)),
                "period": self.period,
                "period_key": period_key(self.period),
                "inbound_tags": self.inbound_tags,
                "last_enforce_at": self.last_enforce_at,
                "last_error": self.last_error,
                "last_stats_count": self.last_stats_count,
                "last_managed_count": self.last_managed_count,
                "last_matched_count": self.last_matched_count,
                "remnawave_cache_identifiers": len(self.remna_cache),
            }

    def list_managed_users(self, limit: int = 500) -> List[dict]:
        managed = self.refresh_managed_users_if_needed()
        out = []
        seen = set()
        for ident, mu in managed.items():
            key = tuple(mu.identifiers)
            if key in seen:
                continue
            seen.add(key)
            raw = mu.raw or {}
            out.append({
                "identifiers": mu.identifiers,
                "limit_bytes": mu.limit_bytes,
                "limit_human": bytes_human(mu.limit_bytes),
                "id": raw.get("id"),
                "username": raw.get("username"),
                "status": raw.get("status"),
            })
            if len(out) >= limit:
                break
        return out

    def force_block(self, user_id: str) -> dict:
        ok = []
        for tag in self.inbound_tags:
            ok.append({"tag": tag, "ok": self.xray.remove_user(tag, user_id)})
        self.db.mark_blocked(user_id, period_key(self.period), 0, 0, "manual_api_block")
        return {"user_id": user_id, "results": ok}

    def run_forever(self) -> None:
        self.xray.prepare_container()
        if bool((self.cfg.get("api") or {}).get("enabled", False)):
            start_api_server(self)
        self.refresh_managed_users_if_needed(force=True)
        while True:
            try:
                self.enforce_once()
            except Exception as exc:
                LOG.exception("Iteration failed: %s", exc)
                with self.state_lock:
                    self.last_error = str(exc)
            time.sleep(self.poll_interval)


class ApiHandler(BaseHTTPRequestHandler):
    daemon_ref: QuotaDaemon
    api_token: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("api: " + fmt, *args)

    def _send(self, status: int, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        if not self.api_token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.api_token}"

    def _require_auth(self) -> bool:
        if not self._auth_ok():
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        pkey = qs.get("period_key", [period_key(self.daemon_ref.period)])[0]
        limit = int(qs.get("limit", ["200"])[0])
        if parsed.path in ("/health", "/api/v1/health"):
            self._send(200, {"ok": True})
        elif parsed.path in ("/status", "/api/v1/status"):
            self._send(200, self.daemon_ref.get_status())
        elif parsed.path == "/api/v1/users":
            self._send(200, {"users": self.daemon_ref.list_managed_users(limit=limit)})
        elif parsed.path == "/api/v1/counters":
            self._send(200, {"period_key": pkey, "counters": self.daemon_ref.db.list_counters(pkey, limit=limit)})
        elif parsed.path == "/api/v1/blocked":
            self._send(200, {"period_key": pkey, "blocked": self.daemon_ref.db.list_blocked(pkey, limit=limit)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/api/v1/enforce":
            try:
                self._send(200, self.daemon_ref.enforce_once())
            except Exception as exc:
                LOG.exception("manual enforce failed: %s", exc)
                self._send(500, {"error": str(exc)})
            return
        m = re.match(r"^/api/v1/users/([^/]+)/block$", parsed.path)
        if m:
            self._send(200, self.daemon_ref.force_block(m.group(1)))
            return
        m = re.match(r"^/api/v1/users/([^/]+)/reset$", parsed.path)
        if m:
            pkey = qs.get("period_key", [period_key(self.daemon_ref.period)])[0]
            self.daemon_ref.db.reset_counter(m.group(1), pkey)
            self._send(200, {"ok": True, "user_id": m.group(1), "period_key": pkey})
            return
        self._send(404, {"error": "not found"})


def start_api_server(daemon: QuotaDaemon) -> None:
    api_cfg = daemon.cfg.get("api") or {}
    host = str(api_cfg.get("listen", "127.0.0.1"))
    port = int(api_cfg.get("port", 8765))
    token = str(api_cfg.get("token", "")).strip()
    ApiHandler.daemon_ref = daemon
    ApiHandler.api_token = token
    server = ThreadingHTTPServer((host, port), ApiHandler)
    thread = threading.Thread(target=server.serve_forever, name="quota-api", daemon=True)
    thread.start()
    LOG.info("HTTP API listening on http://%s:%s", host, port)


def setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Per-node Remnawave/Xray traffic quota enforcer")
    parser.add_argument("-c", "--config", default="/etc/remna-node-quota/config.json")
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    parser.add_argument("--prepare-container", action="store_true", help="Install/copy helper dependencies into remnanode container")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)
    setup_logging(args.log_level)
    cfg = load_config(args.config)
    daemon = QuotaDaemon(cfg)
    if args.prepare_container:
        daemon.xray.prepare_container()
        return 0
    if args.once:
        daemon.enforce_once()
        return 0
    daemon.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
