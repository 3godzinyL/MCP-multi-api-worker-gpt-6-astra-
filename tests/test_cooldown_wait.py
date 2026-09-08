import asyncio
import logging

import pytest

from tests.test_proxy import TOKEN, client_for, live_server, response, settings


@pytest.mark.parametrize("delays,expected_host", [
    ([3, 5, 7], "p1.example"),
    ([30, 3, 10], "p2.example"),
    ([30, 10, 3], "p3.example"),
])
async def test_waits_for_first_available_backend_instead_of_returning_429(delays, expected_host, caplog):
    now, waits, calls = [100.0], [], []

    async def sleep(delay):
        waits.append(delay)
        now[0] += delay

    async def handle(request):
        calls.append(request.url.host)
        return response()

    async with client_for(handle, configuration=settings(cooldown_wait_seconds=180),
                          monotonic=lambda: now[0], sleep=sleep) as (client, runtime):
        for state, delay in zip(runtime.states, delays):
            state.cooldown_until = now[0] + delay
        with caplog.at_level(logging.INFO, logger="responses_proxy"):
            result = await client.post("/v1/responses", json={})
        assert result.status_code == 200
        assert calls == [expected_host]
        assert waits == [3]
        assert runtime.stats["requests"] == 1
        assert runtime.stats["exhausted"] == 0
        assert runtime.stats["cooldown_waits"] == 1
        assert runtime.stats["waiting_requests"] == 0
        assert runtime.status()["cooldown_wait_seconds"] == 180
        assert "event=cooldown_wait" in caplog.text


async def test_all_three_http_rate_limits_wait_then_recover_in_same_request():
    now, waits, calls = [100.0], [], []

    async def sleep(delay):
        waits.append(delay)
        now[0] += delay

    async def handle(request):
        calls.append(request.url.host)
        return response(429, headers={"retry-after": "3"}) if len(calls) <= 3 else response()

    async with client_for(handle, configuration=settings(cooldown_wait_seconds=180),
                          monotonic=lambda: now[0], sleep=sleep) as (client, runtime):
        result = await client.post("/v1/responses", json={})
        assert result.status_code == 200
        assert calls == ["p1.example", "p2.example", "p3.example", "p1.example"]
        assert waits == [3]
        assert runtime.stats["requests"] == 1
        assert runtime.stats["rate_limit_events"] == 3
        assert runtime.stats["exhausted"] == 0


async def test_repeated_rate_limits_share_one_bounded_wait_budget():
    now, waits, calls = [100.0], [], []

    async def sleep(delay):
        waits.append(delay)
        now[0] += delay

    async def handle(request):
        calls.append(request.url.host)
        return response(429, headers={"retry-after": "3"})

    async with client_for(handle, configuration=settings(cooldown_wait_seconds=5),
                          monotonic=lambda: now[0], sleep=sleep) as (client, runtime):
        result = await client.post("/v1/responses", json={})
        assert result.status_code == 429
        assert result.headers["retry-after"] == "1"
        assert calls == ["p1.example", "p2.example", "p3.example"] * 2
        assert waits == [3, 2]
        assert runtime.stats["requests"] == 1
        assert runtime.stats["exhausted"] == 1
        assert runtime.stats["waiting_requests"] == 0


async def test_wait_rechecks_cooldown_extended_by_another_request():
    now, waits = [100.0], []
    runtime = None

    async def sleep(delay):
        waits.append(delay)
        now[0] += delay
        if len(waits) == 1:
            # Another in-flight response reports a rate limit during this wait.
            runtime.states[0].cooldown_until = now[0] + 20

    async with client_for(lambda _: response(), configuration=settings(cooldown_wait_seconds=10),
                          monotonic=lambda: now[0], sleep=sleep) as (client, runtime):
        for state, delay in zip(runtime.states, [3, 5, 7]):
            state.cooldown_until = now[0] + delay
        assert (await client.post("/v1/responses", json={})).status_code == 200
        assert waits == [3, 2]
        assert runtime.states[0].attempts == 0
        assert runtime.states[1].attempts == 1


async def test_wait_does_not_retry_ordinary_server_errors_or_missing_credentials():
    async def sleep(_):
        raise AssertionError("Only cooldowns should cause a wait")

    async with client_for(lambda _: response(503), configuration=settings(cooldown_wait_seconds=180),
                          sleep=sleep) as (client, runtime):
        assert (await client.post("/v1/responses", json={})).status_code == 503
        assert runtime.stats["attempts"] == 3
        for state in runtime.states:
            state.key = None
        assert (await client.post("/v1/responses", json={})).status_code == 503
        assert runtime.stats["attempts"] == 3


async def test_tcp_disconnect_cancels_cooldown_wait_without_contacting_backend():
    async def handle(_):
        raise AssertionError("The client disconnects while all providers are cooling")

    async with live_server(handle, configuration=settings(cooldown_wait_seconds=180)) as (url, runtime):
        for state in runtime.states:
            state.cooldown_until = runtime.clock() + 60
        port = int(url.rsplit(":", 1)[1])
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(("POST /v1/responses HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer "
                          + TOKEN + "\r\nContent-Length: 2\r\n\r\n{}").encode())
            await writer.drain()
            async with asyncio.timeout(2):
                while runtime.stats["waiting_requests"] != 1:
                    await asyncio.sleep(0.01)
        finally:
            writer.close()
            await writer.wait_closed()
        async with asyncio.timeout(2):
            while runtime.stats["waiting_requests"]:
                await asyncio.sleep(0.01)
        assert runtime.stats["attempts"] == 0
        assert runtime.stats["in_flight"] == 0
        assert runtime.stats["exhausted"] == 0
