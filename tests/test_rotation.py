import logging

import httpx
import pytest

from proxy.sse import StreamFailure
from tests.test_proxy import client_for, response, settings
from tests.test_reconnect import COMPLETE, CREATED, RATE_LIMIT


async def test_reconnect_rotates_even_after_the_failed_backend_cooldown_expires(caplog):
    calls, now = [], [100.0]

    async def handle(request):
        calls.append(request.url.host)
        return response(chunks=[CREATED, RATE_LIMIT if len(calls) <= 3 else COMPLETE],
                        headers={"content-type": "text/event-stream"})

    async with client_for(handle, configuration=settings(reconnect_failover=True, rotate_on_failure=True),
                          monotonic=lambda: now[0]) as (client, runtime):
        with caplog.at_level(logging.INFO, logger="responses_proxy"):
            for index in range(4):
                result = await client.post("/v1/responses", json={"stream": True})
                assert (COMPLETE in result.content) is (index == 3)
                # Codex backoff / another operation can outlast the cooldown.
                now[0] += 120
        assert calls == ["p1.example", "p2.example", "p3.example", "p1.example"]
        assert runtime.stats["reconnect_failovers"] == 3
        assert runtime.status()["preferred_provider"] == "provider1"
    assert "event=reconnect_failover from=provider3 to=provider1" in caplog.text


@pytest.mark.parametrize("failure", [429, 503, httpx.ConnectError("private detail")])
async def test_http_failover_keeps_the_working_provider_after_cooldown(failure):
    calls, now = [], [100.0]

    async def handle(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return response(failure)
        return response()

    async with client_for(handle, configuration=settings(rotate_on_failure=True),
                          monotonic=lambda: now[0]) as (client, runtime):
        assert (await client.post("/v1/responses", json={})).status_code == 200
        now[0] += 120
        assert (await client.post("/v1/responses", json={})).status_code == 200
        assert calls == ["p1.example", "p2.example", "p2.example"]
        assert runtime.status()["preferred_provider"] == "provider2"


async def test_late_failures_do_not_rotate_past_the_selected_backend():
    async with client_for(lambda _: response(), configuration=settings(rotate_on_failure=True)) as (_, runtime):
        failure = StreamFailure("sse_rate_limit", 429)
        runtime.block_for_failure(runtime.states[0], failure, {}, "first", reconnect=True)
        assert runtime.status()["preferred_provider"] == "provider2"
        runtime.block_for_failure(runtime.states[0], failure, {}, "late", reconnect=True)
        assert runtime.status()["preferred_provider"] == "provider2"
        runtime.block_for_failure(runtime.states[1], failure, {}, "second", reconnect=True)
        assert runtime.status()["preferred_provider"] == "provider3"
        runtime.block_for_failure(runtime.states[0], failure, {}, "very-late", reconnect=True)
        assert runtime.status()["preferred_provider"] == "provider3"


async def test_rotation_skips_unconfigured_provider_and_keeps_400_on_current_backend():
    async with client_for(lambda _: response(400), configuration=settings(rotate_on_failure=True)) as (client, runtime):
        runtime.states[1].key = None
        runtime.block_for_failure(runtime.states[0], StreamFailure("sse_rate_limit", 429), {}, "first", reconnect=True)
        assert runtime.status()["preferred_provider"] == "provider3"
        assert (await client.post("/v1/responses", json={})).status_code == 400
        assert runtime.status()["preferred_provider"] == "provider3"
        assert runtime.stats["attempts"] == 1


async def test_rotation_waits_and_can_reuse_the_first_backend_to_recover():
    now, waits = [100.0], []

    async def sleep(delay):
        waits.append(delay)
        now[0] += delay

    async with client_for(lambda _: response(), configuration=settings(rotate_on_failure=True, cooldown_wait_seconds=180),
                          monotonic=lambda: now[0], sleep=sleep) as (client, runtime):
        for state in runtime.states:
            runtime.block_for_failure(state, StreamFailure("sse_rate_limit", 429), {}, state.provider.id, reconnect=True)
        assert (await client.post("/v1/responses", json={})).status_code == 200
        assert waits == [30]
        assert runtime.states[0].attempts == 1
        assert runtime.stats["reconnect_failovers"] == 1
        assert runtime.rotation_source is None


async def test_reusing_the_only_available_backend_does_not_leave_a_pending_rotation():
    async with client_for(lambda _: response(), configuration=settings(rotate_on_failure=True),
                          monotonic=lambda: 100) as (client, runtime):
        runtime.block_for_failure(runtime.states[0], StreamFailure("sse_rate_limit", 429), {}, "first", reconnect=True)
        runtime.states[0].cooldown_until = 99
        runtime.states[1].cooldown_until = runtime.states[2].cooldown_until = 200
        for _ in range(2):
            assert (await client.post("/v1/responses", json={})).status_code == 200
        assert runtime.states[0].attempts == 2
        assert runtime.stats["reconnect_failovers"] == 0
        assert runtime.rotation_source is None
