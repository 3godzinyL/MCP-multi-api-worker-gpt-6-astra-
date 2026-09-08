"""Bounded SSE side-channel inspection; never holds back output chunks."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from proxy.telemetry import normalize_usage


@dataclass(frozen=True)
class StreamFailure:
    reason: str
    status: int
    retry_headers: dict[str, str] = field(default_factory=dict)


def _normalized(value) -> str:
    return "".join(c for c in str(value).lower() if c.isalnum())


def classify_error(event: dict, event_name: str) -> StreamFailure | None:
    name = event.get("type", event_name)
    if name not in {"error", "response.failed"} and "error" not in event:
        return None
    response = event.get("response")
    error = response.get("error") if isinstance(response, dict) else event.get("error", event)
    if not isinstance(error, dict):
        return None
    codes = {_normalized(error.get(key, "")) for key in ("code", "type", "status", "status_code")}
    # Explicit request errors retain their semantics, even if their message
    # happens to discuss quotas. Ordinary model output is never inspected here.
    if codes & {"400", "401", "403", "404", "invalidrequesterror", "invalidapikey", "authenticationerror"}:
        return None
    message = str(error.get("message", "")).lower()
    rate_limit = bool(codes & {
        "429", "ratelimitexceeded", "ratelimiterror", "ratelimit", "ratelimitreached",
        "toomanyrequests", "quotaexceeded", "insufficientquota", "tokenlimitexceeded",
    }) or any(term in message for term in (
        "rate limit", "rate_limit", "too many requests", "quota exceeded", "exceeded your current quota",
        "token rate", "tokens per minute", "tokens-per-minute",
    ))
    if rate_limit:
        hints = {}
        for source in (event, error):
            for key, header in (("retry_after", "retry-after"), ("retry_after_seconds", "retry-after"),
                                ("retry_after_ms", "retry-after-ms")):
                value = source.get(key)
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    hints[header] = str(value)
        return StreamFailure("sse_rate_limit", 429, hints)
    if codes & {"408", "500", "502", "503", "504", "servererror", "internalservererror",
                "serviceunavailable", "timeout", "requesttimeout", "gatewaytimeout", "overloadederror"}:
        return StreamFailure("sse_server_error", 503)
    return None


class ResponseStreamObserver:
    def __init__(self, max_event_bytes: int = 1024 * 1024):
        self.max_event_bytes = max_event_bytes
        self.buffer = bytearray()
        self.data = bytearray()
        self.event_name = ""
        self.oversized = False
        self.dropping_line = False
        self.terminal = False
        self.terminal_event = ""
        self.usage = None

    def _dispatch(self) -> StreamFailure | None:
        event = None
        if self.data and not self.oversized:
            try:
                event = json.loads(self.data)
            except (ValueError, UnicodeDecodeError, RecursionError):
                pass
        name = event.get("type", self.event_name) if isinstance(event, dict) else self.event_name
        failure = None
        if not self.terminal:
            if isinstance(event, dict):
                failure = classify_error(event, self.event_name)
            if name in {"response.completed", "response.incomplete", "response.failed", "error"}:
                self.terminal = True
                self.terminal_event = name
                response = event.get("response") if isinstance(event, dict) else None
                if isinstance(response, dict):
                    self.usage = normalize_usage(response.get("usage"))
            elif self.data.strip() == b"[DONE]":
                self.terminal = True
                self.terminal_event = "done"
        self.event_name = ""
        self.data.clear()
        self.oversized = False
        return failure

    def feed(self, chunk: bytes) -> StreamFailure | None:
        self.buffer.extend(chunk)
        while self.buffer:
            lf, cr = self.buffer.find(b"\n"), self.buffer.find(b"\r")
            positions = [p for p in (lf, cr) if p >= 0]
            if not positions:
                break
            end = min(positions)
            if self.buffer[end] == 13 and end + 1 == len(self.buffer):
                break  # CRLF can straddle raw network chunks.
            width = 2 if self.buffer[end:end + 2] == b"\r\n" else 1
            line = bytes(self.buffer[:end])
            del self.buffer[:end + width]
            if self.dropping_line:
                self.dropping_line = False
                continue
            if not line:
                failure = self._dispatch()
                if failure:
                    return failure
            elif line.startswith(b"event:"):
                self.event_name = line[6:].strip()[:100].decode("utf-8", errors="replace")
            elif line.startswith(b"data:") and not self.oversized:
                value = line[5:]
                if value.startswith(b" "):
                    value = value[1:]
                if len(self.data) + len(value) + 1 > self.max_event_bytes:
                    self.oversized = True
                    self.data.clear()
                else:
                    self.data.extend(value + b"\n")
        if len(self.buffer) > self.max_event_bytes:
            self.buffer.clear()
            self.data.clear()
            self.oversized = True
            self.dropping_line = True
        return None
