"""Real Codex client + local fake backends. No Azure calls or production config edits."""
import asyncio
import json
import os
import shutil

import httpx
import pytest
import tomlkit

from tests.test_proxy import TOKEN, live_server, response, settings
from tests.test_reconnect import event

CODEX = shutil.which("codex")
pytestmark = pytest.mark.skipif(CODEX is None, reason="Optional integration test requires Codex CLI")


def generation(attempt, failure):
    response_id, item_id = f"resp_attempt_{attempt}", f"msg_attempt_{attempt}"
    initial = {"id": response_id, "object": "response", "created_at": 0, "status": "in_progress", "output": []}
    part = {"type": "output_text", "text": "RECOVERED", "annotations": []}
    item = {"id": item_id, "type": "message", "role": "assistant", "status": "completed", "content": [part]}
    chunks = [
        event("response.created", sequence_number=0, response=initial),
        event("response.output_item.added", sequence_number=1, output_index=0,
              item={**item, "status": "in_progress", "content": []}),
        event("response.content_part.added", sequence_number=2, output_index=0, item_id=item_id, content_index=0,
              part={"type": "output_text", "text": "", "annotations": []}),
        event("response.output_text.delta", sequence_number=3, output_index=0, item_id=item_id, content_index=0,
              delta="PARTIAL" if failure else "RECOVERED"),
    ]
    if failure == "rate_limit":
        return chunks + [event("response.failed", sequence_number=4, response={
            **initial, "status": "failed", "error": {"code": "rate_limit_exceeded", "message": "Tokens per minute exceeded"}})]
    if failure == "connection":
        return chunks + [httpx.ReadError("Simulated broken backend connection")]
    chunks += [
        event("response.output_text.done", sequence_number=4, output_index=0, item_id=item_id, content_index=0, text="RECOVERED"),
        event("response.content_part.done", sequence_number=5, output_index=0, item_id=item_id, content_index=0, part=part),
        event("response.output_item.done", sequence_number=6, output_index=0, item=item),
        event("response.completed", sequence_number=7, response={
            **initial, "status": "completed", "output": [item],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}),
    ]
    return chunks


@pytest.mark.parametrize("failure_count,kind,stream_retries", [
    (0, "rate_limit", 2), (1, "rate_limit", 2), (2, "rate_limit", 2),
    (1, "connection", 2), (3, "rate_limit", 2), (3, "http_rate_limit", 2),
    (3, "rate_limit", 10), (10, "rate_limit", 10),
])
async def test_real_codex_reconnect_switches_backends(tmp_path, failure_count, kind, stream_retries):
    calls = []
    rotating = stream_retries > 2
    async def handler(request):
        calls.append(request.url.host)
        failure = kind if len(calls) <= failure_count else None
        if failure == "http_rate_limit":
            return response(429, headers={"retry-after": "0.2"})
        return response(chunks=generation(len(calls), failure),
                        headers={"content-type": "text/event-stream", **({"retry-after": "0.1"} if rotating else {})})

    wait_for_cooldown = kind == "http_rate_limit"
    configuration = settings(reconnect_failover=True, rotate_on_failure=rotating,
                             reconnect_cooldown_seconds=5 if rotating else 120,
                             cooldown_wait_seconds=10 if rotating else (3 if wait_for_cooldown else 0))
    async with live_server(handler, configuration=configuration) as (url, runtime):
        codex_home = tmp_path / "isolated-codex"
        workspace = tmp_path / "workspace"
        codex_home.mkdir()
        workspace.mkdir()
        config = {
            "model": "gpt-6-astra", "model_provider": "test_proxy", "model_reasoning_effort": "low",
            "model_providers": {"test_proxy": {
                "name": "Local integration test", "base_url": url + "/v1", "env_key": "TEST_PROXY_TOKEN",
                "wire_api": "responses", "request_max_retries": 0, "stream_max_retries": stream_retries,
                "stream_idle_timeout_ms": 10000, "supports_websockets": False,
            }},
            "features": {"enable_request_compression": False, "responses_websockets": False, "responses_websockets_v2": False},
        }
        (codex_home / "config.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
        env = {**os.environ, "CODEX_HOME": str(codex_home), "TEST_PROXY_TOKEN": TOKEN}
        process = await asyncio.create_subprocess_exec(
            CODEX, "exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only", "--json",
            "Reply with RECOVERED. Do not use tools.", cwd=workspace, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            # Ten real reconnects include Codex's own exponential backoff.
            stdout, stderr = await asyncio.wait_for(process.communicate(), 300 if rotating else 45)
        finally:
            if process.returncode is None:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F",
                                                                  stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    await killer.wait()
                else:
                    process.kill()
                await process.wait()
        output = stdout.decode(errors="replace")
        diagnostic = output + stderr.decode(errors="replace")
        expected_attempts = failure_count + 1 if wait_for_cooldown else min(failure_count + 1, stream_retries + 1)
        assert calls == [f"p{i % 3 + 1}.example" for i in range(expected_attempts)], diagnostic
        assert runtime.stats["reconnect_failovers"] == (0 if wait_for_cooldown else min(failure_count, stream_retries)), diagnostic
        if wait_for_cooldown:
            assert runtime.stats["requests"] == 1
            assert runtime.stats["cooldown_waits"] >= 1
            assert runtime.stats["exhausted"] == 0
        if rotating:
            assert runtime.stats["cooldown_waits"] >= 1
            assert "Reconnecting... 3/10" in output, diagnostic
        if failure_count <= stream_retries or wait_for_cooldown:
            assert process.returncode == 0, diagnostic
            messages = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
            assert any(m.get("item", {}).get("type") == "agent_message" and m["item"].get("text") == "RECOVERED"
                       for m in messages), diagnostic
        else:
            assert process.returncode != 0, diagnostic
        if failure_count and not wait_for_cooldown:
            assert "Reconnecting" in output, diagnostic
