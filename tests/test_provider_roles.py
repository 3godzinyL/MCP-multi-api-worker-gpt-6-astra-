"""Role routing checks against the installed Codex, using loopback responses only."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import tomlkit

from dashboard.routing import verified_child_thread, verified_child_threads, verified_thread_start


def native_codex_command():
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


def _event(name, **value):
    return ("event: " + name + "\ndata: " + json.dumps({"type": name, **value}) + "\n\n").encode()


def _reply(index, item):
    initial = {"id": f"response-{index}", "object": "response", "status": "in_progress", "output": []}
    return b"".join([
        _event("response.created", sequence_number=0, response=initial),
        _event("response.output_item.added", sequence_number=1, output_index=0, item=item),
        _event("response.output_item.done", sequence_number=2, output_index=0, item=item),
        _event("response.completed", sequence_number=3, response={
            **initial, "status": "completed", "output": [item],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}),
    ])


class _ProbeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _ProbeHandler)
        self.calls = []
        self.lock = threading.Lock()
        self.spawned = set()


class _ProbeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Record identities only. Never put model inputs, tokens, or provider
        # authorization headers into the probe report or assertion output.
        safe_headers = {key: self.headers[key] for key in ("thread-id", "session-id", "x-codex-parent-thread-id")
                        if key in self.headers}
        metadata = json.loads(self.headers.get("x-codex-turn-metadata", "{}"))
        metadata = {key: metadata.get(key) for key in ("thread_id", "session_id", "turn_id", "parent_thread_id", "root_turn_id")}
        tools = []
        tool_definitions = body.get("tools", []) + [tool for item in body.get("input", []) for tool in item.get("tools", [])]
        for tool in tool_definitions:
            if tool.get("type") == "namespace":
                tools += [(tool.get("name"), member.get("name")) for member in tool.get("tools", [])]
            else:
                tools.append((None, tool.get("name")))
        with self.server.lock:
            index = len(self.server.calls) + 1
            self.server.calls.append({"path": self.path, "headers": safe_headers, "metadata": metadata})
            thread_id = safe_headers.get("thread-id")
            spawn = not safe_headers.get("x-codex-parent-thread-id") and thread_id not in self.server.spawned
            if spawn:
                self.server.spawned.add(thread_id)
        if spawn:
            namespace, name = next(((ns, name) for ns, name in tools if name == "spawn_agent"), ("collaboration", "spawn_agent"))
            arguments = {"message": "Reply with CHILD_OK. Do not use tools."}
            arguments.update({"task_name": "probe_worker"} if namespace == "collaboration" else {"agent_type": "worker"})
            item = {"id": f"spawn-{index}", "type": "function_call", "call_id": f"spawn-{index}", "name": name,
                    "arguments": json.dumps(arguments),
                    "status": "completed"}
            if namespace:
                item["namespace"] = namespace
        else:
            item = {"id": f"message-{index}", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "CHILD_OK", "annotations": []}]}
        payload = _reply(index, item)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _AppServerProbe:
    def __init__(self, command, folder, base_url, *, token="offline-probe-token", on_event=None):
        self.folder = folder
        codex_dir, workspace = folder / "codex", folder / "workspace"
        codex_dir.mkdir(parents=True)
        workspace.mkdir()
        config = {
            "model": "gpt-6-astra", "model_provider": "routing_probe", "model_reasoning_effort": "low",
            "approval_policy": "never", "sandbox_mode": "danger-full-access",
            "model_providers": {"routing_probe": {
                "name": "Loopback routing probe", "base_url": base_url + "/r/probe-main/v1",
                "env_key": "ROUTING_PROBE_TOKEN", "wire_api": "responses", "requires_openai_auth": False,
                "request_max_retries": 0, "stream_max_retries": 0, "stream_idle_timeout_ms": 10000,
                "supports_websockets": False}},
            "features": {"multi_agent": True, "enable_request_compression": False,
                         "responses_websockets": False, "responses_websockets_v2": False},
            "agents": {"max_concurrent_threads_per_session": 3},
        }
        self.provider_config = config["model_providers"]["routing_probe"]
        (codex_dir / "config.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "USERPROFILE",
                                          "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "SYSTEMDRIVE", "PROGRAMFILES",
                                          "HOMEDRIVE", "HOMEPATH"}}
        environment.update(CODEX_HOME=str(codex_dir), ROUTING_PROBE_TOKEN=token)
        self.process = subprocess.Popen(command + ["app-server", "--stdio"], cwd=workspace, env=environment,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8")
        self.messages = queue.Queue()
        self.events = []
        self.stderr = []
        self.on_event = on_event
        self.event_errors = []
        self.request_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_errors, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if self.on_event and "method" in value:
                try:
                    self.on_event(value)
                except Exception as error:
                    self.event_errors.append(type(error).__name__ + ": " + str(error))
            self.messages.put(value)

    def _read_errors(self):
        for line in self.process.stderr:
            self.stderr.append(line)

    def send(self, method, params=None, *, request_id=None):
        value = {"method": method, "params": params or {}}
        if request_id is not None:
            value["id"] = request_id
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def request(self, method, params=None):
        self.request_id += 1
        self.send(method, params, request_id=self.request_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = self.messages.get(timeout=max(.1, deadline - time.monotonic()))
            if value.get("id") == self.request_id:
                if "error" in value:
                    raise AssertionError({"method": method, "error": value["error"]})
                return value["result"]
            self.events.append(value)
        raise AssertionError("Timed out waiting for " + method)

    def close(self):
        if self.process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


def run_real_probe(folder):
    command = native_codex_command()
    if command is None:
        pytest.skip("Installed Codex CLI is required for the subagent routing probe")
    server = _ProbeServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    peer = _AppServerProbe(command, folder, f"http://127.0.0.1:{server.server_port}")
    try:
        peer.request("initialize", {"clientInfo": {"name": "routing-probe", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
        peer.send("initialized")
        roots, started_turns = [], []
        for index in range(2):
            project = folder / "workspace" / f"project-{index}"
            project.mkdir()
            root = peer.request("thread/start", {"cwd": str(project), "model": "gpt-6-astra",
                                                "modelProvider": "routing_probe", "approvalPolicy": "never",
                                                "sandbox": "danger-full-access"})["thread"]["id"]
            roots.append(root)
            started_turns.append(peer.request("turn/start", {
                "threadId": root, "input": [{"type": "text", "text": "Delegate one local probe to a worker."}],
                "effort": "ultra"})["turn"]["id"])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                peer.events.append(peer.messages.get(timeout=.2))
            except queue.Empty:
                pass
            complete = [event for event in peer.events if event.get("method") == "turn/completed"]
            if len(complete) >= 4:
                break
        threads = [event.get("params", {}).get("thread", {}) for event in peer.events
                   if event.get("method") == "thread/started"]
        turns = [{"thread_id": event.get("params", {}).get("threadId"),
                  "turn_id": event.get("params", {}).get("turn", {}).get("id"),
                  "status": event.get("params", {}).get("turn", {}).get("status")}
                 for event in peer.events if event.get("method") == "turn/completed"]
        bindings = []
        known_threads = {root: f"task-{index}" for index, root in enumerate(roots)}
        for event in peer.events:
            params = event.get("params", {})
            parent = params.get("threadId")
            task_id = known_threads.get(parent)
            child = verified_child_thread(parent, params.get("item"), known_threads, task_id)
            if child:
                known_threads[child] = task_id
                bindings.append({"parent_thread_id": parent, "thread_id": child, "task_id": task_id})
        return {"roots": roots, "started_turns": started_turns, "calls": server.calls,
                "threads": [{key: value.get(key) for key in ("id", "source", "agentRole", "parentThreadId")}
                            for value in threads], "turns": turns, "bindings": bindings}
    finally:
        peer.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_codex_identifies_spawned_thread(tmp_path):
    result = run_real_probe(tmp_path)
    assert len(set(result["roots"])) == 2, result
    assert len({turn["thread_id"] for turn in result["turns"]}) == 4, result
    assert len({turn["turn_id"] for turn in result["turns"]}) == 4, result
    assert all(turn["status"] == "completed" for turn in result["turns"]), result
    assert len({binding["thread_id"] for binding in result["bindings"]}) == 2, result
    calls_by_thread = {call["headers"]["thread-id"]: call for call in result["calls"]}
    assert set(calls_by_thread) == {turn["thread_id"] for turn in result["turns"]}, result
    for binding in result["bindings"]:
        parent = calls_by_thread[binding["parent_thread_id"]]
        child = calls_by_thread[binding["thread_id"]]
        assert child["path"] == parent["path"], result
        assert child["headers"]["session-id"] == parent["headers"]["session-id"], result
        assert child["metadata"]["thread_id"] == binding["thread_id"], result
        assert child["metadata"]["parent_thread_id"] == binding["parent_thread_id"], result
        assert child["metadata"]["turn_id"] != parent["metadata"]["turn_id"], result


def test_role_binding_requires_known_parent_and_real_spawn_event():
    known = {"main": "chat", "foreign": "other-chat"}
    item = {"type": "subAgentActivity", "kind": "started", "agentThreadId": "child"}
    assert verified_child_thread("main", item, known, "chat") == "child"
    assert verified_child_thread("unregistered", item, known, "chat") is None
    assert verified_child_thread("unregistered", item, known, None) is None
    assert verified_child_thread("main", item, known, "other-chat") is None
    assert verified_child_thread("main", {**item, "agentThreadId": "foreign"}, known, "chat") is None
    assert verified_child_thread("main", {**item, "agentThreadId": "main"}, known, "chat") is None
    assert verified_child_thread("main", {**item, "kind": "completed"}, known, "chat") is None
    assert verified_child_thread("main", {"type": "agentMessage", "agentThreadId": "child"}, known, "chat") is None
    assert verified_child_thread("main", {**item, "agentThreadId": []}, known, "chat") is None


def test_legacy_spawn_events_preserve_roles_and_reject_cross_chat_ids():
    known = {"main": "chat", "foreign": "other-chat"}
    item = {"type": "collabAgentToolCall", "tool": "spawnAgent", "status": "completed",
            "receiverThreadIds": ["child-a", "child-b", "child-a", "foreign", "main"]}
    assert verified_child_threads("main", item, known, "chat") == ("child-a", "child-b")
    assert not verified_child_threads("main", {**item, "status": "failed"}, known, "chat")
    assert not verified_child_threads("main", {**item, "tool": "wait"}, known, "chat")
    thread = {"id": "child", "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "main"}}}}
    assert verified_thread_start(thread, known) == ("chat", "child")
    assert verified_thread_start({**thread, "id": "foreign"}, known) is None
    assert verified_thread_start({"id": "child", "parentThreadId": "unknown"}, known) is None


@pytest.mark.parametrize("fixture", ["fake_codex_app_server.py", "fake_team_app_server.py"])
def test_offline_peers_keep_distinct_chats_and_new_turns(tmp_path, fixture):
    peer = _AppServerProbe([sys.executable, str(Path(__file__).parent / "fixtures" / fixture)], tmp_path,
                           "http://127.0.0.1:1")
    try:
        peer.request("initialize", {"clientInfo": {"name": "fixture-probe", "version": "1"}})
        started = []
        for index in range(2):
            workspace = tmp_path / "workspace" / f"project-{index}"
            workspace.mkdir()
            thread_id = peer.request("thread/start", {"cwd": str(workspace)})["thread"]["id"]
            turn_id = peer.request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": "hold"}],
                                                   "outputSchema": {"type": "object"}})["turn"]["id"]
            started.append((thread_id, turn_id, workspace))
        assert started[0][0] != started[1][0]
        assert started[0][1] != started[1][1]
        first_thread, first_turn, workspace = started[0]
        assert peer.request("thread/resume", {"threadId": first_thread, "cwd": str(workspace)})["thread"]["id"] == first_thread
        next_turn = peer.request("turn/start", {"threadId": first_thread, "input": [{"type": "text", "text": "hold"}],
                                                 "outputSchema": {"type": "object"}})["turn"]["id"]
        assert next_turn not in {first_turn, started[1][1]}
    finally:
        peer.close()


class _RoutedProbeHandler(BaseHTTPRequestHandler):
    """The upstream sees no private thread headers; only generated test inputs."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        inputs = body.get("input", [])
        child = any(item.get("type") == "agent_message" for item in inputs)
        has_spawn_result = any(item.get("type") == "function_call_output" for item in inputs)
        with self.server.box.lock:
            index = len(self.server.box.calls) + 1
            self.server.box.calls.append({"provider": self.server.provider_id})
        if child or has_spawn_result:
            if not self.server.role_probe_gate.wait(timeout=15):
                self.server.box.handler_errors.append("Routed Codex probe was not released within 15 seconds")
            item = {"id": f"message-{index}", "type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "CHILD_OK", "annotations": []}]}
        else:
            tool_definitions = body.get("tools", []) + [tool for entry in inputs for tool in entry.get("tools", [])]
            namespaced = any(tool.get("name") == "collaboration" for tool in tool_definitions)
            arguments = {"message": "Reply with CHILD_OK. Do not use tools."}
            arguments.update({"task_name": "probe_worker"} if namespaced else {"agent_type": "worker"})
            item = {"id": f"spawn-{index}", "type": "function_call", "call_id": f"spawn-{index}",
                    "name": "spawn_agent", "arguments": json.dumps(arguments), "status": "completed"}
            if namespaced:
                item["namespace"] = "collaboration"
        payload = _reply(index, item)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_real_codex_ultra_roles_cross_projects_through_rust(tmp_path):
    """Actual CLI subagents exercise role reservations through the Rust binary."""
    from scripts.test_rust_proxy import BINARY, Sandbox

    command = native_codex_command()
    if command is None:
        pytest.skip("Installed Codex CLI is required for the subagent routing probe")
    if not BINARY.is_file():
        pytest.skip("Run cargo build --locked before the Rust/CLI role integration probe")
    gate = threading.Event()
    with Sandbox(max_request_bytes=2_000_000, read_timeout_seconds=20, max_stream_seconds=25) as box:
        for upstream in box.servers:
            upstream.RequestHandlerClass = _RoutedProbeHandler
            upstream.role_probe_gate = gate
        known, specifications, bindings = {}, {}, {}

        def bind_child(event):
            if event.get("method") not in {"item/started", "item/completed"}:
                return
            params = event.get("params", {})
            parent = params.get("threadId")
            task_id = known.get(parent)
            child = verified_child_thread(parent, params.get("item"), known, task_id)
            if child and child not in bindings:
                spec = {**specifications[task_id], "thread_id": child, "role": "auxiliary"}
                result = box.post(spec, path="/admin/routes")
                assert result.status == 200, "Verified subagent route registration was rejected"
                known[child] = task_id
                bindings[child] = spec

        peer = _AppServerProbe(command, tmp_path, f"http://127.0.0.1:{box.port}", token=box.token, on_event=bind_child)
        try:
            peer.request("initialize", {"clientInfo": {"name": "routing-probe", "version": "1"},
                                        "capabilities": {"experimentalApi": True}})
            peer.send("initialized")
            roots = []
            for index in range(2):
                task_id = f"chat-{index}"
                route = f"run-{index}-main"
                project = tmp_path / "workspace" / f"project-{index}"
                project.mkdir()
                config = {"model_providers.routing_probe": {
                    **peer.provider_config, "base_url": f"http://127.0.0.1:{box.port}/r/{route}/v1"}}
                root = peer.request("thread/start", {"cwd": str(project), "model": "gpt-6-astra",
                                                    "modelProvider": "routing_probe", "approvalPolicy": "never",
                                                    "sandbox": "danger-full-access", "config": config})["thread"]["id"]
                roots.append(root)
                known[root] = task_id
                spec = {"id": route, "providers": box.ids[:2], "strategy": "balanced",
                        "project_id": f"project-{index}", "task_id": task_id, "run_id": f"run-{index}",
                        "thread_id": root, "role": "main"}
                specifications[task_id] = spec
                assert box.post(spec, path="/admin/routes").status == 200
                peer.request("turn/start", {"threadId": root, "effort": "ultra",
                                            "input": [{"type": "text", "text": "Delegate one local probe to a worker."}]})
                deadline = time.monotonic() + 8
                while len(bindings) <= index and not peer.event_errors and time.monotonic() < deadline:
                    time.sleep(.02)
                assert not peer.event_errors, peer.event_errors
                assert len(bindings) == index + 1, "Actual CLI did not report the spawned thread"

            state = box.request("GET", "/status").json()
            assert [(provider["main_count"], provider["auxiliary_count"]) for provider in state["providers"]] == [
                (1, 0), (1, 2), (0, 0)]
            gate.set()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    peer.events.append(peer.messages.get(timeout=.1))
                except queue.Empty:
                    pass
                finished = [event for event in peer.events if event.get("method") == "turn/completed"]
                if len(finished) >= 4:
                    break
            assert len(finished) == 4
            assert all(event["params"]["turn"]["status"] == "completed" for event in finished)
            rows = [dict(row) for row in box.rows()]
            assert {row["thread_id"] for row in rows} == set(roots) | set(bindings)
            assert {row["run_id"] for row in rows} == {"run-0", "run-1"}
            for row in rows:
                expected_role = "main" if row["thread_id"] in roots else "auxiliary"
                assert row["role"] == expected_role
                expected_provider = box.ids[0] if row["thread_id"] == roots[0] else box.ids[1]
                assert row["provider"] == expected_provider
            for run_id in ("run-0", "run-1"):
                assert box.post({"run_id": run_id}, path="/admin/routes/release").json()["released"] == 2
            state = box.request("GET", "/status").json()
            assert all(provider["main_count"] == provider["auxiliary_count"] == provider["in_flight"] == 0
                       for provider in state["providers"])
        finally:
            gate.set()
            peer.close()


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory(prefix="three-api-routing-probe-") as directory:
        print(json.dumps(run_real_probe(Path(directory)), indent=2))
