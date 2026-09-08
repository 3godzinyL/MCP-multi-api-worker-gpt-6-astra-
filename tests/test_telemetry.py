import json

import httpx
import pytest

from proxy.sse import ResponseStreamObserver
from proxy.telemetry import TelemetryStore, normalize_usage
from tests.test_proxy import client_for, response, settings

USAGE = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
         "input_tokens_details": {"cached_tokens": 80}, "output_tokens_details": {"reasoning_tokens": 20}}


def test_real_usage_subsets_and_invalid_values():
    usage = normalize_usage(USAGE)
    assert usage == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150, "cached_tokens": 80, "reasoning_tokens": 20}
    assert normalize_usage({"input_tokens": True, "output_tokens": 3}) is None
    assert normalize_usage({"input_tokens": 2}) is None
    assert normalize_usage({"input_tokens": -1, "output_tokens": 3}) is None
    assert normalize_usage({"input_tokens": 5, "output_tokens": 4})["total_tokens"] == 9
    assert normalize_usage({"input_tokens": 5, "output_tokens": 4, "total_tokens": 1})["total_tokens"] == 9
    assert normalize_usage({"input_tokens": 5, "output_tokens": 4})["cached_tokens"] is None


def test_fragmented_sse_terminal_usage_is_recorded_once():
    event = {"type": "response.completed", "response": {"usage": USAGE, "output": [{"text": "not-persisted"}]}}
    raw = ("event: response.completed\r\ndata: " + json.dumps(event) + "\r\n\r\n").encode()
    observer = ResponseStreamObserver()
    for index in range(0, len(raw), 7):
        assert observer.feed(raw[index:index+7]) is None
    assert observer.terminal
    assert observer.usage["total_tokens"] == 150
    observer.feed(b'data: {"type":"response.completed","response":{"usage":{"input_tokens":999,"output_tokens":999}}}\n\n')
    assert observer.usage["total_tokens"] == 150


def test_persistence_dedup_unknown_and_stale_attempt(tmp_path):
    path = tmp_path / "telemetry.sqlite3"
    writer, reader = TelemetryStore(path), TelemetryStore(path)
    attempt = writer.begin("provider2", "one")
    writer.finish(attempt, outcome="completed", status=200, usage=normalize_usage(USAGE))
    writer.finish(attempt, outcome="completed", status=200, usage=normalize_usage(USAGE))
    missing = writer.begin("provider1", "two")
    writer.finish(missing, outcome="failed", status=429)
    writer.begin("provider3", "three")
    writer.recover_interrupted()
    state = reader.snapshot()
    assert state["metrics"]["generations"] == 1
    assert state["metrics"]["total_tokens"] == 150
    assert state["metrics"]["unreported"] == 2
    assert sum(b["tokens"] for b in state["timeline"]) == 150
    writer.close()
    assert reader.snapshot()["metrics"]["cached_tokens"] == 80
    reader.close()


@pytest.mark.parametrize("streaming", [False, True])
async def test_relay_preserves_bytes_and_records_only_safe_usage(tmp_path, streaming):
    store = TelemetryStore(tmp_path / "usage.sqlite3")
    value = {"id": "resp_test", "output": [{"content": "private-response-text"}], "usage": USAGE}
    raw = (("data: " + json.dumps({"type": "response.completed", "response": value}) + "\n\n") if streaming else json.dumps(value)).encode()
    async def handle(request):
        return response(chunks=[raw[:31], raw[31:]], headers={"content-type": "text/event-stream" if streaming else "application/json"})
    async with client_for(handle, telemetry=store) as (client, runtime):
        result = await client.post("/v1/responses", json={"input": "private-prompt-text", "stream": streaming})
        assert result.content == raw
        assert runtime.status()["telemetry_enabled"]
    data = store.snapshot()
    assert data["metrics"]["total_tokens"] == 150
    assert data["metrics"]["generations"] == 1
    rows = json.dumps([tuple(row) for row in store.db.execute("SELECT * FROM api_attempts")])
    assert "private" not in rows
    store.close()


async def test_failover_usage_not_duplicated_by_preflight(tmp_path):
    store = TelemetryStore(tmp_path / "usage.sqlite3")
    completed = ('data: ' + json.dumps({"type": "response.completed", "response": {"usage": USAGE}}) + '\n\n').encode()
    calls = []
    async def handle(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            return response(chunks=[b'data: {"type":"error","code":"rate_limit_exceeded"}\n\n'], headers={"content-type": "text/event-stream"})
        return response(chunks=[completed, completed], headers={"content-type": "text/event-stream"})
    async with client_for(handle, configuration=settings(reconnect_failover=True, rotate_on_failure=True), telemetry=store) as (client, runtime):
        result = await client.post("/v1/responses", json={"stream": True, "input": "x"})
        assert result.content == completed + completed
        assert len(calls) == 2
        assert runtime.stats["failovers"] == 1
    totals = store.snapshot()["metrics"]
    assert totals["total_tokens"] == 150 and totals["attempts"] == 2 and totals["generations"] == 1
    store.close()


async def test_oversized_json_is_forwarded_without_inventing_usage(tmp_path):
    store = TelemetryStore(tmp_path / "usage.sqlite3")
    raw = json.dumps({"output": "x" * (2 * 1024 * 1024), "usage": USAGE}).encode()
    async def handle(request):
        return response(chunks=[raw])
    async with client_for(handle, telemetry=store) as (client, _):
        result = await client.post("/v1/responses", json={"input": "x"})
        assert result.content == raw
    assert store.snapshot()["metrics"]["total_tokens"] is None
    assert store.snapshot()["metrics"]["unreported"] == 1
    store.close()


async def test_accounting_failure_does_not_break_responses():
    class Unavailable:
        def begin(self, *args):
            raise OSError("disk unavailable")
        def finish(self, *args, **kwargs):
            raise OSError("disk unavailable")
    async def handle(request):
        return response()
    async with client_for(handle, telemetry=Unavailable()) as (client, _):
        assert (await client.post("/v1/responses", json={})).status_code == 200


def test_run_accounting_and_events_survive_reload_without_truncation(tmp_path):
    path = tmp_path / "telemetry.sqlite3"
    store = TelemetryStore(path)
    context = {"project_id": "project", "task_id": "chat", "run_id": "run-a",
               "thread_id": "thread-main", "role": "main", "route_id": "route-main"}
    attempt = store.begin("provider1", "request-a", **context)
    store.finish(attempt, status=200, outcome="completed", usage=normalize_usage(USAGE))
    store.finish(attempt, status=200, outcome="completed", usage=normalize_usage(USAGE))
    for index in range(2010):
        store.event("rotation", "provider1", request_id=str(index), **context)
    store.begin("provider2", "request-b", run_id="run-b", task_id="chat", role="auxiliary")
    store.close()
    reader = TelemetryStore(path)
    try:
        usage = reader.run_usage("run-a")
        assert usage["attempts"] == 1 and usage["total_tokens"] == 150
        assert reader.run_usage("run-b")["total_tokens"] is None
        page = reader.list_events("run-a", limit=200)
        events = list(page["items"])
        while page["next_cursor"]:
            page = reader.list_events("run-a", cursor=page["next_cursor"], limit=200)
            events.extend(page["items"])
        assert len(events) == 2012
        assert events[0]["kind"] == "attempt" and events[1]["kind"] == "completed"
        assert all(event["role"] == "main" and event["thread_id"] == "thread-main" for event in events)
        assert len(reader.snapshot()["events"]) == 50
        with pytest.raises(ValueError):
            reader.list_events("run-a", cursor="-1")
    finally:
        reader.close()
