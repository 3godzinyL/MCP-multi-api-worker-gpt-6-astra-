from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Provider:
    id: str
    base_url: str
    deployment: str
    api_key_env: str
    enabled: bool = True
    auth_type: str = "api-key"
    api_version: str = ""
    cooldown_seconds: float = 60.0
    label: str = ""
    tokens_per_minute: int = 1_000_000
    soft_tokens_per_minute: int = 900_000
    hard_tokens_per_minute: int = 950_000

    def url(self, suffix: str = "") -> str:
        return self.base_url.rstrip("/") + "/responses" + suffix


@dataclass(frozen=True)
class Settings:
    providers: tuple[Provider, ...]
    public_model: str = "gpt-6-astra"
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 120.0
    write_timeout_seconds: float = 30.0
    pool_timeout_seconds: float = 10.0
    max_connections: int = 100
    max_request_bytes: int = 16 * 1024 * 1024
    proxy_token_env: str = "LOCAL_RESPONSES_PROXY_TOKEN"
    log_level: str = "INFO"
    reconnect_failover: bool = False
    reconnect_cooldown_seconds: float = 30.0
    cooldown_wait_seconds: float = 0.0
    rotate_on_failure: bool = False
    codex_stream_max_retries: int = 10
    codex_request_max_retries: int = 4


def _positive(data: dict, key: str, default: float, *, zero: bool = False,
              maximum: float | None = None) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ConfigurationError(f"{key}: expected a number")
    try:
        value = float(value)
    except OverflowError:
        raise ConfigurationError(f"{key}: invalid duration or limit") from None
    if (not math.isfinite(value) or value < 0 or (value == 0 and not zero)
            or (maximum is not None and value > maximum)):
        raise ConfigurationError(f"{key}: invalid duration or limit")
    return float(value)


def _text(data: dict, key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ConfigurationError(f"{key}: expected text")
    return value.strip()


def _env_override(data: dict, key: str) -> str:
    env_name = _text(data, key + "_env")
    return os.environ.get(env_name, _text(data, key)).strip() if env_name else _text(data, key)


def provider_token_limits(data: dict) -> dict[str, int]:
    limits = {}
    for name, default in (("tokens_per_minute", 1_000_000),
                          ("soft_tokens_per_minute", 900_000),
                          ("hard_tokens_per_minute", 950_000)):
        value = data.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigurationError(f"{name}: expected an integer")
        if not 1 <= value <= 1_000_000_000:
            raise ConfigurationError(f"{name}: expected an integer between 1 and 1000000000")
        limits[name] = value
    if not limits["soft_tokens_per_minute"] < limits["hard_tokens_per_minute"] <= limits["tokens_per_minute"]:
        raise ConfigurationError(
            "Expected 0 < soft_tokens_per_minute < hard_tokens_per_minute <= tokens_per_minute")
    return limits


def load_config(path: str | Path) -> Settings:
    try:
        if Path(path).stat().st_size > 1024 * 1024:
            raise ConfigurationError("Configuration file exceeds 1 MiB")
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        raise ConfigurationError("Cannot read configuration; check the file and TOML syntax") from None
    server = data.get("proxy", {})
    raw_providers = data.get("providers", [])
    if not isinstance(server, dict) or not isinstance(raw_providers, list) or not 1 <= len(raw_providers) <= 32:
        raise ConfigurationError("Configuration must contain [proxy] and 1 to 32 [[providers]]")
    providers = []
    ids = set()
    for raw in raw_providers:
        if not isinstance(raw, dict):
            raise ConfigurationError("Invalid provider entry")
        provider_id = _text(raw, "id")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", provider_id) or provider_id in ids:
            raise ConfigurationError("Provider IDs must be unique, short letters/digits/underscores")
        ids.add(provider_id)
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigurationError(f"{provider_id}: enabled must be true or false")
        base_url = _env_override(raw, "base_url")
        deployment = _env_override(raw, "deployment")
        auth_type = _text(raw, "auth_type", "api-key")
        if auth_type not in {"api-key", "bearer"}:
            raise ConfigurationError(f"{provider_id}: auth_type must be api-key or bearer")
        try:
            parsed = urlsplit(base_url)
            valid_url = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            valid_url = valid_url and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            valid_url = valid_url and not parsed.path.rstrip("/").endswith("/responses")
            # HTTP is permitted only for local mock servers, never for remote API keys.
            valid_url = valid_url and (parsed.scheme == "https" or parsed.hostname in {"127.0.0.1", "localhost", "::1"})
            _ = parsed.port
        except ValueError:
            valid_url = False
        if enabled and (not valid_url or not deployment):
            raise ConfigurationError(f"{provider_id}: set a valid base_url (without /responses) and deployment")
        api_key_env = _text(raw, "api_key_env", provider_id.upper() + "_API_KEY")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
            raise ConfigurationError(f"{provider_id}: invalid environment variable name")
        providers.append(Provider(
            id=provider_id, base_url=base_url, deployment=deployment, api_key_env=api_key_env,
            enabled=enabled, auth_type=auth_type, api_version=_text(raw, "api_version"),
            cooldown_seconds=_positive(raw, "cooldown_seconds", 60, zero=True, maximum=86400),
            label=_text(raw, "label")[:80], **provider_token_limits(raw),
        ))
    level = _text(server, "log_level", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigurationError("Invalid log_level")
    public_model = _text(server, "public_model", "gpt-6-astra")
    if not public_model:
        raise ConfigurationError("public_model cannot be empty")
    reconnect_failover = server.get("reconnect_failover", False)
    if not isinstance(reconnect_failover, bool):
        raise ConfigurationError("reconnect_failover must be true or false")
    rotate_on_failure = server.get("rotate_on_failure", False)
    if not isinstance(rotate_on_failure, bool):
        raise ConfigurationError("rotate_on_failure must be true or false")
    limits = {}
    for name, default, maximum in (("max_connections", 100, 1024),
                                   ("max_request_bytes", 16 * 1024 * 1024, 64 * 1024 * 1024)):
        value = _positive(server, name, default, maximum=maximum)
        if not isinstance(server.get(name, default), int):
            raise ConfigurationError(f"{name}: expected an integer")
        limits[name] = int(value)
    for name, default in (("codex_stream_max_retries", 10), ("codex_request_max_retries", 4)):
        value = _positive(server, name, default, zero=True, maximum=20)
        if not isinstance(server.get(name, default), int):
            raise ConfigurationError(f"{name}: expected an integer between 0 and 20")
        limits[name] = int(value)
    return Settings(
        providers=tuple(providers), public_model=public_model, log_level=level,
        reconnect_failover=reconnect_failover,
        rotate_on_failure=rotate_on_failure,
        reconnect_cooldown_seconds=_positive(server, "reconnect_cooldown_seconds", 30, maximum=86400),
        cooldown_wait_seconds=_positive(server, "cooldown_wait_seconds", 0, zero=True, maximum=900),
        proxy_token_env=_text(server, "proxy_token_env", "LOCAL_RESPONSES_PROXY_TOKEN"),
        connect_timeout_seconds=_positive(server, "connect_timeout_seconds", 10, maximum=300),
        read_timeout_seconds=_positive(server, "read_timeout_seconds", 120, maximum=3600),
        write_timeout_seconds=_positive(server, "write_timeout_seconds", 30, maximum=300),
        pool_timeout_seconds=_positive(server, "pool_timeout_seconds", 10, maximum=300), **limits,
    )
