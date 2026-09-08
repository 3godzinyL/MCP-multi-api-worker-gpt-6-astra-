from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import re
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import AsyncIterator, Awaitable, Callable, Mapping

import httpx
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from proxy.config import Provider, Settings, load_config
from proxy.credentials import CredentialError, LOCAL_TOKEN_ID, get_secret
from proxy.sse import ResponseStreamObserver, StreamFailure
from proxy.telemetry import normalize_usage
from proxy.windows_transport import windows_connection_reset_guard

LOG = logging.getLogger("responses_proxy")
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    # HTTP client logs can contain endpoints and query strings. Log only our safe fields.
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.disabled = True
        logger.setLevel(logging.CRITICAL)
    logging.getLogger("uvicorn.access").disabled = True


def retry_after_seconds(headers: Mapping[str, str], fallback: float, now: float | None = None) -> float:
    value = headers.get("retry-after", "").strip()
    if value:
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return max(0.0, date.timestamp() - (time.time() if now is None else now))
            except (ValueError, OverflowError, TypeError):
                pass
    for name in ("retry-after-ms", "x-ms-retry-after-ms"):
        try:
            seconds = float(headers.get(name, "")) / 1000
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except ValueError:
            pass
    return fallback


def api_error(status: int, code: str, message: str, request_id: str = "", headers=None) -> JSONResponse:
    response_headers = {"cache-control": "no-store", **(headers or {})}
    if request_id:
        response_headers["x-request-id"] = request_id
    return JSONResponse(
        {"error": {"message": message, "type": "proxy_error", "param": None, "code": code}},
        status_code=status, headers=response_headers,
    )


@dataclass
class ProviderState:
    provider: Provider
    key: str | None = field(default=None, repr=False)
    cooldown_until: float = 0.0
    attempts: int = 0
    in_flight: int = 0
    completed: int = 0
    transport_errors: int = 0
    stream_interruptions: int = 0
    cancellations: int = 0
    cooldown_skips: int = 0
    cooldown_events: int = 0
    cooldown_reason: str = ""
    reconnect_pending: bool = False
    rate_limit_events: int = 0
    statuses: Counter = field(default_factory=Counter)


class StreamInterrupted(RuntimeError):
    """Sanitized exception: never include a URL, body, headers, or upstream error text."""


class RelayResponse(StreamingResponse):
    def __init__(self, runtime: "Runtime", state: ProviderState, upstream: httpx.Response,
                 iterator: AsyncIterator[bytes], first: bytes, request_id: str, telemetry_attempt=None, route=None):
        self.runtime = runtime
        self.provider_state = state
        self.upstream = upstream
        self.iterator = iterator
        self.first = first
        self.request_id = request_id
        self.telemetry_attempt = telemetry_attempt
        self.route = route
        self.json_copy = bytearray()
        self.json_oversized = False
        self.finished = False
        self.interrupted = False
        self.interruption_reason = ""
        headers = {"cache-control": "no-store", "x-accel-buffering": "no", "x-request-id": request_id}
        connection_headers = {s.strip().lower() for s in upstream.headers.get("connection", "").split(",")}
        for name in ("content-type", "content-encoding"):
            if name in upstream.headers and name not in connection_headers:
                headers[name] = upstream.headers[name]
        self.is_sse = "text/event-stream" in headers.get("content-type", "").lower()
        self.encoded = headers.get("content-encoding", "identity").lower() != "identity"
        # Completion accounting is independent of whether retries are enabled.
        self.observer = ResponseStreamObserver() if self.is_sse and not self.encoded else None
        super().__init__(self.chunks(), status_code=upstream.status_code, headers=headers)

    def observe_json(self, chunk):
        if not self.telemetry_attempt or self.is_sse or self.encoded or self.json_oversized:
            return
        if len(self.json_copy) + len(chunk) > 2 * 1024 * 1024:
            self.json_copy.clear()
            self.json_oversized = True
        else:
            self.json_copy.extend(chunk)

    def record_usage(self, outcome, reason):
        usage = self.observer.usage if self.observer else None
        if self.observer and self.observer.terminal_event in {"response.failed", "error"}:
            outcome = "failed"
        if self.finished and not self.is_sse and self.json_copy and not self.json_oversized:
            try:
                value = json.loads(self.json_copy)
                if isinstance(value, dict):
                    usage = normalize_usage(value.get("usage"))
                    if value.get("error") or value.get("status") == "failed":
                        outcome = "failed"
            except (ValueError, UnicodeDecodeError, RecursionError):
                pass
        if self.status_code >= 300:
            outcome = "failed"
        self.runtime.record("finish", self.telemetry_attempt, status=self.status_code, outcome=outcome, reason=reason, usage=usage)
        self.json_copy.clear()

    def prepare_reconnect(self, failure: StreamFailure) -> None:
        self.interrupted = True
        self.interruption_reason = failure.reason
        self.provider_state.stream_interruptions += 1
        self.runtime.stats["stream_interruptions"] += 1
        self.runtime.block_for_failure(self.provider_state, failure, self.upstream.headers, self.request_id,
                                       reconnect=True, route=self.route)
        LOG.warning("event=reconnect_required provider=%s request_id=%s reason=%s",
                    self.provider_state.provider.id, self.request_id, failure.reason)

    async def chunks(self):
        try:
            if self.first:
                self.observe_json(self.first)
                if self.observer and (failure := self.observer.feed(self.first)) and self.runtime.settings.reconnect_failover:
                    self.prepare_reconnect(failure)
                    return
                self.runtime.stats["bytes_forwarded"] += len(self.first)
                yield self.first
                self.first = b""
            async for chunk in self.iterator:
                self.observe_json(chunk)
                if self.observer and (failure := self.observer.feed(chunk)) and self.runtime.settings.reconnect_failover:
                    # End this response without response.completed. Codex's
                    # reconnect sends a NEW request, routed around this cooldown.
                    # Never splice two providers' response IDs/tool calls together.
                    self.prepare_reconnect(failure)
                    return
                self.runtime.stats["bytes_forwarded"] += len(chunk)
                yield chunk
            if self.runtime.settings.reconnect_failover and self.observer and not self.observer.terminal:
                self.prepare_reconnect(StreamFailure("stream_closed_before_completion", 502))
                return
            self.finished = True
        except httpx.TransportError as exc:
            if self.finished:
                # A successfully sent terminal event already ended the response.
                return
            if self.runtime.settings.reconnect_failover and self.is_sse:
                reason = "stream_timeout" if isinstance(exc, httpx.TimeoutException) else "stream_connection_error"
                self.prepare_reconnect(StreamFailure(reason, 504 if isinstance(exc, httpx.TimeoutException) else 502))
                return
            self.interrupted = True
            self.interruption_reason = "stream_timeout" if isinstance(exc, httpx.TimeoutException) else "stream_connection_error"
            self.provider_state.stream_interruptions += 1
            self.runtime.stats["stream_interruptions"] += 1
            LOG.warning("event=stream_interrupted provider=%s request_id=%s error=%s retry=false",
                        self.provider_state.provider.id, self.request_id, type(exc).__name__)
            # Routing has already finished. Never call another provider here.
            if self.is_sse and not self.encoded:
                event = {"type": "error", "code": "upstream_stream_interrupted", "param": None,
                         "message": "Upstream stream interrupted. This request was not retried."}
                yield ("\n\nevent: error\ndata: " + json.dumps(event) + "\n\n").encode()
            else:
                raise StreamInterrupted("Upstream response interrupted; request was not retried") from None

    async def __call__(self, scope, receive, send):
        async def tracked_send(message):
            await send(message)
            # The client may close immediately after this send, before the
            # generator resumes or upstream HTTP EOF arrives. Do not classify
            # that as cancellation. A failed send must still count as one.
            if (message["type"] == "http.response.body" and message.get("body")
                    and self.observer and self.observer.terminal and not self.interrupted):
                self.finished = True

        try:
            await super().__call__(scope, receive, tracked_send)
        finally:
            # Also closes the connection if the client disconnects before iteration starts.
            try:
                await self.upstream.aclose()
            finally:
                self.provider_state.in_flight -= 1
                self.runtime.stats["in_flight"] -= 1
                if self.finished:
                    self.provider_state.completed += 1
                    self.runtime.stats["responses_completed"] += 1
                    outcome = "completed"
                    reason = self.observer.terminal_event if self.observer and self.observer.terminal else "upstream_eof"
                elif self.interrupted:
                    outcome, reason = "upstream_interrupted", self.interruption_reason
                else:
                    self.provider_state.cancellations += 1
                    self.runtime.stats["client_disconnects"] += 1
                    outcome, reason = "client_disconnected", "client_disconnect"
                LOG.info("event=request_finished provider=%s request_id=%s status=%d complete=%s outcome=%s reason=%s",
                         self.provider_state.provider.id, self.request_id, self.status_code, self.finished, outcome, reason)
                self.record_usage(outcome, reason)


class Runtime:
    def __init__(self, settings: Settings, *, transport=None,
                 secrets: Mapping[str, str] | None = None, token: str | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, telemetry=None, config_path=None):
        self.settings = settings
        self.clock = monotonic
        self.sleep = sleep
        self.started = self.clock()
        self.stats = Counter()
        self.telemetry = telemetry
        self.config_path = config_path
        self.secret_overrides = secrets
        self.routes = {}
        self.states = []
        self.preferred_index = 0
        self.rotation_source: ProviderState | None = None
        self.token = token
        if token is None:
            try:
                self.token = get_secret(LOCAL_TOKEN_ID, settings.proxy_token_env)
            except CredentialError:
                LOG.error("event=credential_unavailable target=local_proxy")
        for provider in settings.providers:
            key = None
            if provider.enabled:
                if secrets is not None:
                    key = secrets.get(provider.id)
                else:
                    try:
                        key = get_secret(provider.id, provider.api_key_env)
                    except CredentialError:
                        LOG.error("event=credential_unavailable provider=%s", provider.id)
            self.states.append(ProviderState(provider, key=key))
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=settings.connect_timeout_seconds, read=settings.read_timeout_seconds,
                                  write=settings.write_timeout_seconds, pool=settings.pool_timeout_seconds),
            limits=httpx.Limits(max_connections=settings.max_connections, max_keepalive_connections=20),
            transport=transport, follow_redirects=False, trust_env=False,
        )

    def reload(self):
        if self.config_path is None:
            raise ValueError("This proxy has no editable configuration file")
        settings = load_config(self.config_path)
        previous = {s.provider.id: s for s in self.states}
        preferred = self.states[self.preferred_index].provider.id
        updated = []
        for provider in settings.providers:
            state = previous.get(provider.id) or ProviderState(provider)
            state.provider = provider
            state.key = (self.secret_overrides.get(provider.id) if self.secret_overrides is not None
                         else get_secret(provider.id, provider.api_key_env)) if provider.enabled else None
            updated.append(state)
        self.states = updated
        self.settings = settings
        self.preferred_index = next((i for i, s in enumerate(updated) if s.provider.id == preferred), 0)
        self.rotation_source = None
        return self.status()

    def set_route(self, value):
        route_id = value.get("id")
        ids = value.get("providers")
        if not isinstance(route_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", route_id):
            raise ValueError("Invalid route id")
        if not isinstance(ids, list) or not ids or any(not isinstance(v, str) for v in ids) or len(set(ids)) != len(ids):
            raise ValueError("Select distinct providers")
        known = {s.provider.id: s for s in self.states}
        if any(v not in known or not self.usable(known[v]) for v in ids):
            raise ValueError("Selected API is disabled, unconfigured, or was removed")
        if len({known[v].provider.deployment for v in ids}) != 1:
            raise ValueError("Failover providers must use the same model/deployment")
        strategy = value.get("strategy", "priority")
        if strategy not in {"priority", "balanced"}:
            raise ValueError("Invalid strategy")
        wait = value.get("wait_seconds", self.settings.cooldown_wait_seconds)
        output = value.get("max_output_tokens", 0)
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= 900:
            raise ValueError("Invalid cooldown wait")
        if isinstance(output, bool) or not isinstance(output, int) or not 0 <= output <= 200000:
            raise ValueError("Invalid output token limit")
        if len(self.routes) >= 2048 and route_id not in self.routes:
            raise ValueError("Route limit reached; restart the idle panel and proxy")
        old = self.routes.get(route_id, {})
        route = {"id": route_id, "providers": ids, "strategy": strategy, "preferred": ids[0],
                 "wait_seconds": wait, "max_output_tokens": output, "last_provider": old.get("last_provider"),
                 "attempts": old.get("attempts", {}), "rotation_source": None}
        self.routes[route_id] = route
        return {k: v for k, v in route.items() if k != "rotation_source"}

    def route_order(self, route):
        by_id = {s.provider.id: s for s in self.states}
        states = [by_id[p] for p in route["providers"] if p in by_id]
        if route["strategy"] == "balanced":
            return sorted(states, key=lambda s: (s.in_flight, route["attempts"].get(s.provider.id, 0)))
        pivot = next((i for i, s in enumerate(states) if s.provider.id == route["preferred"]), 0)
        return states[pivot:] + states[:pivot]

    def record(self, method, *args, **kwargs):
        if self.telemetry is not None:
            try:
                return getattr(self.telemetry, method)(*args, **kwargs)
            except Exception:
                # Accounting failures must never break a live AI response.
                LOG.warning("event=telemetry_unavailable operation=%s", method)

    def usable(self, state: ProviderState) -> bool:
        return state.provider.enabled and bool(state.key)

    def rotate_after_failure(self, state: ProviderState, reason: str, request_id: str, *, reconnect=False, route=None) -> None:
        if route is not None:
            order = self.route_order(route)
            index = next((i for i, s in enumerate(order) if s is state), -1)
            choices = order[index + 1:] + order[:max(0, index)]
            target = next((s for s in choices if self.usable(s) and s.cooldown_until <= self.clock()), None)
            if target:
                route["preferred"] = target.provider.id
                if reconnect:
                    route["rotation_source"] = state
                self.record("event", "rotation", state.provider.id, peer=target.provider.id, reason=reason, request_id=request_id)
            return
        if not self.settings.rotate_on_failure or self.states[self.preferred_index] is not state:
            return
        # Late failures from old requests must not skip a provider already
        # selected after the first failure. Advance only the current preference.
        for offset in range(1, len(self.states)):
            next_index = (self.preferred_index + offset) % len(self.states)
            if self.usable(self.states[next_index]):
                self.preferred_index = next_index
                if reconnect:
                    self.rotation_source = state
                LOG.info("event=provider_rotated from=%s to=%s reason=%s request_id=%s",
                         state.provider.id, self.states[next_index].provider.id, reason, request_id)
                self.record("event", "rotation", state.provider.id, peer=self.states[next_index].provider.id,
                            reason=reason, request_id=request_id)
                return

    def block_for_failure(self, state: ProviderState, failure: StreamFailure, headers: Mapping[str, str],
                          request_id: str, *, reconnect: bool, route=None) -> None:
        if failure.status == 429:
            merged = {**failure.retry_headers, **dict(headers)}
            cooldown = retry_after_seconds(merged, state.provider.cooldown_seconds)
            state.rate_limit_events += 1
            self.stats["rate_limit_events"] += 1
        else:
            cooldown = self.settings.reconnect_cooldown_seconds
        if reconnect:
            # A short/zero Retry-After must not route the client's immediate
            # reconnect straight back to the failed backend.
            cooldown = max(cooldown, self.settings.reconnect_cooldown_seconds)
        state.cooldown_until = max(state.cooldown_until, self.clock() + cooldown)
        state.cooldown_reason = failure.reason
        state.cooldown_events += 1
        state.reconnect_pending = state.reconnect_pending or reconnect
        LOG.warning("event=cooldown provider=%s seconds=%.3f reason=%s request_id=%s",
                    state.provider.id, cooldown, failure.reason, request_id)
        self.record("event", "cooldown", state.provider.id, reason=failure.reason, request_id=request_id)
        self.rotate_after_failure(state, failure.reason, request_id, reconnect=reconnect, route=route)

    def status(self):
        now = self.clock()
        return {
            "status": "ready" if self.token and any(self.usable(s) and s.cooldown_until <= now for s in self.states) else "unavailable",
            "uptime_seconds": round(now - self.started, 1),
            "telemetry_enabled": self.telemetry is not None,
            "reconnect_failover": self.settings.reconnect_failover,
            "cooldown_wait_seconds": self.settings.cooldown_wait_seconds,
            "rotate_on_failure": self.settings.rotate_on_failure,
            "routing_version": 1,
            "routes": [{k: v for k, v in r.items() if k != "rotation_source"} for r in self.routes.values()],
            "preferred_provider": self.states[self.preferred_index].provider.id if self.settings.rotate_on_failure else None,
            "stats": {key: self.stats[key] for key in (
                "requests", "attempts", "failovers", "in_flight", "responses_completed", "exhausted",
                "stream_interruptions", "client_disconnects", "bytes_forwarded", "rate_limit_events", "reconnect_failovers",
                "waiting_requests", "cooldown_waits")},
            "providers": [{
                "id": s.provider.id, "enabled": s.provider.enabled, "configured": bool(s.key),
                "available": self.usable(s) and s.cooldown_until <= now,
                "cooldown_remaining_seconds": round(max(0.0, s.cooldown_until - now), 3),
                "attempts": s.attempts, "in_flight": s.in_flight, "responses_completed": s.completed,
                "http_statuses": dict(s.statuses), "transport_errors": s.transport_errors,
                "stream_interruptions": s.stream_interruptions, "client_disconnects": s.cancellations,
                "cooldown_skips": s.cooldown_skips, "cooldown_events": s.cooldown_events,
                "cooldown_reason": s.cooldown_reason if s.cooldown_until > now else "",
                "rate_limit_events": s.rate_limit_events,
            } for s in self.states],
        }

    async def forward(self, payload: dict, suffix: str, request_id: str, extra_headers: dict, route_id=None) -> Response:
        route = self.routes.get(route_id) if route_id else None
        if route_id and route is None:
            return api_error(409, "route_unavailable", "Task route is unavailable. Resume the task from the panel.", request_id)
        self.stats["requests"] += 1
        wait_remaining = route["wait_seconds"] if route else self.settings.cooldown_wait_seconds
        while True:
            result = await self._forward_once(payload, suffix, request_id, extra_headers, route)
            if isinstance(result, Response):
                return result
            now = self.clock()
            eligible = [s for s in (self.route_order(route) if route else self.states) if self.usable(s)]
            cooling = [s for s in eligible if s.cooldown_until > now]
            response_headers = {}
            reason = "attempts_failed"
            if eligible and len(cooling) == len(eligible):
                delay = min(s.cooldown_until for s in cooling) - now
                if wait_remaining > 0:
                    delay = min(delay, wait_remaining)
                    self.stats["cooldown_waits"] += 1
                    self.stats["waiting_requests"] += 1
                    LOG.info("event=cooldown_wait request_id=%s seconds=%.3f", request_id, delay)
                    try:
                        # No downstream headers have been sent. The disconnect
                        # watcher cancels this wait if the caller goes away.
                        await self.sleep(delay)
                    finally:
                        self.stats["waiting_requests"] -= 1
                    wait_remaining -= max(delay, self.clock() - now)
                    # Concurrent streams can extend a cooldown while we wait.
                    # Recheck every provider before attempting another request.
                    continue
                result, reason = 429, "all_providers_cooling"
                response_headers["retry-after"] = str(max(1, math.ceil(delay)))
            self.stats["exhausted"] += 1
            LOG.warning("event=exhausted status=%d reason=%s request_id=%s", result, reason, request_id)
            return api_error(result, "upstream_unavailable", "No upstream is available for this request.",
                             request_id, response_headers)

    async def _forward_once(self, payload: dict, suffix: str, request_id: str, extra_headers: dict, route=None) -> Response | int:
        """Try each currently available provider once, without committing an error response."""
        last_status = 503
        previous = None
        reconnect_source = None
        reason = "unavailable"
        source = route.get("rotation_source") if route else self.rotation_source
        if self.settings.rotate_on_failure and source is not None:
            reconnect_source = source
            previous = reconnect_source.provider.id
            reason = reconnect_source.cooldown_reason
        order = self.states
        if self.settings.rotate_on_failure:
            order = self.states[self.preferred_index:] + self.states[:self.preferred_index]
        if route:
            order = self.route_order(route)
        for state in order:
            if not self.usable(state):
                continue
            if state.cooldown_until > self.clock():
                state.cooldown_skips += 1
                LOG.info("event=cooldown_skip provider=%s request_id=%s", state.provider.id, request_id)
                if state.reconnect_pending and not self.settings.rotate_on_failure:
                    previous = state.provider.id
                    reason = state.cooldown_reason
                    reconnect_source = state
                continue
            state.reconnect_pending = False
            if previous:
                switched = previous != state.provider.id
                if switched:
                    self.stats["failovers"] += 1
                event = "reconnect_failover" if reconnect_source else "failover"
                if reconnect_source:
                    if switched:
                        self.stats["reconnect_failovers"] += 1
                    reconnect_source.reconnect_pending = False
                    if self.rotation_source is reconnect_source:
                        self.rotation_source = None
                    if route and route.get("rotation_source") is reconnect_source:
                        route["rotation_source"] = None
                    reconnect_source = None
                if switched:
                    LOG.warning("event=%s from=%s to=%s reason=%s request_id=%s",
                                event, previous, state.provider.id, reason, request_id)
            provider = state.provider
            if self.settings.rotate_on_failure and route is None:
                self.preferred_index = self.states.index(state)
            body = {**payload, "model": provider.deployment}
            if route:
                route["last_provider"] = provider.id
                route["attempts"][provider.id] = route["attempts"].get(provider.id, 0) + 1
                if route["max_output_tokens"] and not suffix:
                    body["max_output_tokens"] = min(body.get("max_output_tokens") or route["max_output_tokens"], route["max_output_tokens"])
            headers = {"accept-encoding": "identity", "accept": "text/event-stream" if payload.get("stream") else "application/json",
                       "user-agent": "local-responses-proxy/1.0", **extra_headers}
            if provider.auth_type == "api-key":
                headers["api-key"] = state.key
            else:
                headers["authorization"] = "Bearer " + state.key
            params = {"api-version": provider.api_version} if provider.api_version else None
            upstream = None
            transferred = False
            state.attempts += 1
            state.in_flight += 1
            self.stats["attempts"] += 1
            self.stats["in_flight"] += 1
            LOG.info("event=attempt provider=%s request_id=%s", provider.id, request_id)
            attempt_id = self.record("begin", provider.id, request_id, "compact" if suffix else "response", route_id=route["id"] if route else "")
            attempt_reason, attempt_usage = "interrupted", None
            try:
                request = self.client.build_request("POST", provider.url(suffix), json=body, headers=headers, params=params)
                upstream = await self.client.send(request, stream=True)
                state.statuses[str(upstream.status_code)] += 1
                if upstream.status_code in RETRYABLE_STATUSES:
                    last_status = upstream.status_code
                    reason = "http_" + str(last_status)
                    attempt_reason = reason
                    if last_status == 429:
                        self.block_for_failure(state, StreamFailure("http_429", 429), upstream.headers,
                                               request_id, reconnect=False, route=route)
                    else:
                        self.rotate_after_failure(state, reason, request_id, route=route)
                    LOG.warning("event=attempt_failed provider=%s status=%d request_id=%s", provider.id, last_status, request_id)
                    previous = provider.id
                    continue
                iterator = upstream.aiter_raw()
                first = b""
                if 200 <= upstream.status_code < 300:
                    # At most one raw network chunk. No event accumulation or whole-body read.
                    # A read timeout here can fail over: no downstream headers/body were sent.
                    first = await anext(iterator, b"")
                    if self.settings.reconnect_failover and "text/event-stream" in upstream.headers.get("content-type", "").lower() and upstream.headers.get("content-encoding", "identity").lower() == "identity":
                        preflight = ResponseStreamObserver()
                        if failure := preflight.feed(first):
                            attempt_reason, attempt_usage = failure.reason, preflight.usage
                            self.block_for_failure(state, failure, upstream.headers, request_id, reconnect=False, route=route)
                            previous, reason, last_status = provider.id, failure.reason, failure.status
                            continue
                relay = RelayResponse(self, state, upstream, iterator, first, request_id, attempt_id, route)
                transferred = True
                return relay
            except httpx.TransportError as exc:
                state.transport_errors += 1
                last_status = 504 if isinstance(exc, httpx.TimeoutException) else 502
                previous = provider.id
                reason = type(exc).__name__
                attempt_reason = reason
                self.rotate_after_failure(state, reason, request_id, route=route)
                LOG.warning("event=attempt_failed provider=%s error=%s request_id=%s", provider.id, reason, request_id)
            finally:
                if not transferred:
                    try:
                        if upstream is not None:
                            await upstream.aclose()
                    finally:
                        state.in_flight -= 1
                        self.stats["in_flight"] -= 1
                        self.record("finish", attempt_id, status=upstream.status_code if upstream is not None else None,
                                    outcome="failed", reason=attempt_reason, usage=attempt_usage)
        return last_status


async def _while_connected(request: Request, operation):
    """Cancel an upstream waiting for headers/first byte when its caller disconnects."""
    async def disconnected():
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return
    task = asyncio.create_task(operation)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return Response(status_code=499)
    finally:
        watcher.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(watcher, task, return_exceptions=True)


def create_app(settings: Settings, **runtime_options) -> Starlette:
    @asynccontextmanager
    async def lifespan(app):
        configure_logging(settings.log_level)
        app.state.runtime = Runtime(settings, **runtime_options)
        LOG.info("event=started listen=127.0.0.1:4000 configured_providers=%d",
                 sum(app.state.runtime.usable(s) for s in app.state.runtime.states))
        with windows_connection_reset_guard():
            try:
                yield
            finally:
                await app.state.runtime.client.aclose()

    def authenticate(request: Request) -> Response | None:
        runtime = request.app.state.runtime
        if not runtime.token:
            return api_error(503, "proxy_not_configured", "Configure the local proxy token first.")
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + runtime.token).encode()):
            return api_error(401, "invalid_api_key", "A valid local proxy token is required.")
        return None

    async def health(request: Request):
        runtime = request.app.state.runtime
        return JSONResponse({"status": "ok", "ready": runtime.status()["status"] == "ready"}, headers={"cache-control": "no-store"})

    async def status(request: Request):
        denied = authenticate(request)
        return denied if denied is not None else JSONResponse(request.app.state.runtime.status(), headers={"cache-control": "no-store"})

    async def admin(request: Request):
        denied = authenticate(request)
        if denied is not None:
            return denied
        try:
            if request.url.path.endswith("/reload"):
                value = request.app.state.runtime.reload()
            else:
                raw = await request.body()
                if len(raw) > 20000:
                    raise ValueError("Route definition too large")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("Expected a route object")
                value = request.app.state.runtime.set_route(body)
            return JSONResponse(value, headers={"cache-control": "no-store"})
        except (ValueError, OSError, CredentialError) as exc:
            return api_error(400, "configuration_error", str(exc))

    async def models(request: Request):
        denied = authenticate(request)
        if denied is not None:
            return denied
        # Codex's discovery expects its proprietary `models` catalog, whose
        # entries also replace the agent's base instructions. We supply no such
        # overrides: Codex uses its own model defaults and user configuration.
        # OpenAI clients can still discover the public alias through `data`.
        return JSONResponse({"object": "list", "data": [
            {"id": settings.public_model, "object": "model", "created": 0, "owned_by": "local-proxy"}
        ], "models": []})

    async def responses(request: Request):
        denied = authenticate(request)
        if denied is not None:
            return denied
        request_id = uuid.uuid4().hex
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return api_error(415, "unsupported_content_encoding", "Send uncompressed JSON.", request_id)
        content = bytearray()
        try:
            async for part in request.stream():
                content.extend(part)
                if len(content) > settings.max_request_bytes:
                    return api_error(413, "request_too_large", "Request body exceeds the configured limit.", request_id)
        except ClientDisconnect:
            return Response(status_code=499)
        try:
            def reject_constant(_):
                raise ValueError("Non-finite JSON")
            payload = json.loads(content, parse_constant=reject_constant)
            if not isinstance(payload, dict) or ("stream" in payload and not isinstance(payload["stream"], bool)):
                raise ValueError("Invalid request object")
        except (ValueError, UnicodeDecodeError, RecursionError):
            return api_error(400, "invalid_json", "Expected a JSON object and a boolean stream flag.", request_id)
        suffix = "/compact" if request.url.path.endswith("/compact") else ""
        extra = {"openai-beta": request.headers["openai-beta"]} if "openai-beta" in request.headers else {}
        return await _while_connected(request, request.app.state.runtime.forward(payload, suffix, request_id, extra, request.path_params.get("route_id")))

    return Starlette(lifespan=lifespan, routes=[
        Route("/health", health, methods=["GET"]),
        Route("/status", status, methods=["GET"]),
        Route("/admin/routes", admin, methods=["POST"]),
        Route("/admin/reload", admin, methods=["POST"]),
        Route("/r/{route_id}/v1/models", models, methods=["GET"]),
        Route("/r/{route_id}/v1/responses", responses, methods=["POST"]),
        Route("/r/{route_id}/v1/responses/compact", responses, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
        Route("/v1/responses", responses, methods=["POST"]),
        Route("/v1/responses/compact", responses, methods=["POST"]),
    ])
