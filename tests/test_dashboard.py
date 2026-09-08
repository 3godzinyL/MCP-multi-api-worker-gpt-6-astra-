import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from dashboard.changes import compare, save_baseline, snapshot
from dashboard.runner import CodexRunner, RunnerError
from dashboard.server import create_app
from dashboard.store import DashboardStore

FIXTURE = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setattr("dashboard.runner.get_secret", lambda *args: None)
    monkeypatch.setattr("dashboard.server.get_secret", lambda *args: "local-fixture-token")


def make_runner(tmp_path):
    store = DashboardStore(tmp_path / "data" / "panel.sqlite3")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    project = store.add_project(str(workspace))
    runner = CodexRunner(store, tmp_path / "data", "fixture-model", command=[sys.executable, str(FIXTURE)])
    return store, runner, project


async def wait_for(predicate, timeout=12):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.04)


def test_file_counts_use_task_baseline_including_shell_changes(tmp_path):
    (tmp_path / "old.py").write_text("one\ntwo\nthree\n")
    (tmp_path / "delete.js").write_text("old\n")
    (tmp_path / ".env").write_text("SECRET=hidden")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("dependency")
    before = snapshot(tmp_path)
    assert ".env" not in before["files"]
    assert "node_modules/dep.js" not in before["files"]
    (tmp_path / "old.py").write_text("one\nnew\nthree\n")
    (tmp_path / "delete.js").unlink()
    (tmp_path / "created.js").write_text("a\nb\n")
    result = compare(before, snapshot(tmp_path))
    assert (result["files"], result["added"], result["removed"]) == (3, 3, 2)
    assert {c["kind"] for c in result["changes"]} == {"nowy", "usunięty", "zmieniony"}


async def test_app_server_task_messages_tokens_files_and_persistence(tmp_path):
    store, runner, project = make_runner(tmp_path)
    try:
        result = await runner.start_task(project["id"], "create fixture", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "completed" and task["files"] == 1)
        assert task["tokens"]["totalTokens"] == 120
        assert task["added"] == 2 and task["removed"] == 0
        assert (Path(project["path"]) / "created-by-task.txt").read_text() == "one\ntwo\n"
        assert [m["text"] for m in task["messages"] if m["role"] == "assistant"] == ["Task complete"]
        assert task["agents"][0]["status"] == "completed" and runner.active_agents() == 0
        assert "messages" not in runner.public_tasks()[0]
        assert runner.public_tasks(task["id"])[0]["messages"]
        restored = store.tasks()[0]
        assert restored["thread_id"] == task["thread_id"] and restored["files"] == 1
        await wait_for(lambda: not runner._scanning)
        assert task["id"] not in runner._baselines
        continued = await runner.start_task(project["id"], "again", "low", task["id"])
        assert continued["id"] == task["id"]
        await wait_for(lambda: task["state"] == "completed")
    finally:
        await runner.close()
        store.close()


def test_project_data_sources_are_included(tmp_path):
    (tmp_path / "data").mkdir()
    before = snapshot(tmp_path)
    (tmp_path / "data" / "seed.sql").write_text("SELECT 1;\n")
    result = compare(before, snapshot(tmp_path))
    assert result["files"] == 1 and result["added"] == 1
    assert result["changes"][0]["path"] == "data/seed.sql"


@pytest.mark.parametrize("decision,expected_files", [("accept", 1), ("decline", 0)])
async def test_approval_waits_for_actual_user_decision(tmp_path, decision, expected_files):
    store, runner, project = make_runner(tmp_path)
    try:
        result = await runner.start_task(project["id"], "approve", "low", options={"permission_mode": "approval"})
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "awaiting_input")
        assert not (Path(project["path"]) / "created-by-task.txt").exists()
        with pytest.raises(RunnerError):
            await runner.answer("another-task", task["approvals"][0]["id"], "accept")
        await runner.answer(task["id"], task["approvals"][0]["id"], decision)
        await wait_for(lambda: task["state"] == "completed" and not runner._scanning)
        assert task["files"] == expected_files
        assert not task["approvals"]
    finally:
        await runner.close()
        store.close()


async def test_question_and_duplicate_agent_events(tmp_path):
    store, runner, project = make_runner(tmp_path)
    try:
        result = await runner.start_task(project["id"], "ask", "medium")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "awaiting_input")
        item = {"id": "spawn-1", "type": "collabAgentToolCall", "tool": "spawnAgent", "receiverThreadIds": ["child"],
                "agentsStates": {"child": {"status": "running"}}}
        for _ in range(3):
            runner.notification("item/completed", {"threadId": task["thread_id"], "item": item})
        assert len(task["agents"]) == 2 and runner.active_agents() == 2
        item["agentsStates"]["child"]["status"] = "completed"
        runner.notification("item/completed", {"threadId": task["thread_id"], "item": item})
        assert runner.active_agents() == 1
        approval = task["approvals"][0]
        with pytest.raises(RunnerError):
            await runner.answer(task["id"], approval["id"], answers={})
        await runner.answer(task["id"], approval["id"], answers={"name": "test"})
        await wait_for(lambda: task["state"] == "completed")
    finally:
        await runner.close()
        store.close()


async def test_interrupt_and_same_project_collision(tmp_path):
    store, runner, project = make_runner(tmp_path)
    try:
        result = await runner.start_task(project["id"], "hold", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        with pytest.raises(RunnerError, match="trwa już"):
            await runner.start_task(project["id"], "second", "low")
        await runner.stop_task(task["id"])
        await wait_for(lambda: task["state"] == "interrupted")
        assert task["files"] == 0 and runner.active_agents() == 0
    finally:
        await runner.close()
        store.close()


def test_local_session_csrf_validation_and_no_credentials(tmp_path):
    def upstream(request):
        assert request.headers.get("authorization") == "Bearer local-fixture-token"
        return httpx.Response(200, json={"providers": [], "stats": {}, "telemetry_enabled": True})
    def factory(store, directory, model):
        return CodexRunner(store, directory, model, command=[sys.executable, str(FIXTURE)])
    configuration = tmp_path / "providers.toml"
    configuration.write_text('[[providers]]\nid="fixture"\nbase_url="https://fixture.example/v1"\ndeployment="fixture-model"\n', encoding="utf-8")
    app = create_app(data_dir=tmp_path / "data", config_path=configuration,
                     transport=httpx.MockTransport(upstream), runner_factory=factory)
    with TestClient(app, base_url="http://127.0.0.1:4001") as client:
        assert client.get('/ui/api/state').status_code == 401
        assert client.get('/ui/', headers={'host': 'evil.example'}).status_code == 403
        page = client.get('/ui/')
        assert page.status_code == 200 and 'frame-ancestors' in page.headers['content-security-policy']
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
        headers = {'origin': 'http://127.0.0.1:4001', 'x-panel-csrf': csrf}
        assert client.post('/ui/api/projects', json={'path': str(tmp_path)}).status_code == 403
        assert client.post('/ui/api/projects', json={}, headers={**headers, 'origin': 'https://evil.example'}).status_code == 403
        assert client.post('/ui/api/projects', json=[], headers=headers).status_code == 400
        project = client.post('/ui/api/projects', json={'path': str(tmp_path)}, headers=headers).json()
        assert project['path'] == str(tmp_path)
        for body in ({'project_id': project['id'], 'prompt': ''}, {'project_id': [], 'prompt': 'x'}, {'project_id': project['id'], 'prompt': 'x', 'effort': []}):
            assert client.post('/ui/api/tasks', json=body, headers=headers).status_code == 400
        state = client.get('/ui/api/state')
        assert state.status_code == 200
        assert 'local-fixture-token' not in state.text and 'local-fixture-token' not in page.text
        assert client.get('/ui/api/folders', params={'path': str(tmp_path)}).status_code == 200


async def test_continuation_has_own_baseline_and_preserves_previous_run(tmp_path):
    store, runner, project = make_runner(tmp_path)
    try:
        first = await runner.start_task(project["id"], "write once", "low", client_request_id="first-message")
        task = runner.tasks[first["id"]]
        await wait_for(lambda: task["state"] == "completed")
        first_turn = task["turn_id"]
        assert store.load_run(task["id"], first["run_id"])["added"] == 2
        second = await runner.start_task(project["id"], "write same bytes", "low", task["id"], client_request_id="second-message")
        assert second["id"] == first["id"] and second["run_id"] != first["run_id"]
        assert task["files"] == 0 and task["added"] == 0
        await wait_for(lambda: task["state"] == "completed")
        assert task["turn_id"] != first_turn
        assert (task["files"], task["added"], task["removed"]) == (0, 0, 0)
        old = store.load_run(task["id"], first["run_id"])
        current = store.load_run(task["id"], second["run_id"])
        assert old["files"] == 1 and old["changes"]["items"]
        assert current["files"] == 0 and not current["changes"]["items"]
        assert len([message for message in task["messages"] if message["role"] == "assistant"]) == 2
        assert old["messages"]["items"][0]["text"] == "write once"
        assert current["messages"]["items"][0]["text"] == "write same bytes"
        assert len(store.list_runs(project["id"])["items"]) == 2
    finally:
        await runner.close()
        store.close()


async def test_duplicate_submission_is_one_run_and_rejected_chat_survives(tmp_path):
    store, runner, project = make_runner(tmp_path)
    try:
        submissions = await asyncio.gather(*(runner.start_task(project["id"], "hold", "low", client_request_id="same-message") for _ in range(2)))
        assert submissions[0]["id"] == submissions[1]["id"]
        assert submissions[0]["run_id"] == submissions[1]["run_id"]
        task = runner.tasks[submissions[0]["id"]]
        await wait_for(lambda: task["state"] == "running")
        for _ in range(2):
            with pytest.raises(RunnerError, match="Czat|czat"):
                await runner.start_task(project["id"], "second chat", "low", client_request_id="blocked-message")
        assert len(store.tasks()) == 2 and len(store.list_runs(project["id"])["items"]) == 1
        blocked = next(chat for chat in store.tasks() if chat["id"] != task["id"])
        assert blocked["state"] == "idle" and blocked["title"] == "second chat"
        await runner.stop_task(task["id"])
        await wait_for(lambda: task["state"] == "interrupted")
        retry = await runner.start_task(project["id"], "second chat", "low", client_request_id="blocked-message")
        assert retry["id"] == blocked["id"]
        await wait_for(lambda: runner.tasks[retry["id"]]["state"] == "completed")
    finally:
        await runner.close()
        store.close()


async def test_finalizing_remains_visible_until_final_diff_is_durable(tmp_path, monkeypatch):
    store, runner, project = make_runner(tmp_path)
    scan_entered, scan_allowed = asyncio.Event(), asyncio.Event()
    original_scan = runner._scan

    async def delayed_scan(task):
        scan_entered.set()
        await scan_allowed.wait()
        await original_scan(task)

    monkeypatch.setattr(runner, "_scan", delayed_scan)
    try:
        result = await runner.start_task(project["id"], "write", "low")
        task = runner.tasks[result["id"]]
        await scan_entered.wait()
        assert task["state"] == "finalizing"
        assert store.load_run(task["id"], result["run_id"])["state"] == "finalizing"
        scan_allowed.set()
        await wait_for(lambda: task["state"] == "completed")
        run = store.load_run(task["id"], result["run_id"])
        assert run["state"] == "completed" and run["files"] == 1 and run["changes"]["items"]
        assert run["finished_at"] >= run["started_at"] and run["elapsed_seconds"] >= 0
    finally:
        scan_allowed.set()
        await runner.close()
        store.close()


async def test_scans_preserve_observed_edits_reversions_and_partial_results(tmp_path, monkeypatch):
    store, runner, project = make_runner(tmp_path)
    source = Path(project["path"]) / "source.py"
    source.write_text("original\n")
    try:
        result = await runner.start_task(project["id"], "hold", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        source.write_text("changed\nextra\n")
        await runner._scan(task)
        assert (task["files"], task["added"], task["removed"], task["touched_files"]) == (1, 2, 1, 1)
        revision = task["changes_revision"]
        await runner._scan(task)
        assert task["changes_revision"] == revision and len(task["change_history"]) == 1
        with monkeypatch.context() as patch:
            patch.setattr("dashboard.runner.snapshot", lambda root: {"files": {}, "skipped": 1, "incomplete_paths": ["source.py"]})
            await runner._scan(task)
        assert task["scan_status"] == "partial" and task["files"] == 1
        assert task["changes_revision"] == revision
        with monkeypatch.context() as patch:
            def fail_scan(root):
                raise RuntimeError("transient scan failure")
            patch.setattr("dashboard.runner.snapshot", fail_scan)
            await runner._scan(task)
        assert task["state"] == "running" and task["scan_status"] == "error" and task["files"] == 1
        source.write_text("original\n")
        await runner._scan(task)
        assert (task["files"], task["added"], task["removed"], task["touched_files"]) == (0, 0, 0, 1)
        assert task["change_history"][-1]["reverted"] is True
        await runner.stop_task(task["id"])
        await wait_for(lambda: task["state"] == "interrupted")
        restored = store.load_run(task["id"], result["run_id"])
        assert restored["files"] == 0 and restored["touched_files"] == 1
        assert len(restored["change_history"]["items"]) == 2
    finally:
        await runner.close()
        store.close()


async def test_ticker_retries_storage_and_scan_errors_without_losing_messages(tmp_path, monkeypatch):
    store, runner, project = make_runner(tmp_path)
    try:
        result = await runner.start_task(project["id"], "hold", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        save, schedule = store.save_task, runner.schedule_scan
        attempts = {"save": 0, "scan": 0}

        def save_after_lock(value):
            attempts["save"] += 1
            if attempts["save"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return save(value)

        def flaky_schedule(value, **kwargs):
            attempts["scan"] += 1
            if attempts["scan"] == 1:
                raise RuntimeError("transient scanner fault")
            return schedule(value, **kwargs)

        monkeypatch.setattr(store, "save_task", save_after_lock)
        monkeypatch.setattr(runner, "schedule_scan", flaky_schedule)
        for number in range(401):
            runner.message(task, str(number), "assistant", str(number))
        runner.message(task, "long", "assistant", "x" * 65000)
        runner.touch(task)
        await wait_for(lambda: task["id"] not in runner._dirty and attempts["scan"] > 0, timeout=8)
        assert not runner._ticker.done() and attempts["save"] >= 2
        messages = store.load_task(task["id"])["messages"]
        assert len(messages) == 403 and messages[-1]["text"] == "x" * 65000
        assert "messages" not in runner.public_tasks()[0]
        assert runner.public_tasks(task["id"])[0]["messages_has_more"] is True
    finally:
        await runner.close()
        store.close()


async def test_restart_marks_saved_run_interrupted_and_releases_reservations(tmp_path):
    store, runner, project = make_runner(tmp_path)
    chat = runner.create_chat(project["id"])
    chat.update(run_id="surviving-run", state="finalizing", started_at=100.0, agents=[{"id": "old", "status": "running"}],
                changes=[{"path": "old.py", "diff": "+preserved", "added": 1, "removed": 0}], files=1, added=1,
                messages=[{"id": "saved", "run_id": "surviving-run", "role": "assistant", "text": "saved response"}])
    store.save_task(chat)
    restored = CodexRunner(store, tmp_path / "data", "fixture-model", command=[])
    releases = []

    async def release(value):
        releases.append(value)

    restored.route_release = release
    try:
        await restored.release_stale_routes()
        saved = store.load_run(chat["id"], "surviving-run")
        assert saved["state"] == "interrupted" and saved["changes"]["items"][0]["diff"] == "+preserved"
        assert saved["messages"]["items"][0]["text"] == "saved response"
        assert releases == [{"run_id": "surviving-run"}]
        chat["state"] = "idle"  # The original instance represents the crashed process.
    finally:
        await restored.close()
        await runner.close()
        store.close()


async def test_verified_child_routes_and_worker_crash_release_run(tmp_path):
    store, runner, project = make_runner(tmp_path)
    routes, releases = [], []

    async def setup(value):
        routes.append(value)

    async def release(value):
        releases.append(value)

    runner.route_setup, runner.route_release = setup, release
    try:
        result = await runner.start_task(project["id"], "hold", "ultra", options={"api_ids": ["one", "two"]})
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        runner.thread_tasks["other-chat-thread"] = "another-chat"
        for child in ("real-child", "real-child", "other-chat-thread"):
            runner.notification("item/completed", {"threadId": task["thread_id"], "item": {
                "type": "subAgentActivity", "kind": "started", "agentThreadId": child, "agentPath": "worker"}})
        await wait_for(lambda: any(route["role"] == "auxiliary" for route in routes))
        auxiliary = [route for route in routes if route["role"] == "auxiliary"]
        assert len(auxiliary) == 1 and auxiliary[0]["thread_id"] == "real-child"
        assert auxiliary[0]["run_id"] == result["run_id"] and auxiliary[0]["task_id"] == task["id"]
        assert routes[1]["role"] == "main" and routes[1]["thread_id"] == task["thread_id"]
        runner.process.kill()
        await wait_for(lambda: task["state"] == "failed")
        assert {"run_id": result["run_id"]} in releases and runner.active_agents() == 0
    finally:
        await runner.close()
        store.close()


async def test_route_release_waits_for_pending_binding_and_blocks_late_children(tmp_path):
    store, runner, project = make_runner(tmp_path)
    binding, allow_binding = asyncio.Event(), asyncio.Event()
    operations = []

    async def setup(value):
        if value["role"] == "auxiliary":
            binding.set()
            await allow_binding.wait()
        operations.append(value["role"])

    async def release(value):
        operations.append("released")

    runner.route_setup, runner.route_release = setup, release
    try:
        result = await runner.start_task(project["id"], "hold", "ultra", options={"api_ids": ["one", "two"]})
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        event = {"threadId": task["thread_id"], "item": {"type": "subAgentActivity", "kind": "started", "agentThreadId": "child", "agentPath": "worker"}}
        runner.notification("item/completed", event)
        await binding.wait()
        runner.agent(task, "child", status="completed")
        runner.finish(task, "failed")
        await asyncio.sleep(.02)
        assert "released" not in operations
        allow_binding.set()
        await wait_for(lambda: task["state"] == "failed")
        assert operations[-2:] == ["auxiliary", "released"]
        event["item"]["agentThreadId"] = "late-child"
        runner.notification("item/completed", event)
        assert "late-child" not in runner.thread_tasks and runner.active_agents() == 0
    finally:
        allow_binding.set()
        await runner.close()
        store.close()


async def test_restart_recovers_file_written_after_last_progress_flush(tmp_path):
    store, runner, project = make_runner(tmp_path)
    source = Path(project["path"]) / "source.py"
    source.write_text("before\n")
    save_baseline(tmp_path / "data/snapshots/crashed-run.json.gz", snapshot(Path(project["path"])))
    chat = runner.create_chat(project["id"])
    chat.update(run_id="crashed-run", state="running", started_at=100.0)
    store.save_task(chat)
    source.write_text("after\nnew line\n")
    restored = CodexRunner(store, tmp_path / "data", "fixture-model", command=[sys.executable, str(FIXTURE)])
    chat["state"] = "idle"  # This instance represents the process that crashed.
    try:
        await restored.recover_runs()
        previous = store.load_run(chat["id"], "crashed-run")
        assert previous["state"] == "interrupted"
        assert (previous["files"], previous["added"], previous["removed"], previous["touched_files"]) == (1, 2, 1, 1)
        continued = await restored.start_task(project["id"], "hold", "low", chat["id"])
        task = restored.tasks[chat["id"]]
        await wait_for(lambda: task["state"] == "running")
        assert continued["run_id"] != "crashed-run" and task["files"] == 0
        assert store.load_run(chat["id"], "crashed-run")["added"] == 2
    finally:
        await restored.close()
        await runner.close()
        store.close()


async def test_root_completion_waits_for_child_write_before_final_diff(tmp_path):
    store, runner, project = make_runner(tmp_path)
    released = []

    async def release(value):
        released.append(value)

    runner.route_release = release
    try:
        result = await runner.start_task(project["id"], "hold", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        runner.thread_tasks["child"] = task["id"]
        runner.agent(task, "child", status="running")
        runner._thread_turns["child"] = "child-turn"
        runner.notification("turn/completed", {"threadId": task["thread_id"], "turn": {"id": task["turn_id"], "status": "completed"}})
        await asyncio.sleep(2.1)
        assert task["state"] == "finalizing" and not released
        assert next(agent for agent in task["agents"] if agent["id"] == "child")["status"] == "running"
        (Path(project["path"]) / "last-child-write.py").write_text("after root completion\n")
        runner.notification("turn/completed", {"threadId": "child", "turn": {"id": "child-turn", "status": "completed"}})
        await wait_for(lambda: task["state"] == "completed")
        saved = store.load_run(task["id"], result["run_id"])
        assert saved["files"] == 1 and saved["added"] == 1
        assert saved["changes"]["items"][0]["path"] == "last-child-write.py"
        assert released == [{"run_id": result["run_id"]}]
    finally:
        await runner.close()
        store.close()


async def test_stop_during_finalizing_interrupts_child_and_preserves_its_last_write(tmp_path, monkeypatch):
    store, runner, project = make_runner(tmp_path)
    try:
        result = await runner.start_task(project["id"], "hold", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        runner.thread_tasks["child"] = task["id"]
        runner.agent(task, "child", status="running")
        original_request = runner.request
        interruptions = []

        async def child_request(method, params, **kwargs):
            if params.get("threadId") == "child":
                if method == "thread/read":
                    return {"thread": {"turns": [{"id": "child-turn", "status": "inProgress"}]}}
                if method == "turn/interrupt":
                    interruptions.append(params)
                    (Path(project["path"]) / "child-interrupted.py").write_text("saved before interrupt acknowledgement\n")
                    runner.notification("turn/completed", {"threadId": "child", "turn": {"id": "child-turn", "status": "interrupted"}})
                    return {}
            return await original_request(method, params, **kwargs)

        monkeypatch.setattr(runner, "request", child_request)
        runner.notification("turn/completed", {"threadId": task["thread_id"], "turn": {"id": task["turn_id"], "status": "completed"}})
        await asyncio.sleep(.05)
        assert task["state"] == "finalizing"
        await runner.stop_task(task["id"])
        await wait_for(lambda: task["state"] == "interrupted")
        assert interruptions == [{"threadId": "child", "turnId": "child-turn"}]
        saved = store.load_run(task["id"], result["run_id"])
        assert saved["state"] == "interrupted" and saved["files"] == 1 and saved["added"] == 1
        assert runner.active_agents() == 0
    finally:
        await runner.close()
        store.close()


async def test_child_failure_queued_after_root_completion_cannot_become_success(tmp_path):
    store, runner, project = make_runner(tmp_path)
    try:
        result = await runner.start_task(project["id"], "hold", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "running")
        runner.thread_tasks["child"] = task["id"]
        runner.agent(task, "child", status="running")
        runner.notification("turn/completed", {"threadId": task["thread_id"], "turn": {"id": task["turn_id"], "status": "completed"}})
        # Both events arrive in the same read cycle, before finalization runs.
        runner.notification("turn/completed", {"threadId": "child", "turn": {"id": "child-turn", "status": "failed"}})
        await wait_for(lambda: task["state"] == "failed")
        assert store.load_run(task["id"], result["run_id"])["state"] == "failed"
    finally:
        await runner.close()
        store.close()
