"""Owned Codex App Server process, with real task, tool and agent events."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import tomlkit

from dashboard.changes import compare, load_baseline, save_baseline, snapshot
from dashboard.preferences import preferences
from dashboard.experiment import ExperimentMixin
from dashboard.routing import verified_child_threads, verified_thread_start
from dashboard.workspaces import workspace_permission
from proxy.credentials import LOCAL_TOKEN_ID, get_secret

ACTIVE = {"starting", "running", "awaiting_input", "stopping", "finalizing"}
AGENT_ACTIVE = {"pendingInit", "running", "starting"}
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
ROOT = Path(__file__).resolve().parents[1]
PROXY_TOKEN_ENV = "LOCAL_RESPONSES_PROXY_TOKEN"
# Only runtime and tool-discovery paths cross into the worker. In particular,
# API/cloud tokens, sidecar authentication and Python/Node preload hooks do not.
WORKER_ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432", "TEMP", "TMP", "TMPDIR",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "HOME",
    "USERNAME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    "CODEX_HOME", "CARGO_HOME", "RUSTUP_HOME",
})


def codex_command():
    executable = shutil.which("codex")
    if not executable:
        return None
    entry = Path(executable)
    if entry.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        package = entry.parent / "node_modules" / "@openai" / "codex"
        native = next(package.rglob("codex.exe"), None) if package.is_dir() else None
        if native:
            return [str(native)]
        script = package / "bin" / "codex.js"
        if script.is_file() and shutil.which("node"):
            return [shutil.which("node"), str(script)]
    return [str(entry)]


class RunnerError(ValueError):
    pass


class CodexRunner(ExperimentMixin):
    def __init__(self, store, data_dir, model, *, command=None, approval_policy="on-request"):
        self.store, self.data_dir, self.model = store, Path(data_dir), model
        # Codex merges provider tables with global config, including auth fields.
        # A process-private id prevents inherited auth commands or headers.
        self.model_provider = "three_api_panel_" + secrets.token_hex(8)
        self.command = command if command is not None else codex_command()
        self.approval_policy = approval_policy
        self.tasks = {task["id"]: task for task in store.tasks()}
        self.thread_tasks = {task["thread_id"]: task["id"] for task in self.tasks.values() if task.get("thread_id")}
        self.process = None
        self.pending = {}
        self.approvals = {}
        self._next_id = 0
        self._startup_lock = asyncio.Lock()
        self._submission_lock = asyncio.Lock()
        self._route_lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._reader = self._stderr = self._ticker = None
        self._jobs = set()
        self._dirty = set()
        self._scanning = set()
        self._rescan = set()
        self._baselines = {}
        self._finalizing = {}
        self._released_runs = set()
        self._routes_to_release = set()
        self._binding_threads = set()
        self._recoverable = {}
        self._closing = False
        self._stderr_tail = ""
        self._ready = False
        self._models = []
        self.route_setup = None
        self.route_release = None
        self.run_usage = None
        self.proxy_url = "http://127.0.0.1:4000"
        self.proxy_token = None
        self.credential_env_names = set()
        self._turn_waiters = {}
        self._thread_turns = {}
        self._retired_turns = set()
        self.last_error = ""
        self._secrets = []
        with contextlib.suppress(Exception):
            value = get_secret(LOCAL_TOKEN_ID, PROXY_TOKEN_ENV)
            if value:
                self._secrets.append(value)
                self.proxy_token = value
        for task in self.tasks.values():
            if task["state"] in ACTIVE:
                self._recoverable[task["id"]] = task.get("run_id")
                task["state"] = "interrupted"
                task["approvals"] = []
                task.setdefault("messages", []).append({"id": secrets.token_hex(8), "run_id": task.get("run_id"), "role": "error", "text": "Panel został uruchomiony ponownie. Poprzednia praca została zatrzymana; możesz ją kontynuować."})
                task["finished_at"] = time.time()
                task["elapsed_seconds"] = (max(0, task["finished_at"] - task["started_at"])
                                           if task.get("started_at") is not None else None)
                task["scan_status"] = "interrupted"
                for agent in task.get("agents", []):
                    if agent["status"] in AGENT_ACTIVE:
                        agent["status"] = "interrupted"
                self.touch(task, save=True)
            if task.get("run_id"):
                self._routes_to_release.add(task["run_id"])

    def clean(self, value, limit=None):
        text = str(value or "")
        for secret in self._secrets:
            text = text.replace(secret, "[ukryty klucz]")
        if limit is None:
            return text
        return text[:limit] + ("\n… Skrócono podgląd długiej odpowiedzi." if len(text) > limit else "")

    def _persist(self, task):
        # The temporary UI placeholder is not a thread created by Codex.
        task["agents_count"] = sum(agent.get("id") != "main" for agent in task.get("agents", []))
        if self.run_usage is not None and task.get("run_id"):
            try:
                task["usage"] = self.run_usage(task["run_id"])
            except Exception:
                # Telemetry is independent of durable conversation storage.
                pass
        self.store.save_task(task)

    def touch(self, task, *, save=False):
        task["updated"] = time.time()
        self._dirty.add(task["id"])
        if save:
            try:
                self._persist(task)
            except Exception as exc:
                self.last_error = self.clean(str(exc), 1000)
            else:
                self._dirty.discard(task["id"])

    def activity(self, title, **fields):
        try:
            self.store.add_activity(title, **fields)
        except Exception as exc:
            self.last_error = self.clean(str(exc), 1000)

    def status(self):
        return {"available": bool(self.command) and not self._closing,
                "status": "connected" if self._ready else "idle" if self.command else "missing",
                "error": self.last_error}

    def public_tasks(self, selected=None):
        result = []
        ordered = sorted(self.tasks.values(), key=lambda t: t["updated"], reverse=True)
        chosen = ordered[:100]
        if selected in self.tasks and self.tasks[selected] not in chosen:
            chosen.append(self.tasks[selected])
        for task in chosen:
            public = {k: v for k, v in task.items() if not k.startswith("_") and k not in {"preferences", "change_history", "touched_paths", "thread_token_totals"}}
            if task["id"] != selected:
                public = {k: v for k, v in public.items() if k not in {"messages", "changes", "approvals"}}
                if "experiment" in public:
                    public["experiment"] = {k: v for k, v in public["experiment"].items() if k not in {"original_prompt", "plan", "lanes"}}
            else:
                # Full conversation and changes remain available via cursor reads.
                public["messages"] = task.get("messages", [])[-100:]
                public["messages_has_more"] = len(task.get("messages", [])) > 100
                public["changes"] = task.get("changes", [])[:100]
                public["changes_has_more"] = len(task.get("changes", [])) > 100
            result.append(public)
        return result

    def active_agents(self):
        return sum(a["status"] in AGENT_ACTIVE for t in self.tasks.values() for a in t.get("agents", []))

    def _job(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._jobs.add(task)
        def done(job):
            self._jobs.discard(job)
            if not job.cancelled() and (error := job.exception()) is not None:
                self.last_error = self.clean(str(error), 1000)
        task.add_done_callback(done)
        return task

    def _arguments(self):
        args = list(self.command or []) + ["app-server", "--stdio"]
        provider = tomlkit.inline_table()
        provider.update(self.provider_config())
        overrides = ['model_provider=' + json.dumps(self.model_provider), 'sandbox_mode="workspace-write"',
                     'model=' + json.dumps(self.model),
                     'model_providers.' + self.model_provider + '=' + provider.as_string(),
                     'features.enable_request_compression=false', 'features.responses_websockets=false',
                     'features.responses_websockets_v2=false',
                     'sandbox_workspace_write.writable_roots=[]', 'sandbox_workspace_write.network_access=false']
        source = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
        try:
            config = tomllib.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            config = {}
        # The panel has its own owned worker; expired unrelated connector logins
        # must not delay ordinary local coding tasks. No global config is edited.
        for name in config.get("mcp_servers", {}):
            if re.fullmatch(r"[A-Za-z0-9_-]+", name):
                overrides.append("mcp_servers." + name + ".enabled=false")
        for override in overrides:
            args.extend(["-c", override])
        return args

    def worker_environment(self):
        blocked = {name.upper() for name in self.credential_env_names}
        result = {key: value for key, value in os.environ.items()
                  if key.upper() in WORKER_ENV_ALLOWLIST and key.upper() not in blocked}
        if self.proxy_token:
            result[PROXY_TOKEN_ENV] = self.proxy_token
        return result

    def provider_config(self, route_id=None, prefs=None):
        prefs = prefs or preferences(self.store)
        base = self.proxy_url.rstrip("/")
        return {"name": "3API local proxy", "base_url": base + ("/r/" + route_id if route_id else "") + "/v1",
                "env_key": PROXY_TOKEN_ENV, "wire_api": "responses", "requires_openai_auth": False,
                "stream_max_retries": prefs["stream_retries"], "request_max_retries": prefs["request_retries"],
                "stream_idle_timeout_ms": 600000, "supports_websockets": False}

    async def models(self):
        await self.ensure_started()
        if not self._models:
            self._models = (await self.request("model/list", {"includeHidden": True})).get("data", [])
        return self._models

    async def skills(self, cwd):
        await self.ensure_started()
        data = await self.request("skills/list", {"cwds": [cwd], "forceReload": True})
        return next(iter(data.get("data", [])), {"skills": [], "errors": []})

    def task_config(self, task, route_id=None):
        prefs = task.get("preferences") or preferences(self.store)
        config = {"features.multi_agent": True, "agents.max_concurrent_threads_per_session": prefs["max_agents"],
                  "model_providers." + self.model_provider: self.provider_config(route_id, prefs),
                  "features.enable_request_compression": False, "features.responses_websockets": False,
                  "features.responses_websockets_v2": False}
        if prefs["context_window"]:
            config["model_context_window"] = prefs["context_window"]
        if prefs["compact_at"]:
            config["model_auto_compact_token_limit"] = prefs["compact_at"]
        if prefs["skill_overrides"]:
            config["skills.config"] = prefs["skill_overrides"]
        return config

    def permissions(self, task, *, read_only=False):
        yolo = task.get("permission_mode") == "yolo"
        if read_only:
            return "never", "read-only", {"type": "readOnly"}
        return ("never", "danger-full-access", {"type": "dangerFullAccess"}) if yolo else (
            "on-request", "workspace-write", {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False})

    async def setup_route(self, task, route_id, ids, strategy="priority", *, role="main", thread_id=None):
        if self.route_setup is None:
            return None
        async with self._route_lock:
            if task.get("_stop_requested") or task["state"] not in ACTIVE or task["state"] == "finalizing" or task.get("run_id") in self._released_runs:
                return None
            prefs = task["preferences"]
            await self.route_setup({"id": route_id, "providers": ids, "strategy": strategy,
                                    "wait_seconds": prefs["rate_limit_wait"], "max_output_tokens": prefs["max_output_tokens"],
                                    "project_id": task["project_id"], "task_id": task["id"],
                                    "run_id": task.get("run_id"), "thread_id": thread_id or "",
                                    "role": "" if task.get("mode") == "experimental" else role,
                                    "model": task.get("model", self.model)})
        return route_id

    async def _release_run(self, run_id):
        async with self._route_lock:
            if not run_id or run_id in self._released_runs:
                return
            if self.route_release is None:
                self._routes_to_release.add(run_id)
                return
            try:
                await self.route_release({"run_id": run_id})
            except Exception as exc:
                self._routes_to_release.add(run_id)
                self.last_error = self.clean(str(exc), 1000)
            else:
                self._released_runs.add(run_id)
                self._routes_to_release.discard(run_id)
                self._binding_threads.difference_update(key for key in tuple(self._binding_threads) if key[0] == run_id)

    async def release_stale_routes(self):
        if not self._closing and (self._ticker is None or self._ticker.done()):
            self._ticker = asyncio.create_task(self._tick())
        for run_id in list(self._routes_to_release):
            await self._release_run(run_id)

    async def recover_runs(self):
        """Attribute writes made after the last tick to the interrupted run."""
        async with self._recovery_lock:
            for task_id, run_id in list(self._recoverable.items()):
                task = self.tasks[task_id]
                self._scanning.add(task_id)
                try:
                    baseline_file = self.data_dir / "snapshots" / (str(run_id) + ".json.gz")
                    if not baseline_file.is_file():
                        baseline_file = self.data_dir / "snapshots" / (task_id + ".json.gz")
                    if baseline_file.is_file():
                        try:
                            self._baselines[task_id] = await asyncio.to_thread(load_baseline, baseline_file)
                        except (OSError, ValueError, EOFError):
                            task["scan_status"] = "unavailable"
                            task["scan_error"] = "Nie udało się odczytać porównania sprzed przerwanego uruchomienia; zachowano ostatnie zapisane zmiany."
                        else:
                            await self._scan(task)
                            if task.get("scan_status") == "error":
                                raise RunnerError(task["scan_error"])
                    else:
                        task["scan_status"] = "unavailable"
                        task["scan_error"] = "Nie znaleziono porównania sprzed przerwanego uruchomienia; zachowano ostatnie zapisane zmiany."
                    self._persist(task)
                except Exception as exc:
                    self.last_error = self.clean(str(exc), 1000)
                    self.touch(task)
                else:
                    self._recoverable.pop(task_id, None)
                    self._dirty.discard(task_id)
                finally:
                    self._scanning.discard(task_id)
                    self._baselines.pop(task_id, None)

    async def ensure_started(self):
        async with self._startup_lock:
            await self.release_stale_routes()
            if self._ready and self.process and self.process.returncode is None:
                return
            if self._closing or not self.command:
                raise RunnerError("Nie znaleziono Codex CLI. Uruchom instalację projektu i otwórz panel ponownie.")
            if self.process and self.process.returncode is None:
                await self._kill_owned()
            self._stderr_tail = ""
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            self.process = await asyncio.create_subprocess_exec(*self._arguments(), cwd=ROOT,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self.worker_environment(), limit=16 * 1024 * 1024, **flags)
            self._reader = asyncio.create_task(self._read(self.process))
            self._stderr = asyncio.create_task(self._read_stderr(self.process))
            try:
                await self.request("initialize", {"clientInfo": {"name": "three_api_panel", "title": "3API Panel", "version": "1.0.0"},
                                                  "capabilities": {"experimentalApi": True}}, timeout=25)
                await self.send({"method": "initialized"})
                self._ready = True
                self.last_error = ""
                if self._ticker is None or self._ticker.done():
                    self._ticker = asyncio.create_task(self._tick())
            except Exception:
                await self._kill_owned()
                raise

    async def send(self, value):
        if not self.process or self.process.returncode is not None:
            raise RunnerError("Połączenie z Codex zostało zamknięte.")
        async with self._write_lock:
            self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.process.stdin.drain()

    async def request(self, method, params, *, timeout=60):
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise RunnerError("Codex nie odpowiedział na czas. Spróbuj ponownie.") from None
        finally:
            self.pending.pop(request_id, None)

    async def _read_stderr(self, process):
        while raw := await process.stderr.read(8192):
            self._stderr_tail = (self._stderr_tail + self.clean(raw.decode("utf-8", "replace"), 8192))[-5000:]

    async def _read(self, process):
        try:
            while raw := await process.stdout.readline():
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and (isinstance(message["id"], bool) or not isinstance(message["id"], (str, int))):
                    continue
                if "method" in message:
                    try:
                        if not isinstance(message["method"], str) or not isinstance(message.get("params") or {}, dict):
                            raise ValueError("Invalid notification envelope")
                        if "id" in message:
                            await self.server_request(message)
                        else:
                            self.notification(message["method"], message.get("params") or {})
                    except Exception:
                        # One malformed event or approval must not disconnect
                        # all other running chats. Do not log its raw payload.
                        self.last_error = "Nie udało się przetworzyć zdarzenia Codex. Pozostałe czaty nadal działają."
                        if "id" in message:
                            with contextlib.suppress(Exception):
                                await self.send({"id": message["id"], "error": {"code": -32602, "message": "Invalid request parameters."}})
                elif (future := self.pending.get(message.get("id"))) is not None and not future.done():
                    if "error" in message:
                        remote_error = message["error"]
                        description = remote_error.get("message", "Błąd Codex.") if isinstance(remote_error, dict) else "Błąd Codex."
                        future.set_exception(RunnerError(self.clean(description, 2000)))
                    else:
                        future.set_result(message.get("result") or {})
        except Exception as exc:
            self.last_error = self.clean(str(exc), 1000)
        finally:
            if self.process is process:
                self._ready = False
                # EOF can precede process termination. Stop the owned worker
                # before waking jobs that will publish their final file scan.
                if not self._closing and process.returncode is None:
                    await self._kill_owned()
                for future in list(self.pending.values()):
                    if not future.done():
                        future.set_exception(RunnerError("Codex zakończył połączenie. " + self._stderr_tail[-1000:]))
                for future in list(self._turn_waiters.values()):
                    if not future.done():
                        future.set_exception(RunnerError("Codex zakończył połączenie. Kopie robocze i historia zostały zachowane."))
                if not self._closing:
                    for task in self.tasks.values():
                        if task["state"] in ACTIVE and task["state"] != "finalizing":
                            if task.get("_stop_requested"):
                                self.finish(task, "interrupted")
                            else:
                                self.fail(task, "Połączenie z Codex zostało przerwane. " + self._stderr_tail[-1000:])

    def create_chat(self, project_id, title="Nowy czat", **fields):
        task = self.store.create_chat(project_id, title=title, **fields)
        # A retry can read an older checkpoint while the original task is
        # streaming. Keep the live dictionary owned by its running coroutine.
        return self.tasks.setdefault(task["id"], task)

    def add_project(self, workspace=None, *, workspace_factory=None, client_request_id=None, request_fingerprint=None):
        workspace = workspace or {}
        project = self.store.add_project(workspace.get("path"), name=workspace.get("name"),
                                         kind=workspace.get("kind", "project"),
                                         access_mode=workspace.get("access_mode", "project"),
                                         workspace_factory=workspace_factory, client_request_id=client_request_id,
                                         request_fingerprint=request_fingerprint)
        # Re-adding a removed path restores the same chats and run history.
        for task in self.store.tasks():
            if task["project_id"] == project["id"] and task["id"] not in self.tasks:
                self.tasks[task["id"]] = task
                if task.get("thread_id"):
                    self.thread_tasks[task["thread_id"]] = task["id"]
        return project

    async def remove_project(self, project_id):
        async with self._submission_lock:
            if not self.store.project(project_id):
                error = RunnerError("Nie znaleziono projektu.")
                error.status_code = 404
                raise error
            tasks = [task for task in self.tasks.values() if task["project_id"] == project_id]
            if any(task["state"] in ACTIVE or task["id"] in self._recoverable or task["id"] in self._scanning
                   for task in tasks):
                error = RunnerError("Zakończ lub zatrzymaj zadania przed usunięciem projektu z panelu.")
                error.status_code = 409
                raise error
            # Flush pending messages before hiding their project. No filesystem
            # deletion is part of this operation, including for temporary chats.
            for task in tasks:
                self._persist(task)
            result = self.store.archive_project(project_id)
            task_ids = {task["id"] for task in tasks}
            for task_id in task_ids:
                self.tasks.pop(task_id, None)
                self._dirty.discard(task_id)
            for thread_id, task_id in tuple(self.thread_tasks.items()):
                if task_id in task_ids:
                    self.thread_tasks.pop(thread_id, None)
                    self._thread_turns.pop(thread_id, None)
            return result

    async def start_task(self, project_id, prompt, effort="medium", continue_task=None, *, options=None, client_request_id=None):
        # The admission decision and request id are serialized, including model
        # discovery. Two retries cannot both reserve a directory or create runs.
        async with self._submission_lock:
            await self.recover_runs()
            return await self._submit_task(project_id, prompt, effort, continue_task,
                                           options=options, client_request_id=client_request_id)

    async def _submit_task(self, project_id, prompt, effort, continue_task, *, options, client_request_id):
        project = self.store.project(project_id)
        if not project:
            raise RunnerError("Wybierz projekt, w którym Codex ma pracować.")
        if client_request_id is not None:
            if not isinstance(client_request_id, str) or not client_request_id.strip() or len(client_request_id) > 200:
                raise RunnerError("Nieprawidłowy identyfikator wysyłanej wiadomości.")
            previous = self.store.find_run_by_request(client_request_id)
            if previous:
                if previous["project_id"] != project_id or (continue_task and previous["task_id"] != continue_task):
                    raise RunnerError("Identyfikator wiadomości należy do innego czatu.")
                return {"id": previous["task_id"], "run_id": previous.get("run_id", previous["id"]), "state": previous["state"]}
        if any(self.tasks[task_id]["project_id"] == project_id for task_id in self._recoverable):
            raise RunnerError("Trwa odzyskiwanie i zapisywanie poprzedniego uruchomienia. Spróbuj ponownie za chwilę.")
        folder = Path(project["path"])
        if not folder.is_dir() or folder.resolve() != folder:
            raise RunnerError("Folder projektu nie jest już dostępny pod zapisaną ścieżką.")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
            raise RunnerError("Wpisz zadanie (maksymalnie 32 000 znaków).")
        if not isinstance(effort, str) or effort not in EFFORTS:
            raise RunnerError("Wybierz poziom rozumowania z listy.")
        task = self.tasks.get(continue_task) if continue_task else None
        if not task and client_request_id and not continue_task:
            task = next((t for t in self.tasks.values()
                         if t.get("pending_client_request_id") == client_request_id and t["project_id"] == project_id), None)
        if continue_task and (not task or task["project_id"] != project_id):
            raise RunnerError("Nie znaleziono zadania w wybranym projekcie.")
        if task and task["id"] in self._scanning:
            raise RunnerError("Trwa zapisywanie końcowego porównania plików. Spróbuj ponownie za chwilę.")
        options = options or {}
        prefs = preferences(self.store)
        mode = options.get("mode", task.get("mode", "standard") if task else "standard")
        permission = options.get("permission_mode", prefs["permission_mode"])
        if mode not in {"standard", "experimental"} or permission not in {"yolo", "approval"}:
            raise RunnerError("Wybierz poprawny tryb pracy i uprawnienia.")
        permission = workspace_permission(project, permission)
        api_ids = options.get("api_ids", task.get("api_ids", []) if task else [])
        if not isinstance(api_ids, list) or any(not isinstance(v, str) for v in api_ids) or len(set(api_ids)) != len(api_ids):
            raise RunnerError("Wybierz różne API z listy.")
        if mode == "experimental" and len(api_ids) != 3:
            raise RunnerError("Eksperyment wymaga trzech API z tym samym modelem: planisty i dwóch wykonawców.")
        model = options.get("model", self.model)
        if not isinstance(model, str) or not model or len(model) > 200:
            raise RunnerError("Wybierz model API.")
        if task and task.get("run_id") and task.get("mode", "standard") != mode:
            raise RunnerError("Aby zmienić zwykły tryb na eksperyment lub odwrotnie, wybierz Nowe zadanie.")
        if task is None:
            task = self.create_chat(project_id, title=self.clean(prompt.strip().splitlines()[0], 90),
                                    pending_client_request_id=client_request_id)
        if any(t["project_id"] == project_id and t["state"] in ACTIVE for t in self.tasks.values()):
            error = RunnerError("W tym projekcie trwa już zadanie. Nowy czat został zapisany; zakończ lub zatrzymaj bieżącą pracę.")
            error.task_id = task["id"]
            raise error
        if sum(t["state"] in ACTIVE for t in self.tasks.values()) >= 3:
            error = RunnerError("Trwają już trzy zadania. Czat został zapisany; poczekaj na zakończenie jednego z nich.")
            error.task_id = task["id"]
            raise error
        # The model catalog is supplied by the installed Codex, not a hardcoded alias.
        if self.route_setup is not None:
            catalog = await self.models()
            info = next((m for m in catalog if m["model"] == model), None)
            supported = {e["reasoningEffort"] for e in info["supportedReasoningEfforts"]} if info else {"low", "medium", "high", "xhigh"}
            needed = {"ultra"} if mode == "experimental" else {effort}
            if not needed <= supported:
                raise RunnerError("Ten model nie zgłasza wybranego poziomu rozumowania w Codexie.")
        user_id = secrets.token_hex(12)
        previous_task = copy.deepcopy(task)
        if task.get("thread_id") and task.get("turn_id"):
            self._retired_turns.add((task["thread_id"], task["turn_id"]))
        previous_totals = copy.deepcopy(task.get("thread_token_totals", task.get("thread_tokens", {})))
        run_id = secrets.token_hex(12)
        self._baselines.pop(task["id"], None)
        self._finalizing.pop(task["id"], None)
        for thread_id in [key for key, value in self.thread_tasks.items() if value == task["id"]]:
            self.thread_tasks.pop(thread_id, None)
            self._thread_turns.pop(thread_id, None)
        task.update(state="starting", effort=effort, approvals=[], _stop_requested=False, turn_id=None,
                    mode=mode, permission_mode=permission, api_ids=api_ids, model=model, preferences=prefs,
                    _budget_stop_pending=False, run_id=run_id, client_request_id=client_request_id,
                    pending_client_request_id=None, started_at=time.time(), finished_at=None,
                    elapsed_seconds=None, files=0, added=0, removed=0, touched_files=0, touched_paths=[],
                    changes=[], change_history=[], changes_revision=0, scan_status="pending", scan_skipped=0,
                    scan_error=None, tokens=None, usage=None, agents=[], agents_count=1, route_id=None,
                    thread_tokens={}, _token_baselines=previous_totals)
        task.setdefault("messages", []).append({"id": user_id, "run_id": run_id, "role": "user", "text": self.clean(prompt)})
        self.agent(task, task.get("thread_id") or "main", status="starting", name="Codex", description="Agent główny · " + self.model)
        task["updated"] = time.time()
        # Admission is acknowledged only once the run and idempotency key are
        # durable. Unlike a best-effort progress flush this may reject a start.
        try:
            self._persist(task)
        except Exception:
            task.clear()
            task.update(previous_task)
            raise
        self._dirty.discard(task["id"])
        self.activity("Uruchamianie zadania", subtitle=project["name"], icon="sparkles")
        self._job(self._run_experiment(task, project, prompt, user_id) if mode == "experimental"
                  else self._start_turn(task, project, prompt, effort, user_id))
        return {"id": task["id"], "run_id": run_id, "state": task["state"]}

    async def _start_turn(self, task, project, prompt, effort, user_id):
        try:
            await self.prepare_baseline(task, project)
            await self.ensure_started()
            if task.get("_stop_requested"):
                self.finish(task, "interrupted")
                return
            route_id = await self.setup_route(task, task["run_id"] + "-main", task.get("api_ids", []), "balanced" if effort == "ultra" else "priority")
            task["route_id"] = route_id
            approval, sandbox, policy = self.permissions(task)
            params = {"cwd": project["path"], "model": task.get("model", self.model), "modelProvider": self.model_provider,
                      "approvalPolicy": approval, "sandbox": sandbox,
                      "developerInstructions": task["preferences"]["main_prompt"], "config": self.task_config(task, route_id)}
            if task.get("thread_id"):
                result = await self.request("thread/resume", {**params, "threadId": task["thread_id"], "excludeTurns": True})
            else:
                result = await self.request("thread/start", params)
            thread_id = result["thread"]["id"]
            task["thread_id"] = thread_id
            task["agents"] = [a for a in task["agents"] if a["id"] != "main"]
            self.thread_tasks[thread_id] = task["id"]
            if route_id:
                await self.setup_route(task, route_id, task.get("api_ids", []), "balanced" if effort == "ultra" else "priority", thread_id=thread_id)
            self.agent(task, thread_id, status="running", name="Codex", description="Agent główny · " + self.model)
            if task.get("_stop_requested"):
                self.finish(task, "interrupted")
                return
            result = await self.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}],
                "clientUserMessageId": user_id, "effort": effort, "approvalPolicy": approval,
                "sandboxPolicy": policy})
            task["turn_id"] = result["turn"]["id"]
            if task["state"] == "starting":
                task["state"] = "running"
            self.touch(task, save=True)
            if task.get("_stop_requested") and task["state"] in ACTIVE:
                await self.stop_task(task["id"])
        except asyncio.CancelledError:
            self.finish(task, "interrupted")
            raise
        except Exception as exc:
            self.fail(task, self.clean(str(exc), 2000))

    async def prepare_baseline(self, task, project):
        baseline_file = self.data_dir / "snapshots" / (task["run_id"] + ".json.gz")
        before = await asyncio.to_thread(snapshot, Path(project["path"]))
        await asyncio.to_thread(save_baseline, baseline_file, before)
        self._baselines[task["id"]] = before
        task["scan_status"] = "partial" if before.get("skipped") else "ready"
        self.touch(task, save=True)

    def agent(self, task, thread_id, *, status=None, name=None, description=None):
        agent = next((a for a in task["agents"] if a["id"] == thread_id), None)
        if not agent:
            agent = {"id": thread_id, "name": name or "Agent " + str(len(task["agents"])), "status": "pendingInit", "description": "Agent pomocniczy"}
            task["agents"].append(agent)
        if status:
            agent["status"] = status
        if name:
            agent["name"] = self.clean(name, 100)
        if description:
            agent["description"] = self.clean(description, 250)
        return agent

    def bind_child(self, task, parent_thread_id, child_thread_id):
        if (not child_thread_id or child_thread_id == task.get("thread_id")
                or self.thread_tasks.get(parent_thread_id) != task["id"]
                or task.get("mode") == "experimental" or not task.get("route_id")
                or task["state"] not in ACTIVE or task["state"] == "finalizing"):
            return
        key = (task.get("run_id"), child_thread_id)
        if key in self._binding_threads:
            return
        self._binding_threads.add(key)
        self._job(self._bind_child(task, key))

    async def _bind_child(self, task, key):
        run_id, child_thread_id = key
        if task.get("run_id") != run_id:
            return
        try:
            await self.setup_route(task, task["route_id"], task.get("api_ids", []),
                                   "balanced" if task.get("effort") == "ultra" else "priority",
                                   role="auxiliary", thread_id=child_thread_id)
        except Exception as exc:
            self._binding_threads.discard(key)
            self.message(task, "routing-" + child_thread_id, "error", "Nie udało się przypisać API do agenta pomocniczego: " + self.clean(str(exc), 1000))
            self.touch(task, save=True)
            await self.stop_task(task["id"])

    def message(self, task, item_id, role, text, title=None, *, delta=False, thread_id=None):
        if thread_id:
            item_id = thread_id + ":" + item_id
        if task.get("run_id"):
            item_id = task["run_id"] + ":" + item_id
        # Imported histories can contain legacy strings or messages without an
        # id. Keep their content while matching only current structured items.
        message = next((m for m in task["messages"] if isinstance(m, dict) and m.get("id") == item_id), None)
        if message is None:
            message = {"id": item_id, "run_id": task.get("run_id"), "role": role, "text": ""}
            task["messages"].append(message)
        if thread_id:
            message["thread_id"] = thread_id
            agent = next((a for a in task["agents"] if a["id"] == thread_id), {})
            message["agent_name"] = agent.get("name", "Codex")
        message["text"] = self.clean(str(message.get("text") or "") + text if delta else text)
        if title:
            message["title"] = self.clean(title, 160)

    def notification(self, method, params):
        thread_id = params.get("threadId")
        if method == "thread/started":
            thread = params.get("thread") or {}
            verified = verified_thread_start(thread, self.thread_tasks)
            if not verified:
                return
            task_id, thread_id = verified
            source = thread.get("source")
            spawned = source.get("subAgent", {}).get("thread_spawn", {}) if isinstance(source, dict) and isinstance(source.get("subAgent"), dict) else {}
            parent = thread.get("parentThreadId") or spawned.get("parent_thread_id")
            if parent in self.thread_tasks:
                task = self.tasks[task_id]
                if task["state"] not in ACTIVE:
                    return
                self.thread_tasks[thread_id] = task_id
                self.agent(task, thread_id, name=thread.get("agentNickname") or spawned.get("agent_nickname"),
                           description=thread.get("agentRole") or spawned.get("agent_role"))
                self.bind_child(task, parent, thread_id)
                self.touch(task)
            return
        task = self.tasks.get(self.thread_tasks.get(thread_id))
        if not task or task["state"] not in ACTIVE:
            return
        turn_id = (params.get("turn") or {}).get("id") if method in {"turn/started", "turn/completed"} else params.get("turnId")
        if turn_id and (thread_id, turn_id) in self._retired_turns:
            return
        expected_turn = self._thread_turns.get(thread_id)
        if method != "turn/started" and turn_id and expected_turn and turn_id != expected_turn:
            return
        root = thread_id == task["thread_id"]
        experimental = task.get("mode") == "experimental"
        if method == "turn/started":
            if (root and task["state"] == "finalizing") or task["state"] not in ACTIVE:
                return
            self._thread_turns[thread_id] = params["turn"]["id"]
            if root:
                task["turn_id"] = params["turn"]["id"]
                if not task.get("_stop_requested"):
                    task["state"] = "running"
            self.agent(task, thread_id, status="running")
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            if root and task["state"] == "finalizing":
                return
            if root and task.get("turn_id") and turn.get("id") != task["turn_id"]:
                return
            status = turn.get("status", "failed")
            self.agent(task, thread_id, status=status)
            self._thread_turns.pop(thread_id, None)
            waiter = self._turn_waiters.get(thread_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(turn)
            if root and not experimental:
                if turn.get("error"):
                    self.message(task, "error-" + str(turn.get("id")), "error", self.clean(turn["error"].get("message", "Błąd zadania.")))
                self.finish(task, "interrupted" if task.get("_stop_requested") else status)
        elif method == "thread/status/changed":
            status = params.get("status") or {}
            mapped = {"active": "running", "idle": "idle" if root else "completed", "systemError": "errored", "notLoaded": "closed"}.get(status.get("type"))
            if mapped:
                self.agent(task, thread_id, status=mapped)
        elif method == "thread/tokenUsage/updated":
            usage = (params.get("tokenUsage") or {}).get("total")
            if usage:
                baseline = task.get("_token_baselines", {}).get(thread_id, {})
                task.setdefault("thread_token_totals", {})[thread_id] = usage
                task.setdefault("thread_tokens", {})[thread_id] = {
                    key: max(0, (value or 0) - (baseline.get(key) or 0)) for key, value in usage.items()}
                task["tokens"] = {key: sum(v.get(key) or 0 for v in task["thread_tokens"].values()) for key in usage}
                self.agent(task, thread_id)["tokens"] = task["thread_tokens"][thread_id].get("totalTokens")
                budget = task.get("preferences", {}).get("task_token_budget", 0)
                if budget and task["tokens"].get("totalTokens", 0) >= budget and task["state"] in ACTIVE and not task.get("_budget_stop_pending"):
                    task["_budget_stop_pending"] = True
                    self.message(task, "token-budget", "error", "Osiągnięto budżet tokenów zadania. Zapisane pliki i wątki pozostają dostępne do kontynuacji.")
                    self._job(self.stop_task(task["id"]))
        elif method == "item/agentMessage/delta":
            self.message(task, params["itemId"], "assistant", str(params.get("delta", "")), delta=True, thread_id=thread_id)
        elif method == "item/commandExecution/outputDelta":
            self.message(task, params["itemId"], "tool", str(params.get("delta", "")), delta=True, thread_id=thread_id)
        elif method in {"item/started", "item/completed"}:
            self.item(task, thread_id, params.get("item") or {}, completed=method.endswith("completed"))
        elif method == "error":
            error = params.get("error") or {}
            self.message(task, "error-" + secrets.token_hex(6), "error", self.clean(error.get("message", "Błąd połączenia z AI.")))
        else:
            return
        self.touch(task)

    def item(self, task, thread_id, item, *, completed):
        kind, item_id = item.get("type"), item.get("id", secrets.token_hex(8))
        for target in verified_child_threads(thread_id, item, self.thread_tasks, task["id"]):
            self.thread_tasks[target] = task["id"]
            self.bind_child(task, thread_id, target)
        if kind == "agentMessage":
            self.message(task, item_id, "assistant", str(item.get("text", "")), thread_id=thread_id)
            if completed:
                lane = next((l for l in task.get("experiment", {}).get("lanes", []) if l.get("thread_id") == thread_id), None)
                if lane is not None:
                    lane["last_output"] = self.clean(item.get("text", ""))
        elif kind == "commandExecution":
            command = str(item.get("command", ""))
            output = str(item.get("aggregatedOutput") or "")
            footer = "\nKod zakończenia: " + str(item.get("exitCode")) if completed and item.get("exitCode") is not None else ""
            self.message(task, item_id, "tool", command + ("\n\n" + output if output else "") + footer,
                         "Polecenie wykonane" if completed else "Wykonywanie polecenia", thread_id=thread_id)
            if not completed:
                self.activity("Codex uruchamia polecenie", subtitle=self.clean(command, 140), icon="terminal")
        elif kind == "fileChange":
            paths = [str(c.get("path", "")) for c in item.get("changes", [])]
            self.message(task, item_id, "tool", "\n".join(paths), "Zapisano zmiany" if completed and item.get("status") == "completed" else "Zmiany w plikach", thread_id=thread_id)
            if completed:
                self.schedule_scan(task)
        elif kind == "collabAgentToolCall":
            states = item.get("agentsStates") or {}
            for target in item.get("receiverThreadIds", []):
                if self.thread_tasks.get(target) != task["id"]:
                    continue
                self.agent(task, target, status=states.get(target, {}).get("status"), description=item.get("prompt"))
            for target, value in states.items():
                if self.thread_tasks.get(target) != task["id"]:
                    continue
                self.agent(task, target, status=value.get("status"))
            self.message(task, item_id, "tool", str(item.get("tool", "")) + " · " + str(len(item.get("receiverThreadIds", []))) + " agentów", "Współpraca agentów", thread_id=thread_id)
        elif kind == "subAgentActivity":
            target = item.get("agentThreadId")
            if target and self.thread_tasks.get(target) == task["id"]:
                self.agent(task, target, name=item.get("agentPath"), status={"started": "running", "completed": "completed", "interrupted": "interrupted"}.get(item.get("kind")))
        elif kind in {"mcpToolCall", "dynamicToolCall", "webSearch", "contextCompaction"}:
            title = {"webSearch": "Wyszukiwanie", "contextCompaction": "Porządkowanie kontekstu"}.get(kind, "Narzędzie")
            self.message(task, item_id, "tool", str(item.get("tool") or item.get("query") or ("Zakończono" if completed else "W toku")), title, thread_id=thread_id)
        # Reasoning content is intentionally never collected or exposed.

    def finish(self, task, status):
        if task["id"] in self._finalizing or task["state"] not in ACTIVE:
            return
        status = status if status in {"completed", "failed", "interrupted"} else "failed"
        task["state"] = "finalizing"
        self.agent(task, task.get("thread_id") or "main", status=status)
        for approval in task.get("approvals", []):
            self.approvals.pop(approval["id"], None)
        task["approvals"] = []
        task["finished_at"] = time.time()
        task["elapsed_seconds"] = (max(0, task["finished_at"] - task["started_at"])
                                   if task.get("started_at") is not None else None)
        self.touch(task, save=True)
        self._finalizing[task["id"]] = self._job(self._finalize(task, task.get("run_id"), status))

    async def _finalize(self, task, run_id, status):
        try:
            status = await self._finish_children(task, status)
            task["finished_at"] = time.time()
            task["elapsed_seconds"] = (max(0, task["finished_at"] - task["started_at"])
                                       if task.get("started_at") is not None else None)
            await self._release_run(run_id)
            # A scan already in flight may predate the final file write. Wait
            # for it, then scan once more before publishing the terminal state.
            while task["id"] in self._scanning:
                await asyncio.sleep(.02)
            if task["id"] in self._baselines:
                self._scanning.add(task["id"])
                await self._scan(task)
            elif task.get("scan_status") == "pending":
                task["scan_status"] = "unavailable"
            if task.get("scan_status") == "error" and status == "completed":
                status = "failed"
                self.message(task, "final-scan-error", "error",
                             "Nie udało się zapisać końcowego porównania plików. Zachowano rozmowę i ostatnie odczytane zmiany.")
            while True:
                try:
                    final = dict(task, state=status, updated=time.time())
                    self._persist(final)
                    break
                except Exception as exc:
                    self.last_error = self.clean(str(exc), 1000)
                    await asyncio.sleep(.5)
            # The completed flag cannot overtake its durable final diff.
            task.update(final)
            self._dirty.discard(task["id"])
            self.activity({"completed": "Zadanie ukończone", "failed": "Zadanie zakończone błędem", "interrupted": "Zadanie zatrzymane"}[status],
                          subtitle=task["title"], level="error" if status == "failed" else "info", icon="check" if status == "completed" else "stop")
        finally:
            self._finalizing.pop(task["id"], None)
            self._baselines.pop(task["id"], None)

    def _active_children(self, task):
        return [agent for agent in task.get("agents", [])
                if agent["id"] not in {"main", task.get("thread_id")} and agent["status"] in AGENT_ACTIVE]

    def _children_result(self, task, status):
        if status != "completed":
            return status
        children = [agent for agent in task.get("agents", []) if agent["id"] not in {"main", task.get("thread_id")}]
        if any(agent["status"] in {"failed", "errored"} for agent in children):
            return "failed"
        if task.get("_stop_requested") or any(agent["status"] != "completed" for agent in children):
            return "interrupted"
        return status

    async def _finish_children(self, task, status):
        """Do not scan final files while an owned child can still write them."""
        active = self._active_children(task)
        if not active:
            return self._children_result(task, status)

        def worker_exited():
            return self.process is None or self.process.returncode is not None

        async def wait_children(*, cancelable=False):
            while (self._active_children(task) and not worker_exited()
                   and not (cancelable and (task.get("_stop_requested") or self._closing))):
                await asyncio.sleep(.02)

        if status == "completed" and not worker_exited():
            # A root can finish while its children are still doing useful work.
            # Only an explicit stop, shutdown or failure cancels that work.
            await wait_children(cancelable=True)

        async def interrupt_child(agent):
            thread_id = agent["id"]
            turn_id = self._thread_turns.get(thread_id)
            if not turn_id:
                try:
                    result = await self.request("thread/read", {"threadId": thread_id, "includeTurns": True}, timeout=3)
                    turns = (result.get("thread") or {}).get("turns") or []
                    current = turns[-1] if turns else {}
                    if current.get("status") in {"completed", "failed", "interrupted"}:
                        self.agent(task, thread_id, status=current["status"])
                        return
                    turn_id = current.get("id")
                except Exception:
                    return
            if turn_id and agent["status"] in AGENT_ACTIVE:
                with contextlib.suppress(Exception):
                    await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=5)

        if self._active_children(task) and not worker_exited():
            await asyncio.gather(*(interrupt_child(agent) for agent in self._active_children(task)), return_exceptions=True)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wait_children(), 5)
        if self._active_children(task) and not worker_exited():
            self.message(task, "child-stop-timeout", "error", "Agent pomocniczy nie potwierdził zatrzymania. Zatrzymano proces Codex przed zapisaniem końcowych zmian.")
            await self._kill_owned()
        if worker_exited():
            for agent in self._active_children(task):
                self.agent(task, agent["id"], status="interrupted")
                self._thread_turns.pop(agent["id"], None)
        return self._children_result(task, status)

    def fail(self, task, message):
        self.last_error = self.clean(message, 2000)
        self.message(task, "error-" + secrets.token_hex(6), "error", self.last_error)
        self.finish(task, "failed")

    def schedule_scan(self, task, *, final=False):
        if final and task["id"] in self._scanning:
            self._rescan.add(task["id"])
        if task["id"] not in self._scanning and task["id"] in self._baselines and not self._closing and task["state"] != "finalizing":
            self._scanning.add(task["id"])
            self._job(self._scan(task))

    async def _scan(self, task):
        try:
            project = self.store.project(task["project_id"])
            if not project or not Path(project["path"]).is_dir():
                raise OSError("Project directory is unavailable")
            delta = await self.experiment_progress(task) if task.get("mode") == "experimental" else None
            if delta is None:
                after = await asyncio.to_thread(snapshot, Path(project["path"]))
                delta = await asyncio.to_thread(compare, self._baselines[task["id"]], after)
                delta["working_copies"] = False
            # File previews stay local; redact the configured credentials if a
            # task accidentally writes one of them into a source file.
            for change in delta["changes"]:
                change["diff"] = self.clean(change["diff"])
            uncertain = delta.pop("scan_incomplete_paths", [])
            if uncertain:
                found = {change["path"] for change in delta["changes"]}
                for old in task.get("changes", []):
                    if old["path"] not in found and any(not path or old["path"] == path or old["path"].startswith(path.rstrip("/") + "/") for path in uncertain):
                        delta["changes"].append(dict(old))
                delta["files"] = len({change["path"] for change in delta["changes"]})
                delta["added"] = sum(change["added"] for change in delta["changes"])
                delta["removed"] = sum(change["removed"] for change in delta["changes"])
            self._observe_changes(task, delta["changes"])
            task.update(delta)
            task["scan_status"] = "partial" if delta.get("scan_skipped") else "complete"
            task["scan_error"] = "Nie udało się odczytać wszystkich zmian w folderze." if delta.get("scan_skipped") else None
            self.touch(task, save=True)
        except Exception as exc:
            task["scan_error"] = "Nie udało się odczytać wszystkich zmian w folderze."
            task["scan_status"] = "error"
            self.last_error = self.clean(str(exc), 1000)
            self.touch(task)
        finally:
            self._scanning.discard(task["id"])
            if task["id"] in self._rescan:
                self._rescan.discard(task["id"])
                self.schedule_scan(task)
            elif task["state"] not in ACTIVE:
                self._baselines.pop(task["id"], None)

    def _observe_changes(self, task, changes):
        old = {change["path"]: change for change in task.get("changes", [])}
        new = {change["path"]: change for change in changes}
        changed = [path for path in sorted(old.keys() | new.keys()) if old.get(path) != new.get(path)]
        if not changed:
            return
        task["changes_revision"] = task.get("changes_revision", 0) + 1
        touched = set(task.get("touched_paths", []))
        history = task.setdefault("change_history", [])
        for path in changed:
            touched.add(path)
            entry = dict(new[path]) if path in new else {"path": path, "kind": "przywrócony", "added": 0, "removed": 0, "binary": False, "diff": "", "reverted": True}
            entry.update(id=secrets.token_hex(12), run_id=task.get("run_id"), revision=task["changes_revision"], time=time.time())
            history.append(entry)
        task["touched_paths"] = sorted(touched)
        task["touched_files"] = len(touched)

    async def _tick(self):
        last_scan = 0
        while not self._closing:
            await asyncio.sleep(.5)
            for task_id in list(self._dirty):
                try:
                    self._persist(self.tasks[task_id])
                except Exception as exc:
                    self.last_error = self.clean(str(exc), 1000)
                else:
                    self._dirty.discard(task_id)
            if time.monotonic() - last_scan >= 4:
                last_scan = time.monotonic()
                for task in self.tasks.values():
                    if task["state"] in ACTIVE and task["state"] != "finalizing":
                        try:
                            self.schedule_scan(task)
                        except Exception as exc:
                            self.last_error = self.clean(str(exc), 1000)
                await self.release_stale_routes()
                await self.recover_runs()

    async def stop_task(self, task_id):
        task = self.tasks.get(task_id)
        if not task:
            raise RunnerError("Nie znaleziono zadania.")
        if task["state"] not in ACTIVE or (task["state"] == "finalizing" and not self._active_children(task)):
            return {"state": task["state"]}
        task["_stop_requested"] = True
        if task.get("thread_id") and task.get("turn_id"):
            threads = [(tid, turn) for tid, turn in self._thread_turns.items() if self.thread_tasks.get(tid) == task_id]
            if not threads and task["state"] != "finalizing":
                threads = [(task["thread_id"], task["turn_id"])]
            async def interrupt(tid, turn):
                with contextlib.suppress(RunnerError):
                    await self.request("turn/interrupt", {"threadId": tid, "turnId": turn}, timeout=15)
            await asyncio.gather(*(interrupt(tid, turn) for tid, turn in threads))
        await self._release_run(task.get("run_id"))
        if task["state"] in ACTIVE and task["state"] != "finalizing":
            task["state"] = "stopping"
            self.touch(task, save=True)
            if not self._closing:
                self._job(self._stop_deadline(task, task.get("run_id")))
        return {"state": task["state"]}

    async def _stop_deadline(self, task, run_id):
        await asyncio.sleep(15)
        if task.get("run_id") == run_id and task["state"] == "stopping":
            self.message(task, "root-stop-timeout", "error",
                         "Codex nie potwierdził zatrzymania. Zatrzymano proces przed zapisaniem końcowych zmian.")
            await self._kill_owned()
            self.finish(task, "interrupted")

    async def server_request(self, message):
        method, params = message["method"], message.get("params") or {}
        task = self.tasks.get(self.thread_tasks.get(params.get("threadId")))
        if task and (task["state"] not in ACTIVE or task["state"] == "finalizing" or task.get("_stop_requested")):
            await self.send({"id": message["id"], "error": {"code": -32000, "message": "The task is finishing; no new approval is accepted."}})
            return
        supported = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                     "item/permissions/requestApproval", "item/tool/requestUserInput"}
        if not task or method not in supported:
            await self.send({"id": message["id"], "error": {"code": -32601, "message": "This panel cannot handle this request."}})
            if task:
                self.message(task, "unsupported-" + secrets.token_hex(6), "error", "Codex poprosił o funkcję, której panel nie obsługuje: " + method)
                self.touch(task)
            return
        approval_id = secrets.token_hex(12)
        question = method == "item/tool/requestUserInput"
        description = "\n".join(str(v) for v in (params.get("reason"), params.get("command"), params.get("cwd"), params.get("grantRoot")) if v)
        if method == "item/permissions/requestApproval":
            description += "\n" + json.dumps(params.get("permissions") or {}, ensure_ascii=False, indent=2)
        self.approvals[approval_id] = {"rpc_id": message["id"], "task_id": task["id"], "method": method, "params": params}
        if task.get("permission_mode") == "yolo" and not question:
            # The user explicitly selected full access for this task.
            await self.answer(task["id"], approval_id, "accept")
            return
        public = {"id": approval_id, "kind": "input" if question else "approval", "title": "Codex ma pytanie" if question else "Zgoda na operację",
                  "description": self.clean(description, 10000)}
        if question:
            public["questions"] = [{"id": q["id"], "question": self.clean(q.get("question"), 6000),
                "isSecret": bool(q.get("isSecret")), "options": [
                    {"label": self.clean(o.get("label"), 500), "description": self.clean(o.get("description"), 1500)}
                    for o in (q.get("options") or [])[:20]]} for q in (params.get("questions") or [])[:10]]
        task["approvals"].append(public)
        task["state"] = "awaiting_input"
        self.touch(task, save=True)
        self.activity("Codex czeka na Twoją odpowiedź", subtitle=task["title"], icon="clock")

    async def answer(self, task_id, approval_id, decision=None, answers=None):
        approval = self.approvals.get(approval_id)
        if not approval or approval["task_id"] != task_id:
            raise RunnerError("Ta prośba jest już nieaktualna.")
        method, params = approval["method"], approval["params"]
        if method == "item/tool/requestUserInput":
            if not isinstance(answers, dict):
                raise RunnerError("Wpisz odpowiedź na pytanie.")
            values = {}
            for question in params.get("questions", []):
                value = answers.get(question["id"])
                if not isinstance(value, str) or not value.strip() or len(value) > 10000:
                    raise RunnerError("Uzupełnij odpowiedzi na wszystkie pytania.")
                values[question["id"]] = {"answers": [value]}
            result = {"answers": values}
        else:
            if decision not in {"accept", "decline"}:
                raise RunnerError("Wybierz Zezwól lub Odmów.")
            result = {"decision": decision}
            if method == "item/permissions/requestApproval":
                result = {"permissions": params.get("permissions", {}) if decision == "accept" else {}, "scope": "turn"}
        await self.send({"id": approval["rpc_id"], "result": result})
        self.approvals.pop(approval_id, None)
        task = self.tasks[task_id]
        task["approvals"] = [a for a in task["approvals"] if a["id"] != approval_id]
        if not task["approvals"] and task["state"] == "awaiting_input":
            task["state"] = "running"
        self.touch(task, save=True)
        return {"ok": True}

    async def _kill_owned(self):
        process = self.process
        if process and process.returncode is None:
            if os.name == "nt":
                taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
                environment = self.worker_environment()
                environment.pop(PROXY_TOKEN_ENV, None)
                killer = await asyncio.create_subprocess_exec(str(taskkill), "/PID", str(process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    await asyncio.wait_for(killer.wait(), 8)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        killer.kill()
                    await asyncio.wait_for(killer.wait(), 3)
            else:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 8)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await asyncio.wait_for(process.wait(), 5)

    async def close(self):
        self._closing = True
        for task in self.tasks.values():
            if task["state"] in ACTIVE:
                with contextlib.suppress(Exception):
                    await self.stop_task(task["id"])
        await self._kill_owned()
        jobs = [self._reader, self._stderr, self._ticker, *self._jobs]
        finalizers = set(self._finalizing.values())
        for job in jobs:
            if job in finalizers:
                continue
            if job and not job.done():
                job.cancel()
        await asyncio.gather(*(j for j in jobs if j and j not in finalizers), return_exceptions=True)
        for task in self.tasks.values():
            if task["state"] in ACTIVE and task["state"] != "finalizing":
                self.finish(task, "interrupted")
        if self._finalizing:
            try:
                await asyncio.wait_for(asyncio.gather(*list(self._finalizing.values()), return_exceptions=True), 12)
            except asyncio.TimeoutError:
                self.last_error = "Nie udało się zapisać wszystkich wyników przed zamknięciem panelu."
        await self.release_stale_routes()
        for task_id in list(self._dirty):
            self.touch(self.tasks[task_id], save=True)
