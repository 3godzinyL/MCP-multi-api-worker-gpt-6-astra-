import json
import sqlite3
import threading
import time

import pytest

from dashboard.store import DashboardStore


def make_store(tmp_path, **options):
    folder = tmp_path / "workspace"
    folder.mkdir(exist_ok=True)
    store = DashboardStore(tmp_path / "panel.sqlite3", **options)
    project = store.add_project(str(folder))
    return store, project


def collect(method, *args, limit=37, **kwargs):
    items, cursor = [], None
    while True:
        page = method(*args, cursor=cursor, limit=limit, **kwargs)
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return items


def start_run(task, run_id, **fields):
    task.update(run_id=run_id, state="running", started_at=time.time(), finished_at=None,
                elapsed_seconds=None, agents_count=1, api_ids=["local-mock"],
                files=0, added=0, removed=0, touched_files=0, touched_paths=[],
                changes_revision=0, scan_status="pending", usage=None,
                client_request_id="request-" + run_id, changes=[], change_history=[])
    task.update(fields)
    return task


def test_empty_chats_and_more_than_100_chats_survive_restart(tmp_path):
    store, project = make_store(tmp_path)
    ids = []
    try:
        for index in range(125):
            task = store.create_chat(project["id"], title=f"Czat {index}")
            ids.append(task["id"])
        assert store.list_runs(project["id"])["items"] == []
    finally:
        store.close()

    store = DashboardStore(tmp_path / "panel.sqlite3")
    try:
        assert len(store.tasks()) == 125
        assert {task["id"] for task in collect(store.list_chats, project["id"])} == set(ids)
        assert store.load_task(ids[0])["title"] == "Czat 0"
        assert store.load_task(ids[1])["state"] == "idle"
        assert store.load_task(ids[1])["messages"] == []
        assert "messages" not in store.list_chats(project["id"])["items"][0]
        assert store.list_chats("another-project")["items"] == []
    finally:
        store.close()


def test_long_messages_and_complete_history_are_not_trimmed(tmp_path):
    store, project = make_store(tmp_path)
    try:
        task = start_run(store.create_chat(project["id"]), "first")
        task["messages"] = [{"id": str(index), "run_id": "first", "role": "assistant", "text": str(index)}
                            for index in range(801)]
        task["messages"][0]["text"] = "a" * 90000
        store.save_task(task)
        task["messages"][-1]["text"] = "stream finished"
        store.save_task(task)
        # A caller with an abbreviated payload cannot delete the saved archive.
        store.save_task({"id": task["id"], "project_id": project["id"], "messages": task["messages"][-2:]})
        messages = collect(store.list_messages, task["id"], "first")
        assert len(messages) == 801
        assert messages[0]["text"] == "a" * 90000
        assert messages[-1]["text"] == "stream finished"
        assert len(store.tasks()[0]["messages"]) == 801
        run = store.load_run(task["id"], "first", limit=10)
        assert len(run["messages"]["items"]) == 10
        assert run["messages"]["next_cursor"] == "10"
    finally:
        store.close()


def test_new_run_resets_its_counts_and_keeps_previous_results(tmp_path):
    store, project = make_store(tmp_path)
    try:
        task = start_run(store.create_chat(project["id"]), "first")
        task["messages"] = [{"id": "same-cli-item", "run_id": "first", "role": "assistant", "text": "First result"}]
        task.update(state="completed", finished_at=task["started_at"] + 3600, elapsed_seconds=3600,
                    agents_count=2, files=1, added=8, removed=3, touched_files=1,
                    touched_paths=["main.py"], changes_revision=1, scan_status="complete",
                    changes=[{"path": "main.py", "added": 8, "removed": 3, "diff": "+new\n-old"}],
                    change_history=[{"id": "observation-1", "run_id": "first", "path": "main.py", "revision": 1}],
                    usage={"total_tokens": 150})
        store.save_task(task)
        previous = store.load_run(task["id"], "first")
        start_run(task, "second")
        task["messages"].append({"id": "same-cli-item", "run_id": "second", "role": "assistant", "text": "Second result"})
        store.save_task(task)
        assert store.load_run(task["id"], "first") == previous
        current = store.load_run(task["id"], "second")
        assert (current["files"], current["added"], current["removed"]) == (0, 0, 0)
        assert current["usage"] is None and current["finished_at"] is None
        assert [item["text"] for item in current["messages"]["items"]] == ["Second result"]
        assert len(store.load_task(task["id"])["messages"]) == 2
        assert [run["id"] for run in collect(store.list_runs, project["id"], task["id"], limit=1)] == ["second", "first"]
        assert store.load_run("another-chat", "first") is None
    finally:
        store.close()


def test_reverted_change_keeps_observation_without_counting_poll_again(tmp_path):
    store, project = make_store(tmp_path)
    try:
        task = start_run(store.create_chat(project["id"]), "first")
        task.update(files=1, added=2, touched_files=1, touched_paths=["file.py"], changes_revision=1,
                    changes=[{"path": "file.py", "added": 2, "removed": 0}],
                    change_history=[{"id": "edit", "run_id": "first", "path": "file.py", "revision": 1}])
        store.save_task(task)
        store.save_task(task)
        assert len(store.list_change_history(task["id"], "first")["items"]) == 1
        task.update(files=0, added=0, changes=[], changes_revision=2)
        task["change_history"].append({"id": "revert", "run_id": "first", "path": "file.py", "revision": 2, "reverted": True})
        store.save_task(task)
        task_id = task["id"]
    finally:
        store.close()
    store = DashboardStore(tmp_path / "panel.sqlite3")
    try:
        run = store.load_run(task_id, "first")
        assert run["files"] == 0 and run["added"] == 0 and run["touched_files"] == 1
        assert run["changes"]["items"] == []
        assert [item["id"] for item in run["change_history"]["items"]] == ["edit", "revert"]
    finally:
        store.close()


def test_legacy_payloads_migrate_without_inventing_measurements(tmp_path):
    path = tmp_path / "panel.sqlite3"
    original = {"id": "old-chat", "project_id": "old-project", "title": "Zachowaj", "created": 7, "updated": 11,
                "state": "completed", "thread_id": "old-thread", "messages": [
                    {"id": str(index), "role": "assistant", "text": f"Original {index}"} for index in range(700)],
                "changes": [{"path": "old.py", "added": 4, "removed": 2, "diff": "+original\n-old"}],
                "files": 1, "added": 4, "removed": 2, "tokens": {"totalTokens": 30}, "custom": {"keep": True}}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,project_id TEXT,updated REAL,payload TEXT)")
        db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (original["id"], original["project_id"], 11, json.dumps(original)))
        db.execute("CREATE TABLE panel_settings(key TEXT PRIMARY KEY,payload TEXT)")
        db.execute("INSERT INTO panel_settings VALUES('preferences',?)", (json.dumps({"locale": "pl"}),))
    for _ in range(2):
        store = DashboardStore(path)
        try:
            task = store.load_task("old-chat")
            assert task["custom"] == original["custom"]
            assert task["thread_id"] == "old-thread"
            assert task["changes"] == original["changes"]
            assert [message["text"] for message in task["messages"]] == [message["text"] for message in original["messages"]]
            runs = store.list_runs("old-project")["items"]
            assert len(runs) == 1
            assert all(runs[0][key] is None for key in ("started_at", "finished_at", "elapsed_seconds", "usage", "agents_count", "touched_files"))
            assert (runs[0]["files"], runs[0]["added"], runs[0]["removed"]) == (1, 4, 2)
            assert len(collect(store.list_messages, "old-chat", task["run_id"])) == 700
            assert store.list_changes("old-chat", task["run_id"])["items"] == original["changes"]
            assert store.list_change_history("old-chat", task["run_id"])["items"][0]["path"] == "old.py"
            assert store.get_setting("preferences") == {"locale": "pl"}
            assert store.db.execute("PRAGMA user_version").fetchone()[0] == 3
        finally:
            store.close()


def test_request_id_uniqueness_is_atomic_with_task_and_details(tmp_path):
    store, project = make_store(tmp_path)
    try:
        first = start_run(store.create_chat(project["id"]), "first", client_request_id="same-request")
        store.save_task(first)
        assert store.find_run_by_request("same-request")["task_id"] == first["id"]
        second = store.create_chat(project["id"])
        start_run(second, "second", client_request_id="same-request")
        second["messages"] = [{"id": "uncommitted", "run_id": "second", "text": "Do not commit"}]
        with pytest.raises(sqlite3.IntegrityError):
            store.save_task(second)
        assert store.load_task(second["id"])["state"] == "idle"
        assert store.list_messages(second["id"])["items"] == []
        assert store.load_run(second["id"], "second") is None
        assert len(store.list_runs(project["id"])["items"]) == 1
        with pytest.raises(ValueError, match="identyfikatora"):
            store.save_run({"id": "first", "client_request_id": "changed"})
    finally:
        store.close()


def test_legacy_column_identity_and_duplicate_message_ids_are_preserved(tmp_path):
    path = tmp_path / "panel.sqlite3"
    messages = [{"id": "reused", "text": "Original"}, {"id": "reused", "text": "Continuation"},
                {"text": "No ID"}, {"text": "No ID"}, "legacy text"]
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,project_id TEXT,updated REAL,payload TEXT)")
        for index in range(125):
            payload = {"state": "completed", "files": 0, "messages": messages}
            db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (f"task-{index}", "project", index, json.dumps(payload)))
    store = DashboardStore(path)
    try:
        assert len(store.tasks()) == 125
        assert len(collect(store.list_runs, "project")) == 125
        for task in store.tasks():
            assert task["project_id"] == "project"
            assert len(task["messages"]) == 5
            assert task["messages"][0]["text"] == "Original"
            assert task["messages"][1]["text"] == "Continuation"
            assert task["messages"][-1] == "legacy text"
            store.save_task(task)
            assert len(store.load_task(task["id"])["messages"]) == 5
            assert len(store.list_messages(task["id"], task["run_id"])["items"]) == 5
    finally:
        store.close()


def test_busy_writer_is_retried_then_history_commits(tmp_path):
    store, project = make_store(tmp_path, busy_timeout_ms=25, lock_retries=5)
    locked = threading.Event()
    errors = []

    def other_writer():
        try:
            with sqlite3.connect(tmp_path / "panel.sqlite3") as connection:
                connection.execute("BEGIN IMMEDIATE")
                locked.set()
                time.sleep(0.16)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=other_writer)
    thread.start()
    try:
        assert locked.wait(2)
        task = store.create_chat(project["id"], "Po blokadzie")
        assert store.load_task(task["id"])["title"] == "Po blokadzie"
    finally:
        thread.join(2)
        store.close()
    assert not errors and not thread.is_alive()


def test_lock_timeout_does_not_leave_open_or_partial_transaction(tmp_path):
    store, project = make_store(tmp_path, busy_timeout_ms=5, lock_retries=0)
    try:
        with sqlite3.connect(tmp_path / "panel.sqlite3") as other:
            other.execute("BEGIN IMMEDIATE")
            with pytest.raises(sqlite3.OperationalError):
                store.create_chat(project["id"], "Blocked")
            assert not store.db.in_transaction
        assert store.tasks() == []
        assert store.create_chat(project["id"], "Recovered")["state"] == "idle"
    finally:
        store.close()


def test_api_usage_and_activity_are_persistent_and_paginated(tmp_path):
    store, project = make_store(tmp_path)
    try:
        task = start_run(store.create_chat(project["id"]), "first")
        store.save_task(task)
        events = [{"id": index, "time": 100 + index, "provider": "local-mock", "kind": "completed"} for index in range(600)]
        store.save_api_events(task["id"], "first", events)
        store.save_api_events(task["id"], "first", events)
        store.save_run({"id": "first", "usage": {"total_tokens": 123, "unreported": 1}})
        assert len(collect(store.list_api_events, task["id"], "first")) == 600
        assert store.load_run(task["id"], "first")["usage"]["total_tokens"] == 123
        for index in range(605):
            store.add_activity(str(index))
        assert len(store.activity()) == 30
        assert len(collect(store.list_activity)) == 605
    finally:
        store.close()


@pytest.mark.parametrize("cursor,limit", [("bad", 5), ("-1", 5), (True, 5), (None, 0), (None, 501), (None, True)])
def test_history_pagination_rejects_invalid_values(tmp_path, cursor, limit):
    store, project = make_store(tmp_path)
    try:
        with pytest.raises(ValueError):
            store.list_runs(project["id"], cursor=cursor, limit=limit)
    finally:
        store.close()
