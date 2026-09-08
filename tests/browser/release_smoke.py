"""Real Windows release smoke: bootstrap, tokenless HTTP, mock runs, restart and MCP.

Run this file with Python 3.11+ from the source checkout; the unpacked release does
not need tests, Rust, Node.js, an existing venv, or an installed Codex CLI:

    python tests/browser/release_smoke.py --root <GitHub/.../3api-windows-x64> \
        --work-dir <GitHub/.../new-smoke-work>

Both directories must resolve beneath a directory named GitHub. The release must
be a fresh extraction and work-dir must be new or empty. The script intentionally
keeps its local fixture, logs and JSON report for inspection. Do not include these
working directories in the final deliverable. Python dependencies are downloaded
by the release's unchanged bootstrap.py. All model traffic stays on loopback.
"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
import hashlib
from html.parser import HTMLParser
import http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener

MODEL = "release-smoke-model"
PROVIDER = "release-smoke"
PROXY_TOKEN_ENV = "LOCAL_RESPONSES_PROXY_TOKEN"
MOCK_KEY_ENV = "THREE_API_RELEASE_MOCK_KEY"
FINAL_STATES = {"completed", "failed", "error", "interrupted", "cancelled", "canceled"}
CREATE_HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_HTTP = 8 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def validate_location(path, label):
    path = Path(path)
    require(not path.is_symlink(), f"{label} must not be a symbolic link")
    resolved = path.resolve()
    parents = list(resolved.parents)
    require(any(parent.name.casefold() == "github" for parent in parents), f"{label} must be inside the local GitHub directory")
    require(not any(character in str(resolved) for character in '\r\n%!?^&|<>"'),
            f"{label} contains characters unsafe for the isolated Windows batch shim")
    return resolved


def verify_clean_extraction(root):
    require(root.is_dir(), "Release root does not exist")
    for name in ("target", "node_modules", ".venv", "providers.toml", "data", "logs", "backups", "__pycache__"):
        require(not (root / name).exists(), f"Fresh extraction must not contain {name}")
    manifest_path = root / "RELEASE_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest.get("kind") == "3api-windows-x64", "Wrong release manifest kind")
    files = manifest.get("files")
    require(isinstance(files, dict) and "3api.exe" in files, "Release manifest has no executable")
    observed = set()
    for path in root.rglob("*"):
        require(not path.is_symlink() and path.resolve().is_relative_to(root), "Extraction contains a link or path escape")
        if path.is_file():
            observed.add(path.relative_to(root).as_posix())
    require(observed == set(files) | {"RELEASE_MANIFEST.json"}, "Extraction contains missing or unexpected files; unpack the ZIP into a new directory")
    for relative, record in files.items():
        parts = PurePosixPath(relative).parts
        require(not any(part in {"", ".", ".."} for part in parts) and not relative.startswith("/")
                and "\\" not in relative and ":" not in relative, "Unsafe release manifest path")
        path = root / relative
        require(path.resolve().is_relative_to(root) and path.is_file(), "Manifest points outside extraction")
        require(path.stat().st_size == record["bytes"] and digest(path) == record["sha256"],
                f"Fresh extraction differs from manifest: {relative}")
    return {"payload_files": len(files), "exe_sha256": digest(root / "3api.exe")}


class Report:
    def __init__(self, directory, private_values):
        self.directory = directory
        self.private_values = private_values
        self.document = {"schema_version": 1, "started_at": time.time(), "status": "running", "checks": [],
                         "scope": "Windows release with a loopback provider and deterministic fake Codex App Server",
                         "limitations": ["No real AI provider was contacted.", "HTTP checks do not replace browser rendering tests.",
                                         "The installed Python runtime and package network are prerequisites; Python is not bundled."]}
        self.save()

    def redact(self, text):
        for value in self.private_values:
            text = text.replace(value, "[test secret redacted]")
        return text

    def save(self):
        value = self.redact(json.dumps(self.document, indent=2, ensure_ascii=False))
        (self.directory / "release-smoke-report.json").write_text(value + "\n", encoding="utf-8")

    @contextmanager
    def check(self, name):
        record = {"name": name, "status": "running"}
        self.document["checks"].append(record)
        self.save()
        print("CHECK: " + name, flush=True)
        started = time.monotonic()
        details = {}
        try:
            yield details
        except BaseException as exc:
            record.update(status="failed", error=self.redact(f"{type(exc).__name__}: {exc}"))
            raise
        else:
            record.update(status="passed", **details)
            print("PASS: " + name, flush=True)
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            self.save()


def environment(work, proxy_token, mock_key):
    allowed = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PROGRAMFILES", "PROGRAMFILES(X86)",
               "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    system_root = Path(env.get("SYSTEMROOT", r"C:\Windows"))
    profile = work / "profile"
    locations = {"USERPROFILE": profile, "HOME": profile, "APPDATA": profile / "AppData/Roaming",
                 "LOCALAPPDATA": profile / "AppData/Local", "CODEX_HOME": work / "codex-home",
                 "TEMP": work / "temp", "TMP": work / "temp", "PIP_CACHE_DIR": work / "pip-cache"}
    for key, path in locations.items():
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env.update(PATH=os.pathsep.join((str(work / "bin"), str(system_root / "System32"), str(system_root))),
               PATHEXT=".COM;.EXE;.BAT;.CMD", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
               PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONUTF8="1",
               **{PROXY_TOKEN_ENV: proxy_token, MOCK_KEY_ENV: mock_key})
    # Never call manage.py init/configure-codex: the test supplies both credentials
    # through environment variables and keeps its provider TOML in work-dir.
    return env


class LoggedProcess:
    def __init__(self, command, cwd, env, log_path, report):
        self.tail = deque(maxlen=15)
        self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=CREATE_HIDDEN)
        def drain():
            with log_path.open("w", encoding="utf-8") as stream:
                for raw in self.process.stdout:
                    line = report.redact(raw.decode("utf-8", errors="replace"))
                    stream.write(line)
                    stream.flush()
                    self.tail.append(line.strip())
        self.reader = threading.Thread(target=drain, daemon=True)
        self.reader.start()

    def wait(self, timeout):
        result = self.process.wait(timeout=timeout)
        self.reader.join(timeout=3)
        return result

    def force_stop(self):
        if self.process.poll() is None:
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
            # This exact Popen instance owns the whole tree. No search by port/name.
            subprocess.run([str(taskkill), "/PID", str(self.process.pid), "/T", "/F"], capture_output=True,
                           creationflags=CREATE_HIDDEN, timeout=15, check=False)
        if self.process.poll() is None:
            self.process.kill()
        self.wait(15)

    def finish(self, timeout):
        try:
            code = self.wait(timeout)
        except subprocess.TimeoutExpired:
            self.force_stop()
            raise AssertionError("Owned process exceeded its time limit") from None
        require(code == 0, "Owned process failed; inspect the redacted log")


def bootstrap(root, work, env, report, timeout):
    # The copied bootstrap creates its own .venv with the current base Python.
    process = LoggedProcess([sys.executable, "-I", "-B", str(root / "bootstrap.py")], root, env,
                            work / "bootstrap.log", report)
    process.finish(timeout)
    python = root / ".venv/Scripts/python.exe"
    require(python.is_file(), "Bootstrap did not create the release's Python environment")
    check = LoggedProcess([str(python), "-I", "-B", "-c",
                           "import httpx,keyring,starlette,tomlkit,uvicorn,psutil; print('WORKER_IMPORTS_OK')"],
                          root, env, work / "worker-imports.log", report)
    check.finish(30)
    require("WORKER_IMPORTS_OK" in check.tail, "Worker dependency import check produced no completion marker")
    return python


# The mock emits the same App Server events as tests/fixtures/fake_codex_app_server.py,
# plus unique IDs, model discovery and a real HTTP request through the Rust route.
# Only its explicitly embedded project can be written and only loopback is contacted.
FAKE_CODEX = r'''
import json, os, sys, time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

PROJECT = Path(__PROJECT__).resolve()
MODEL = __MODEL__
threads = {}
turn_number = 0

def send(value):
    print(json.dumps(value), flush=True)

def notify(method, tid, turn_id, **values):
    send({"method": method, "params": {"threadId": tid, "turnId": turn_id, **values}})

def provider(config):
    if isinstance(config, dict):
        if isinstance(config.get("base_url"), str):
            return config
        for value in config.values():
            found = provider(value)
            if found:
                return found
    return None

for line in sys.stdin:
    message = json.loads(line)
    method, rid = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        send({"id": rid, "result": {"userAgent": "3api-release-offline-fixture"}})
    elif method == "model/list":
        send({"id": rid, "result": {"data": [{"id": MODEL, "model": MODEL, "displayName": "Local release mock",
            "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": effort}
                for effort in ["low", "medium", "high", "xhigh", "max", "ultra"]]}]}})
    elif method == "skills/list":
        send({"id": rid, "result": {"data": [{"cwd": str(PROJECT), "skills": [], "errors": []}]}})
    elif method in {"thread/start", "thread/resume"}:
        if Path(params.get("cwd", "")).resolve() != PROJECT:
            raise RuntimeError("Fixture refused a project outside the smoke workspace")
        tid = params.get("threadId") or "release-thread-" + str(len(threads) + 1)
        previous = threads.get(tid, {})
        threads[tid] = {"provider": provider(params.get("config", {})), "count": previous.get("count", 0)}
        send({"id": rid, "result": {"thread": {"id": tid}}})
    elif method == "turn/start":
        tid = params["threadId"]
        turn_number += 1
        turn = "release-turn-" + str(turn_number)
        send({"id": rid, "result": {"turn": {"id": turn, "status": "inProgress", "items": []}}})
        notify("turn/started", tid, turn, turn={"id": turn, "status": "inProgress", "items": []})
        try:
            settings = threads[tid]["provider"]
            endpoint = urlsplit(settings["base_url"])
            if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port or endpoint.username:
                raise RuntimeError("Fixture refused a non-loopback API route")
            data = json.dumps({"model": MODEL, "input": "release smoke model call", "stream": False, "store": False}).encode()
            request = Request(settings["base_url"].rstrip("/") + "/responses", data=data,
                headers={"Content-Type": "application/json", "thread-id": tid, "session-id": tid,
                         "Authorization": "Bearer " + os.environ["LOCAL_RESPONSES_PROXY_TOKEN"]})
            with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
                result = json.loads(response.read(1024 * 1024))
            if result.get("status") != "completed":
                raise RuntimeError("Mock API did not complete")
            prompt = " ".join(item.get("text", "") for item in params["input"])
            content = "one\ntwo\nthree\n" if "second" in prompt else "one\ntwo\n"
            (PROJECT / "created-by-task.txt").write_text(content, encoding="utf-8", newline="\n")
            time.sleep(.2)
            threads[tid]["count"] += 1
            count = threads[tid]["count"]
            usage = {"inputTokens": 11, "outputTokens": 7, "totalTokens": 18, "cachedInputTokens": 3, "reasoningOutputTokens": 4}
            notify("thread/tokenUsage/updated", tid, turn, tokenUsage={"last": usage, "total": {key: value * count for key, value in usage.items()}})
            notify("item/completed", tid, turn, item={"id": turn + "-command", "type": "commandExecution", "command": "write fixture file",
                "aggregatedOutput": "RELEASE_FIXTURE_OK", "exitCode": 0, "status": "completed"})
            notify("item/completed", tid, turn, item={"id": turn + "-message", "type": "agentMessage", "text": "Task complete"})
            notify("turn/completed", tid, turn, turn={"id": turn, "status": "completed", "items": [], "error": None})
        except Exception as error:
            notify("turn/completed", tid, turn, turn={"id": turn, "status": "failed", "items": [],
                "error": {"message": "Release mock failed: " + type(error).__name__}})
    elif method == "turn/interrupt":
        send({"id": rid, "result": {}})
        notify("turn/completed", params["threadId"], "interrupted", turn={"id": "interrupted", "status": "interrupted"})
    elif rid is not None:
        send({"id": rid, "error": {"code": -32601, "message": "Unsupported offline fixture method"}})
'''


def install_codex_fixture(python, project, work, env):
    binary_dir = work / "bin"
    binary_dir.mkdir()
    fixture = binary_dir / "fake_app_server.py"
    fixture.write_text(FAKE_CODEX.replace("__PROJECT__", repr(str(project))).replace("__MODEL__", repr(MODEL)), encoding="utf-8")
    shim = binary_dir / "codex.cmd"
    shim.write_text(f'@echo off\n"{python}" -I -B "{fixture}"\nexit /b %errorlevel%\n', encoding="utf-8", newline="\r\n")
    # No %* forwarding: CLI configuration arguments never become batch commands.
    initialized = {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "shim-self-check", "version": "1"}}}
    result = subprocess.run([str(shim), "app-server", "--stdio"], input=json.dumps(initialized) + "\n", capture_output=True,
                            text=True, env=env, cwd=work, creationflags=CREATE_HIDDEN, timeout=15)
    require(result.returncode == 0, "The isolated Codex shim failed to start")
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    require(len(messages) == 1 and messages[0].get("id") == 1 and "result" in messages[0], "Codex shim did not complete JSON-RPC initialize")


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def reply(self, status, value):
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 1024 * 1024 or self.path != "/v1/responses" or self.headers.get("Authorization") != "Bearer " + self.server.mock_key:
            self.server.invalid_calls += 1
            self.close_connection = True
            self.reply(400, {"error": {"message": "Invalid local smoke request"}})
            return
        body = json.loads(self.rfile.read(size))
        if body.get("model") != MODEL or body.get("stream") is True:
            self.server.invalid_calls += 1
            self.reply(400, {"error": {"message": "Unexpected mock model or stream mode"}})
            return
        self.server.calls += 1
        self.reply(200, {"id": "release-response-" + str(self.server.calls), "object": "response", "status": "completed",
                         "model": MODEL, "output": [{"id": "release-message", "type": "message", "role": "assistant",
                                                     "status": "completed", "content": [{"type": "output_text", "text": "Task complete", "annotations": []}]}],
                         "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
                                   "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 4}}})

    def do_GET(self):
        if self.path == "/v1/models":
            self.reply(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self.reply(404, {"error": {"message": "No mock endpoint"}})


def mock_provider(mock_key):
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    server.daemon_threads = True
    server.mock_key, server.calls, server.invalid_calls = mock_key, 0, 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def write_config(work, port):
    path = work / "providers.toml"
    path.write_text(f'''[proxy]
public_model = "{MODEL}"
proxy_token_env = "{PROXY_TOKEN_ENV}"
allow_loopback_upstreams = true
connect_timeout_seconds = 3
read_timeout_seconds = 20
cooldown_wait_seconds = 0
log_level = "WARNING"
[[providers]]
id = "{PROVIDER}"
enabled = true
base_url = "http://127.0.0.1:{port}/v1"
deployment = "{MODEL}"
api_key_env = "{MOCK_KEY_ENV}"
auth_type = "bearer"
cooldown_seconds = 0
''', encoding="utf-8")
    return path


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Panel:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPCookieProcessor(self.cookies))
        self.csrf = None

    def request(self, method, path, data=None, *, expected=(200,), headers=None, csrf=True):
        supplied = {"Accept": "application/json"}
        if method != "GET":
            supplied.update({"Origin": self.base, "Content-Type": "application/json"})
            if csrf and self.csrf:
                supplied["x-panel-csrf"] = self.csrf
        supplied.update(headers or {})
        payload = json.dumps(data).encode("utf-8") if data is not None else None
        request = Request(self.base + path, data=payload, method=method, headers=supplied)
        try:
            response = self.opener.open(request, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(MAX_HTTP + 1)
            require(len(raw) <= MAX_HTTP, "Panel response exceeded smoke safety limit")
            require(response.status in expected, f"{method} {path.split('?')[0]} returned HTTP {response.status}, expected {expected}")
            return raw, response.headers

    def json(self, path, data=None, *, expected=(200,), method=None):
        raw, _ = self.request(method or ("POST" if data is not None else "GET"), path, data, expected=expected)
        return json.loads(raw)

    def open(self):
        raw, headers = self.request("GET", "/ui/")
        class Markup(HTMLParser):
            token_input = False
            csrf = None
            workspace = False
            def handle_starttag(self, tag, attributes):
                values = dict(attributes)
                if values.get("id") == "workspace":
                    self.workspace = True
                if tag == "input" and values.get("id") == "token":
                    self.token_input = True
                if tag == "meta" and values.get("name") == "csrf-token":
                    self.csrf = values.get("content")
        markup = Markup()
        markup.feed(raw.decode("utf-8"))
        require(markup.workspace and not markup.token_input, "GET /ui/ still displays a token login instead of the application")
        require(markup.csrf and markup.csrf != "__CSRF__", "Panel did not establish a CSRF session")
        cookies = headers.get_all("Set-Cookie") or []
        require(cookies and all("httponly" in cookie.lower() and "samesite=" in cookie.lower() for cookie in cookies),
                "Session cookies must be HttpOnly and SameSite")
        require(self.cookies, "GET /ui/ did not create local session cookies")
        self.csrf = markup.csrf
        state = self.json("/ui/api/state")
        require(state.get("schema_version") == 2, "Final backend state schema_version must be 2")
        return state


def ports():
    import socket
    reservations = []
    try:
        for _ in range(2):
            for attempt in range(100):
                port = 16000 + secrets.randbelow(16000)
                reservation = socket.socket()
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    reservation.bind(("127.0.0.1", port))
                except OSError:
                    reservation.close()
                    continue
                reservations.append(reservation)
                break
            else:
                raise AssertionError("Could not allocate loopback ports")
        return [reservation.getsockname()[1] for reservation in reservations]
    finally:
        for reservation in reservations:
            reservation.close()


def wait_until(callback, *, timeout=45, description="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = callback()
        if result:
            return result
        time.sleep(.2)
    raise AssertionError("Timed out waiting for " + description)


def start_application(root, work, config, env, report, attempt):
    proxy_port, panel_port = ports()
    process = LoggedProcess([str(root / "3api.exe"), "serve", "--config", str(config), "--data-dir", str(work / "data"),
                             "--project-dir", str(root), "--proxy-port", str(proxy_port), "--panel-port", str(panel_port)],
                            root, env, work / f"application-{attempt}.log", report)
    panel = Panel(panel_port)
    def ready():
        require(process.process.poll() is None, "Rust application exited before health was ready; inspect its log")
        try:
            return panel.json("/health").get("application") == "3api-rust-panel"
        except (URLError, ConnectionError, TimeoutError):
            return False
    try:
        wait_until(ready, timeout=60, description="Rust/private-worker readiness")
    except BaseException:
        process.force_stop()
        raise
    return process, panel, proxy_port


def stop_application(process, panel, *, normal):
    if process.process.poll() is not None:
        require(not normal or process.process.returncode == 0, "Application exited with an error")
        return
    try:
        panel.open()
        state = panel.json("/ui/api/state")
        for task in state.get("tasks", []):
            if task.get("state") not in FINAL_STATES | {"idle", "pending", "closed"}:
                panel.json(f"/ui/api/tasks/{task['id']}/stop", {}, expected=(200, 202))
        panel.json("/ui/api/shutdown", {})
        process.finish(35)
    except BaseException:
        process.force_stop()
        if normal:
            raise


def items(panel, path):
    result, cursor, seen = [], None, set()
    for _ in range(100):
        query = {"limit": 1}
        if cursor is not None:
            query["cursor"] = cursor
        response = panel.json(path + ("&" if "?" in path else "?") + urlencode(query))
        require(isinstance(response.get("items"), list), "Paginated endpoint omitted items")
        result.extend(response["items"])
        cursor = response.get("next_cursor")
        if cursor in (None, ""):
            return result
        require(cursor not in seen, "Paginated endpoint repeated its cursor")
        seen.add(cursor)
    raise AssertionError("Pagination did not terminate")


def run_detail(panel, task_id, run_id):
    response = panel.json(f"/ui/api/tasks/{task_id}/runs/{run_id}?limit=100")
    result = dict(response.get("run", response))
    for field in ("messages", "changes", "api_events"):
        page = response.get(field, result.get(field))
        if isinstance(page, dict):
            require(not page.get("next_cursor"), "Unexpectedly long deterministic run detail")
            page = page.get("items")
        require(isinstance(page, list), f"Run detail omitted its {field} page")
        result[field] = page
    return result


def identifier(response):
    for field in ("chat", "task", "project"):
        if isinstance(response.get(field), dict):
            response = response[field]
            break
    value = response.get("id") or response.get("task_id")
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value), "Endpoint returned no valid persistent ID")
    return value


def complete_run(panel, project_id, task_id, prompt):
    request = {"project_id": project_id, "continue_task": task_id, "prompt": prompt,
               "client_request_id": secrets.token_hex(16), "mode": "standard", "effort": "medium", "api_ids": [PROVIDER]}
    started = panel.json("/ui/api/tasks", request, expected=(200, 202))
    require(started.get("id") == task_id and started.get("run_id"), "Task acceptance did not preserve chat ID or allocate run_id")
    run_id = started["run_id"]
    repeated = panel.json("/ui/api/tasks", request, expected=(200, 202))
    require(repeated.get("id") == task_id and repeated.get("run_id") == run_id, "client_request_id retry created another run")
    def finished():
        detail = run_detail(panel, task_id, run_id)
        if detail.get("state") in FINAL_STATES:
            require(detail["state"] == "completed", "Mock task ended in " + str(detail["state"]))
            return detail
        return None
    return wait_until(finished, timeout=60, description="completed mock run and final persisted results")


def verify_run(detail, project_id, task_id, added):
    require(detail.get("project_id") == project_id and detail.get("task_id") == task_id, "Run belongs to the wrong project/chat")
    require(detail.get("state") == "completed", "Run is not completed")
    require((detail.get("files"), detail.get("added"), detail.get("removed")) == (1, added, 0), "Run diff is not relative to its own baseline")
    require(detail.get("agents_count") == 1, "Run must report one actual fixture agent")
    require(PROVIDER in detail.get("api_ids", []), "Run did not save the API it used")
    require(detail["messages"] and detail["changes"] and detail["api_events"], "Completed run lost messages, changes or API history")
    require(any("created-by-task.txt" in str(change.get("path", change.get("file", ""))) for change in detail["changes"]),
            "The observed fixture file is missing from the diff")
    require(isinstance(detail.get("started_at"), (int, float)) and isinstance(detail.get("finished_at"), (int, float))
            and detail["finished_at"] >= detail["started_at"], "New run has no ordered Unix timestamps")
    require(isinstance(detail.get("elapsed_seconds"), (int, float)) and detail["elapsed_seconds"] >= 0, "New run has no elapsed time")


def durable_signature(detail):
    fields = ("id", "task_id", "project_id", "state", "started_at", "finished_at", "elapsed_seconds", "agents_count",
              "api_ids", "files", "added", "removed", "touched_files", "changes_revision", "scan_status", "usage",
              "messages", "changes", "api_events")
    return json.dumps({field: detail.get(field) for field in fields}, sort_keys=True, ensure_ascii=False)


class Mcp:
    def __init__(self, root, config, data, proxy_port, env, report):
        self.report = report
        self.process = subprocess.Popen([str(root / "3api.exe"), "mcp", "--config", str(config), "--data-dir", str(data),
                                         "--proxy-url", f"http://127.0.0.1:{proxy_port}"], cwd=root, env=env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_HIDDEN)
        self.output, self.errors, self.messages = queue.Queue(), [], []
        def read():
            try:
                for line in self.process.stdout:
                    try:
                        self.output.put(json.loads(line))
                    except ValueError:
                        self.output.put(AssertionError("MCP stdout was not JSON"))
            finally:
                self.output.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.stderr_reader = threading.Thread(target=lambda: self.errors.append(self.process.stderr.read()), daemon=True)
        self.reader.start()
        self.stderr_reader.start()

    def send(self, message):
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def call(self, method, params):
        rid = len(self.messages) + 1
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        reply = self.output.get(timeout=20)
        require(isinstance(reply, dict) and reply.get("jsonrpc") == "2.0" and reply.get("id") == rid,
                "MCP ended, emitted invalid JSON-RPC or answered a notification")
        require("result" in reply and "error" not in reply, "MCP request failed: " + method)
        self.messages.append(reply)
        return reply["result"]

    def tool(self, name, arguments=None):
        result = self.call("tools/call", {"name": name, "arguments": arguments or {}})
        require(result.get("isError") is False, "MCP tool failed: " + name)
        return json.loads(result["content"][0]["text"])

    def close(self):
        try:
            self.process.stdin.close()
            self.process.wait(timeout=10)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.stderr_reader.join(timeout=2)
        raw = json.dumps(self.messages).encode() + b"".join(self.errors)
        require(self.process.returncode == 0, "MCP did not exit cleanly on stdin EOF")
        require(all(secret.encode() not in raw for secret in self.report.private_values), "MCP exposed a test credential")
        require(b"created-by-task.txt" not in raw and b"release smoke first" not in raw, "MCP exposed fixture content")


def smoke(root, work, report, env, mock_key, bootstrap_timeout):
    application = panel = mock = None
    normal_exit = False
    try:
        with report.check("fresh extraction integrity; no target, node_modules or venv") as details:
            details.update(verify_clean_extraction(root))
        with report.check("release bootstrap and private Python worker imports"):
            python = bootstrap(root, work, env, report, bootstrap_timeout)
        project = work / "project"
        project.mkdir()
        with report.check("isolated fake Codex CLI on PATH; JSON-RPC initialize"):
            install_codex_fixture(python, project, work, env)
        mock = mock_provider(mock_key)
        config = write_config(work, mock.server_port)
        with report.check("Rust release configuration and first HTTP/private-worker startup"):
            check = LoggedProcess([str(root / "3api.exe"), "check", "--config", str(config)], root, env, work / "config-check.log", report)
            check.finish(20)
            application, panel, proxy_port = start_application(root, work, config, env, report, 1)
        with report.check("GET /ui/ without token; local cookies, CSRF, Host and Origin"):
            Panel(int(urlsplit(panel.base).port)).request("GET", "/ui/api/state", expected=(401,))
            panel.open()
            panel.request("GET", "/ui/api/state", expected=(403,), headers={"Origin": "https://example.invalid"})
            panel.request("GET", "/ui/api/state", expected=(403,), headers={"Host": "example.invalid"})
            panel.request("POST", "/ui/api/chats", {}, expected=(403,), csrf=False)
        with report.check("two empty chats are persisted before their first task"):
            project_id = identifier(panel.json("/ui/api/projects", {"path": str(project)}, expected=(200, 201)))
            task_id = identifier(panel.json("/ui/api/chats", {"project_id": project_id}, expected=(200, 201)))
            empty_id = identifier(panel.json("/ui/api/chats", {"project_id": project_id}, expected=(200, 201)))
            require(task_id != empty_id, "Two new chats received the same ID")
            require({task_id, empty_id}.issubset({item["id"] for item in items(panel, f"/ui/api/projects/{project_id}/chats")}),
                    "Empty chat disappeared before a task was submitted")
        with report.check("first mock task, idempotent submission, saved diff and API history") as details:
            first = complete_run(panel, project_id, task_id, "release smoke first")
            verify_run(first, project_id, task_id, 2)
            first_id = first["id"]
            original = durable_signature(first)
            require((project / "created-by-task.txt").read_text(encoding="utf-8") == "one\ntwo\n", "Fixture file was not actually written")
            for _ in range(3):
                state = panel.json("/ui/api/state?" + urlencode({"project_id": project_id, "task_id": task_id, "run_id": first_id}))
                require(state.get("schema_version") == 2, "Polling reverted state schema")
                require(durable_signature(run_detail(panel, task_id, first_id)) == original, "Polling changed a completed run or erased its diff")
            details.update(files=first["files"], added=first["added"], removed=first["removed"], agents_count=first["agents_count"])
        with report.check("continuation has a new baseline and preserves the previous run") as details:
            second = complete_run(panel, project_id, task_id, "release smoke second")
            verify_run(second, project_id, task_id, 1)
            second_id = second["id"]
            require(second_id != first_id, "Continuation reused the previous run_id")
            require(durable_signature(run_detail(panel, task_id, first_id)) == original, "Continuation overwrote the first run")
            require((project / "created-by-task.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n", "Second fixture edit was not written")
            history = items(panel, f"/ui/api/projects/{project_id}/history?task_id={task_id}")
            require({row["id"] for row in history} == {first_id, second_id}, "Paginated history lost or duplicated a run")
            require(mock.calls == 2 and mock.invalid_calls == 0, "Idempotent submissions did not produce exactly two valid loopback API calls")
            second_original = durable_signature(second)
            details.update(files=second["files"], added=second["added"], removed=second["removed"], api_calls=mock.calls)
        with report.check("graceful application shutdown and restart with the same data"):
            stop_application(application, panel, normal=True)
            application = None
            application, panel, proxy_port = start_application(root, work, config, env, report, 2)
            panel.open()
            chats = items(panel, f"/ui/api/projects/{project_id}/chats")
            require({task_id, empty_id}.issubset({row["id"] for row in chats}), "Restart lost a populated or empty chat")
            require(durable_signature(run_detail(panel, task_id, first_id)) == original, "Restart changed the first run's persisted results")
            require(durable_signature(run_detail(panel, task_id, second_id)) == second_original, "Restart changed continuation results")
            require({row["id"] for row in items(panel, f"/ui/api/projects/{project_id}/history")} == {first_id, second_id},
                    "Project history did not survive restart")
        with report.check("release MCP initialize, initialized, tools/list and tools/call") as details:
            mcp = Mcp(root, config, work / "data", proxy_port, env, report)
            try:
                initialized = mcp.call("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "3api-release-smoke", "version": "1"}})
                require(initialized.get("serverInfo"), "MCP initialize has no server information")
                mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                tools = mcp.call("tools/list", {}).get("tools", [])
                names = {tool["name"] for tool in tools}
                require({"3api_status", "3api_providers", "3api_usage", "3api_projects", "3api_tasks"}.issubset(names), "MCP tools/list is incomplete")
                require(mcp.tool("3api_status").get("reachable") is True, "MCP could not reach the restarted Rust proxy")
                projects = mcp.tool("3api_projects").get("projects", [])
                require(project_id in {row["id"] for row in projects}, "MCP could not read the persistent project")
                tasks = mcp.tool("3api_tasks", {"project_id": project_id}).get("tasks", [])
                require(task_id in {row["id"] for row in tasks}, "MCP could not read the persistent chat")
                details.update(listed_tools=len(tools), tools_called=["3api_status", "3api_projects", "3api_tasks"])
            finally:
                mcp.close()
        with report.check("final graceful shutdown; release payload stayed unchanged"):
            stop_application(application, panel, normal=True)
            application = None
            manifest = json.loads((root / "RELEASE_MANIFEST.json").read_text(encoding="utf-8"))
            require(all(digest(root / relative) == record["sha256"] for relative, record in manifest["files"].items()),
                    "Runtime smoke modified a shipped release file")
            require(not (root / "target").exists() and not (root / "node_modules").exists(), "Runtime unexpectedly required a build or Node modules")
        normal_exit = True
    finally:
        if application:
            stop_application(application, panel, normal=False)
        if mock:
            mock.shutdown()
            mock.server_close()
        report.document["status"] = "passed" if normal_exit else "failed"
        report.document["finished_at"] = time.time()
        report.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Freshly unpacked 3api-windows-x64 directory under GitHub")
    parser.add_argument("--work-dir", type=Path, required=True, help="New or empty local test directory under GitHub; kept for inspection")
    parser.add_argument("--bootstrap-timeout", type=int, default=300, help="Maximum seconds for first dependency installation (default: 300)")
    args = parser.parse_args(argv)
    report = None
    try:
        require(os.name == "nt", "This release smoke requires Windows x64")
        require(sys.version_info >= (3, 11), "Python 3.11+ is required")
        root = validate_location(args.root, "Release root")
        work = validate_location(args.work_dir, "Work directory")
        require(not work.is_relative_to(root) and not root.is_relative_to(work), "Release and work directories must be separate")
        require(not work.exists() or work.is_dir() and not any(work.iterdir()), "Work directory must be new or empty")
        require(30 <= args.bootstrap_timeout <= 1800, "Bootstrap timeout must be between 30 and 1800 seconds")
        work.mkdir(parents=True, exist_ok=True)
        proxy_token, mock_key = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
        report = Report(work, [proxy_token, mock_key])
        env = environment(work, proxy_token, mock_key)
        smoke(root, work, report, env, mock_key, args.bootstrap_timeout)
    except BaseException as exc:
        text = f"{type(exc).__name__}: {exc}"
        if report:
            text = report.redact(text)
            report.document.update(status="failed", error=text, finished_at=time.time())
            report.save()
        print("FAIL: " + text, flush=True)
        return 1
    print("PASS: fresh Windows release, Python bootstrap, tokenless local session, two chats/two mock runs, restart and MCP", flush=True)
    print("Report: " + str(work / "release-smoke-report.json"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
