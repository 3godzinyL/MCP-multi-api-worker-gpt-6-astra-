"""Local, credential-free accounting. Never persists prompts or response text."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


def normalize_usage(value):
    if not isinstance(value, dict):
        return None

    def count(number):
        return number if type(number) is int and 0 <= number <= 10**15 else None

    incoming, outgoing = count(value.get("input_tokens")), count(value.get("output_tokens"))
    if incoming is None or outgoing is None:
        return None
    input_details = value.get("input_tokens_details") or {}
    output_details = value.get("output_tokens_details") or {}
    cached = count(input_details.get("cached_tokens")) if isinstance(input_details, dict) else None
    reasoning = count(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None
    return {
        "input_tokens": incoming, "output_tokens": outgoing,
        "total_tokens": max(count(value.get("total_tokens")) or 0, incoming + outgoing),
        # Cache and reasoning are subsets, never added to total a second time.
        "cached_tokens": min(cached, incoming) if cached is not None else None,
        "reasoning_tokens": min(reasoning, outgoing) if reasoning is not None else None,
    }


class TelemetryStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        deadline = time.monotonic() + 5
        while True:
            try:
                self.db.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    self.db.close()
                    raise
                time.sleep(.025)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS telemetry_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_attempts (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL, provider TEXT NOT NULL,
                kind TEXT NOT NULL, started REAL NOT NULL, finished REAL,
                status INTEGER, outcome TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
                reasoning_tokens INTEGER, total_tokens INTEGER);
            CREATE INDEX IF NOT EXISTS attempts_finished ON api_attempts(finished);
            CREATE TABLE IF NOT EXISTS api_events (
                id INTEGER PRIMARY KEY, time REAL NOT NULL, kind TEXT NOT NULL,
                provider TEXT NOT NULL, peer TEXT NOT NULL, reason TEXT NOT NULL,
                request_id TEXT NOT NULL, tokens INTEGER);
            CREATE INDEX IF NOT EXISTS events_time ON api_events(time);
        """)
        with self.db:
            # Serialize schema inspection with Rust's migration before deciding to ALTER.
            self.db.execute("BEGIN IMMEDIATE")
            for table in ("api_attempts", "api_events"):
                columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for column in ("route_id", "project_id", "task_id", "run_id", "thread_id", "role"):
                    if column not in columns:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                self.db.execute(f"CREATE INDEX IF NOT EXISTS {table}_run ON {table}(run_id)")
            self.db.execute("INSERT OR IGNORE INTO telemetry_meta VALUES('created',?)", (str(time.time()),))

    def recover_interrupted(self):
        with self.lock, self.db:
            self.db.execute("UPDATE api_attempts SET outcome='interrupted',reason='proxy_restarted',finished=? WHERE finished IS NULL", (time.time(),))

    def begin(self, provider, request_id, kind="response", route_id="", *, project_id="", task_id="", run_id="", thread_id="", role=""):
        attempt = uuid.uuid4().hex
        context = {"route_id": route_id, "project_id": project_id, "task_id": task_id,
                   "run_id": run_id, "thread_id": thread_id, "role": role}
        context = {key: str(value or "")[:128] for key, value in context.items()}
        with self.lock, self.db:
            self.db.execute("""INSERT INTO api_attempts(id,request_id,provider,kind,started,outcome,
                route_id,project_id,task_id,run_id,thread_id,role) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (attempt, request_id, provider, kind, time.time(), "running", *context.values()))
        self.event("attempt", provider, request_id=request_id, **context)
        return attempt

    def finish(self, attempt, *, status=None, outcome="interrupted", reason="", usage=None):
        if not attempt:
            return
        usage = usage or {}
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM api_attempts WHERE id=? AND finished IS NULL", (attempt,)).fetchone()
            if not row:
                return  # One accounting record even if terminal events repeat.
            self.db.execute("""UPDATE api_attempts SET finished=?,status=?,outcome=?,reason=?,
                input_tokens=?,output_tokens=?,cached_tokens=?,reasoning_tokens=?,total_tokens=? WHERE id=?""",
                (time.time(), status, outcome, reason[:100], usage.get("input_tokens"), usage.get("output_tokens"),
                 usage.get("cached_tokens"), usage.get("reasoning_tokens"), usage.get("total_tokens"), attempt))
        context = {key: row[key] for key in ("route_id", "project_id", "task_id", "run_id", "thread_id", "role")}
        self.event(outcome, row["provider"], request_id=row["request_id"], reason=reason,
                   tokens=usage.get("total_tokens"), **context)

    def event(self, kind, provider, *, peer="", reason="", request_id="", tokens=None,
              route_id="", project_id="", task_id="", run_id="", thread_id="", role=""):
        with self.lock, self.db:
            self.db.execute("""INSERT INTO api_events(time,kind,provider,peer,reason,request_id,tokens,
                route_id,project_id,task_id,run_id,thread_id,role) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (time.time(), kind[:50], provider[:40], peer[:40], reason[:100], request_id[:100], tokens,
                 *(str(value or "")[:128] for value in (route_id, project_id, task_id, run_id, thread_id, role))))

    def run_usage(self, run_id):
        """Unknown token usage stays null; retries are attempts, not generations."""
        with self.lock:
            row = self.db.execute("""SELECT COUNT(*) AS attempts,
                SUM(CASE WHEN outcome='completed' AND kind='response' THEN 1 ELSE 0 END) AS generations,
                SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,
                SUM(cached_tokens) AS cached_tokens,SUM(reasoning_tokens) AS reasoning_tokens,
                SUM(total_tokens) AS total_tokens FROM api_attempts WHERE run_id=?""", (run_id,)).fetchone()
            value = dict(row)
            value["generations"] = value["generations"] or 0
            value["api_ids"] = [r[0] for r in self.db.execute(
                "SELECT DISTINCT provider FROM api_attempts WHERE run_id=? ORDER BY provider", (run_id,))]
            return value

    def list_events(self, run_id, *, cursor=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        after = 0
        if cursor not in (None, ""):
            if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdigit() or len(cursor) > 20:
                raise ValueError("Invalid cursor")
            after = int(cursor)
            if after > 2**63 - 1:
                raise ValueError("Invalid cursor")
        with self.lock:
            rows = self.db.execute("SELECT * FROM api_events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                                   (run_id, after, limit + 1)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": str(items[-1]["id"]) if len(rows) > limit else None}

    def has_run_events(self, run_id):
        with self.lock:
            return self.db.execute("SELECT 1 FROM api_events WHERE run_id=? LIMIT 1", (run_id,)).fetchone() is not None

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        sums = """COUNT(*) AS attempts,
            SUM(CASE WHEN outcome='completed' AND kind='response' THEN 1 ELSE 0 END) AS generations,
            SUM(CASE WHEN finished IS NOT NULL AND total_tokens IS NULL THEN 1 ELSE 0 END) AS unreported,
            SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,
            SUM(cached_tokens) AS cached_tokens,SUM(reasoning_tokens) AS reasoning_tokens,
            SUM(total_tokens) AS total_tokens"""
        with self.lock:
            metrics = dict(self.db.execute("SELECT " + sums + " FROM api_attempts").fetchone())
            metrics["generations"] = metrics["generations"] or 0
            metrics["unreported"] = metrics["unreported"] or 0
            metrics["providers"] = [dict(r) for r in self.db.execute("SELECT provider AS id," + sums + " FROM api_attempts GROUP BY provider")]
            metrics["routes"] = [dict(r) for r in self.db.execute("SELECT route_id AS id," + sums + " FROM api_attempts WHERE route_id<>'' GROUP BY route_id")]
            metrics["since"] = float(self.db.execute("SELECT value FROM telemetry_meta WHERE key='created'").fetchone()[0])
            buckets = {int(r["bucket"]): r["tokens"] for r in self.db.execute(
                "SELECT CAST(finished/120 AS INTEGER) AS bucket,SUM(total_tokens) AS tokens FROM api_attempts WHERE finished>=? GROUP BY bucket", (now - 3600,))}
            last = int(now // 120)
            timeline = [{"time": bucket * 120, "tokens": buckets.get(bucket) or 0} for bucket in range(last - 29, last + 1)]
            events = [dict(r) for r in self.db.execute("SELECT * FROM api_events ORDER BY id DESC LIMIT 50")]
        return {"metrics": metrics, "timeline": timeline, "events": events}

    def close(self):
        with self.lock:
            self.db.close()
