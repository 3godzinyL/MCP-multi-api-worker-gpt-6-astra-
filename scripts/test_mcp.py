"""Offline MCP protocol/privacy smoke test; requires only Python's standard library.

Usage: python scripts/test_mcp.py [path/to/3api]
The executable, SQLite files, environment and loopback mock are isolated from any
running 3API instance. This script never writes to the OS credential store.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time


SECRET = "MCP_TEST_PRIVATE_TOKEN_DO_NOT_EXPORT_0123456789"
PRIVATE = "MCP_TEST_PRIVATE_CONTENT_DO_NOT_EXPORT"


class ProxyMock(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "normal"
    visits: list[str] = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        type(self).visits.append(self.path)
        if self.path != "/status" or self.headers.get("Authorization") != "Bearer " + SECRET:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/leak")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        if self.mode == "oversize":
            self.send_header("Content-Length", str(256 * 1024 + 1))
            self.end_headers()
            self.close_connection = True
            return
        if self.mode == "chunked_oversize":
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for _ in range(33):
                    self.wfile.write(b"2000\r\n" + b"x" * 8192 + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        payload = json.dumps({
            "status": "ready", "uptime_seconds": 4.5, "token": SECRET,
            "telemetry_enabled": True, "reconnect_failover": True,
            "routes": [{"prompt": PRIVATE}], "unexpected": PRIVATE,
            "stats": {"requests": 3, "in_flight": 0, "prompt": PRIVATE},
            "providers": [{
                "id": "provider1", "enabled": True, "configured": True,
                "available": True, "attempts": 2, "api_key": SECRET,
                "base_url": PRIVATE, "cooldown_reason": PRIVATE,
            }, {"id": PRIVATE, "api_key": SECRET}],
        }).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class Session:
    def __init__(self, binary: Path, config: Path, data: Path, port: int):
        env = dict(os.environ, THREE_API_MCP_TEST_TOKEN=SECRET)
        self.process = subprocess.Popen([
            str(binary), "mcp", "--config", str(config), "--data-dir", str(data),
            "--proxy-url", f"http://127.0.0.1:{port}",
        ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.output: queue.Queue = queue.Queue()
        self.errors: list[bytes] = []
        self.messages: list[dict] = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.reader.start()
        self.stderr_reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.output.put(json.loads(line))
                except Exception as error:
                    self.output.put(AssertionError(f"Non-JSON stdout: {type(error).__name__}"))
        finally:
            self.output.put(None)

    def _read_stderr(self):
        self.errors.append(self.process.stderr.read())

    def send(self, message):
        self.raw(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")

    def raw(self, data: bytes):
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def receive(self):
        value = self.output.get(timeout=8)
        assert value is not None, "MCP exited before replying"
        if isinstance(value, Exception):
            raise value
        assert value.get("jsonrpc") == "2.0", "Invalid JSON-RPC stdout"
        self.messages.append(value)
        return value

    def call(self, method, params=None, ident=1):
        message = {"jsonrpc": "2.0", "id": ident, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        result = self.receive()
        assert result["id"] == ident, "Notification unexpectedly produced a reply"
        return result

    def initialize(self, version="2025-11-25"):
        result = self.call("initialize", {
            "protocolVersion": version, "capabilities": {},
            "clientInfo": {"name": "3api-offline-test", "version": "1"},
        }, "init")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def tool(self, name, arguments=None):
        result = self.call("tools/call", {"name": name, "arguments": arguments or {}})["result"]
        assert result["isError"] is False, "Tool unexpectedly failed"
        data = json.loads(result["content"][0]["text"])
        if "structuredContent" in result:
            assert data == result["structuredContent"]
        return data

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise AssertionError("MCP did not stop after stdin EOF")
        finally:
            self.reader.join(timeout=2)
            self.stderr_reader.join(timeout=2)
            self.process.stdout.close()
            self.process.stderr.close()
        assert self.process.returncode == 0, "MCP process failed"
        exposed = json.dumps(self.messages).encode() + b"".join(self.errors)
        assert SECRET.encode() not in exposed, "Credential was exposed"
        assert PRIVATE.encode() not in exposed, "Private metadata was exposed"


def fixture(root: Path):
    config = root / "providers.toml"
    config.write_text(
        '[proxy]\npublic_model = "test-model"\nproxy_token_env = "THREE_API_MCP_TEST_TOKEN"\n'
        '[[providers]]\nid = "provider1"\nenabled = false\n', encoding="utf-8")
    data = root / "data"
    data.mkdir()
    with sqlite3.connect(data / "dashboard.sqlite3") as db:
        db.executescript("""
            CREATE TABLE projects (id TEXT PRIMARY KEY,path TEXT,name TEXT,created REAL);
            CREATE TABLE tasks (id TEXT PRIMARY KEY,project_id TEXT,updated REAL,payload TEXT);
        """)
        db.execute("INSERT INTO projects VALUES(?,?,?,?)", ("project1", PRIVATE, "Test project", time.time()))
        for index in range(125):
            payload = {
                "title": PRIVATE, "messages": [{"text": PRIVATE}], "changes": [{"diff": PRIVATE}],
                "command": PRIVATE, "state": "completed", "files": 2, "added": 3, "removed": 1,
                "tokens": {"totalTokens": 15},
            }
            db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (f"task{index}", "project1", time.time(), json.dumps(payload)))
    with sqlite3.connect(data / "telemetry.sqlite3") as db:
        db.executescript("""
            CREATE TABLE api_attempts (
                provider TEXT,kind TEXT,outcome TEXT,finished REAL,input_tokens INTEGER,
                output_tokens INTEGER,cached_tokens INTEGER,reasoning_tokens INTEGER,total_tokens INTEGER,reason TEXT);
            CREATE INDEX attempts_finished ON api_attempts(finished);
        """)
        db.execute("INSERT INTO api_attempts VALUES(?,?,?,?,?,?,?,?,?,?)", ("provider1", "response", "completed", time.time(), 10, 5, 2, 1, 15, PRIVATE))
        db.execute("INSERT INTO api_attempts VALUES(?,?,?,?,?,?,?,?,?,?)", ("provider1", "response", "failed", time.time(), None, None, None, None, None, PRIVATE))
        db.execute("INSERT INTO api_attempts VALUES(?,?,?,?,?,?,?,?,?,?)", ("provider1", "response", "running", None, None, None, None, None, None, PRIVATE))
    return config, data


def hashes(data: Path):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in data.glob("*.sqlite3")}


def check(binary: Path):
    with tempfile.TemporaryDirectory(prefix="3api-mcp-test-") as temporary:
        config, data = fixture(Path(temporary))
        before = hashes(data)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProxyMock)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        session = Session(binary, config, data, server.server_port)
        try:
            assert session.call("tools/list")["error"]["code"] == -32002
            assert session.initialize("2099-01-01")["result"]["protocolVersion"] == "2025-11-25"
            session.send({"jsonrpc": "2.0", "method": "unknown/notification"})
            session.send({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "3api_status"}})
            assert session.call("ping", ident=0)["result"] == {}
            assert session.call("ping", ident=-3)["result"] == {}
            assert session.call("ping", ident=2.5)["result"] == {}
            assert session.call("ping", ident=None)["error"]["code"] == -32600
            listed = session.call("tools/list")["result"]["tools"]
            assert {tool["name"] for tool in listed} == {"3api_status", "3api_providers", "3api_usage", "3api_projects", "3api_tasks"}
            assert all(tool["annotations"]["readOnlyHint"] for tool in listed)
            status = session.tool("3api_status")
            assert status["reachable"] and status["stats"]["requests"] == 3
            assert status["public_model"] == "test-model"
            providers = session.tool("3api_providers")["providers"]
            assert len(providers) == 1 and providers[0]["attempts"] == 2
            assert providers[0]["enabled"] is False  # Static configuration wins.
            projects = session.tool("3api_projects")["projects"]
            assert projects[0]["name"] == "Test project" and "path" not in projects[0]
            tasks = session.tool("3api_tasks", {"limit": 1, "project_id": "project1"})
            assert tasks["has_more"] and len(tasks["tasks"]) == 1
            assert tasks["tasks"][0]["total_tokens"] == 15
            following = session.tool("3api_tasks", {"limit": 1, "project_id": "project1", "cursor": tasks["next_cursor"]})
            assert following["tasks"][0]["id"] != tasks["tasks"][0]["id"]
            assert session.tool("3api_tasks", {"project_id": "unknown"})["tasks"] == []
            usage = session.tool("3api_usage")
            assert usage["metrics"]["attempts"] == 2
            assert usage["metrics"]["generations"] == 1
            assert usage["metrics"]["unreported"] == 1
            assert usage["metrics"]["total_tokens"] == 15
            assert usage["metrics"]["cached_tokens"] == 2
            assert usage["truncated"] is False
            for resource in session.call("resources/list")["result"]["resources"]:
                content = session.call("resources/read", {"uri": resource["uri"]})["result"]["contents"][0]
                assert content["mimeType"] == "application/json"
                assert isinstance(json.loads(content["text"]), dict)
            assert session.call("resources/read", {"uri": "file:///etc/passwd"})["error"]["code"] == -32002
            for arguments in [{"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1.5}, {"path": "../secret"}]:
                assert session.call("tools/call", {"name": "3api_projects", "arguments": arguments})["error"]["code"] == -32602
            assert session.call("tools/call", {"name": "3api_tasks", "arguments": {"project_id": "../secret"}})["error"]["code"] == -32602
            assert session.call("tools/call", {"name": "shell", "arguments": {}})["error"]["code"] == -32602
            assert session.call("execute")["error"]["code"] == -32601
            session.raw(b"{broken-json\n")
            assert session.receive()["error"]["code"] == -32700
            session.raw(b"[]\n")
            assert session.receive()["error"]["code"] == -32600
            session.raw(b"\xff\n")
            assert session.receive()["error"]["code"] == -32700
            for mode in ["redirect", "oversize", "chunked_oversize"]:
                ProxyMock.mode = mode
                status = session.tool("3api_status")
                assert status["reachable"] is False
                assert status["reason"] in {"proxy_status_unavailable", "proxy_status_too_large"}
            assert set(ProxyMock.visits) == {"/status"}, "MCP followed an HTTP redirect"
        finally:
            ProxyMock.mode = "normal"
            session.close()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
        # The server must reject an oversized unterminated frame before EOF.
        large = Session(binary, config, data, 1)
        try:
            large.raw(b"x" * (1024 * 1024 + 1))
            assert large.receive()["error"]["code"] == -32700
        finally:
            large.close()
        # Old clients receive the version they requested, without newer fields.
        old = Session(binary, config, data, 1)
        try:
            assert old.initialize("2024-11-05")["result"]["protocolVersion"] == "2024-11-05"
            assert "annotations" not in old.call("tools/list")["result"]["tools"][0]
            result = old.call("tools/call", {"name": "3api_tasks", "arguments": {}})["result"]
            assert "structuredContent" not in result
        finally:
            old.close()
        assert hashes(data) == before, "MCP modified a SQLite database"
        # The real worker migration keeps MCP's established tool names and safe metadata.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from dashboard.store import DashboardStore
        store = DashboardStore(data / "dashboard.sqlite3")
        try:
            assert len(store.tasks()) == 125
        finally:
            store.close()
        migrated_hashes = hashes(data)
        migrated = Session(binary, config, data, 1)
        try:
            migrated.initialize()
            result = migrated.tool("3api_tasks", {"limit": 50, "project_id": "project1"})
            all_tasks = list(result["tasks"])
            while result["next_cursor"]:
                result = migrated.tool("3api_tasks", {"limit": 50, "project_id": "project1", "cursor": result["next_cursor"]})
                all_tasks.extend(result["tasks"])
            assert len(all_tasks) == 125 and len({task["id"] for task in all_tasks}) == 125
            assert all(task["run_id"] and task["state"] == "completed" for task in all_tasks)
        finally:
            migrated.close()
        assert hashes(data) == migrated_hashes, "MCP modified a migrated database"
        for unsafe in ["https://example.com", "http://192.168.0.1", "http://127.0.0.1/private", "http://user:pass@127.0.0.1"]:
            process = subprocess.run([str(binary), "mcp", "--config", str(config), "--data-dir", str(data), "--proxy-url", unsafe], input=b"", capture_output=True, timeout=5)
            assert process.returncode != 0 and process.stdout == b"", "Unsafe proxy URL was accepted"
    print("MCP offline checks passed: protocol, 5 tools, 3 resources, redaction, read-only legacy/migrated DB, 125 paginated chats, limits, loopback and redirects.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, nargs="?", default=Path(__file__).resolve().parents[1] / "target" / "debug" / ("3api.exe" if os.name == "nt" else "3api"))
    args = parser.parse_args()
    if not args.binary.is_file():
        parser.error("Build the Rust binary with cargo build, or pass its absolute path.")
    check(args.binary.resolve())
