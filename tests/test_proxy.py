import asyncio
import gzip
import json
import logging
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest
import uvicorn

from proxy.config import Provider, Settings
from proxy.server import Runtime, create_app, retry_after_seconds

TOKEN = "local-test-token"
KEYS = {f"provider{i}": f"secret-key-{i}" for i in range(1, 4)}
HEADERS = {"authorization": "Bearer " + TOKEN}


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def aclose(self):
        self.closed = True


def response(status=200, *, chunks=None, headers=None):
    return httpx.Response(status, headers={"content-type": "application/json", **(headers or {})},
                          stream=BytesStream(*(chunks if chunks is not None else [b'{"id":"resp_test","output":[]}'])))


def settings(**kwargs):
    return Settings(providers=tuple(Provider(
        id=f"provider{i}", base_url=f"https://p{i}.example/openai/v1", deployment=f"deployment-{i}",
        api_key_env=f"TEST_KEY_{i}", cooldown_seconds=30,
    ) for i in range(1, 4)), **kwargs)


@asynccontextmanager
async def client_for(handler, *, configuration=None, **options):
    app = create_app(configuration or settings(), transport=httpx.MockTransport(handler), secrets=KEYS, token=TOKEN, **options)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4000", headers=HEADERS) as client:
            yield client, app.state.runtime


@pytest.mark.parametrize("statuses,expected", [
    ([429, 200, 200], ["p1.example", "p2.example"]),
    ([429, 429, 200], ["p1.example", "p2.example", "p3.example"]),
    ([200, 200, 200], ["p1.example"]),
])
async def test_requested_failover_scenarios(statuses, expected):
    calls, streams = [], []
    async def handle(request):
        calls.append(request.url.host)
        i = int(request.url.host[1]) - 1
        result = response(statuses[i], headers={"retry-after": "20"})
        streams.append(result.stream)
        return result
    async with client_for(handle) as (client, runtime):
        result = await client.post("/v1/responses", json={"model": "public", "input": "test"})
        assert result.status_code == 200
        assert result.json()["id"] == "resp_test"
        assert calls == expected
        assert runtime.stats["failovers"] == len(expected) - 1
        assert runtime.stats["in_flight"] == 0
        assert all(s.closed for s in streams)
        assert all(s.reads == 0 for s in streams[:-1])


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 501, 302])
async def test_nonretryable_status_is_relayed_without_failover(status):
    calls = []
    async def handle(request):
        calls.append(request.url.host)
        return response(status, chunks=[b'{"error":{"code":"unchanged"}}'])
    async with client_for(handle) as (client, runtime):
        result = await client.post("/v1/responses", json={"input": "test"})
        assert result.status_code == status
        assert result.json()["error"]["code"] == "unchanged"
        assert calls == ["p1.example"]
        assert runtime.stats["failovers"] == 0


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
async def test_transient_status_fails_over(status):
    calls = []
    async def handle(request):
        calls.append(request.url.host)
        return response(status if len(calls) == 1 else 200)
    async with client_for(handle) as (client, _):
        assert (await client.post("/v1/responses", json={})).status_code == 200
        assert calls == ["p1.example", "p2.example"]


@pytest.mark.parametrize("exception", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
async def test_transport_failure_before_headers_fails_over(exception):
    calls = []
    async def handle(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            raise exception("sensitive-upstream-detail")
        return response()
    async with client_for(handle) as (client, runtime):
        assert (await client.post("/v1/responses", json={})).status_code == 200
        assert calls == ["p1.example", "p2.example"]
        assert runtime.states[0].transport_errors == 1


async def test_timeout_before_first_stream_byte_fails_over_and_closes():
    calls, streams = [], []
    async def handle(request):
        calls.append(request.url.host)
        result = response(chunks=[httpx.ReadTimeout("private")] if len(calls) == 1 else [b"data: ok\n\n"],
                          headers={"content-type": "text/event-stream"})
        streams.append(result.stream)
        return result
    async with client_for(handle) as (client, runtime):
        result = await client.post("/v1/responses", json={"stream": True})
        assert result.content == b"data: ok\n\n"
        assert calls == ["p1.example", "p2.example"]
        assert all(stream.closed for stream in streams)
        assert runtime.stats["in_flight"] == 0


async def test_interrupted_stream_never_retries_and_reports_error(caplog):
    calls = []
    first = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"first"}\n\n'
    async def handle(request):
        calls.append(request.url.host)
        return response(chunks=[first, httpx.ReadTimeout("DO_NOT_LOG_KEY_OR_ENDPOINT")], headers={"content-type": "text/event-stream"})
    async with client_for(handle) as (client, runtime):
        with caplog.at_level(logging.INFO, logger="responses_proxy"):
            result = await client.post("/v1/responses", json={"stream": True})
        assert result.content.startswith(first)
        assert b'"code": "upstream_stream_interrupted"' in result.content
        assert calls == ["p1.example"]
        assert runtime.stats["stream_interruptions"] == 1
        assert runtime.stats["responses_completed"] == 0
        assert runtime.stats["in_flight"] == 0
        assert "DO_NOT_LOG_KEY_OR_ENDPOINT" not in caplog.text
        assert "retry=false" in caplog.text


async def test_cooldown_skips_then_returns_to_primary():
    now, calls = [100.0], []
    async def handle(request):
        calls.append(request.url.host)
        return response(429 if len(calls) == 1 else 200, headers={"retry-after": "7"})
    async with client_for(handle, monotonic=lambda: now[0]) as (client, runtime):
        assert (await client.post("/v1/responses", json={})).status_code == 200
        assert runtime.states[0].cooldown_until == 107
        await client.post("/v1/responses", json={})
        assert calls == ["p1.example", "p2.example", "p2.example"]
        now[0] = 107.1
        await client.post("/v1/responses", json={})
        assert calls[-1] == "p1.example"


@pytest.mark.parametrize("headers,expected", [
    ({"retry-after": "15"}, 15),
    ({"retry-after": "0"}, 0),
    ({"retry-after": "1.5"}, 1.5),
    ({"retry-after": "invalid"}, 30),
    ({"retry-after": "-1"}, 30),
    ({"retry-after": "NaN"}, 30),
    ({"retry-after": "Infinity"}, 30),
    ({}, 30),
    ({"x-ms-retry-after-ms": "2500"}, 2.5),
    ({"retry-after-ms": "3500"}, 3.5),
    ({"retry-after": "20", "retry-after-ms": "1"}, 20),
])
def test_retry_after_values(headers, expected):
    assert retry_after_seconds(headers, 30) == expected


def test_retry_after_http_date():
    date = format_datetime(datetime.fromtimestamp(1100, timezone.utc), usegmt=True)
    assert retry_after_seconds({"retry-after": date}, 30, now=1000) == 100
    assert retry_after_seconds({"retry-after": date}, 30, now=1200) == 0


async def test_all_cooling_returns_429_without_calling_upstream_again():
    calls = []
    async def handle(request):
        calls.append(request.url.host)
        return response(429, headers={"retry-after": "20"})
    async with client_for(handle) as (client, runtime):
        first = await client.post("/v1/responses", json={})
        second = await client.post("/v1/responses", json={})
        assert first.status_code == second.status_code == 429
        assert 1 <= int(second.headers["retry-after"]) <= 20
        assert len(calls) == 3
        assert runtime.stats["exhausted"] == 2


@pytest.mark.parametrize("status", [500, 503])
async def test_all_failed_returns_last_status(status):
    async with client_for(lambda _: response(status)) as (client, runtime):
        result = await client.post("/v1/responses", json={})
        assert result.status_code == status
        assert result.json()["error"]["code"] == "upstream_unavailable"
        assert runtime.stats["attempts"] == 3


@pytest.mark.parametrize("path", ["/v1/responses", "/v1/responses/compact"])
async def test_responses_payload_deployment_auth_and_query_preserved(path, caplog):
    recorded = []
    configuration = settings()
    configuration = replace(configuration, providers=(
        replace(configuration.providers[0], auth_type="bearer", api_version="2025-04-01-preview"),
        *configuration.providers[1:]))
    payload = {"model": "client-alias", "input": [{"type": "function_call_output", "call_id": "call_a", "output": "result"}],
               "tools": [{"type": "function", "name": "test", "parameters": {"type": "object"}}],
               "store": False, "reasoning": {"effort": "high"}, "include": ["reasoning.encrypted_content"]}
    async def handle(request):
        recorded.append(request)
        return response()
    async with client_for(handle, configuration=configuration) as (client, runtime):
        with caplog.at_level(logging.INFO):
            result = await client.post(path, json=payload, headers={"api-key": "client-should-not-forward", "openai-beta": "responses=v1"})
        assert result.status_code == 200
        request = recorded[0]
        assert request.url.path == "/openai" + path
        assert request.url.params["api-version"] == "2025-04-01-preview"
        assert json.loads(request.content) == {**payload, "model": "deployment-1"}
        assert request.headers["authorization"] == "Bearer " + KEYS["provider1"]
        assert "api-key" not in request.headers
        assert request.headers["openai-beta"] == "responses=v1"
        assert TOKEN not in str(request.headers)
        status_json = json.dumps(runtime.status())
        for key in KEYS.values():
            assert key not in status_json + caplog.text
        assert "p1.example" not in status_json + caplog.text


async def test_provider_keys_remain_separate_during_failover():
    seen = []
    async def handle(request):
        seen.append((request.url.host, request.headers["api-key"], json.loads(request.content)["model"]))
        assert "authorization" not in request.headers
        return response(429 if len(seen) < 3 else 200)
    async with client_for(handle) as (client, _):
        await client.post("/v1/responses", json={"model": "alias"})
    assert seen == [(f"p{i}.example", f"secret-key-{i}", f"deployment-{i}") for i in range(1, 4)]


async def test_health_status_auth_and_validation():
    calls = []
    async def handle(request):
        calls.append(request)
        return response()
    async with client_for(handle, configuration=settings(max_request_bytes=50)) as (client, _):
        assert (await client.get("/health", headers={"authorization": ""})).status_code == 200
        assert (await client.get("/status", headers={"authorization": ""})).status_code == 401
        assert (await client.post("/v1/responses", json={}, headers={"authorization": "bad"})).status_code == 401
        assert (await client.post("/v1/responses", content=b"{" )).status_code == 400
        assert (await client.post("/v1/responses", json=[])).status_code == 400
        assert (await client.post("/v1/responses", json={"stream": "true"})).status_code == 400
        assert (await client.post("/v1/responses", content=b'{"x":NaN}')).status_code == 400
        assert (await client.post("/v1/responses", content=b" " * 51)).status_code == 413
        assert (await client.post("/v1/responses", content=b"x", headers={"content-encoding": "zstd"})).status_code == 415
        assert (await client.get("/status")).json()["stats"]["attempts"] == 0
        catalog = (await client.get("/v1/models")).json()
        assert catalog["data"][0]["id"] == "gpt-6-astra"
        assert catalog["models"] == []  # Do not override Codex's agent instructions.
        assert calls == []


async def test_sse_bytes_and_headers_passthrough():
    chunks = [b"event: response.created\ndata: {", b'"type":"response.created"}\n\n', b'data: [DONE]\n\n']
    async def handle(_):
        return response(chunks=chunks, headers={"content-type": "text/event-stream", "server": "private-server",
                                              "x-provider": "private", "content-length": "900", "connection": "keep-alive"})
    async with client_for(handle) as (client, _):
        result = await client.post("/v1/responses", json={"stream": True})
        assert result.content == b"".join(chunks)
        assert result.headers["x-accel-buffering"] == "no"
        for name in ["server", "x-provider", "content-length", "connection"]:
            assert name not in result.headers


async def test_encoded_body_is_not_double_decoded():
    original = b'{"id":"compressed"}'
    async with client_for(lambda _: response(chunks=[gzip.compress(original)], headers={"content-encoding": "gzip"})) as (client, _):
        result = await client.post("/v1/responses", json={})
        assert result.content == original


class GatedStream(httpx.AsyncByteStream):
    def __init__(self):
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.first = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"first"}\n\n'
        self.last = b'event: response.completed\ndata: {"type":"response.completed"}\n\n'

    async def __aiter__(self):
        yield self.first
        await self.release.wait()
        yield self.last

    async def aclose(self):
        self.closed.set()


@asynccontextmanager
async def live_server(handler, *, configuration=None):
    app = create_app(configuration or settings(), transport=httpx.MockTransport(handler), secrets=KEYS, token=TOKEN)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="error", access_log=False, lifespan="on", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    task.result()
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}", app.state.runtime
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


async def test_actual_tcp_stream_arrives_before_upstream_finishes():
    stream = GatedStream()
    async def handle(_):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    async with live_server(handle) as (url, runtime):
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            async with client.stream("POST", url + "/v1/responses", headers=HEADERS, json={"stream": True}) as result:
                iterator = result.aiter_raw()
                first = await asyncio.wait_for(anext(iterator), 1)
                assert first == stream.first
                assert not stream.release.is_set()
                stream.release.set()
                assert b"".join([part async for part in iterator]) == stream.last
        assert runtime.stats["attempts"] == 1
        await asyncio.wait_for(stream.closed.wait(), 1)


@pytest.mark.parametrize("reconnect", [False, True])
async def test_tcp_client_disconnect_closes_upstream_without_retry(reconnect):
    stream = GatedStream()
    async def handle(_):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    async with live_server(handle, configuration=settings(reconnect_failover=reconnect)) as (url, runtime):
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            async with client.stream("POST", url + "/v1/responses", headers=HEADERS, json={"stream": True}) as result:
                assert await anext(result.aiter_raw()) == stream.first
            await asyncio.wait_for(stream.closed.wait(), 2)
        assert runtime.stats["attempts"] == 1
        assert runtime.stats["in_flight"] == 0
        assert runtime.stats["responses_completed"] == 0
        assert runtime.stats["client_disconnects"] == 1
        assert runtime.states[0].cooldown_events == 0


async def test_disconnect_before_upstream_headers_cancels_attempt():
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def handle(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async with live_server(handle) as (url, runtime):
        port = int(url.rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(("POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer " + TOKEN + "\r\nContent-Length: 2\r\n\r\n{}").encode())
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 1)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(cancelled.wait(), 1)
        assert runtime.stats["attempts"] == 1
        assert runtime.stats["in_flight"] == 0
