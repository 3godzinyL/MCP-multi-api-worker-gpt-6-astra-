#!/usr/bin/env python3
"""Black-box tests of the built Rust proxy, using only Python's standard library.

Build first: cargo build --locked
Run: python scripts/test_rust_proxy.py
Release: python scripts/test_rust_proxy.py --binary target/release/3api.exe

Every test uses temporary configuration/data, random fake credentials and three
local mock upstreams. No actual provider, user configuration or credential vault
is changed. A missing binary is an error, never a skipped test.
"""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit
import uuid


ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / ("3api.exe" if os.name == "nt" else "3api")
USAGE = {
    "input_tokens": 11,
    "output_tokens": 7,
    "total_tokens": 18,
    "input_tokens_details": {"cached_tokens": 3},
    "output_tokens_details": {"reasoning_tokens": 4},
}


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def event(kind: str, **values: object) -> bytes:
    return b"event: " + kind.encode() + b"\ndata: " + encoded({"type": kind, **values}) + b"\n\n"


def completed() -> bytes:
    return event("response.completed", response={"id": "mock-stream", "status": "completed", "usage": USAGE})


def available_proxy_port() -> int:
    # Port 0 selects the OS's ephemeral client range. Between closing that
    # reservation and spawning Rust, another concurrent HTTP client can take
    # the same port (especially during Windows executable scanning). Probe a
    # random unprivileged port below the usual Linux/Windows ephemeral ranges.
    for _ in range(100):
        candidate = 16000 + uuid.uuid4().int % 16000
        with socket.socket() as reservation:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                reservation.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate
    raise AssertionError("Could not reserve a local test proxy port after 100 probes")


@dataclass
class Reply:
    status: int = 200
    body: bytes | None = None
    chunks: tuple[bytes, ...] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    drop: bool = False
    release: threading.Event | None = None
    pause_before: int = 1


@dataclass
class Result:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> object:
        return json.loads(self.body)


class MockServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request: socket.socket, client_address: object) -> None:
        # Record unexpected handler failures rather than hiding them in stderr.
        import traceback
        self.box.handler_errors.append(traceback.format_exc())


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        server = self.server
        assert isinstance(server, MockServer)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with server.box.lock:
            server.box.calls.append({
                "provider": server.provider_id,
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            })
            reply = server.replies.popleft() if server.replies else Reply()
        content = reply.body if reply.body is not None else encoded({
            "id": "mock-" + server.provider_id,
            "status": "completed",
            "output": [],
            "usage": USAGE,
        })
        try:
            self.send_response(reply.status)
            self.send_header("Content-Type", "text/event-stream" if reply.chunks is not None else "application/json")
            self.send_header("Connection", "close")
            if reply.chunks is None:
                self.send_header("Content-Length", str(len(content)))
            else:
                self.send_header("Transfer-Encoding", "chunked")
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.end_headers()
            if reply.chunks is None:
                self.wfile.write(content)
            else:
                for index, chunk in enumerate(reply.chunks):
                    if reply.release is not None and index == reply.pause_before:
                        if not reply.release.wait(8):
                            server.box.handler_errors.append("Test did not release the gated upstream within 8 seconds")
                            return
                    if chunk:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                if not reply.drop:
                    self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Expected when the proxy stops reading a failed or terminal stream.
            pass
        finally:
            self.close_connection = True


class Sandbox:
    """Owns only its test process, temporary files and mock loopback servers."""

    def __init__(self, *, provider_options: dict[str, object] | None = None, **options: object):
        self.options = options
        self.provider_options = provider_options or {}
        self.temporary = tempfile.TemporaryDirectory(prefix="3api-rust-e2e-")
        self.root = Path(self.temporary.name)
        self.config = self.root / "providers.toml"
        self.data = self.root / "data"
        self.log_path = self.root / "proxy.log"
        unique = uuid.uuid4().hex[:12]
        self.token = f"fake-local-token-{unique}"
        self.prompt = f"fake-private-prompt-{unique}"
        self.ids = [f"e2e_{unique}_{index}" for index in range(1, 4)]
        self.keys = [f"fake-provider-key-{unique}-{index}" for index in range(1, 4)]
        self.token_env = "THREE_API_E2E_TOKEN_" + unique.upper()
        self.key_envs = [f"THREE_API_E2E_KEY_{unique.upper()}_{index}" for index in range(1, 4)]
        self.deployments = ["mock-deployment", "mock-deployment", "different-deployment"]
        self.servers: list[MockServer] = []
        self.threads: list[threading.Thread] = []
        self.calls: list[dict] = []
        self.handler_errors: list[str] = []
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.log_file = None
        self.env = os.environ.copy()
        for variable in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
            self.env.pop(variable, None)
        self.env[self.token_env] = self.token
        for name, key in zip(self.key_envs, self.keys):
            self.env[name] = key
        self.port = 0

    def __enter__(self) -> Sandbox:
        try:
            for provider_id in self.ids:
                server = MockServer(("127.0.0.1", 0), MockHandler)
                server.box = self
                server.provider_id = provider_id
                server.replies = deque()
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
                thread.start()
                self.servers.append(server)
                self.threads.append(thread)
            self.write_config()
            self.port = available_proxy_port()
            self.log_file = self.log_path.open("wb")
            self.process = subprocess.Popen(
                [str(BINARY), "proxy", "--config", str(self.config), "--data-dir", str(self.data), "--port", str(self.port)],
                cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL, stdout=self.log_file, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AssertionError(f"Proxy exited before readiness ({self.process.returncode}): {self.logs()}")
                try:
                    if self.request("GET", "/health", authenticated=False, timeout=0.2).status == 200:
                        return self
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(0.02)
            raise AssertionError("Rust proxy did not become ready within 12 seconds: " + self.logs())
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if exc_type is None and self.handler_errors:
                raise AssertionError("Unexpected upstream handler failure:\n" + "\n".join(self.handler_errors))
            if exc_type is None and self.process is not None and self.process.poll() is not None:
                raise AssertionError("Proxy stopped unexpectedly during a test: " + self.logs())
        finally:
            self.close()

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)
        if self.log_file is not None:
            self.log_file.close()
        self.temporary.cleanup()

    def write_config(self) -> None:
        settings = {
            "public_model": "public-e2e",
            "proxy_token_env": self.token_env,
            "allow_loopback_upstreams": True,
            "cooldown_wait_seconds": 0,
            "reconnect_failover": True,
            "rotate_on_failure": True,
            "reconnect_cooldown_seconds": 30,
            "connect_timeout_seconds": 2,
            "read_timeout_seconds": 4,
            "write_timeout_seconds": 4,
            "pool_timeout_seconds": 2,
            "max_request_bytes": 4096,
            "max_response_bytes": 16384,
            "max_stream_bytes": 32768,
            "max_stream_seconds": 10,
            **self.options,
        }
        lines = ["[proxy]", *(f"{name} = {json.dumps(value)}" for name, value in settings.items())]
        for index, server in enumerate(self.servers):
            fields = {
                "id": self.ids[index], "enabled": True,
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "deployment": self.deployments[index], "api_key_env": self.key_envs[index],
                "auth_type": "bearer" if index == 0 else "api-key",
                "api_version": "2099-01-01" if index == 0 else "",
                "cooldown_seconds": 30,
                **self.provider_options,
            }
            lines.extend(["", "[[providers]]", *(f"{name} = {json.dumps(value)}" for name, value in fields.items())])
        self.config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def enqueue(self, index: int, *replies: Reply) -> None:
        with self.lock:
            self.servers[index].replies.extend(replies)

    def open_request(self, method: str, path: str, *, value: object | None = None,
                     body: bytes | list[bytes] | None = None, headers: dict[str, str] | None = None,
                     authenticated: bool = True, chunked: bool = False, timeout: float = 5) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        supplied = {"Content-Type": "application/json"}
        if authenticated:
            supplied["Authorization"] = "Bearer " + self.token
        supplied.update(headers or {})
        if value is not None:
            body = encoded(value)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=supplied, encode_chunked=chunked)
            return connection, connection.getresponse()
        except BaseException:
            connection.close()
            raise

    def request(self, method: str, path: str, **kwargs: object) -> Result:
        connection, response = self.open_request(method, path, **kwargs)
        try:
            return Result(response.status, dict(response.getheaders()), response.read())
        finally:
            connection.close()

    def post(self, value: object | None = None, path: str = "/v1/responses", **kwargs: object) -> Result:
        return self.request("POST", path, value={} if value is None else value, **kwargs)

    def logs(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""

    def restart(self) -> dict:
        """Restart this isolated process without discarding its private accounting."""
        self.process.terminate()
        self.process.wait(timeout=5)
        self.port = available_proxy_port()
        self.process = subprocess.Popen(
            [str(BINARY), "proxy", "--config", str(self.config), "--data-dir", str(self.data), "--port", str(self.port)],
            cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL, stdout=self.log_file, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + 12
        while True:
            try:
                return self.request("GET", "/status", timeout=0.2).json()
            except (OSError, http.client.HTTPException):
                if time.monotonic() >= deadline or self.process.poll() is not None:
                    raise AssertionError("Restarted test proxy did not become ready") from None
                time.sleep(0.04)

    def trace(self) -> list[str]:
        with self.lock:
            return [call["provider"] for call in self.calls]

    def rows(self) -> list[sqlite3.Row]:
        connection = sqlite3.connect(f"{self.data.joinpath('telemetry.sqlite3').as_uri()}?mode=ro", uri=True, timeout=3)
        try:
            connection.row_factory = sqlite3.Row
            return connection.execute("SELECT * FROM api_attempts ORDER BY started, id").fetchall()
        finally:
            connection.close()


class RustProxyTests(unittest.TestCase):
    maxDiff = 3000

    def assert_clean(self, box: Sandbox, *values: object) -> None:
        text = "\n".join(str(value) for value in values)
        for secret in [box.token, box.prompt, *box.keys]:
            self.assertNotIn(secret, text)
        for server in box.servers:
            self.assertNotIn(f"http://127.0.0.1:{server.server_port}", text)

    def test_health_and_authentication_on_all_sensitive_routes(self) -> None:
        with Sandbox() as box:
            self.assertEqual(box.request("GET", "/health", authenticated=False).status, 200)
            for method, path in [("GET", "/status"), ("GET", "/v1/models"), ("POST", "/v1/responses"),
                                 ("POST", "/v1/responses/compact"), ("POST", "/admin/routes"), ("POST", "/admin/reload")]:
                for headers in [{}, {"Authorization": "Bearer wrong-token"}]:
                    with self.subTest(method=method, path=path, supplied=bool(headers)):
                        result = box.request(method, path, body=b"{}", authenticated=False, headers=headers)
                        self.assertEqual(result.status, 401)
                        self.assert_clean(box, result.body)
            self.assertEqual(box.trace(), [])

    def test_duplicate_authorization_is_rejected(self) -> None:
        with Sandbox() as box:
            connection = http.client.HTTPConnection("127.0.0.1", box.port, timeout=3)
            try:
                connection.putrequest("GET", "/status")
                connection.putheader("Authorization", "Bearer " + box.token)
                connection.putheader("Authorization", "Bearer " + box.token)
                connection.endheaders()
                self.assertEqual(connection.getresponse().status, 401)
            finally:
                connection.close()
            self.assertEqual(box.trace(), [])

    def test_host_origin_fetch_site_and_security_headers(self) -> None:
        with Sandbox() as box:
            for headers in [{"Host": "attacker.example"}, {"Host": "127.0.0.1:1"},
                            {"Origin": "https://attacker.example"}, {"Origin": "null"},
                            {"Origin": "http://127.0.0.1:1"}, {"Sec-Fetch-Site": "cross-site"}]:
                with self.subTest(headers=headers):
                    result = box.request("GET", "/status", headers=headers)
                    self.assertEqual(result.status, 403)
                    self.assertEqual(result.headers.get("x-frame-options"), "DENY")
                    self.assertEqual(result.headers.get("x-content-type-options"), "nosniff")
                    self.assertEqual(result.headers.get("cache-control"), "no-store")
            result = box.request("GET", "/status", headers={"Host": f"localhost:{box.port}", "Origin": f"http://localhost:{box.port}"})
            self.assertEqual(result.status, 200)
            self.assertEqual(box.trace(), [])

    def test_public_model_and_upstream_credential_isolation(self) -> None:
        with Sandbox() as box:
            catalog = box.request("GET", "/v1/models")
            self.assertEqual(catalog.status, 200)
            self.assertEqual(catalog.json()["data"][0]["id"], "public-e2e")
            self.assertEqual(catalog.json()["models"], [])
            payload = {"model": "public-e2e", "input": [{"type": "function_call_output", "call_id": "call1", "output": box.prompt}],
                       "tools": [{"type": "function", "name": "local_tool", "parameters": {"type": "object"}}],
                       "reasoning": {"effort": "high"}, "include": ["reasoning.encrypted_content"], "store": False}
            result = box.post(payload, headers={"api-key": "fake-client-key", "Cookie": "local-session=private", "OpenAI-Beta": "responses=v1", "X-Untrusted": "private"})
            self.assertEqual(result.status, 200)
            self.assertEqual(box.trace(), [box.ids[0]])
            call = box.calls[0]
            self.assertEqual(json.loads(call["body"]), {**payload, "model": box.deployments[0]})
            self.assertEqual(call["headers"]["authorization"], "Bearer " + box.keys[0])
            self.assertEqual(call["headers"]["openai-beta"], "responses=v1")
            for header in ["api-key", "cookie", "x-untrusted"]:
                self.assertNotIn(header, call["headers"])
            self.assertNotIn(box.token, str(call))
            self.assertEqual(urlsplit(call["path"]).path, "/v1/responses")
            self.assertEqual(parse_qs(urlsplit(call["path"]).query), {"api-version": ["2099-01-01"]})
            self.assert_clean(box, box.request("GET", "/status").body, box.logs())

    def test_failover_keeps_each_providers_model_and_key_separate(self) -> None:
        with Sandbox() as box:
            box.enqueue(0, Reply(status=429))
            box.enqueue(1, Reply(status=503))
            self.assertEqual(box.post().status, 200)
            self.assertEqual(box.trace(), box.ids)
            for index, call in enumerate(box.calls):
                self.assertEqual(json.loads(call["body"])["model"], box.deployments[index])
                expected_header = "authorization" if index == 0 else "api-key"
                forbidden_header = "api-key" if index == 0 else "authorization"
                self.assertEqual(call["headers"][expected_header], ("Bearer " if index == 0 else "") + box.keys[index])
                self.assertNotIn(forbidden_header, call["headers"])
                self.assertNotIn(box.token, str(call))

    def test_429_failover_stays_sticky_after_zero_cooldown(self) -> None:
        with Sandbox() as box:
            box.enqueue(0, Reply(status=429, headers={"Retry-After": "0"}))
            self.assertEqual(box.post().status, 200)
            self.assertEqual(box.post().status, 200)
            self.assertEqual(box.trace(), [box.ids[0], box.ids[1], box.ids[1]])
            self.assertEqual(box.request("GET", "/status").json()["preferred_provider"], box.ids[1])

    def test_503_failover_and_sticky_followup(self) -> None:
        with Sandbox() as box:
            box.enqueue(0, Reply(status=503))
            self.assertEqual(box.post().status, 200)
            self.assertEqual(box.post().status, 200)
            self.assertEqual(box.trace(), [box.ids[0], box.ids[1], box.ids[1]])

    def test_400_is_not_retried_and_error_details_are_sanitized(self) -> None:
        with Sandbox() as box:
            secret_error = encoded({"error": {"message": " ".join([box.token, box.prompt, *box.keys])}})
            box.enqueue(0, Reply(status=400, body=secret_error, headers={"X-Provider-Secret": box.keys[0]}))
            result = box.post({"input": box.prompt})
            self.assertEqual(result.status, 400)
            self.assertEqual(result.json()["error"]["code"], "upstream_rejected_request")
            self.assertEqual(box.trace(), [box.ids[0]])
            self.assertNotIn("x-provider-secret", result.headers)
            self.assert_clean(box, result.body, result.headers, box.logs())

    def test_redirect_cannot_forward_credentials_to_another_destination(self) -> None:
        with Sandbox() as box:
            location = f"http://127.0.0.1:{box.servers[1].server_port}/v1/responses?secret={box.keys[0]}"
            box.enqueue(0, Reply(status=302, headers={"Location": location}))
            result = box.post()
            self.assertEqual(result.status, 502)
            self.assertNotIn("location", result.headers)
            self.assertEqual(box.trace(), [box.ids[0]])
            self.assert_clean(box, result.body, result.headers, box.logs())

    def test_all_rate_limited_backends_are_not_immediately_retried(self) -> None:
        with Sandbox() as box:
            for index in range(3):
                box.enqueue(index, Reply(status=429, headers={"Retry-After": "30"}))
            self.assertEqual(box.post().status, 429)
            again = box.post()
            self.assertEqual(again.status, 429)
            self.assertTrue(1 <= int(again.headers["retry-after"]) <= 30)
            self.assertEqual(box.trace(), box.ids)

    def test_compact_uses_compact_endpoint_and_preserves_payload(self) -> None:
        with Sandbox() as box:
            payload = {"model": "public-e2e", "input": [{"type": "message", "role": "user", "content": "compact me"}], "instructions": "keep facts"}
            self.assertEqual(box.post(payload, path="/v1/responses/compact").status, 200)
            call = box.calls[0]
            self.assertEqual(urlsplit(call["path"]).path, "/v1/responses/compact")
            self.assertEqual(json.loads(call["body"]), {**payload, "model": box.deployments[0]})
            self.assertEqual(box.rows()[0]["kind"], "compact")

    def test_admin_route_uses_only_selected_providers_and_caps_tokens(self) -> None:
        with Sandbox() as box:
            result = box.post({"id": "task_one", "providers": [box.ids[1]], "strategy": "priority", "max_output_tokens": 100}, path="/admin/routes")
            self.assertEqual(result.status, 200)
            self.assertEqual(box.post({"max_output_tokens": 900}, path="/r/task_one/v1/responses").status, 200)
            self.assertEqual(box.trace(), [box.ids[1]])
            self.assertEqual(json.loads(box.calls[0]["body"])["max_output_tokens"], 100)
            self.assertEqual(box.rows()[0]["route_id"], "task_one")
            self.assertEqual(box.post(path="/r/unknown/v1/responses").status, 409)
            self.assertEqual(box.trace(), [box.ids[1]])

    def test_admin_rejects_mismatched_deployments_and_invalid_routes(self) -> None:
        with Sandbox() as box:
            for payload in [{"id": "mixed", "providers": [box.ids[0], box.ids[2]]},
                            {"id": "duplicate", "providers": [box.ids[0], box.ids[0]]},
                            {"id": "missing", "providers": ["missing-provider"]},
                            {"id": "../escape", "providers": [box.ids[0]]},
                            {"id": "empty", "providers": []}]:
                with self.subTest(route=payload["id"]):
                    self.assertEqual(box.post(payload, path="/admin/routes").status, 400)
            self.assertEqual(box.request("GET", "/status").json()["routes"], [])
            self.assertEqual(box.trace(), [])

    def test_verified_threads_share_route_but_keep_global_roles_and_run_telemetry(self) -> None:
        with Sandbox() as box:
            def register(project, role, thread):
                return box.post({"id": f"route-{project}", "providers": box.ids[:2],
                                 "project_id": project, "task_id": f"chat-{project}",
                                 "run_id": f"run-{project}", "thread_id": thread, "role": role},
                                path="/admin/routes")
            self.assertEqual(register("one", "main", "").status, 200)
            self.assertEqual(register("one", "main", "main-one").status, 200)
            self.assertEqual(register("one", "auxiliary", "child-one").status, 200)
            self.assertEqual(register("two", "main", "main-two").status, 200)
            self.assertEqual(register("two", "auxiliary", "child-two").status, 200)
            state = box.request("GET", "/status").json()
            self.assertEqual([(p["main_count"], p["auxiliary_count"]) for p in state["providers"]],
                             [(1, 0), (1, 2), (0, 0)])
            self.assertEqual(len(state["routes"]), 4)
            for project, thread in [("one", "main-one"), ("one", "child-one"),
                                    ("two", "main-two"), ("two", "child-two"), ("one", "main-one")]:
                self.assertEqual(box.post(path=f"/r/route-{project}/v1/responses",
                                          headers={"thread-id": thread, "session-id": "shared-session"}).status, 200)
            self.assertEqual(box.trace(), [box.ids[i] for i in (0, 1, 1, 1, 0)])
            rows = box.rows()
            self.assertEqual({row["run_id"] for row in rows}, {"run-one", "run-two"})
            self.assertEqual({row["thread_id"] for row in rows}, {"main-one", "main-two", "child-one", "child-two"})
            self.assertEqual({row["role"] for row in rows}, {"main", "auxiliary"})
            self.assertEqual(box.post({"run_id": "run-one"}, path="/admin/routes/release").json()["released"], 2)
            state = box.request("GET", "/status").json()
            self.assertEqual([(p["main_count"], p["auxiliary_count"], p["in_flight"]) for p in state["providers"]],
                             [(0, 0, 0), (1, 1, 0), (0, 0, 0)])
            self.assertEqual(box.post({"run_id": "run-one"}, path="/admin/routes/release").json()["released"], 0)
            self.assertEqual(register("one", "auxiliary", "late-child").status, 409)
            self.assertEqual(box.post(path="/r/route-one/v1/responses", headers={"thread-id": "main-one"}).status, 409)

    def test_concurrent_project_main_reservations_are_atomic(self) -> None:
        with Sandbox() as box, ThreadPoolExecutor(max_workers=2) as pool:
            ready = threading.Barrier(2)
            def register(index):
                ready.wait(timeout=2)
                return box.post({"id": f"route-{index}", "providers": box.ids[:2], "role": "main",
                                 "project_id": f"project-{index}", "task_id": f"chat-{index}",
                                 "run_id": f"run-{index}", "thread_id": f"thread-{index}"}, path="/admin/routes")
            results = list(pool.map(register, (1, 2)))
            self.assertEqual([result.status for result in results], [200, 200])
            self.assertEqual({result.json()["assigned_provider"] for result in results}, set(box.ids[:2]))
            state = box.request("GET", "/status").json()
            self.assertEqual([provider["main_count"] for provider in state["providers"]], [1, 1, 0])

    def test_child_request_waits_for_verified_registration_and_never_inherits_main(self) -> None:
        with Sandbox() as box, ThreadPoolExecutor(max_workers=1) as pool:
            route = {"id": "route-parent", "providers": box.ids[:2], "role": "main",
                     "project_id": "project", "task_id": "chat", "run_id": "run", "thread_id": "parent-thread"}
            self.assertEqual(box.post(route, path="/admin/routes").status, 200)
            child = pool.submit(box.post, path="/r/route-parent/v1/responses",
                                headers={"thread-id": "child-thread", "session-id": "parent-thread",
                                         "x-codex-parent-thread-id": "parent-thread"})
            time.sleep(0.1)
            self.assertEqual(box.trace(), [])
            self.assertEqual(box.post({**route, "role": "auxiliary", "thread_id": "child-thread"}, path="/admin/routes").status, 200)
            self.assertEqual(child.result(timeout=4).status, 200)
            self.assertEqual(box.trace(), [box.ids[1]])
            self.assertEqual(box.post(path="/r/route-parent/v1/responses", headers={"thread-id": "forged-thread",
                                     "x-openai-subagent": "collab_spawn", "x-codex-parent-thread-id": "parent-thread"}).status, 409)
            self.assertEqual(box.post(path="/r/route-parent/v1/responses").status, 409)
            self.assertEqual(box.trace(), [box.ids[1]])

    def test_role_cooldown_wait_is_bounded_and_release_interrupts_it(self) -> None:
        with Sandbox() as box, ThreadPoolExecutor(max_workers=1) as pool:
            route = {"id": "waiting", "providers": [box.ids[0]], "role": "main", "wait_seconds": 2,
                     "project_id": "project", "task_id": "chat", "run_id": "run", "thread_id": "thread"}
            self.assertEqual(box.post(route, path="/admin/routes").status, 200)
            box.enqueue(0, Reply(status=429, headers={"Retry-After": "0.1"}))
            self.assertEqual(box.post(path="/r/waiting/v1/responses", headers={"thread-id": "thread"}).status, 200)
            self.assertEqual(box.trace(), [box.ids[0], box.ids[0]])
            box.enqueue(0, Reply(status=429, headers={"Retry-After": "30"}))
            waiting = pool.submit(box.post, path="/r/waiting/v1/responses", headers={"thread-id": "thread"})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if box.request("GET", "/status").json()["stats"]["waiting_requests"]:
                    break
                time.sleep(0.01)
            self.assertEqual(box.post({"run_id": "run"}, path="/admin/routes/release").status, 200)
            self.assertNotEqual(waiting.result(timeout=1.5).status, 200)
            self.assertEqual(box.request("GET", "/status").json()["providers"][0]["main_count"], 0)
            self.assertEqual(box.trace(), [box.ids[0]] * 3)

    def test_retry_after_applies_to_503_and_first_byte_timeout_fails_over(self) -> None:
        with Sandbox() as box:
            box.enqueue(0, Reply(status=503, headers={"Retry-After": "30"}))
            self.assertEqual(box.post().status, 200)
            self.assertGreater(box.request("GET", "/status").json()["providers"][0]["cooldown_remaining_seconds"], 20)
        with Sandbox() as box:
            for index in range(3):
                box.enqueue(index, Reply(status=503, headers={"Retry-After": "30"}))
            self.assertEqual(box.post().status, 503)
            self.assertEqual(box.post().status, 503)
            self.assertEqual(box.trace(), box.ids)
        with Sandbox(read_timeout_seconds=0.1) as box:
            release = threading.Event()
            box.enqueue(0, Reply(chunks=(completed(),), release=release, pause_before=0))
            try:
                self.assertEqual(box.post({"stream": True}).status, 200)
                self.assertEqual(box.trace(), box.ids[:2])
                self.assertGreater(box.request("GET", "/status").json()["providers"][0]["transport_errors"], 0)
            finally:
                release.set()

    def test_soft_token_budget_moves_sticky_role_and_keeps_selected_model_cohort(self) -> None:
        with Sandbox() as box:
            route = {"id": "budget-route", "providers": box.ids[:2], "role": "main",
                     "project_id": "project", "task_id": "chat", "run_id": "budget-run", "thread_id": "thread"}
            self.assertEqual(box.post(route, path="/admin/routes").status, 200)
            box.enqueue(0, Reply(body=encoded({"status": "completed", "usage": {
                "input_tokens": 899_000, "output_tokens": 1000, "total_tokens": 900_000}})))
            request = lambda: box.post(path="/r/budget-route/v1/responses", headers={"thread-id": "thread"})
            self.assertEqual(request().status, 200)
            state = box.request("GET", "/status").json()
            self.assertEqual(state["providers"][0]["token_budget"]["used_tokens"], 900_000)
            self.assertTrue(state["providers"][0]["token_budget"]["soft_reached"])
            box.enqueue(1, Reply(body=encoded({"status": "completed", "usage": {
                "input_tokens": 950_000, "output_tokens": 0, "total_tokens": 950_000}})))
            self.assertEqual(request().status, 200)
            # With the alternative hard-blocked, the soft provider still accepts a safe request.
            self.assertEqual(request().status, 200)
            self.assertEqual(box.trace(), [box.ids[0], box.ids[1], box.ids[0]])
            state = box.request("GET", "/status").json()
            self.assertTrue(state["providers"][1]["token_budget"]["hard_reached"])
            self.assertFalse(state["providers"][1]["available"])
            self.assertEqual(state["routes"][0]["assigned_provider"], box.ids[0])
            self.assert_clean(box, encoded(state), box.logs())

    def test_token_reservation_blocks_next_request_without_interrupting_stream(self) -> None:
        limits = {"tokens_per_minute": 10_000, "soft_tokens_per_minute": 9000, "hard_tokens_per_minute": 9500}
        release = threading.Event()
        with Sandbox(provider_options=limits) as box:
            self.assertEqual(box.post({"id": "limited", "providers": [box.ids[0]]}, path="/admin/routes").status, 200)
            first = event("response.created", response={"id": "mock-active"})
            box.enqueue(0, Reply(chunks=(first, completed()), release=release))
            connection, response = box.open_request("POST", "/r/limited/v1/responses",
                                                     value={"stream": True, "max_output_tokens": 6000})
            try:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(len(first)), first)
                state = box.request("GET", "/status").json()
                self.assertGreaterEqual(state["providers"][0]["token_budget"]["reserved_tokens"], 6000)
                denied = box.post({"max_output_tokens": 6000}, path="/r/limited/v1/responses")
                self.assertEqual(denied.status, 429)
                self.assertEqual(denied.json()["error"]["code"], "token_budget_exhausted")
                self.assertGreaterEqual(int(denied.headers["retry-after"]), 1)
                self.assertEqual(box.trace(), [box.ids[0]])
                release.set()
                self.assertIn(b"response.completed", response.read())
            finally:
                release.set()
                connection.close()
            self.assertEqual(box.post({"max_output_tokens": 6000}, path="/r/limited/v1/responses").status, 200)
            state = box.request("GET", "/status").json()
            budget = state["providers"][0]["token_budget"]
            self.assertEqual((budget["reserved_tokens"], budget["estimated_tokens"], budget["used_tokens"]), (0, 0, 36))

    def test_token_budget_wait_is_bounded_and_run_release_wakes_request(self) -> None:
        with Sandbox() as box, ThreadPoolExecutor(max_workers=1) as pool:
            route = {"id": "budget-wait", "providers": [box.ids[0]], "role": "main", "wait_seconds": 2,
                     "project_id": "project", "task_id": "chat", "run_id": "run", "thread_id": "thread"}
            self.assertEqual(box.post(route, path="/admin/routes").status, 200)
            box.enqueue(0, Reply(body=encoded({"status": "completed", "usage": {"input_tokens": 950_000, "output_tokens": 0}})))
            self.assertEqual(box.post(path="/r/budget-wait/v1/responses", headers={"thread-id": "thread"}).status, 200)
            waiting = pool.submit(box.post, path="/r/budget-wait/v1/responses", headers={"thread-id": "thread"})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if box.request("GET", "/status").json()["stats"]["waiting_requests"]:
                    break
                time.sleep(0.01)
            self.assertEqual(box.post({"run_id": "run"}, path="/admin/routes/release").status, 200)
            self.assertNotEqual(waiting.result(timeout=1.5).status, 200)
            self.assertEqual(box.trace(), [box.ids[0]])
            state = box.request("GET", "/status").json()
            self.assertEqual(state["stats"]["waiting_requests"], 0)
            self.assertGreater(state["stats"]["token_budget_waits"], 0)

    def test_token_budget_survives_restart_and_expires_without_duplicate_usage(self) -> None:
        with Sandbox() as box:
            box.enqueue(0, Reply(body=encoded({"status": "completed", "usage": {"input_tokens": 950_000, "output_tokens": 0}})))
            self.assertEqual(box.post().status, 200)
            state = box.restart()
            self.assertEqual(state["providers"][0]["token_budget"]["used_tokens"], 950_000)
            self.assertTrue(state["providers"][0]["token_budget"]["hard_reached"])
            self.assertEqual(box.post().status, 200)
            self.assertEqual(box.trace(), box.ids[:2])
            with closing(sqlite3.connect(box.data / "telemetry.sqlite3", timeout=3)) as db, db:
                db.execute("UPDATE api_token_budget SET time=time-61 WHERE state<>'reserved'")
            state = box.restart()
            self.assertEqual(state["providers"][0]["token_budget"]["total_tokens"], 0)
            self.assertEqual(box.post().status, 200)
            self.assertEqual(box.trace(), [box.ids[0], box.ids[1], box.ids[0]])
            self.assertEqual(len(box.rows()), 3)

    def test_error_release_and_proxy_restart_keep_history_without_reservations(self) -> None:
        with Sandbox() as box:
            route = {"id": "route", "providers": box.ids[:2], "role": "main",
                     "project_id": "project", "task_id": "chat", "run_id": "failed-run", "thread_id": "thread"}
            self.assertEqual(box.post(route, path="/admin/routes").status, 200)
            box.enqueue(0, Reply(status=400))
            self.assertEqual(box.post(path="/r/route/v1/responses", headers={"thread-id": "thread"}).status, 400)
            self.assertEqual(box.post({"run_id": "failed-run"}, path="/admin/routes/release").json()["released"], 1)
            self.assertEqual(box.post({**route, "id": "next", "run_id": "next-run"}, path="/admin/routes").status, 200)
            self.assertEqual(box.post(path="/r/next/v1/responses", headers={"thread-id": "thread"}).status, 200)
            box.process.terminate()
            box.process.wait(timeout=5)
            box.port = available_proxy_port()
            box.process = subprocess.Popen(
                [str(BINARY), "proxy", "--config", str(box.config), "--data-dir", str(box.data), "--port", str(box.port)],
                cwd=ROOT, env=box.env, stdin=subprocess.DEVNULL, stdout=box.log_file, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            deadline = time.monotonic() + 12
            while True:
                try:
                    state = box.request("GET", "/status", timeout=0.2).json()
                    break
                except (OSError, http.client.HTTPException):
                    if time.monotonic() >= deadline or box.process.poll() is not None:
                        self.fail("Restarted test proxy did not become ready")
                    time.sleep(0.04)
            self.assertEqual(state["routes"], [])
            self.assertTrue(all(p["main_count"] == p["auxiliary_count"] == p["in_flight"] == 0 for p in state["providers"]))
            self.assertEqual({row["run_id"] for row in box.rows()}, {"failed-run", "next-run"})

    def test_failed_reload_is_atomic_and_does_not_echo_configuration(self) -> None:
        with Sandbox() as box:
            box.config.write_text('private_key="' + box.keys[0] + '"\nmalformed = [', encoding="utf-8")
            rejected = box.post(path="/admin/reload")
            self.assertEqual(rejected.status, 400)
            self.assert_clean(box, rejected.body, box.logs())
            self.assertEqual(box.post().status, 200)
            box.write_config()
            self.assertEqual(box.post(path="/admin/reload").status, 200)
            self.assertEqual(box.post().status, 200)

    def test_invalid_json_types_and_encoding_never_reach_provider(self) -> None:
        with Sandbox() as box:
            for body in [b"{", b"[]", b'{"x":NaN}', b'{"stream":"true"}', b'{"max_output_tokens":0}', b'{"max_output_tokens":200001}']:
                with self.subTest(body=body):
                    self.assertEqual(box.request("POST", "/v1/responses", body=body).status, 400)
            self.assertEqual(box.post(headers={"Content-Encoding": "gzip"}).status, 415)
            self.assertEqual(box.trace(), [])

    def test_content_length_request_limit(self) -> None:
        with Sandbox(max_request_bytes=256) as box:
            result = box.post({"input": "x" * 257})
            self.assertEqual(result.status, 413)
            self.assertEqual(box.trace(), [])

    def test_chunked_request_is_accepted_without_content_length(self) -> None:
        with Sandbox() as box:
            result = box.request("POST", "/v1/responses", body=[b'{"input":', b'"chunked body"}'], chunked=True)
            self.assertEqual(result.status, 200)
            self.assertEqual(json.loads(box.calls[0]["body"])["input"], "chunked body")

    def test_chunked_request_limit_cannot_be_bypassed(self) -> None:
        with Sandbox(max_request_bytes=256) as box:
            result = box.request("POST", "/v1/responses", body=[b'{"input":"', b"x" * 128, b"y" * 128, b'"}'], chunked=True)
            self.assertEqual(result.status, 413)
            self.assertEqual(box.trace(), [])

    def test_oversized_upstream_json_is_bounded_and_sanitized(self) -> None:
        with Sandbox(max_response_bytes=256) as box:
            box.enqueue(0, Reply(body=encoded({"output": "x" * 257, "private": box.keys[0]})))
            result = box.post()
            self.assertEqual(result.status, 502)
            self.assertEqual(box.trace(), [box.ids[0]])
            self.assert_clean(box, result.body, box.logs())

    def test_invalid_json_and_encoded_upstream_responses_are_rejected(self) -> None:
        with Sandbox() as box:
            box.enqueue(0, Reply(body=("invalid " + box.keys[0]).encode()), Reply(body=b"compressed", headers={"Content-Encoding": "gzip"}))
            for _ in range(2):
                result = box.post()
                self.assertEqual(result.status, 502)
                self.assert_clean(box, result.body)
            self.assertEqual(box.trace(), [box.ids[0], box.ids[0]])

    def test_stream_bytes_are_preserved_including_unicode_and_comments(self) -> None:
        with Sandbox() as box:
            prefix = b": keepalive\n\n" + event("response.output_text.delta", delta="Zażółć — rate limit is just text")
            wire = prefix + completed()
            box.enqueue(0, Reply(chunks=(wire[:31], wire[31:83], wire[83:])))
            result = box.post({"stream": True})
            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, wire)
            self.assertEqual(box.trace(), [box.ids[0]])
            self.assertEqual(result.headers.get("x-accel-buffering"), "no")
            self.assertNotIn("server", result.headers)

    def test_stream_first_event_arrives_before_upstream_finishes(self) -> None:
        release = threading.Event()
        with Sandbox() as box:
            first = event("response.output_text.delta", delta="first")
            last = completed()
            box.enqueue(0, Reply(chunks=(first, last), release=release))
            connection, response = box.open_request("POST", "/v1/responses", value={"stream": True}, timeout=2)
            try:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(len(first)), first)
                self.assertFalse(release.is_set())
                release.set()
                self.assertEqual(response.read(), last)
            finally:
                release.set()
                connection.close()

    def test_crlf_split_between_network_chunks_preserves_every_byte(self) -> None:
        release = threading.Event()
        with Sandbox() as box:
            prelude = event("response.created", response={"id": "mock-crlf"})
            frame = event("response.output_text.delta", delta="crlf").replace(b"\n", b"\r\n")
            first, rest = prelude + frame[:-1], frame[-1:] + completed()
            box.enqueue(0, Reply(chunks=(first, rest), release=release))
            connection, response = box.open_request("POST", "/v1/responses", value={"stream": True}, timeout=2)
            try:
                # The first complete event proves that the first upstream chunk
                # has arrived. A gate may legitimately hold the trailing CR
                # until it can determine whether the next byte is an LF.
                prefix = response.read(len(prelude))
                self.assertEqual(prefix, prelude)
                release.set()
                self.assertEqual(prefix + response.read(), first + rest)
            finally:
                release.set()
                connection.close()

    def test_terminal_event_does_not_append_a_second_answer(self) -> None:
        with Sandbox() as box:
            terminal = completed()
            box.enqueue(0, Reply(chunks=(terminal + event("response.output_text.delta", delta="unexpected second answer"),)))
            result = box.post({"stream": True})
            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, terminal)

    def test_dropped_stream_never_splices_in_a_second_provider(self) -> None:
        with Sandbox() as box:
            first = event("response.output_text.delta", delta="partial first provider")
            box.enqueue(0, Reply(chunks=(first,), drop=True))
            connection, response = box.open_request("POST", "/v1/responses", value={"stream": True})
            try:
                self.assertEqual(response.status, 200)
                try:
                    content = response.read()
                except http.client.IncompleteRead as error:
                    content = error.partial
                self.assertEqual(content, first)
            finally:
                connection.close()
            self.assertEqual(box.trace(), [box.ids[0]])
            self.assertEqual(box.post().status, 200)
            self.assertEqual(box.trace(), [box.ids[0], box.ids[1]])
            status = box.request("GET", "/status").json()
            self.assertEqual(status["stats"]["stream_interruptions"], 1)
            self.assertEqual(status["stats"]["in_flight"], 0)

    def test_connection_dropped_before_first_byte_can_fail_over(self) -> None:
        with Sandbox() as box:
            box.enqueue(0, Reply(chunks=(), drop=True))
            box.enqueue(1, Reply(chunks=(completed(),)))
            result = box.post({"stream": True})
            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, completed())
            self.assertEqual(box.trace(), box.ids[:2])

    def test_client_disconnect_releases_capacity_without_retry_or_cooldown(self) -> None:
        release = threading.Event()
        with Sandbox() as box:
            first = event("response.output_text.delta", delta="client leaves")
            box.enqueue(0, Reply(chunks=(first, completed()), release=release))
            connection, response = box.open_request("POST", "/v1/responses", value={"stream": True})
            try:
                self.assertEqual(response.read(len(first)), first)
                response.close()
                connection.close()
                deadline = time.monotonic() + 3
                while True:
                    status = box.request("GET", "/status").json()
                    if status["stats"]["in_flight"] == 0 or time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
                self.assertEqual(status["stats"]["in_flight"], 0)
                self.assertEqual(status["stats"]["client_disconnects"], 1)
                self.assertEqual(status["stats"]["stream_interruptions"], 0)
                self.assertEqual(status["providers"][0]["cooldown_events"], 0)
                self.assertEqual(box.trace(), [box.ids[0]])
            finally:
                release.set()
                response.close()
                connection.close()

    def test_sse_rate_limit_before_content_can_fail_over(self) -> None:
        with Sandbox() as box:
            quota = event("error", error={"code": "rate_limit_exceeded", "message": box.keys[0], "retry_after": 30})
            box.enqueue(0, Reply(chunks=(quota,)))
            box.enqueue(1, Reply(chunks=(completed(),)))
            result = box.post({"stream": True})
            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, completed())
            self.assertEqual(box.trace(), box.ids[:2])
            self.assert_clean(box, result.body, box.logs())

    def test_split_sse_error_is_sanitized_without_retry(self) -> None:
        release = threading.Event()
        with Sandbox() as box:
            first = event("response.output_text.delta", delta="partial")
            error = event("error", error={"code": "invalid_api_key", "message": " ".join([box.token, box.prompt, *box.keys])})
            box.enqueue(0, Reply(chunks=(first, error[:35], error[35:]), release=release))
            connection, response = box.open_request("POST", "/v1/responses", value={"stream": True})
            try:
                self.assertEqual(response.read(len(first)), first)
                release.set()
                rest = response.read()
                self.assertIn(b"upstream_stream_interrupted", rest)
                self.assert_clean(box, rest, box.logs())
            finally:
                release.set()
                connection.close()
            self.assertEqual(box.trace(), [box.ids[0]])

    def test_json_and_stream_usage_are_persisted_without_prompt_or_keys(self) -> None:
        with Sandbox() as box:
            self.assertEqual(box.post({"input": box.prompt}).status, 200)
            box.enqueue(0, Reply(chunks=(completed(),)))
            self.assertEqual(box.post({"stream": True, "input": box.prompt}).body, completed())
            rows = box.rows()
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row["provider"], box.ids[0])
                self.assertEqual(row["outcome"], "completed")
                self.assertEqual(row["status"], 200)
                self.assertEqual(tuple(row[key] for key in ["input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"]), (11, 7, 18, 3, 4))
                self.assertIsNotNone(row["finished"])
            self.assert_clean(box, [dict(row) for row in rows], box.logs())

    def test_cli_check_rejects_private_targets_and_url_credentials(self) -> None:
        with Sandbox() as box:
            original = box.config.read_text(encoding="utf-8")
            endpoint = f"http://127.0.0.1:{box.servers[0].server_port}/v1"
            for bad in ["https://169.254.169.254/v1", "https://10.0.0.1/v1", "https://[::ffff:127.0.0.1]/v1",
                        "https://user:" + box.keys[0] + "@example.com/v1", "http://example.com/v1"]:
                with self.subTest(target=bad.split("@")[-1]):
                    box.config.write_text(original.replace(endpoint, bad), encoding="utf-8")
                    result = subprocess.run([str(BINARY), "check", "--config", str(box.config)], cwd=ROOT, env=box.env,
                                            stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    self.assertNotEqual(result.returncode, 0)
                    self.assert_clean(box, result.stdout, result.stderr)
            self.assertEqual(box.trace(), [])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=BINARY, help="Built Rust 3api executable")
    arguments, unittest_arguments = parser.parse_known_args()
    BINARY = arguments.binary.resolve()
    if not BINARY.is_file():
        parser.error(f"Rust binary not found: {BINARY}. Run cargo build --locked first.")
    # All scenarios use one immutable build. Running a copy also avoids locking
    # target/debug/3api.exe against another developer's linker on Windows.
    with tempfile.TemporaryDirectory(prefix="3api-rust-e2e-binary-") as binary_directory:
        BINARY = Path(shutil.copy2(BINARY, Path(binary_directory) / BINARY.name))
        run = unittest.main(argv=[__file__, *unittest_arguments], verbosity=2, exit=False)
    sys.exit(0 if run.result.wasSuccessful() else 1)
