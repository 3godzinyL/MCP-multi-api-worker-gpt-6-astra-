"""Deterministic offline UI fixture, not the production Rust gateway.

Open http://127.0.0.1:44101/ui/. All projects, usage and provider assignments are
fictional. This script never opens provider configuration, credentials, Codex or
project files. A prompt starts a run which stays running until stopped. Use
``[demo:completed]``, ``[demo:failed]``, ``[demo:awaiting_input]`` or
``[demo:finalizing]`` in the prompt for a deterministic initial state.
Browser tests use /ui/api/__demo/reset and /control, available only here.
--data-dir preserves fictional state across demo process restarts.
"""
from __future__ import annotations

import argparse
import copy
import json
import mimetypes
from pathlib import Path
import re
import secrets
import signal
import sys
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dashboard.preferences import MAIN_PROMPT, COORDINATOR_PROMPT

ASSETS = ROOT / "dashboard" / "assets"
BASE_TIME = 1_788_860_000.0
MODEL = "gpt-6-astra"
COOKIE = "three_api_demo_session"
ACTIVE = {"starting", "queued", "running", "awaiting_input", "finalizing", "stopping"}
FINISHED = {"completed", "failed", "interrupted"}
DETAILS = {"messages", "changes", "agents", "api_events", "approvals"}
DEFAULTS = {"permission_mode": "approval", "effort": "ultra", "main_prompt": MAIN_PROMPT,
            "coordinator_prompt": COORDINATOR_PROMPT, "context_window": 0, "compact_at": 0,
            "max_output_tokens": 0, "task_token_budget": 0, "max_agents": 6, "stream_retries": 10,
            "request_retries": 4, "rate_limit_wait": 180}


class DemoRequestConflict(ValueError):
    pass


def creation_request(body):
    request_id = body.get("client_request_id")
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", request_id)):
        raise ValueError("Invalid client request ID")
    signature = json.dumps({key: value for key, value in body.items() if key != "client_request_id"}, sort_keys=True, ensure_ascii=False)
    return request_id, signature


def page_of(items, query, prefix=""):
    cursor = max(0, int(query.get(prefix + "cursor", ["0"])[0] or 0))
    limit = max(1, min(100, int(query.get(prefix + "limit", query.get("limit", ["100"]))[0])))
    end = cursor + limit
    return {"items": copy.deepcopy(items[cursor:end]), "next_cursor": str(end) if end < len(items) else None}


def sample_changes(label="dashboard"):
    values = []
    for index, path in enumerate([f"src/{label}/workspace.ts", f"src/{label}/history.ts", "src/styles/panel.css"]):
        added, removed = [(24, 4), (12, 2), (8, 0)][index]
        diff = [f"--- a/{path}", f"+++ b/{path}", "@@ -1,8 +1,28 @@", " export function renderWorkspace() {"]
        diff += ["-  refreshTransientState();"] * removed
        diff += [f"+  preserveRunHistory({line + 1});" for line in range(added)]
        diff += [" }", " // Changes stay attached to their original run."]
        values.append({"path": path, "kind": "modified", "added": added, "removed": removed, "diff": "\n".join(diff)})
    return values


class DemoStore:
    def __init__(self, data_dir=None):
        self.lock = threading.RLock()
        self.path = Path(data_dir).resolve() / "demo-state.json" if data_dir else None
        self.sessions = {}
        self.summary_only = False
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.reset("showcase")

    def persist(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pending = self.path.with_suffix(".tmp")
            pending.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            pending.replace(self.path)

    def tick(self):
        self.data["sequence"] += 1
        return BASE_TIME + self.data["sequence"]

    def reset(self, scenario="showcase"):
        self.summary_only = False
        self.data = {"sequence": 0, "projects": [], "tasks": {}, "runs": {}, "requests": {}, "chat_requests": {}, "workspace_requests": {},
                     "settings": copy.deepcopy(DEFAULTS), "agents_files": {}, "provider_config": {}}
        for index, name in enumerate(["Studio", "Website", "Automations"]):
            self.data["projects"].append({"id": f"demo-project-{index}", "path": f"C:/Projects/{name}", "name": name, "created": BASE_TIME + index,
                                          "kind": "project", "access_mode": "project", "archived_at": None})
        if scenario != "empty":
            design = self.create_chat({"project_id": "demo-project-0", "title": "Workspace refresh", "id": "demo-chat-design"})
            first = self.start_run({"project_id": design["project_id"], "continue_task": design["id"], "prompt": "Plan a calm workspace with persistent project history.", "id": "demo-run-discovery"})
            self.transition(first["id"], "completed", elapsed=864)
            second = self.start_run({"project_id": design["project_id"], "continue_task": design["id"], "prompt": "Keep every chat, show agent roles and make the layout work on mobile.", "id": "demo-run-design"})
            self.transition(second["id"], "completed", elapsed=3672)
            self.create_chat({"project_id": "demo-project-0", "title": "Release checklist", "id": "demo-chat-checklist"})
            website = self.create_chat({"project_id": "demo-project-1", "title": "Website accessibility", "id": "demo-chat-website"})
            self.start_run({"project_id": website["project_id"], "continue_task": website["id"], "prompt": "Review navigation, keyboard focus and responsive pages.", "id": "demo-run-website"})
        self.persist()

    def project(self, project_id):
        return next((p for p in self.data["projects"] if p["id"] == project_id), None)

    def create_project(self, body):
        request_id, signature = creation_request(body)
        requests = self.data.setdefault("workspace_requests", {})
        if request_id and request_id in requests:
            previous = requests[request_id]
            if previous["signature"] != signature:
                raise DemoRequestConflict("This workspace request ID has different parameters")
            if self.project(previous["project"]["id"]).get("archived_at"):
                raise DemoRequestConflict("The workspace from this request is archived")
            return copy.deepcopy(previous["project"])
        kind = body.get("kind", "project")
        if kind not in {"project", "chat"}:
            raise ValueError("Unknown workspace kind")
        access = "project" if kind == "project" else body.get("access_mode", "isolated")
        if kind == "chat" and access not in {"isolated", "full"}:
            raise ValueError("Unknown chat access")
        if access == "full" and body.get("full_access_confirmed") is not True:
            raise ValueError("Full computer access requires explicit confirmation")
        path = str(body.get("path") or "").strip().replace("\\", "/").rstrip("/")
        if kind == "project" and not path:
            raise ValueError("Project folder is required")
        existing = next((p for p in self.data["projects"] if p["path"].casefold() == path.casefold()), None) if kind == "project" else None
        if existing:
            existing["archived_at"] = None
            if body.get("name"):
                existing["name"] = str(body["name"])
            if request_id:
                requests[request_id] = {"signature": signature, "project": copy.deepcopy(existing)}
            self.persist()
            return existing
        now = self.tick()
        project_id = f"demo-workspace-{self.data['sequence']}"
        result = {"id": project_id, "name": str(body.get("name") or (path.rsplit("/", 1)[-1] if kind == "project" else "Czat")),
                  "kind": kind, "access_mode": access, "path": path if kind == "project" else f"C:/Temp/3api-chats/{project_id}",
                  "created": now, "archived_at": None}
        self.data["projects"].append(result)
        if request_id:
            requests[request_id] = {"signature": signature, "project": copy.deepcopy(result)}
        self.persist()
        return result

    def create_chat(self, body):
        request_id, signature = creation_request(body)
        project_id = body.get("project_id")
        if not self.project(project_id) or self.project(project_id).get("archived_at"):
            raise ValueError("Unknown demo project")
        requests = self.data.setdefault("chat_requests", {})
        if request_id and request_id in requests:
            previous = requests[request_id]
            if previous["signature"] != signature:
                raise DemoRequestConflict("This chat request ID has different parameters")
            return self.data["tasks"][previous["task_id"]]
        now = self.tick()
        task_id = body.get("id") or f"demo-chat-{self.data['sequence']}"
        task = {"id": task_id, "task_id": task_id, "project_id": project_id, "title": body.get("title") or "Nowy czat",
                "state": "idle", "created": now, "updated": now, "run_id": None, "latest_run_id": None, "thread_id": None,
                "model": MODEL, "mode": "standard", "effort": "ultra", "permission_mode": "approval", "api_ids": [],
                "messages": [], "changes": [], "agents": [], "approvals": [], "files": 0, "added": 0, "removed": 0,
                "touched_files": 0, "changes_revision": 0, "scan_status": "complete"}
        self.data["tasks"][task_id] = task
        if request_id:
            requests[request_id] = {"signature": signature, "task_id": task_id}
        self.persist()
        return task

    def active_assignments(self):
        return [{"project_id": run["project_id"], "task_id": run["task_id"], "run_id": run["id"], "thread_id": agent["thread_id"],
                 "role": agent["role"], "provider_id": agent["provider_id"], "api_id": agent["provider_id"]}
                for run in self.data["runs"].values() if run["state"] in ACTIVE for agent in run["agents"]]

    def choose_provider(self, selected, role, assignments):
        counts = {item: [sum(a["provider_id"] == item and a["role"] == kind for a in assignments) for kind in ("main", "auxiliary")] for item in selected}
        if role == "main":
            score = lambda item: (0 if sum(counts[item]) == 0 else 1 if counts[item][0] == 0 else 2, sum(counts[item]), selected.index(item))
        else:
            score = lambda item: (0 if counts[item][1] and counts[item][0] == 0 else 1 if sum(counts[item]) == 0 else 2, sum(counts[item]), selected.index(item))
        return min(selected, key=score)

    def start_run(self, body):
        project_id = body.get("project_id")
        request_key = str(project_id) + ":" + str(body.get("client_request_id", ""))
        if body.get("client_request_id") and request_key in self.data["requests"]:
            return self.data["runs"][self.data["requests"][request_key]]
        task_id = body.get("continue_task") or body.get("task_id")
        task = self.data["tasks"].get(task_id) if task_id else self.create_chat(body)
        if not task or task["project_id"] != project_id:
            raise ValueError("Unknown chat in this demo project")
        if task["state"] in ACTIVE:
            raise ValueError("This demo chat already has an active run")
        prompt = body.get("prompt", "").strip()
        if not prompt:
            raise ValueError("A prompt is required")
        now = self.tick()
        run_id = body.get("id") or f"demo-run-{self.data['sequence']}"
        selected = [item for item in body.get("api_ids", ["demo1", "demo2", "demo3"]) if item in {"demo1", "demo2", "demo3"}]
        if not selected:
            raise ValueError("Select a demo provider")
        effort, agents, assignments = body.get("effort", "ultra"), [], self.active_assignments()
        for index, role in enumerate(["main", "auxiliary"] if effort == "ultra" else ["main"]):
            provider = self.choose_provider(selected, role, assignments)
            agents.append({"id": f"{run_id}-{role}", "thread_id": f"{run_id}-{role}", "name": "Codex" if index == 0 else "Atlas",
                           "role": role, "provider_id": provider, "status": "running", "description": "Workspace implementation" if index == 0 else "Independent review and checks"})
            assignments.append({"provider_id": provider, "role": role})
        changes = sample_changes("website" if project_id == "demo-project-1" else "dashboard")
        messages = [{"id": run_id + "-user", "role": "user", "text": prompt, "time": now},
                    {"id": run_id + "-assistant", "role": "assistant", "text": "The project history is loaded. I am implementing the workspace and reviewing changes in parallel.", "time": now + 1},
                    {"id": run_id + "-tool", "role": "tool", "title": "Project changes", "agent_name": "Atlas", "text": "Reviewed 3 source files. The original run baseline is preserved.", "time": now + 2}]
        run = {"id": run_id, "task_id": task["id"], "project_id": project_id, "title": prompt[:90], "state": "running", "started_at": now,
               "finished_at": None, "elapsed_seconds": 124, "agents_count": len(agents), "api_ids": list(dict.fromkeys(a["provider_id"] for a in agents)),
               "selected_api_ids": selected, "files": len(changes), "added": sum(c["added"] for c in changes), "removed": sum(c["removed"] for c in changes),
               "touched_files": len(changes), "changes_revision": 1, "scan_status": "complete",
               "usage": {"input_tokens": 18420, "output_tokens": 7320, "cached_tokens": 9600, "total_tokens": 25740},
               "messages": messages, "changes": changes, "agents": agents, "approvals": [], "updated": now,
               "api_events": [{"id": run_id + "-api-" + str(index), "time": now + index, "provider_id": a["provider_id"], "provider": a["provider_id"],
                               "role": a["role"], "thread_id": a["thread_id"], "kind": "completed", "input_tokens": 9210, "output_tokens": 3660, "total_tokens": 12870} for index, a in enumerate(agents)]}
        self.data["runs"][run_id] = run
        task["messages"] += copy.deepcopy(messages)
        task.update({"run_id": run_id, "latest_run_id": run_id, "thread_id": task.get("thread_id") or task["id"] + "-thread", "model": body.get("model", MODEL),
                     "mode": body.get("mode", "standard"), "effort": effort, "permission_mode": body.get("permission_mode", "approval"), "api_ids": selected})
        if task["title"] == "Nowy czat":
            task["title"] = prompt[:60]
        self.sync_task(run)
        if body.get("client_request_id"):
            self.data["requests"][request_key] = run_id
        requested = re.search(r"\[demo:(completed|failed|interrupted|awaiting_input|finalizing|starting|queued)\]", prompt)
        if requested:
            self.transition(run_id, requested[1])
        self.persist()
        return run

    def sync_task(self, run):
        task = self.data["tasks"][run["task_id"]]
        if task["run_id"] != run["id"]:
            return
        for key in ("state", "updated", "files", "added", "removed", "touched_files", "changes_revision", "scan_status", "agents", "changes", "approvals",
                    "api_events", "started_at", "finished_at", "elapsed_seconds", "agents_count", "usage"):
            task[key] = copy.deepcopy(run[key])
        task["tokens"] = {"totalTokens": run["usage"]["total_tokens"]}

    def transition(self, run_id, state, elapsed=None):
        if state not in ACTIVE | FINISHED:
            raise ValueError("Unknown demo state")
        run = self.data["runs"][run_id]
        run["state"], run["updated"] = state, self.tick()
        if elapsed is not None:
            run["elapsed_seconds"] = elapsed
        run["finished_at"] = run["started_at"] + run["elapsed_seconds"] if state in FINISHED else None
        run["scan_status"] = "scanning" if state == "finalizing" else "complete"
        for agent in run["agents"]:
            agent["status"] = state
        run["approvals"] = [{"id": run_id + "-approval", "kind": "command", "title": "Review the next operation", "description": "Demo: continue the local accessibility review."}] if state == "awaiting_input" else []
        if state in FINISHED and not any(m["id"] == run_id + "-" + state for m in run["messages"]):
            message = {"id": run_id + "-" + state, "role": "error" if state == "failed" else "assistant", "time": run["updated"], "text": {
                "completed": "Workspace updated. Chat history, provider assignments and file changes are preserved for this run.",
                "failed": "Demo provider returned an error. Saved messages and changes remain available for review.",
                "interrupted": "Run stopped. All observed changes remain in its history."}[state]}
            run["messages"].append(message)
            self.data["tasks"][run["task_id"]]["messages"].append(copy.deepcopy(message))
        self.sync_task(run)
        self.persist()
        return run

    def providers(self):
        assignments, result = self.active_assignments(), []
        for index, label in enumerate(["Primary", "Reserve", "Studio"], 1):
            provider_id = f"demo{index}"
            owned = [a for a in assignments if a["provider_id"] == provider_id]
            config = self.data.setdefault("provider_config", {}).get(provider_id, {})
            limit, soft, hard = (config.get(key, default) for key, default in [("tokens_per_minute", 1_000_000), ("soft_tokens_per_minute", 900_000), ("hard_tokens_per_minute", 950_000)])
            used = sum(event["total_tokens"] for run in self.data["runs"].values() for event in run["api_events"] if event["provider_id"] == provider_id)
            reserved = 4000 * len(owned)
            result.append({"id": provider_id, "label": "Demo · " + label, "model": MODEL, "deployment": MODEL,
                           "base_url": "http://127.0.0.1:44100/v1", "enabled": True, "available": True, "configured": True, "auth_type": "bearer",
                           "cooldown_seconds": 60, "cooldown_remaining_seconds": 0, "main_count": sum(a["role"] == "main" for a in owned),
                           "auxiliary_count": sum(a["role"] == "auxiliary" for a in owned), "in_flight": len(owned), "assignments": owned, "rate_limit_events": 0,
                           **config, "tokens_per_minute": limit, "soft_tokens_per_minute": soft, "hard_tokens_per_minute": hard,
                           "token_budget": {"limit": limit, "soft_limit": soft, "hard_limit": hard, "used_tokens": used, "reserved_tokens": reserved,
                                            "estimated_tokens": 0, "total_tokens": used + reserved, "soft_reached": used + reserved >= soft,
                                            "hard_reached": used + reserved >= hard, "retry_after_seconds": 30 if used + reserved >= hard else 0, "window_seconds": 60}})
        return result

    def summary(self, item):
        return {key: copy.deepcopy(value) for key, value in item.items() if key not in DETAILS}

    def state(self, query):
        project_id, task_id, run_id = (query.get(key, [""])[0] for key in ("project_id", "task_id", "run_id"))
        providers, runs = self.providers(), list(self.data["runs"].values())
        tasks = [copy.deepcopy(task) if task["id"] == task_id and not self.summary_only else self.summary(task) for task in self.data["tasks"].values()]
        usage = {key: sum(run["usage"].get(key, 0) for run in runs) for key in ("input_tokens", "output_tokens", "cached_tokens", "total_tokens")}
        usage.update({"generations": len(runs) * 2, "since": BASE_TIME, "unreported": 0, "providers": [
            {"id": p["id"], "generations": len(runs), "attempts": len(runs), "input_tokens": 9210 * len(runs), "output_tokens": 3660 * len(runs),
             "total_tokens": 12870 * len(runs), "cached_tokens": 4800 * len(runs)} for p in providers]})
        projects = [copy.deepcopy(project) for project in self.data["projects"] if not project.get("archived_at")]
        tasks = [task for task in tasks if any(project["id"] == task["project_id"] for project in projects)]
        for project in projects:
            project["change_summary"] = {key: sum(run[key] for run in runs if run["project_id"] == project["id"]) for key in ("files", "added", "removed", "touched_files")}
        return {"schema_version": 2, "requested_project_id": project_id, "requested_task_id": task_id, "requested_run_id": run_id,
                "project_id": project_id, "task_id": task_id, "run_id": run_id, "projects": projects, "tasks": tasks,
                "proxy": {"status": "ready", "providers": providers, "assignments": self.active_assignments(), "routes": [], "stats": {"requests": len(runs) * 2, "responses_completed": len(runs) * 2}},
                "runner": {"available": True, "version": "offline-demo"}, "defaults": copy.deepcopy(self.data["settings"]), "model": MODEL,
                "models": [{"model": MODEL, "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]}],
                "active_agents": len(self.active_assignments()), "metrics": usage,
                "timeline": [{"time": BASE_TIME + i * 60, "tokens": [320, 780, 510, 1040, 870, 1500][i % 6]} for i in range(30)] if runs else [],
                "activity": [{"time": run["started_at"], "title": self.data["tasks"][run["task_id"]]["title"], "subtitle": self.project(run["project_id"])["name"],
                              "kind": "task", "icon": "check" if run["state"] == "completed" else "terminal"} for run in reversed(runs)], "demo": True}


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        super().__init__(address, DemoHandler)
        self.store = store


class DemoHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, value, status=200, content_type="application/json; charset=utf-8", headers=None):
        payload = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for key, item in {"content-type": content_type, "content-length": str(len(payload)), "cache-control": "no-store",
                          "x-content-type-options": "nosniff", "x-frame-options": "DENY", **(headers or {})}.items():
            self.send_header(key, item)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Expected when a browser cancels deliberately delayed polling.

    def local(self):
        return self.headers.get("host", "") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("cookie", ""))
            session_id = cookie[COOKIE].value if COOKIE in cookie else ""
        except Exception:
            return "", None
        return session_id, self.server.store.sessions.get(session_id)

    def guard(self, write=False):
        if not self.local():
            self.send({"error": "Local demo access only"}, 403)
            return False
        _, session = self.session()
        if not session:
            self.send({"error": "Session expired", "code": "session_expired"}, 401)
            return False
        if write:
            if self.headers.get("origin", "") != f"http://{self.headers.get('host', '')}":
                self.send({"error": "Origin rejected"}, 403)
                return False
            if self.headers.get("x-panel-csrf") != session["csrf"]:
                self.send({"error": "CSRF rejected", "code": "csrf_expired"}, 403)
                return False
        return True

    def do_GET(self):
        parsed = urlsplit(self.path)
        if not self.local():
            return self.send({"error": "Local demo access only"}, 403)
        if parsed.path == "/health":
            return self.send({"status": "ok", "application": "3api-offline-demo"})
        if parsed.path in {"/", "/ui", "/ui/"}:
            session_id, session = self.session()
            if not session:
                session_id, session = secrets.token_urlsafe(24), {"csrf": secrets.token_urlsafe(24)}
                self.server.store.sessions[session_id] = session
            content = (ASSETS / "index.html").read_text(encoding="utf-8").replace("__CSRF__", session["csrf"])
            for marker, filename in (("__COMPOSER__", "composer.html"), ("__CONTROLS_DIALOGS__", "dialogs.html")):
                content = content.replace(marker, (ASSETS / filename).read_text(encoding="utf-8"))
            return self.send(content.encode("utf-8"), content_type="text/html; charset=utf-8", headers={
                "set-cookie": f"{COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/ui; Max-Age=43200",
                "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if parsed.path.startswith("/ui/assets/"):
            path = (ASSETS / unquote(parsed.path.removeprefix("/ui/assets/"))).resolve()
            if not path.is_relative_to(ASSETS.resolve()) or not path.is_file():
                return self.send({"error": "Asset not found"}, 404)
            media_type = "application/javascript" if path.suffix == ".js" else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return self.send(path.read_bytes(), content_type=media_type)
        if not self.guard():
            return
        route, query, store = parsed.path.removeprefix("/ui/api"), parse_qs(parsed.query, keep_blank_values=True), self.server.store
        with store.lock:
            try:
                if route == "/state":
                    return self.send(store.state(query))
                if route == "/providers":
                    return self.send({"providers": store.providers()})
                if route == "/capabilities":
                    return self.send({"models": store.state({})["models"]})
                if route == "/settings":
                    return self.send(store.data["settings"])
                if route == "/skills":
                    return self.send({"skills": [], "errors": []})
                if route == "/folders":
                    folders = [{"name": p["name"], "path": p["path"]} for p in store.data["projects"] if p.get("kind", "project") == "project"]
                    query_text = query.get("q", [""])[0].casefold()
                    return self.send({"path": query.get("path", [""])[0] or "C:/Projects", "parent": "C:/", "folders": [f for f in folders if query_text in f["name"].casefold()], "total": len(folders)})
                match = re.fullmatch(r"/projects/([^/]+)/(chats|history|agents)", route)
                if match:
                    project_id, action = match.groups()
                    project = store.project(project_id)
                    if not project:
                        return self.send({"error": "Project not found"}, 404)
                    if action == "agents":
                        return self.send({"path": project["path"] + "/AGENTS.md", "exists": True, "content": store.data["agents_files"].get(project_id, "# Demo project\nKeep work local and preserve history.\n"), "version": "demo-v1"})
                    items = [store.summary(item) for item in store.data["tasks" if action == "chats" else "runs"].values() if item["project_id"] == project_id]
                    if action == "history":
                        items.sort(key=lambda item: item["started_at"], reverse=True)
                        if query.get("task_id", [""])[0]:
                            items = [item for item in items if item["task_id"] == query["task_id"][0]]
                    return self.send(page_of(items, query))
                match = re.fullmatch(r"/tasks/([^/]+)/runs/([^/]+)", route)
                if match:
                    task_id, run_id = match.groups()
                    run = store.data["runs"].get(run_id)
                    if not run or run["task_id"] != task_id:
                        return self.send({"error": "Run not found"}, 404)
                    result = store.summary(run)
                    result.update({"agents": copy.deepcopy(run["agents"]), "approvals": copy.deepcopy(run["approvals"])})
                    for key in ("messages", "changes", "api_events"):
                        part = page_of(run[key], query, key + "_")
                        result[key], result[key + "_next_cursor"] = part["items"], part["next_cursor"]
                    return self.send(result)
                return self.send({"error": "Unknown demo endpoint"}, 404)
            except (KeyError, ValueError, TypeError) as error:
                return self.send({"error": str(error)}, 400)

    def do_POST(self):
        if not self.guard(write=True):
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            if not 0 <= length <= 1_000_000:
                return self.send({"error": "Request is too large"}, 413)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("JSON object required")
        except (ValueError, TypeError):
            return self.send({"error": "Invalid JSON body"}, 400)
        route, store = urlsplit(self.path).path.removeprefix("/ui/api"), self.server.store
        with store.lock:
            try:
                if route == "/__demo/reset":
                    store.reset(body.get("scenario", "showcase"))
                    return self.send({"ok": True})
                if route == "/__demo/control":
                    if "summary_only" in body:
                        store.summary_only = bool(body["summary_only"])
                    if body.get("expire_sessions"):
                        store.sessions.clear()
                    if body.get("state"):
                        store.transition(body.get("run_id") or store.data["tasks"][body["task_id"]]["run_id"], body["state"], body.get("elapsed_seconds"))
                    if body.get("append_change"):
                        run = store.data["runs"][body.get("run_id") or store.data["tasks"][body["task_id"]]["run_id"]]
                        run["changes"].append({"path": "src/persisted-update.ts", "kind": "added", "added": 2, "removed": 0, "diff": "@@ -0,0 +1,2 @@\n+export const durable = true;\n+export const revision = 2;"})
                        run["files"] += 1
                        run["added"] += 2
                        run["touched_files"] += 1
                        run["changes_revision"] += 1
                        run["updated"] = store.tick()
                        store.sync_task(run)
                    store.persist()
                    return self.send({"ok": True})
                if route == "/chats":
                    return self.send(copy.deepcopy(store.create_chat(body)))
                if route == "/projects":
                    return self.send(copy.deepcopy(store.create_project(body)))
                if route == "/providers":
                    provider_id = body.get("id")
                    if provider_id not in {"demo1", "demo2", "demo3"}:
                        raise ValueError("Edit an existing fictional demo API")
                    limits = {key: body.get(key, default) for key, default in [("tokens_per_minute", 1_000_000), ("soft_tokens_per_minute", 900_000), ("hard_tokens_per_minute", 950_000)]}
                    if not all(type(value) is int and value <= 1_000_000_000 for value in limits.values()) or not 0 < limits["soft_tokens_per_minute"] < limits["hard_tokens_per_minute"] <= limits["tokens_per_minute"]:
                        raise ValueError("Invalid demo token limits")
                    store.data.setdefault("provider_config", {})[provider_id] = {**limits, **{key: body[key] for key in ("label", "base_url", "auth_type", "api_version", "cooldown_seconds", "enabled") if key in body}}
                    store.persist()
                    return self.send({"id": provider_id, "message": "Zapisano ustawienia API demo."})
                if route == "/tasks":
                    run = store.start_run(body)
                    return self.send({"id": run["task_id"], "run_id": run["id"], "state": run["state"]})
                if route == "/settings":
                    store.data["settings"].update({key: value for key, value in body.items() if key in DEFAULTS})
                    store.persist()
                    return self.send(store.data["settings"])
                if route in {"/logout", "/shutdown"}:
                    store.sessions.pop(self.session()[0], None)
                    return self.send({"ok": True})
                if route == "/skills":
                    return self.send({"ok": True})
                match = re.fullmatch(r"/tasks/([^/]+)/(stop|approval|route)", route)
                if match:
                    task_id, action = match.groups()
                    if action in {"stop", "approval"}:
                        store.transition(store.data["tasks"][task_id]["run_id"], "interrupted" if action == "stop" else "running")
                    return self.send({"ok": True})
                match = re.fullmatch(r"/projects/([^/]+)/agents", route)
                if match:
                    project_id = match[1]
                    store.data["agents_files"][project_id] = body["content"]
                    store.persist()
                    return self.send({"path": store.project(project_id)["path"] + "/AGENTS.md", "exists": True, "version": "demo-v1"})
                return self.send({"error": "This operation is unavailable in the offline demo"}, 404)
            except DemoRequestConflict as error:
                return self.send({"error": str(error), "code": "request_conflict"}, 409)
            except (KeyError, ValueError, TypeError) as error:
                return self.send({"error": str(error)}, 400)

    def do_DELETE(self):
        if not self.guard(write=True):
            return
        route, store = urlsplit(self.path).path.removeprefix("/ui/api"), self.server.store
        match = re.fullmatch(r"/projects/([^/]+)", route)
        if not match:
            return self.send({"error": "Unknown demo endpoint"}, 404)
        with store.lock:
            project = store.project(match[1])
            if not project:
                return self.send({"error": "Project not found"}, 404)
            if any(task["project_id"] == project["id"] and task["state"] in ACTIVE for task in store.data["tasks"].values()):
                return self.send({"error": "Zakończ lub zatrzymaj zadania przed usunięciem projektu z panelu."}, 409)
            project["archived_at"] = store.tick()
            store.persist()
            return self.send({"id": project["id"], "removed": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=44101)
    parser.add_argument("--data-dir", type=Path, help="Optional directory for fictional demo state only")
    args = parser.parse_args()
    server = DemoServer(("127.0.0.1", args.port), DemoStore(args.data_dir))

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"Offline demo: http://127.0.0.1:{server.server_port}/ui/ (fictional data, no provider calls)", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
