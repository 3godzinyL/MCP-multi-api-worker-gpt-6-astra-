import asyncio
import json
import logging

import httpx
import pytest
from starlette.requests import ClientDisconnect

from proxy.sse import ResponseStreamObserver
from tests.test_proxy import HEADERS, client_for, live_server, response, settings


def event(kind, **fields):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, **fields}) + "\n\n").encode()


CREATED = event("response.created", response={"id": "resp_first", "status": "in_progress"})
DELTA = event("response.output_text.delta", delta="Some generated text")
COMPLETE = event("response.completed", response={"id": "resp_ok", "status": "completed", "output": []})
RATE_LIMIT = event("error", code="rate_limit_exceeded", message="Tokens per minute exceeded. SECRET_DETAIL")


@pytest.mark.parametrize("frame", [
    RATE_LIMIT,
    event("response.failed", response={"error": {"code": "rate_limit_exceeded", "message": "TPM"}}),
    event("error", error={"code": "429", "message": "Quota exceeded"}),
    event("error", code="TooManyRequests", message="Unavailable"),
    event("error", code="insufficient_quota", message="Quota exceeded"),
    event("error", message="Your tokens per minute limit was exceeded"),
])
def test_recognizes_rate_limit_error_forms(frame):
    failure = ResponseStreamObserver().feed(frame)
    assert failure.status == 429
    assert failure.reason == "sse_rate_limit"


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
def test_sse_fragmentation_and_utf8(ending):
    observer = ResponseStreamObserver()
    frame = event("error", code="rate_limit_exceeded", message="Przekroczono limit żądań").replace(b"\n", ending)
    found = []
    for byte in frame + b" ":  # The extra byte disambiguates a final bare CR.
        failure = observer.feed(bytes([byte]))
        if failure:
            found.append(failure)
    assert len(found) == 1 and found[0].status == 429


def test_observer_bounds_memory_and_does_not_treat_model_output_as_errors():
    observer = ResponseStreamObserver(max_event_bytes=100)
    for _ in range(30):
        assert observer.feed(b"x" * 90) is None
        assert len(observer.buffer) <= 100
        assert len(observer.data) <= 100
    observer.feed(b"\n\n")
    assert observer.feed(event("response.output_text.delta", delta="rate_limit_exceeded quota exceeded")) is None
    observer.feed(COMPLETE)
    assert observer.terminal


async def test_rate_limit_in_first_sse_chunk_fails_over_within_same_request():
    calls = []
    async def handler(request):
        calls.append(request.url.host)
        return response(chunks=[RATE_LIMIT] if len(calls) == 1 else [CREATED, COMPLETE],
                        headers={"content-type": "text/event-stream", "retry-after": "17"})
    async with client_for(handler, configuration=settings(reconnect_failover=True)) as (client, runtime):
        result = await client.post("/v1/responses", json={"stream": True})
        assert result.status_code == 200
        assert result.content == CREATED + COMPLETE
        assert calls == ["p1.example", "p2.example"]
        assert 16 <= runtime.status()["providers"][0]["cooldown_remaining_seconds"] <= 17


@pytest.mark.parametrize("failure", [
    RATE_LIMIT,
    event("response.failed", response={"error": {"code": "rate_limit_exceeded"}}),
    event("error", code="server_error", message="Internal problem"),
    httpx.ReadTimeout("SECRET_DETAIL"),
    httpx.RemoteProtocolError("SECRET_DETAIL"),
    None,  # A clean TCP EOF without response.completed must also trigger reconnect.
])
async def test_next_request_after_stream_failure_uses_second_provider(failure, caplog):
    calls, streams = [], []
    async def handler(request):
        calls.append(request.url.host)
        chunks = [CREATED, DELTA] + ([] if failure is None else [failure]) if len(calls) == 1 else [CREATED, COMPLETE]
        result = response(chunks=chunks, headers={"content-type": "text/event-stream"})
        streams.append(result.stream)
        return result
    async with client_for(handler, configuration=settings(reconnect_failover=True)) as (client, runtime):
        with caplog.at_level(logging.INFO, logger="responses_proxy"):
            first = await client.post("/v1/responses", json={"stream": True})
            assert first.content == CREATED + DELTA
            assert b"response.completed" not in first.content
            assert calls == ["p1.example"]  # No splicing of a second response into this stream.
            second = await client.post("/v1/responses", json={"stream": True})
        assert second.content == CREATED + COMPLETE
        assert calls == ["p1.example", "p2.example"]
        assert runtime.stats["reconnect_failovers"] == 1
        assert runtime.stats["in_flight"] == 0
        assert all(stream.closed for stream in streams)
        assert "SECRET_DETAIL" not in caplog.text
        assert "event=reconnect_failover from=provider1 to=provider2" in caplog.text


async def test_two_stream_quota_failures_reconnect_to_third_provider():
    calls = []
    async def handler(request):
        calls.append(request.url.host)
        return response(chunks=[CREATED, RATE_LIMIT if len(calls) < 3 else COMPLETE],
                        headers={"content-type": "text/event-stream"})
    async with client_for(handler, configuration=settings(reconnect_failover=True)) as (client, runtime):
        for _ in range(2):
            result = await client.post("/v1/responses", json={"stream": True})
            assert b"response.completed" not in result.content
        result = await client.post("/v1/responses", json={"stream": True})
        assert b"response.completed" in result.content
        assert calls == ["p1.example", "p2.example", "p3.example"]
        assert runtime.stats["reconnect_failovers"] == 2


async def test_stream_retry_after_json_and_primary_recovery():
    now, calls = [100.0], []
    async def handler(request):
        calls.append(request.url.host)
        chunks = [CREATED, event("error", code="rate_limit_exceeded", retry_after=49)] if len(calls) == 1 else [CREATED, COMPLETE]
        return response(chunks=chunks, headers={"content-type": "text/event-stream"})
    async with client_for(handler, configuration=settings(reconnect_failover=True), monotonic=lambda: now[0]) as (client, runtime):
        await client.post("/v1/responses", json={"stream": True})
        assert runtime.states[0].cooldown_until == 149
        await client.post("/v1/responses", json={"stream": True})
        now[0] = 150
        await client.post("/v1/responses", json={"stream": True})
        assert calls == ["p1.example", "p2.example", "p1.example"]


async def test_zero_retry_after_still_routes_reconnect_to_second_provider():
    calls = []
    async def handler(request):
        calls.append(request.url.host)
        return response(chunks=[CREATED, RATE_LIMIT if len(calls) == 1 else COMPLETE],
                        headers={"content-type": "text/event-stream", "retry-after": "0"})
    async with client_for(handler, configuration=settings(reconnect_failover=True), monotonic=lambda: 100) as (client, runtime):
        await client.post("/v1/responses", json={"stream": True})
        assert runtime.states[0].cooldown_until == 130
        await client.post("/v1/responses", json={"stream": True})
        assert calls == ["p1.example", "p2.example"]


@pytest.mark.parametrize("http_status,chunks", [
    (400, [b'{"error":{"code":"rate_limit_exceeded"}}']),
    (200, [CREATED, event("error", code="invalid_request_error", message="Invalid quota parameter")]),
    (200, [CREATED, event("response.incomplete", response={"incomplete_details": {"reason": "max_output_tokens"}})]),
    (200, [CREATED, COMPLETE]),
    (200, [CREATED, COMPLETE, httpx.ReadError("late TCP error after valid terminal event")]),
])
async def test_reconnect_mode_keeps_400_and_normal_completions_on_primary(http_status, chunks):
    calls = []
    async def handler(request):
        calls.append(request.url.host)
        return response(http_status, chunks=chunks, headers={"content-type": "text/event-stream" if http_status == 200 else "application/json"})
    async with client_for(handler, configuration=settings(reconnect_failover=True)) as (client, runtime):
        for _ in range(2):
            result = await client.post("/v1/responses", json={"stream": True})
            assert result.status_code == http_status
        assert calls == ["p1.example", "p1.example"]
        assert runtime.states[0].cooldown_events == 0
        assert runtime.stats["reconnect_failovers"] == 0


@pytest.mark.parametrize("reconnect", [False, True])
@pytest.mark.parametrize("ending,reason", [
    (COMPLETE, "response.completed"),
    (event("response.incomplete", response={"status": "incomplete"}), "response.incomplete"),
    (event("error", code="invalid_request_error"), "error"),
    (b"data: [DONE]\n\n", "done"),
], ids=["completed", "incomplete", "error", "done"])
@pytest.mark.parametrize("fragmented", [False, True])
async def test_client_disconnect_after_terminal_event_is_completed(reconnect, ending, reason, fragmented, caplog):
    closed = asyncio.Event()

    class OpenEndedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in ([CREATED, ending[:-1], ending[-1:]] if fragmented else [ending]):
                yield chunk
            # The client has a terminal event, but HTTP EOF has not arrived.
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async def handler(_):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=OpenEndedStream())

    expected = (CREATED if fragmented else b"") + ending
    with caplog.at_level(logging.INFO, logger="responses_proxy"):
        async with live_server(handler, configuration=settings(reconnect_failover=reconnect)) as (url, runtime):
            async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
                async with client.stream("POST", url + "/v1/responses", headers=HEADERS, json={"stream": True}) as result:
                    received = bytearray()
                    async for chunk in result.aiter_raw():
                        received.extend(chunk)
                        if bytes(received) == expected:
                            break
                    assert bytes(received) == expected
                await asyncio.wait_for(closed.wait(), 2)
            assert runtime.stats["responses_completed"] == 1
            assert runtime.states[0].completed == 1
            assert runtime.stats["client_disconnects"] == 0
            assert runtime.stats["stream_interruptions"] == 0
            assert runtime.stats["in_flight"] == 0
            assert runtime.states[0].cooldown_events == 0
    assert f"complete=True outcome=completed reason={reason}" in caplog.text


async def test_terminal_event_rejected_by_downstream_is_not_completed(caplog):
    async with client_for(lambda _: response(chunks=[COMPLETE], headers={"content-type": "text/event-stream"}),
                          configuration=settings(reconnect_failover=True)) as (_, runtime):
        relay = await runtime.forward({"stream": True}, "", "disconnect-test", {})

        async def send(message):
            if message["type"] == "http.response.body":
                raise ConnectionResetError("DOWNSTREAM_SECRET_DETAIL")

        async def receive():
            raise AssertionError("ASGI 2.4 uses send errors to detect disconnects")

        with caplog.at_level(logging.INFO, logger="responses_proxy"), pytest.raises(ClientDisconnect):
            await relay({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert runtime.stats["responses_completed"] == 0
        assert runtime.stats["client_disconnects"] == 1
        assert runtime.stats["in_flight"] == 0
        assert runtime.states[0].cooldown_events == 0
        assert relay.upstream.is_closed
    assert "complete=False outcome=client_disconnected reason=client_disconnect" in caplog.text
    assert "DOWNSTREAM_SECRET_DETAIL" not in caplog.text
