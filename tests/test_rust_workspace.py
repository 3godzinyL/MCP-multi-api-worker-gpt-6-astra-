"""Public Rust HTTP + private Python worker against a local Codex/API fixture."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys
from urllib.error import URLError

import pytest

from tests.browser import release_smoke as smoke


ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "3api.exe"
pytestmark = pytest.mark.skipif(os.name != "nt" or not BINARY.is_file(),
                                reason="Windows integration requires cargo build --locked")


def test_public_workspace_preserves_chats_runs_and_archives_across_restart(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    private_values = [secrets.token_urlsafe(40), secrets.token_urlsafe(40)]
    env = smoke.environment(tmp_path, *private_values)
    report = smoke.Report(tmp_path, private_values)
    smoke.install_codex_fixture(sys.executable, project, tmp_path, env)
    upstream = smoke.mock_provider(private_values[1])
    config = smoke.write_config(tmp_path, upstream.server_port)
    process = panel = None

    def start(number):
        proxy_port, panel_port = smoke.ports()
        child = smoke.LoggedProcess([
            str(BINARY), "serve", "--config", str(config), "--data-dir", str(tmp_path / "data"),
            "--project-dir", str(ROOT), "--python", sys.executable,
            "--proxy-port", str(proxy_port), "--panel-port", str(panel_port),
        ], ROOT, env, tmp_path / f"workspace-{number}.log", report)
        client = smoke.Panel(panel_port)

        def ready():
            assert child.process.poll() is None, "Rust exited before its private worker was ready"
            try:
                return client.json("/health").get("application") == "3api-rust-panel"
            except (URLError, ConnectionError, TimeoutError):
                return False

        try:
            smoke.wait_until(ready, timeout=30, description="public Rust panel")
            client.open()  # A real automatic session; no login token is submitted.
        except BaseException:
            child.force_stop()
            raise
        return child, client

    try:
        process, panel = start(1)
        project_request = {"path": str(project), "name": "Local fixture", "client_request_id": secrets.token_hex(16)}
        project_id = smoke.identifier(panel.json("/ui/api/projects", project_request))
        assert smoke.identifier(panel.json("/ui/api/projects", project_request)) == project_id
        chat_request = {"project_id": project_id, "title": "Persistent chat", "client_request_id": secrets.token_hex(16)}
        chat_id = smoke.identifier(panel.json("/ui/api/chats", chat_request, expected=(200, 201)))
        assert smoke.identifier(panel.json("/ui/api/chats", chat_request, expected=(200, 201))) == chat_id
        panel.json("/ui/api/chats", {**chat_request, "title": "Changed request"}, expected=(409,))
        empty_id = smoke.identifier(panel.json("/ui/api/chats", {"project_id": project_id}, expected=(200, 201)))
        assert chat_id != empty_id
        first = smoke.complete_run(panel, project_id, chat_id, "release smoke first")
        smoke.verify_run(first, project_id, chat_id, 2)
        second = smoke.complete_run(panel, project_id, chat_id, "release smoke second")
        smoke.verify_run(second, project_id, chat_id, 1)
        signatures = {run["id"]: smoke.durable_signature(run) for run in (first, second)}
        assert len(signatures) == 2
        assert upstream.calls == 2 and upstream.invalid_calls == 0
        settings = panel.json("/ui/api/settings")
        assert settings["main_prompt"].strip() and settings["coordinator_prompt"].strip()
        settings["main_prompt"] = "My saved instructions — keep this text."
        panel.json("/ui/api/settings", settings)
        token_budget = panel.json("/ui/api/state")["proxy"]["providers"][0]["token_budget"]
        assert (token_budget["limit"], token_budget["soft_limit"], token_budget["hard_limit"]) == (1_000_000, 900_000, 950_000)

        isolated_request = {"kind": "chat", "name": "Temporary conversation", "client_request_id": secrets.token_hex(16)}
        isolated = panel.json("/ui/api/projects", isolated_request)
        assert panel.json("/ui/api/projects", isolated_request)["id"] == isolated["id"]
        panel.json("/ui/api/projects", {**isolated_request, "name": "Different request"}, expected=(409,))
        assert isolated["kind"] == "chat" and isolated["access_mode"] == "isolated"
        scratch = Path(isolated["path"])
        assert scratch.is_dir() and scratch.is_relative_to(tmp_path / "temp")
        (scratch / "user-notes.txt").write_text("Keep this scratch file", encoding="utf-8")
        panel.json("/ui/api/projects", {"kind": "chat", "access_mode": "full"}, expected=(400,))
        full = panel.json("/ui/api/projects", {"kind": "chat", "access_mode": "full", "full_access_confirmed": True})
        assert full["access_mode"] == "full"

        panel.request("DELETE", f"/ui/api/projects/{project_id}", expected=(403,), csrf=False)
        panel.request("DELETE", f"/ui/api/projects/{project_id}", expected=(403,),
                      headers={"Origin": "https://untrusted.example"})
        removed = panel.json(f"/ui/api/projects/{project_id}", method="DELETE")
        assert removed["removed"] is True
        panel.json("/ui/api/projects", project_request, expected=(409,))
        assert (project / "created-by-task.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n"
        assert project_id not in {row["id"] for row in panel.json("/ui/api/state")["projects"]}
        smoke.stop_application(process, panel, normal=True)
        process = panel = None
        process, panel = start(2)
        assert project_id not in {row["id"] for row in panel.json("/ui/api/state")["projects"]}
        assert panel.json("/ui/api/settings")["main_prompt"] == settings["main_prompt"]
        assert panel.json("/ui/api/projects", isolated_request)["path"] == isolated["path"]
        assert (scratch / "user-notes.txt").read_text(encoding="utf-8") == "Keep this scratch file"
        restored = panel.json("/ui/api/projects", {"path": str(project)})
        assert restored["id"] == project_id
        assert smoke.identifier(panel.json("/ui/api/chats", chat_request, expected=(200, 201))) == chat_id
        assert {chat_id, empty_id}.issubset({row["id"] for row in smoke.items(panel, f"/ui/api/projects/{project_id}/chats")})
        for run_id, signature in signatures.items():
            assert smoke.durable_signature(smoke.run_detail(panel, chat_id, run_id)) == signature
        assert {row["id"] for row in smoke.items(panel, f"/ui/api/projects/{project_id}/history")} == set(signatures)
        smoke.stop_application(process, panel, normal=True)
        process = panel = None
    finally:
        if process is not None:
            smoke.stop_application(process, panel, normal=False)
        upstream.shutdown()
        upstream.server_close()
