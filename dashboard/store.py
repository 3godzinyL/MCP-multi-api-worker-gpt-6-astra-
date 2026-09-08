from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path


_RUN_FIELDS = (
    "state", "started_at", "finished_at", "elapsed_seconds", "agents_count", "api_ids",
    "files", "added", "removed", "touched_files", "touched_paths", "changes_revision",
    "scan_status", "usage", "client_request_id", "thread_id", "turn_id", "title",
    "mode", "effort", "model", "tokens", "agents", "scan_skipped", "scan_error",
)
_DETAIL_FIELDS = {"messages", "changes", "change_history", "api_events"}
_CHAT_PRIVATE_FIELDS = _DETAIL_FIELDS | {"approvals", "preferences", "touched_paths"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def creation_fingerprint(body):
    value = {key: item for key, item in body.items() if key != "client_request_id"}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


class IdempotencyConflict(ValueError):
    pass


def _creation_key(client_request_id):
    if client_request_id is not None and (not isinstance(client_request_id, str) or
            not 1 <= len(client_request_id) <= 128 or not client_request_id.isascii() or
            any(not (character.isalnum() or character in "_-.") for character in client_request_id)):
        raise ValueError("Niepoprawny identyfikator wysyłanego polecenia.")


def _page(cursor, limit):
    if cursor in (None, ""):
        offset = 0
    elif isinstance(cursor, str) and cursor.isascii() and cursor.isdecimal() and len(cursor) <= 19:
        offset = int(cursor)
    elif isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0:
        offset = cursor
    else:
        raise ValueError("Nieprawidłowy kursor historii.")
    if offset > 2**63 - 1:
        raise ValueError("Nieprawidłowy kursor historii.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("Limit historii musi wynosić od 1 do 500.")
    return offset, limit


class DashboardStore:
    """Persistent chats and runs, compatible with the existing task table.

    Task payloads remain available to the runner and MCP readers. Detail tables
    keep complete histories; only reads are paginated. A task checkpoint and its
    latest run are committed in a single transaction.
    """

    def __init__(self, path: Path, *, busy_timeout_ms=5000, lock_retries=2):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._lock_retries = max(0, min(10, int(lock_retries)))
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=busy_timeout_ms / 1000)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=" + str(max(0, int(busy_timeout_ms))))
        try:
            self._retry(lambda: self.db.execute("PRAGMA journal_mode=WAL"))
            self._write(self._initialize)
        except BaseException:
            self.db.close()
            raise

    def _retry(self, operation):
        for attempt in range(self._lock_retries + 1):
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xff
                locked = code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or "locked" in str(exc).lower()
                if not locked or attempt == self._lock_retries:
                    raise
                time.sleep(0.025 * 2**attempt)

    def _write(self, operation):
        def transaction():
            try:
                # Acquire the writer before reading to avoid transaction-upgrade
                # failures while another connection is checkpointing a task.
                self.db.execute("BEGIN IMMEDIATE")
                value = operation()
                self.db.commit()
                return value
            except BaseException:
                self.db.rollback()
                raise

        with self.lock:
            return self._retry(transaction)

    def _initialize(self):
        statements = (
            """CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name TEXT NOT NULL, created REAL NOT NULL,
                kind TEXT NOT NULL DEFAULT 'project', access_mode TEXT NOT NULL DEFAULT 'project',
                archived_at REAL)""",
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, updated REAL NOT NULL, payload TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS tasks_updated ON tasks(updated)",
            "CREATE INDEX IF NOT EXISTS tasks_project_updated ON tasks(project_id, updated DESC, id)",
            "CREATE TABLE IF NOT EXISTS panel_settings (key TEXT PRIMARY KEY, payload TEXT NOT NULL)",
            """CREATE TABLE IF NOT EXISTS creation_requests (
                operation TEXT NOT NULL, client_request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                resource_id TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(operation, client_request_id))""",
            """CREATE TABLE IF NOT EXISTS task_activity (
                id INTEGER PRIMARY KEY, time REAL NOT NULL, payload TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, project_id TEXT NOT NULL,
                updated REAL NOT NULL, client_request_id TEXT, payload TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS runs_project ON runs(project_id)",
            "CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id)",
            """CREATE UNIQUE INDEX IF NOT EXISTS runs_client_request
                ON runs(client_request_id) WHERE client_request_id IS NOT NULL""",
            """CREATE TABLE IF NOT EXISTS chat_messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
                item_id TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(task_id, run_id, item_id))""",
            "CREATE INDEX IF NOT EXISTS chat_messages_task ON chat_messages(task_id, seq)",
            "CREATE INDEX IF NOT EXISTS chat_messages_run ON chat_messages(task_id, run_id, seq)",
            """CREATE TABLE IF NOT EXISTS run_changes (
                seq INTEGER PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
                position INTEGER NOT NULL, payload TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS run_changes_run ON run_changes(task_id, run_id, position)",
            """CREATE TABLE IF NOT EXISTS run_change_history (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
                item_id TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(task_id, run_id, item_id))""",
            "CREATE INDEX IF NOT EXISTS run_change_history_run ON run_change_history(task_id, run_id, seq)",
            """CREATE TABLE IF NOT EXISTS run_api_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
                item_id TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(task_id, run_id, item_id))""",
            "CREATE INDEX IF NOT EXISTS run_api_events_run ON run_api_events(task_id, run_id, seq)",
        )
        for statement in statements:
            self.db.execute(statement)
        project_columns = {row[1] for row in self.db.execute("PRAGMA table_info(projects)")}
        for name, declaration in (("kind", "TEXT NOT NULL DEFAULT 'project'"),
                                  ("access_mode", "TEXT NOT NULL DEFAULT 'project'"), ("archived_at", "REAL")):
            if name not in project_columns:
                self.db.execute("ALTER TABLE projects ADD COLUMN " + name + " " + declaration)
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
            # v1 changed 'updated' during polling; it is not a measured finish.
            for row in self.db.execute("SELECT id,project_id,updated,payload FROM tasks").fetchall():
                task = json.loads(row["payload"])
                task.update(id=row["id"], project_id=row["project_id"], updated=row["updated"])
                self._save_task(task)
            self.db.execute("PRAGMA user_version=2")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 3:
            self.db.execute("PRAGMA user_version=3")

    def get_setting(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT payload FROM panel_settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        self._write(lambda: self.db.execute(
            "INSERT INTO panel_settings(key,payload) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
            (key, _json(value))))

    def projects(self, *, include_archived=False):
        with self.lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM projects WHERE (? OR archived_at IS NULL) ORDER BY created", (include_archived,))]

    def project(self, project_id, *, include_archived=False):
        with self.lock:
            row = self.db.execute("SELECT * FROM projects WHERE id=? AND (? OR archived_at IS NULL)",
                                  (project_id, include_archived)).fetchone()
            return dict(row) if row else None

    def _creation_replay(self, operation, client_request_id, fingerprint):
        if client_request_id is None:
            return None
        row = self.db.execute("SELECT fingerprint,resource_id FROM creation_requests WHERE operation=? AND client_request_id=?",
                              (operation, client_request_id)).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise IdempotencyConflict("Ten identyfikator został już użyty do utworzenia innego projektu lub czatu.")
        resource = self.project(row["resource_id"]) if operation == "project" else self.load_task(row["resource_id"])
        if resource is None or (operation == "chat" and not self.project(resource["project_id"])):
            raise IdempotencyConflict("Ten projekt został usunięty z panelu. Utwórz nowe zgłoszenie, aby dodać go ponownie.")
        return resource

    def _remember_creation(self, operation, client_request_id, fingerprint, resource_id):
        if client_request_id is not None:
            self.db.execute("INSERT INTO creation_requests(operation,client_request_id,fingerprint,resource_id,created) VALUES(?,?,?,?,?)",
                            (operation, client_request_id, fingerprint, resource_id, time.time()))

    def add_project(self, folder: str | None = None, *, name=None, kind="project", access_mode="project",
                    workspace_factory=None, client_request_id=None, request_fingerprint=None):
        _creation_key(client_request_id)
        if client_request_id is not None and workspace_factory is not None and not request_fingerprint:
            raise ValueError("Brak identyfikacji tworzonego obszaru pracy.")
        fingerprint = request_fingerprint or creation_fingerprint({"path": folder, "name": name, "kind": kind, "access_mode": access_mode})
        def insert():
            existing = self._creation_replay("project", client_request_id, fingerprint)
            if existing is not None:
                return existing
            # Resolve replay while holding the SQLite writer, before allocating
            # a temporary directory, including concurrent HTTP retries.
            workspace = workspace_factory() if workspace_factory is not None else {
                "path": folder, "name": name, "kind": kind, "access_mode": access_mode}
            project = self._add_project(workspace["path"], name=workspace.get("name"),
                                        kind=workspace.get("kind", "project"), access_mode=workspace.get("access_mode", "project"))
            self._remember_creation("project", client_request_id, fingerprint, project["id"])
            return project
        return self._write(insert)

    def _add_project(self, folder, *, name, kind, access_mode):
        path = Path(folder).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("Wybierz istniejący folder projektu.")
        if path == Path(path.anchor) or path == Path.home().resolve():
            raise ValueError("Wybierz folder projektu, zamiast całego dysku lub katalogu użytkownika.")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 200):
            raise ValueError("Podaj nazwę do 200 znaków.")
        if kind not in {"project", "chat"} or access_mode not in ({"isolated", "full"} if kind == "chat" else {"project"}):
            raise ValueError("Wybierz poprawny rodzaj obszaru pracy i dostęp.")
        self.db.execute("INSERT OR IGNORE INTO projects(id,path,name,created,kind,access_mode) VALUES(?,?,?,?,?,?)",
                        (secrets.token_hex(8), str(path), (name or path.name).strip(), time.time(), kind, access_mode))
        # Removing an entry from the panel never removes its archive or changes
        # its stored access scope when that path is deliberately added again.
        self.db.execute("UPDATE projects SET archived_at=NULL,name=COALESCE(?,name) WHERE path=?",
                        (name.strip() if name else None, str(path)))
        return dict(self.db.execute("SELECT * FROM projects WHERE path=?", (str(path),)).fetchone())

    def archive_project(self, project_id):
        def archive():
            project = self.project(project_id)
            if project is None:
                raise ValueError("Nie znaleziono projektu.")
            states = {"starting", "running", "awaiting_input", "stopping", "finalizing"}
            if any(json.loads(row[0]).get("state") in states for row in self.db.execute(
                    "SELECT payload FROM tasks WHERE project_id=?", (project_id,))):
                raise ValueError("Zakończ lub zatrzymaj zadania przed usunięciem projektu z panelu.")
            self.db.execute("UPDATE projects SET archived_at=? WHERE id=?", (time.time(), project_id))
            return {"id": project_id, "removed": True}

        return self._write(archive)

    def close(self):
        with self.lock:
            self.db.close()

    def tasks(self):
        """Restore all chats for the worker. HTTP callers use list_chats instead."""
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("""SELECT payload FROM tasks
                WHERE NOT EXISTS (SELECT 1 FROM projects WHERE projects.id=tasks.project_id AND archived_at IS NOT NULL)
                ORDER BY updated DESC, id""")]

    def load_task(self, task_id):
        with self.lock:
            row = self.db.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def create_chat(self, project_id, title="Nowy czat", task_id=None, *, client_request_id=None, request_fingerprint=None, **fields):
        _creation_key(client_request_id)
        fingerprint = request_fingerprint or creation_fingerprint({"project_id": project_id, "title": title, **fields})
        def insert():
            replay = self._creation_replay("chat", client_request_id, fingerprint)
            if replay is not None:
                return replay
            if not self.project(project_id):
                raise ValueError("Nie znaleziono projektu.")
            existing = self.load_task(task_id) if task_id else None
            if existing:
                if existing["project_id"] != project_id:
                    raise ValueError("Czat należy do innego projektu.")
                self._remember_creation("chat", client_request_id, fingerprint, existing["id"])
                return existing
            now = time.time()
            task = {"title": title, "created": now, "updated": now, "state": "idle",
                    "thread_id": None, "turn_id": None, "run_id": None, "messages": [],
                    "changes": [], "change_history": [], "agents": [], "approvals": [],
                    "files": 0, "added": 0, "removed": 0, "tokens": None}
            task.update(fields)
            task.update(id=task_id or secrets.token_hex(12), project_id=project_id)
            self._save_task(task)
            self._remember_creation("chat", client_request_id, fingerprint, task["id"])
            return task

        return self._write(insert)

    def save_task(self, task):
        self._write(lambda: self._save_task(task))

    def _save_task(self, value):
        old = self.load_task(value["id"]) or {}
        if old.get("project_id") is not None and value.get("project_id", old["project_id"]) != old["project_id"]:
            raise ValueError("Czat należy do innego projektu.")
        task = {**old, **value}
        task.setdefault("updated", time.time())
        run_id = task.get("run_id")
        legacy = not run_id and (task.get("state", "idle") != "idle" or task.get("messages") or task.get("changes"))
        if legacy:
            run_id = "legacy-" + task["id"]
            task["run_id"] = run_id

        # Empty or abbreviated lists must not erase older messages. Item IDs are
        # scoped to a run because a resumed CLI may reuse a previous item ID.
        if "messages" in task:
            self._save_items("chat_messages", task["id"], run_id or "", task["messages"])
            task["messages"] = [json.loads(row[0]) for row in self.db.execute(
                "SELECT payload FROM chat_messages WHERE task_id=? ORDER BY seq", (task["id"],))]

        if run_id:
            run = {key: task[key] for key in _RUN_FIELDS if key in task}
            run.update(id=run_id, task_id=task["id"], project_id=task["project_id"], updated=task["updated"])
            for key in ("changes", "change_history", "api_events"):
                if key in task:
                    run[key] = task[key]
            if "agents_count" not in run and "agents" in task:
                run["agents_count"] = len(task["agents"])
            if legacy and not run.get("change_history"):
                run["change_history"] = [dict(change, id="legacy-change-" + str(i), run_id=run_id,
                                              time=None, revision=task.get("changes_revision"))
                                         for i, change in enumerate(task.get("changes", [])) if isinstance(change, dict)]
                task["change_history"] = run["change_history"]
            if "touched_files" not in run and "touched_paths" in run:
                run["touched_files"] = len(set(run["touched_paths"]))
            self._save_run(run)

        self.db.execute("""INSERT INTO tasks(id,project_id,updated,payload) VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET project_id=excluded.project_id,updated=excluded.updated,payload=excluded.payload""",
                        (task["id"], task["project_id"], task["updated"], _json(task)))

    def _save_items(self, table, task_id, run_id, values):
        # Table names are internal constants, never request input.
        occurrences = {}
        legacy_runs, legacy_occurrences = {}, {}
        if table == "chat_messages" and any(not isinstance(item, dict) for item in values):
            for row in self.db.execute("SELECT run_id,payload FROM chat_messages WHERE task_id=? ORDER BY seq", (task_id,)):
                if not isinstance(json.loads(row["payload"]), dict):
                    legacy_runs.setdefault(row["payload"], []).append(row["run_id"])
        for item in values:
            item_run = (item.get("run_id") or run_id) if isinstance(item, dict) else run_id
            if table == "chat_messages" and not isinstance(item, dict):
                # A v1 string has no field in which to retain its run identity.
                # Recover it from the archive instead of duplicating the old
                # message in every continuation's newly created run.
                encoded = _json(item)
                occurrence = legacy_occurrences.get(encoded, 0)
                legacy_occurrences[encoded] = occurrence + 1
                saved_runs = legacy_runs.get(encoded, [])
                if occurrence < len(saved_runs):
                    item_run = saved_runs[occurrence]
            payload = dict(item, run_id=item_run) if isinstance(item, dict) and item_run else item
            identifier = item.get("id") if isinstance(item, dict) else None
            if identifier is None:
                identity = ["content", hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()]
            else:
                identity = ["id", str(identifier)]
            base = _json([item_run, *identity])
            occurrence = occurrences.get(base, 0)
            occurrences[base] = occurrence + 1
            # A v1 payload may already contain duplicate CLI IDs. Preserve those
            # entries while keeping repeated checkpoints of the same list stable.
            item_id = _json([*identity, occurrence])
            self.db.execute(f"""INSERT INTO {table}(task_id,run_id,item_id,payload) VALUES(?,?,?,?)
                ON CONFLICT(task_id,run_id,item_id) DO UPDATE SET payload=excluded.payload""",
                            (task_id, item_run, item_id, _json(payload)))

    def save_run(self, run):
        return self._write(lambda: self._save_run(run))

    def _save_run(self, value):
        run_id = value.get("id") or value["run_id"]
        row = self.db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        old = json.loads(row[0]) if row else {}
        run = {**old, **{key: item for key, item in value.items() if key not in _DETAIL_FIELDS}}
        run["id"] = run_id
        if old and (old["task_id"] != run["task_id"] or old["project_id"] != run["project_id"]):
            raise ValueError("Uruchomienie należy do innego czatu lub projektu.")
        if old.get("client_request_id") and run.get("client_request_id") != old["client_request_id"]:
            raise ValueError("Nie można zmienić identyfikatora przyjętego polecenia.")
        for key in ("state", "started_at", "finished_at", "elapsed_seconds", "agents_count",
                    "files", "added", "removed", "touched_files", "changes_revision", "scan_status", "usage"):
            run.setdefault(key, None)
        run.setdefault("api_ids", [])
        run["updated"] = value.get("updated", time.time())
        request_id = run.get("client_request_id") or None
        self.db.execute("""INSERT INTO runs(id,task_id,project_id,updated,client_request_id,payload) VALUES(?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,client_request_id=excluded.client_request_id,payload=excluded.payload""",
                        (run_id, run["task_id"], run["project_id"], run["updated"], request_id, _json(run)))
        if "messages" in value:
            self._save_items("chat_messages", run["task_id"], run_id, value["messages"])
        if "changes" in value:
            self.db.execute("DELETE FROM run_changes WHERE task_id=? AND run_id=?", (run["task_id"], run_id))
            self.db.executemany("INSERT INTO run_changes(task_id,run_id,position,payload) VALUES(?,?,?,?)",
                                [(run["task_id"], run_id, i, _json(change)) for i, change in enumerate(value["changes"])])
        for field, table in (("change_history", "run_change_history"), ("api_events", "run_api_events")):
            if field in value:
                self._save_items(table, run["task_id"], run_id, value[field])
        return run

    def find_run_by_request(self, client_request_id):
        if not client_request_id:
            return None
        with self.lock:
            row = self.db.execute("SELECT payload FROM runs WHERE client_request_id=?", (client_request_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def _list(self, sql, params, *, cursor, limit, transform=None):
        offset, limit = _page(cursor, limit)
        with self.lock:
            rows = self.db.execute(sql + " LIMIT ? OFFSET ?", (*params, limit + 1, offset)).fetchall()
        items = [json.loads(row[0]) for row in rows[:limit]]
        if transform:
            items = [transform(item) for item in items]
        return {"items": items, "next_cursor": str(offset + limit) if len(rows) > limit else None}

    def list_chats(self, project_id, cursor=None, limit=50):
        def public(task):
            result = {key: value for key, value in task.items() if key not in _CHAT_PRIVATE_FIELDS and not key.startswith("_")}
            if isinstance(result.get("experiment"), dict):
                result["experiment"] = {key: value for key, value in result["experiment"].items()
                                        if key not in {"original_prompt", "plan", "lanes"}}
            return result

        return self._list("SELECT payload FROM tasks WHERE project_id=? ORDER BY updated DESC, id", (project_id,),
                          cursor=cursor, limit=limit, transform=public)

    def list_runs(self, project_id, task_id=None, cursor=None, limit=50):
        return self._list("SELECT payload FROM runs WHERE project_id=? AND (? IS NULL OR task_id=?) ORDER BY rowid DESC",
                          (project_id, task_id, task_id), cursor=cursor, limit=limit)

    def load_run(self, task_id, run_id=None, *, cursor=None, limit=100):
        with self.lock:
            if run_id is None:
                row = self.db.execute("SELECT payload FROM runs WHERE id=?", (task_id,)).fetchone()
            else:
                row = self.db.execute("SELECT payload FROM runs WHERE task_id=? AND id=?", (task_id, run_id)).fetchone()
            if not row:
                return None
            run = json.loads(row[0])
            task_id, run_id = run["task_id"], run["id"]
            for key, method in (("messages", self.list_messages), ("changes", self.list_changes),
                                ("change_history", self.list_change_history), ("api_events", self.list_api_events)):
                run[key] = method(task_id, run_id, cursor=cursor, limit=limit)
            return run

    def list_messages(self, task_id, run_id=None, cursor=None, limit=100):
        return self._list("SELECT payload FROM chat_messages WHERE task_id=? AND (? IS NULL OR run_id=?) ORDER BY seq",
                          (task_id, run_id, run_id), cursor=cursor, limit=limit)

    def list_changes(self, task_id, run_id, cursor=None, limit=100):
        return self._list("SELECT payload FROM run_changes WHERE task_id=? AND run_id=? ORDER BY position, seq",
                          (task_id, run_id), cursor=cursor, limit=limit)

    def list_change_history(self, task_id, run_id, cursor=None, limit=100):
        return self._list("SELECT payload FROM run_change_history WHERE task_id=? AND run_id=? ORDER BY seq",
                          (task_id, run_id), cursor=cursor, limit=limit)

    def list_api_events(self, task_id, run_id, cursor=None, limit=100):
        return self._list("SELECT payload FROM run_api_events WHERE task_id=? AND run_id=? ORDER BY seq",
                          (task_id, run_id), cursor=cursor, limit=limit)

    def save_api_events(self, task_id, run_id, events):
        def save():
            if not self.db.execute("SELECT 1 FROM runs WHERE task_id=? AND id=?", (task_id, run_id)).fetchone():
                raise ValueError("Nie znaleziono uruchomienia w czacie.")
            self._save_items("run_api_events", task_id, run_id, events)

        self._write(save)

    def add_activity(self, title, *, subtitle="", kind="task", level="info", icon="terminal"):
        event = {"time": time.time(), "title": title, "subtitle": subtitle, "kind": kind, "level": level, "icon": icon}
        self._write(lambda: self.db.execute("INSERT INTO task_activity(time,payload) VALUES(?,?)", (event["time"], _json(event))))

    def activity(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM task_activity ORDER BY id DESC LIMIT 30")]

    def list_activity(self, cursor=None, limit=100):
        return self._list("SELECT payload FROM task_activity ORDER BY id DESC", (), cursor=cursor, limit=limit)
