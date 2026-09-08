"""Preferences and team lifecycle regressions using only local mock peers."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from dashboard.experiment import ExperimentMixin, validate_plan
from dashboard.preferences import DEFAULTS, preferences, validate_preferences
from dashboard.runner import CodexRunner
from dashboard.store import DashboardStore


def plan():
    return {"summary": "A small change and independent review", "contract": "Keep the existing greeting format.",
            "workers": [{"name": "Implementation", "task": "Write a.txt", "owned_paths": ["a.txt"],
                         "validation": "Read a.txt and verify its exact content."},
                        {"name": "Review", "task": "Review the original requirements without writes.",
                         "owned_paths": [], "validation": "Read original files and report findings."}]}


def test_partial_preferences_fill_prompts_and_preserve_user_content_after_restart(tmp_path):
    database = tmp_path / "panel.sqlite3"
    custom = "My instructions\nZachowaj moje znaki: ąę 😀.\n"
    store = DashboardStore(database)
    store.set_setting("preferences", {"main_prompt": custom, "request_retries": 2})
    first = preferences(store)
    assert first["main_prompt"] == custom
    assert first["coordinator_prompt"] == DEFAULTS["coordinator_prompt"]
    assert first["request_retries"] == 2
    first["skill_overrides"].append({"path": "fixture", "enabled": False})
    assert preferences(store)["skill_overrides"] == []
    saved = validate_preferences({"coordinator_prompt": ""}, preferences(store))
    store.set_setting("preferences", saved)
    store.close()

    reopened = DashboardStore(database)
    try:
        actual = preferences(reopened)
        assert actual["main_prompt"] == custom
        assert actual["coordinator_prompt"] == ""
        assert actual["request_retries"] == 2
    finally:
        reopened.close()


@pytest.mark.parametrize("stored", [None, [], {"main_prompt": None, "coordinator_prompt": 42}])
def test_invalid_legacy_settings_get_usable_default_prompts(tmp_path, stored):
    store = DashboardStore(tmp_path / "panel.sqlite3")
    try:
        store.set_setting("preferences", stored)
        actual = preferences(store)
        assert actual["main_prompt"] == DEFAULTS["main_prompt"]
        assert actual["coordinator_prompt"] == DEFAULTS["coordinator_prompt"]
        assert len(actual["main_prompt"]) <= 32000
        assert len(actual["coordinator_prompt"]) <= 32000
    finally:
        store.close()


@pytest.mark.parametrize("field,value", [("summary", None), ("contract", " "), ("workers", [None, {}])])
def test_malformed_plan_is_a_validation_error(field, value):
    candidate = plan()
    candidate[field] = value
    with pytest.raises(ValueError):
        validate_plan(candidate)


@pytest.mark.parametrize("path", [".", "../a.txt", "/a.txt", "C:/a.txt", "src/*.py", "src\\a.py",
                                 "src/node_modules/a.js", "src/.git/config", ".env.local",
                                 "config/key.pem", "src/\x00a.py"])
def test_invalid_or_uncopiable_owned_paths_are_rejected(path):
    candidate = plan()
    candidate["workers"][0]["owned_paths"] = [path]
    with pytest.raises(ValueError):
        validate_plan(candidate)


def test_plan_overlap_rejects_without_mutating_input():
    candidate = plan()
    candidate["workers"][0]["owned_paths"] = ["src/"]
    candidate["workers"][1]["owned_paths"] = ["SRC/main.py"]
    original = copy.deepcopy(candidate)
    with pytest.raises(ValueError, match="sam plik"):
        validate_plan(candidate)
    assert candidate == original


class MockTeam(ExperimentMixin):
    """In-process App Server protocol peer with real source copies and merge."""

    permissions = CodexRunner.permissions

    def __init__(self, directory, candidate=None):
        self.data_dir = directory
        self.candidate = candidate or plan()
        self.model_provider = "local_mock"
        self.thread_tasks = {}
        self._turn_waiters = {}
        self.requests = []
        self.routes = []
        self.threads = {}
        self.saved_states = []
        self.failure_method = None
        self.terminal = {"status": "completed"}
        self.task = {"id": "chat", "project_id": "project", "run_id": "run", "model": "fixture-model",
                     "state": "starting", "agents": [], "messages": [], "permission_mode": "yolo",
                     "preferences": copy.deepcopy(DEFAULTS), "api_ids": ["one", "two", "three"]}

    async def ensure_started(self):
        pass

    async def prepare_baseline(self, task, project):
        pass

    async def setup_route(self, task, route_id, ids, **kwargs):
        self.routes.append({"id": route_id, "providers": ids, **kwargs})
        return route_id

    def task_config(self, task, route_id):
        return {"features.multi_agent": True}

    def agent(self, task, tid, **fields):
        agent = next((entry for entry in task["agents"] if entry["id"] == tid), None)
        if agent is None:
            agent = {"id": tid}
            task["agents"].append(agent)
        agent.update(fields)
        return agent

    def touch(self, task, **kwargs):
        self.saved_states.append([lane.get("state") for lane in task.get("experiment", {}).get("lanes", [])])

    def message(self, task, item_id, role, text, *args):
        task["messages"].append({"id": item_id, "role": role, "text": text})

    def clean(self, value, limit=None):
        return str(value)[:limit]

    def finish(self, task, state):
        task["state"] = state

    def fail(self, task, text):
        task["state"] = "failed"
        self.message(task, "failed", "error", text)

    async def request(self, method, params):
        self.requests.append((method, copy.deepcopy(params)))
        if self.failure_method == method:
            raise OSError("Local mock disconnected")
        if method in {"thread/start", "thread/resume"}:
            tid = params.get("threadId", "thread-" + str(len(self.threads)))
            self.threads[tid] = params
            return {"thread": {"id": tid}}
        if method == "turn/start":
            tid = params["threadId"]

            def complete():
                lane = next(entry for entry in self.task["experiment"]["lanes"] if entry.get("thread_id") == tid)
                if params.get("outputSchema"):
                    lane["last_output"] = json.dumps(self.candidate)
                elif lane["id"] == "worker1":
                    Path(self.threads[tid]["cwd"], "a.txt").write_text("hello\n", encoding="utf-8")
                self._turn_waiters[tid].set_result(copy.deepcopy(self.terminal))

            asyncio.get_running_loop().call_soon(complete)
            return {"turn": {"id": "turn-" + tid}}
        raise AssertionError("Unexpected protocol method: " + method)


async def test_ultra_team_contract_routing_and_read_only_review_use_real_protocol_choices(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    team = MockTeam(tmp_path / "data")
    await team._run_experiment(team.task, {"path": str(source)}, "Write a greeting", "message")
    assert team.task["state"] == "completed", team.task["messages"]
    assert (source / "a.txt").read_text(encoding="utf-8") == "hello\n"
    starts = [params for method, params in team.requests if method == "thread/start"]
    turns = [params for method, params in team.requests if method == "turn/start"]
    assert len(starts) == len(turns) == 3
    assert all(turn["effort"] == "ultra" for turn in turns)
    assert starts[0]["sandbox"] == starts[2]["sandbox"] == "read-only"
    assert starts[0]["config"]["features.multi_agent"] is False
    assert starts[2]["config"]["features.multi_agent"] is False
    assert starts[1]["sandbox"] == "danger-full-access"
    assert turns[2]["sandboxPolicy"] == {"type": "readOnly"}
    assert all(team.candidate["contract"] in start["developerInstructions"] for start in starts[1:])
    assert team.candidate["workers"][0]["validation"] in turns[1]["input"][0]["text"]
    assigned = {route["id"]: route for route in team.routes if route.get("thread_id")}
    assert assigned["run-worker1"]["providers"] == ["two", "one", "three"]
    assert assigned["run-worker2"]["providers"] == ["three", "one", "two"]
    assert team._turn_waiters == {}


@pytest.mark.parametrize("failed_method", ["thread/start", "turn/start"])
async def test_failed_lane_state_is_durable_and_waiter_is_removed(tmp_path, failed_method):
    team = MockTeam(tmp_path)
    lane = {"id": "planner", "name": "Planner", "path": str(tmp_path), "api_ids": ["one"], "instructions": "Plan"}
    team.task["experiment"] = {"lanes": [lane]}
    team.failure_method = failed_method
    with pytest.raises(OSError, match="mock disconnected"):
        await team._lane(team.task, lane, "Plan", effort="ultra", read_only=True)
    assert lane["state"] == "failed"
    assert team.saved_states[-1] == ["failed"]
    assert team._turn_waiters == {}
    assert all(agent["status"] == "failed" for agent in team.task["agents"])


async def test_stop_before_lane_start_does_not_create_new_thread(tmp_path):
    team = MockTeam(tmp_path)
    lane = {"id": "worker1", "name": "Worker", "path": str(tmp_path), "api_ids": ["one"], "instructions": "Work"}
    team.task["experiment"] = {"lanes": [lane]}
    team.task["_stop_requested"] = True
    result = await team._lane(team.task, lane, "Work", effort="ultra")
    assert result["status"] == lane["state"] == "interrupted"
    assert team.requests == []
    assert team.saved_states[-1] == ["interrupted"]


async def test_missing_saved_copies_do_not_silently_repeat_work(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    team = MockTeam(tmp_path / "data")
    team.task["experiment"] = {"phase": "workers", "path": str(tmp_path / "missing"), "plan": plan(),
                               "lanes": [{"id": "planner"}, {"id": "worker1"}, {"id": "worker2"}]}
    await team._run_experiment(team.task, {"path": str(source)}, "Resume", "resume-message")
    assert team.task["state"] == "failed"
    assert "brakuje zapisanych kopii" in team.task["messages"][-1]["text"]
    assert team.requests == []
    assert not (source / "a.txt").exists()
