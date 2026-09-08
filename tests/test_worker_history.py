import asyncio
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from dashboard.runner import ACTIVE, CodexRunner, RunnerError
from dashboard.server import create_app
from dashboard.store import DashboardStore, IdempotencyConflict, creation_fingerprint
from tests.test_dashboard import FIXTURE, make_runner, wait_for
from tests.test_panel_upgrade import configuration, vault


@pytest.fixture(autouse=True)
def worker_credentials(monkeypatch):
    monkeypatch.setattr("dashboard.runner.get_secret", lambda *args: None)


async def test_legacy_string_messages_survive_repeated_continuations(tmp_path):
    store, runner, project = make_runner(tmp_path)
    try:
        chat = runner.create_chat(project["id"])
        chat.update(state="completed", messages=["Old text", "Old text", {"role": "assistant", "text": "No id"}])
        store.save_task(chat)
        chat = runner.tasks[chat["id"]] = store.load_task(chat["id"])
        legacy_run = chat["run_id"]
        original = store.list_messages(chat["id"], legacy_run)["items"]
        for index in range(2):
            result = await runner.start_task(project["id"], "continue " + str(index), "low", chat["id"])
            await wait_for(lambda: chat["state"] == "completed")
            run = store.load_run(chat["id"], result["run_id"])
            assert len(run["messages"]["items"]) == 3
            assert run["messages"]["items"][0]["text"] == "continue " + str(index)
            assert run["agents_count"] == 1 and len(run["agents"]) == 1
            assert run["scan_status"] == "complete" and run["scan_error"] is None
        assert store.list_messages(chat["id"], legacy_run)["items"] == original
        assert len(store.load_task(chat["id"])["messages"]) == 9
    finally:
        await runner.close()
        store.close()


async def test_final_scan_error_preserves_last_diff_without_success_flag(tmp_path, monkeypatch):
    store, runner, project = make_runner(tmp_path)
    try:
        result = await runner.start_task(project["id"], "hold", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        (Path(project["path"]) / "saved.py").write_text("saved before scan error\n")
        await runner._scan(task)
        def broken_scan(_):
            raise OSError("fixture failure")
        monkeypatch.setattr("dashboard.runner.snapshot", broken_scan)
        runner.notification("turn/completed", {"threadId": task["thread_id"], "turn": {
            "id": task["turn_id"], "status": "completed"}})
        await wait_for(lambda: task["state"] not in ACTIVE)
        run = store.load_run(task["id"], result["run_id"])
        assert task["state"] == run["state"] == "failed"
        assert run["files"] == 1 and run["added"] == 1
        assert run["changes"]["items"][0]["path"] == "saved.py"
        assert run["scan_status"] == "error" and run["scan_error"]
        assert run["messages"]["items"][-1]["role"] == "error"
    finally:
        await runner.close()
        store.close()


async def test_unstarted_placeholder_does_not_count_as_real_agent(tmp_path):
    store, runner, project = make_runner(tmp_path)
    runner.command = []
    try:
        result = await runner.start_task(project["id"], "hold", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "failed")
        assert store.load_run(task["id"], result["run_id"])["agents_count"] == 0
    finally:
        await runner.close()
        store.close()


async def test_late_child_turn_and_completed_chat_approval_are_ignored(tmp_path, monkeypatch):
    store, runner, project = make_runner(tmp_path)
    sent = []
    try:
        result = await runner.start_task(project["id"], "hold", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        runner.thread_tasks["child"] = task["id"]
        runner.agent(task, "child", status="running")
        runner._thread_turns["child"] = "current-child-turn"
        runner.notification("turn/completed", {"threadId": "child", "turn": {"id": "old-child-turn", "status": "completed"}})
        assert runner._thread_turns["child"] == "current-child-turn"
        assert next(a for a in task["agents"] if a["id"] == "child")["status"] == "running"
        runner.notification("turn/completed", {"threadId": "child", "turn": {"id": "current-child-turn", "status": "completed"}})
        await runner.stop_task(task["id"])
        await wait_for(lambda: task["state"] == "interrupted")
        async def capture(value):
            sent.append(value)
        monkeypatch.setattr(runner, "send", capture)
        await runner.server_request({"id": "late", "method": "item/commandExecution/requestApproval", "params": {
            "threadId": task["thread_id"], "turnId": task["turn_id"], "command": "late"}})
        assert task["state"] == "interrupted" and not task["approvals"]
        assert sent[0]["id"] == "late" and "error" in sent[0]
    finally:
        await runner.close()
        store.close()


async def test_malformed_worker_requests_do_not_disconnect_running_chat(tmp_path):
    store, runner, project = make_runner(tmp_path)
    try:
        result = await runner.start_task(project["id"], "hold", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        source = asyncio.StreamReader()
        messages = [[], {"id": [], "result": {}}, {"id": "bad", "method": "item/tool/requestUserInput", "params": ["bad"]},
                    {"method": "item/agentMessage/delta", "params": {"threadId": task["thread_id"], "turnId": task["turn_id"],
                     "itemId": "after-invalid", "delta": "Still connected"}}]
        for message in messages:
            source.feed_data(json.dumps(message).encode() + b"\n")
        source.feed_eof()
        await runner._read(SimpleNamespace(stdout=source))
        assert task["state"] == "running" and runner.status()["status"] == "connected"
        assert task["messages"][-1]["text"] == "Still connected"
    finally:
        await runner.close()
        store.close()


async def test_previous_root_turn_cannot_finish_continuation_before_start_response(tmp_path, monkeypatch):
    store, runner, project = make_runner(tmp_path)
    allow_start = asyncio.Event()
    try:
        first = await runner.start_task(project["id"], "first", "low")
        task = runner.tasks[first["id"]]
        await wait_for(lambda: task["state"] == "completed")
        old_turn, old_thread = task["turn_id"], task["thread_id"]
        entered = asyncio.Event()
        request = runner.request
        async def delayed_start(method, params, **kwargs):
            if method == "turn/start":
                entered.set()
                await allow_start.wait()
            return await request(method, params, **kwargs)
        monkeypatch.setattr(runner, "request", delayed_start)
        second = await runner.start_task(project["id"], "second", "low", task["id"])
        await entered.wait()
        runner.notification("turn/completed", {"threadId": old_thread, "turn": {"id": old_turn, "status": "completed"}})
        runner.notification("item/agentMessage/delta", {"threadId": old_thread, "turnId": old_turn,
                                                       "itemId": "late", "delta": "Late old content"})
        assert task["state"] == "starting" and task["messages"][-1]["text"] == "second"
        allow_start.set()
        await wait_for(lambda: task["state"] == "completed")
        assert store.load_run(task["id"], second["run_id"])["files"] == 0
    finally:
        allow_start.set()
        await runner.close()
        store.close()


async def test_unacknowledged_stop_terminates_root_before_final_file_scan(tmp_path, monkeypatch):
    store, runner, project = make_runner(tmp_path)
    try:
        result = await runner.start_task(project["id"], "hold", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        request, kill, sleep = runner.request, runner._kill_owned, asyncio.sleep
        killed = []
        async def no_acknowledgement(method, params, **kwargs):
            if method == "turn/interrupt":
                return {}
            return await request(method, params, **kwargs)
        async def final_write_then_kill():
            if runner.process.returncode is None:
                (Path(project["path"]) / "last-root-write.py").write_text("saved immediately before stop\n")
                killed.append(True)
            await kill()
        async def short_deadline(seconds):
            await sleep(0 if seconds == 15 else seconds)
        monkeypatch.setattr(runner, "request", no_acknowledgement)
        monkeypatch.setattr(runner, "_kill_owned", final_write_then_kill)
        monkeypatch.setattr("dashboard.runner.asyncio.sleep", short_deadline)
        await runner.stop_task(task["id"])
        await wait_for(lambda: task["state"] not in ACTIVE)
        saved = store.load_run(task["id"], result["run_id"])
        assert killed and runner.process.returncode is not None
        assert task["state"] == saved["state"] == "interrupted"
        assert saved["files"] == 1 and saved["added"] == 1
    finally:
        await runner.close()
        store.close()


async def test_worker_eof_stops_process_before_publishing_last_changes(tmp_path, monkeypatch):
    store, runner, project = make_runner(tmp_path)
    try:
        task = runner.create_chat(project["id"])
        task.update(run_id="eof-run", state="running", started_at=time.time(), thread_id="eof-thread")
        runner.thread_tasks[task["thread_id"]] = task["id"]
        runner.agent(task, task["thread_id"], status="running")
        await runner.prepare_baseline(task, project)
        output = asyncio.StreamReader()
        output.feed_eof()
        process = runner.process = SimpleNamespace(stdout=output, returncode=None)
        async def last_write_on_kill():
            await asyncio.sleep(.1)
            (Path(project["path"]) / "eof.py").write_text("last write before process exit\n")
            process.returncode = 1
        monkeypatch.setattr(runner, "_kill_owned", last_write_on_kill)
        await runner._read(process)
        await wait_for(lambda: task["state"] == "failed")
        run = store.load_run(task["id"], "eof-run")
        assert run["files"] == 1 and run["added"] == 1
    finally:
        runner.process = None
        await runner.close()
        store.close()


async def test_project_removal_blocks_active_work_and_restores_history(tmp_path):
    store, runner, project = make_runner(tmp_path)
    try:
        result = await runner.start_task(project["id"], "hold", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        with pytest.raises(RunnerError) as blocked:
            await runner.remove_project(project["id"])
        assert blocked.value.status_code == 409
        await runner.stop_task(task["id"])
        await wait_for(lambda: task["state"] == "interrupted")
        runner._recoverable[task["id"]] = task["run_id"]
        with pytest.raises(RunnerError) as recovering:
            await runner.remove_project(project["id"])
        assert recovering.value.status_code == 409
        runner._recoverable.clear()
        before = store.load_run(task["id"], result["run_id"])
        assert await runner.remove_project(project["id"]) == {"id": project["id"], "removed": True}
        assert not store.projects() and not store.tasks() and not runner.tasks
        assert Path(project["path"]).is_dir()
        assert store.load_run(task["id"], result["run_id"]) == before
        restored = runner.add_project({"path": project["path"]})
        assert restored["id"] == project["id"] and task["id"] in runner.tasks
        assert store.list_runs(project["id"])["items"][0]["id"] == result["run_id"]
    finally:
        await runner.close()
        store.close()


def test_archived_project_persists_and_cannot_reassign_chat(tmp_path):
    store, runner, project = make_runner(tmp_path)
    chat = store.create_chat(project["id"], "Keep title")
    try:
        with pytest.raises(ValueError, match="innego projektu"):
            store.save_task({**chat, "project_id": "other"})
        store.archive_project(project["id"])
    finally:
        store.close()
    store = DashboardStore(tmp_path / "data/panel.sqlite3")
    try:
        assert store.projects() == store.tasks() == []
        assert store.projects(include_archived=True)[0]["archived_at"]
        restored = store.add_project(project["path"])
        assert restored["id"] == project["id"]
        assert store.tasks()[0]["title"] == "Keep title"
    finally:
        store.close()


def test_http_chat_workspace_scope_removal_and_restart(tmp_path, vault, monkeypatch):
    monkeypatch.setattr("dashboard.workspaces.tempfile.gettempdir", lambda: str(tmp_path))
    config = configuration(tmp_path)
    def factory(store, directory, model):
        return CodexRunner(store, directory, model, command=[sys.executable, str(FIXTURE)])
    def app():
        return create_app(data_dir=tmp_path / "data", config_path=config,
                          transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})), runner_factory=factory)
    first = app()
    with TestClient(first, base_url="http://127.0.0.1:4001") as client:
        csrf = re.search('name="csrf-token" content="([^"]+)"', client.get("/ui/").text)[1]
        headers = {"origin": "http://127.0.0.1:4001", "x-panel-csrf": csrf}
        assert client.post("/ui/api/projects", headers=headers, json={"kind": "chat", "access_mode": "full"}).status_code == 400
        workspace = client.post("/ui/api/projects", headers=headers, json={"kind": "chat", "name": "Private chat"}).json()
        assert workspace["kind"] == "chat" and workspace["access_mode"] == "isolated"
        chat = client.post("/ui/api/chats", headers=headers, json={"project_id": workspace["id"]}).json()
        Path(workspace["path"], "keep.txt").write_text("Keep user file\n")
        start = client.post("/ui/api/tasks", headers=headers, json={"project_id": workspace["id"], "continue_task": chat["id"],
            "prompt": "hold", "effort": "low", "permission_mode": "yolo", "api_ids": ["provider1"]})
        assert start.status_code == 202
        assert first.state.runner.tasks[chat["id"]]["permission_mode"] == "approval"
        assert client.delete("/ui/api/projects/" + workspace["id"], headers=headers).status_code == 409
        client.post("/ui/api/tasks/" + chat["id"] + "/stop", headers=headers)
        deadline = time.monotonic() + 12
        while first.state.runner.tasks[chat["id"]]["state"] in ACTIVE:
            assert time.monotonic() < deadline
            time.sleep(.02)
        for project in first.state.store.projects():
            assert client.delete("/ui/api/projects/" + project["id"], headers=headers).status_code == 200
        assert client.get("/ui/api/state").json()["projects"] == []
        assert Path(workspace["path"], "keep.txt").read_text() == "Keep user file\n"
    second = app()
    with TestClient(second, base_url="http://127.0.0.1:4001") as client:
        csrf = re.search('name="csrf-token" content="([^"]+)"', client.get("/ui/").text)[1]
        headers = {"origin": "http://127.0.0.1:4001", "x-panel-csrf": csrf}
        assert client.get("/ui/api/state").json()["projects"] == []
        restored = client.post("/ui/api/projects", headers=headers, json={"path": workspace["path"]}).json()
        assert restored["id"] == workspace["id"] and restored["access_mode"] == "isolated"
        assert restored["name"] == "Private chat"
        assert client.get("/ui/api/projects/" + workspace["id"] + "/chats").json()["items"][0]["id"] == chat["id"]


async def test_chat_creation_retries_preserve_live_unflushed_messages(tmp_path):
    store, runner, project = make_runner(tmp_path)
    try:
        chat = runner.create_chat(project["id"], title="Live chat", client_request_id="create-live")
        result = await runner.start_task(project["id"], "hold", "low", chat["id"])
        await wait_for(lambda: chat["state"] == "running")
        runner.message(chat, "unflushed", "assistant", "Still streaming")
        repeated = runner.create_chat(project["id"], title="Live chat", client_request_id="create-live")
        assert repeated is chat and runner.tasks[chat["id"]] is chat
        assert chat["messages"][-1]["text"] == "Still streaming" and chat["run_id"] == result["run_id"]
        with pytest.raises(IdempotencyConflict):
            runner.create_chat(project["id"], title="Changed body", client_request_id="create-live")
        assert len(store.tasks()) == 1 and len(store.list_runs(project["id"])["items"]) == 1
    finally:
        await runner.close()
        store.close()


def test_creation_retries_are_atomic_between_independent_connections(tmp_path):
    database = tmp_path / "panel.sqlite3"
    stores = [DashboardStore(database), DashboardStore(database)]
    allocated, lock = [], Lock()
    fingerprint = creation_fingerprint({"kind": "chat", "name": "Concurrent"})
    def prepare():
        with lock:
            folder = tmp_path / ("workspace-" + str(len(allocated)))
            folder.mkdir()
            allocated.append(folder)
        return {"path": str(folder), "kind": "chat", "access_mode": "isolated", "name": "Concurrent"}
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            projects = list(executor.map(lambda store: store.add_project(workspace_factory=prepare,
                client_request_id="same-workspace", request_fingerprint=fingerprint), stores))
            chats = list(executor.map(lambda store: store.create_chat(projects[0]["id"], "Concurrent chat",
                client_request_id="same-chat"), stores))
        assert projects[0] == projects[1] and len(allocated) == 1
        assert chats[0] == chats[1] and len(stores[0].tasks()) == 1
        another = tmp_path / "another"
        another.mkdir()
        project2 = stores[0].add_project(str(another))
        with pytest.raises(IdempotencyConflict):
            stores[0].create_chat(project2["id"], "Concurrent chat", client_request_id="same-chat")
    finally:
        for store in stores:
            store.close()


def test_creation_mapping_failure_rolls_back_project_and_chat(tmp_path):
    store, runner, project = make_runner(tmp_path)
    folder = tmp_path / "new-project"
    folder.mkdir()
    try:
        store.db.execute("""CREATE TRIGGER reject_creation BEFORE INSERT ON creation_requests
            BEGIN SELECT RAISE(ABORT, 'fixture mapping failure'); END""")
        with pytest.raises(sqlite3.IntegrityError):
            store.add_project(str(folder), client_request_id="atomic-project")
        with pytest.raises(sqlite3.IntegrityError):
            store.create_chat(project["id"], client_request_id="atomic-chat")
        assert len(store.projects()) == 1 and not store.tasks()
        assert store.db.execute("SELECT COUNT(*) FROM creation_requests").fetchone()[0] == 0
        assert not store.db.in_transaction
        store.db.execute("DROP TRIGGER reject_creation")
        assert store.add_project(str(folder), client_request_id="atomic-project")["path"] == str(folder)
        assert store.create_chat(project["id"], client_request_id="atomic-chat")["state"] == "idle"
    finally:
        store.close()


def test_http_creation_retries_survive_restart_without_extra_temp_folders(tmp_path, vault, monkeypatch):
    monkeypatch.setattr("dashboard.workspaces.tempfile.gettempdir", lambda: str(tmp_path))
    from dashboard.server import prepare_workspace
    preparations = []
    def tracked_prepare(body, directory):
        preparations.append(dict(body))
        return prepare_workspace(body, directory)
    monkeypatch.setattr("dashboard.server.prepare_workspace", tracked_prepare)
    config = configuration(tmp_path)
    def app():
        return create_app(data_dir=tmp_path / "data", config_path=config,
                          transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
                          runner_factory=lambda store, directory, model: CodexRunner(store, directory, model, command=[]))
    workspace_body = {"kind": "chat", "name": "Replay workspace", "client_request_id": "workspace-retry"}
    with TestClient(app(), base_url="http://127.0.0.1:4001") as client:
        csrf = re.search('name="csrf-token" content="([^"]+)"', client.get("/ui/").text)[1]
        headers = {"origin": "http://127.0.0.1:4001", "x-panel-csrf": csrf}
        workspace = client.post("/ui/api/projects", json=workspace_body, headers=headers).json()
        assert client.post("/ui/api/projects", json=workspace_body, headers=headers).json() == workspace
        chat_body = {"project_id": workspace["id"], "title": "Replay chat", "client_request_id": "chat-retry"}
        chat = client.post("/ui/api/chats", json=chat_body, headers=headers).json()
        assert client.post("/ui/api/chats", json=chat_body, headers=headers).json() == chat
        for endpoint, body, field in (("projects", workspace_body, "name"), ("chats", chat_body, "title")):
            conflict = client.post("/ui/api/" + endpoint, json={**body, field: "Changed body"}, headers=headers)
            assert conflict.status_code == 409 and conflict.json()["code"] == "request_conflict"
            for invalid in ("", True, "with space", "ą", "x" * 129):
                assert client.post("/ui/api/" + endpoint, json={**body, "client_request_id": invalid}, headers=headers).status_code == 400
        assert len(preparations) == 1
    with TestClient(app(), base_url="http://127.0.0.1:4001") as client:
        csrf = re.search('name="csrf-token" content="([^"]+)"', client.get("/ui/").text)[1]
        headers = {"origin": "http://127.0.0.1:4001", "x-panel-csrf": csrf}
        assert client.post("/ui/api/projects", json=workspace_body, headers=headers).json() == workspace
        assert client.post("/ui/api/chats", json=chat_body, headers=headers).json() == chat
        assert len(preparations) == 1
        assert client.delete("/ui/api/projects/" + workspace["id"], headers=headers).status_code == 200
        for endpoint, body in (("projects", workspace_body), ("chats", chat_body)):
            replay = client.post("/ui/api/" + endpoint, json=body, headers=headers)
            assert replay.status_code == 409 and replay.json()["code"] == "request_conflict"
        assert len(preparations) == 1 and Path(workspace["path"]).is_dir()
        assert all(project["id"] != workspace["id"] for project in client.get("/ui/api/state").json()["projects"])
