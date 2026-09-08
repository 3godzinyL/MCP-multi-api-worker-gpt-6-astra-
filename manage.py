from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import tomlkit

from proxy.config import ConfigurationError, load_config
from proxy.credentials import CredentialError, LOCAL_TOKEN_ID, get_secret, set_secret
from proxy.dpapi import decrypt, encrypt

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "providers.toml"


def initialize():
    if not CONFIG.exists():
        CONFIG.write_bytes((ROOT / "providers.example.toml").read_bytes())
        print("Created providers.toml; configure the three endpoints and deployments.")
    settings = load_config(CONFIG)
    if not get_secret(LOCAL_TOKEN_ID, settings.proxy_token_env):
        set_secret(LOCAL_TOKEN_ID, secrets.token_urlsafe(48))
        print("Local proxy token saved in Windows Credential Manager.")


def proxy_running() -> bool:
    settings = load_config(CONFIG)
    token = get_secret(LOCAL_TOKEN_ID, settings.proxy_token_env)
    if not token:
        return False
    try:
        with httpx.Client(trust_env=False, timeout=3, follow_redirects=False) as client:
            response = client.get("http://127.0.0.1:4100/status", headers={"authorization": "Bearer " + token})
            response.raise_for_status()
            status = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    # A proxy in cooldown is still running and must not be started twice.
    return (isinstance(status, dict) and status.get("status") in {"ready", "unavailable"}
            and isinstance(status.get("stats"), dict)
            and isinstance(status.get("providers"), list)
            and all(isinstance(provider, dict) for provider in status["providers"])
            and [provider.get("id") for provider in status["providers"]] == [p.id for p in settings.providers])


def codex_path():
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"


def configure_codex():
    settings = load_config(CONFIG)
    if not get_secret(LOCAL_TOKEN_ID, settings.proxy_token_env):
        raise ConfigurationError("Run manage.py init first")
    path = codex_path()
    original = path.read_bytes() if path.exists() else b""
    try:
        doc = tomlkit.parse(original.decode("utf-8-sig"))
    except Exception:
        raise ConfigurationError("Cannot parse the existing Codex configuration") from None
    doc["model"] = settings.public_model
    doc["model_provider"] = "local_proxy"
    # Keep other providers/profiles available for rollback. Replace only our own entry.
    if "model_providers" not in doc:
        doc["model_providers"] = tomlkit.table()
    providers = doc["model_providers"]
    provider = tomlkit.table()
    provider.update({
        "name": "Local Responses Proxy", "base_url": "http://127.0.0.1:4100/v1",
        "wire_api": "responses", "request_max_retries": settings.codex_request_max_retries,
        "stream_max_retries": settings.codex_stream_max_retries if settings.reconnect_failover else 0,
        "stream_idle_timeout_ms": 600000, "supports_websockets": False,
    })
    auth = tomlkit.table()
    auth.update({"command": str(Path(sys.executable).resolve()),
                 "args": [str(ROOT / "manage.py"), "codex-token"],
                 "timeout_ms": 5000, "refresh_interval_ms": 300000})
    provider["auth"] = auth
    providers["local_proxy"] = provider
    if "features" not in doc:
        doc["features"] = tomlkit.table()
    doc["features"]["enable_request_compression"] = False
    doc["features"]["responses_websockets"] = False
    doc["features"]["responses_websockets_v2"] = False
    # Stateless input is what permits routing successive turns to independent deployments.
    # Codex sends store=false for Responses; no provider-specific response state is configured.
    result = tomlkit.dumps(doc).encode("utf-8")
    tomlkit.parse(result.decode("utf-8"))
    if result == original:
        print("Codex is already configured for the local proxy.")
        return
    backup = None
    if original:
        backup_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CodexLocalResponsesProxy" / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = backup_root / ("config-" + stamp + ".toml.dpapi")
        backup.write_bytes(encrypt(original))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("config.local-proxy.tmp")
    temporary.write_bytes(result)
    temporary.replace(path)
    print("Codex endpoint: http://127.0.0.1:4100/v1")
    print("Codex stream reconnect attempts:", provider["stream_max_retries"])
    print("Codex HTTP retry attempts:", provider["request_max_retries"])
    print("HTTP failover and backend selection are handled by the local proxy.")
    if backup:
        print("Encrypted backup:", backup)
    print("Restart Codex and use a new task so it reads the provider settings.")


async def check_upstreams(generate: bool):
    settings = load_config(CONFIG)
    async with httpx.AsyncClient(timeout=90, trust_env=False, follow_redirects=False) as client:
        async def check(provider):
            if not provider.enabled:
                return {"provider": provider.id, "enabled": False}
            key = get_secret(provider.id, provider.api_key_env)
            if not key:
                return {"provider": provider.id, "configured": False}
            headers = {"api-key": key} if provider.auth_type == "api-key" else {"authorization": "Bearer " + key}
            params = {"api-version": provider.api_version} if provider.api_version else None
            try:
                if generate:
                    response = await client.post(provider.url(), headers=headers, params=params, json={
                        "model": provider.deployment, "input": "Reply with OK.", "store": False,
                        "max_output_tokens": 32, "reasoning": {"effort": "low"},
                    })
                    return {"provider": provider.id, "deployment": provider.deployment, "http_status": response.status_code}
                response = await client.get(provider.base_url.rstrip("/") + "/models", headers=headers, params=params)
                result = {"provider": provider.id, "http_status": response.status_code}
                if response.status_code == 200:
                    models = [entry.get("id") for entry in response.json().get("data", [])]
                    result["model_count"] = len(models)
                    result["deployment_in_catalog"] = provider.deployment in models
                return result
            except httpx.TransportError as exc:
                return {"provider": provider.id, "error": type(exc).__name__}
        for result in await asyncio.gather(*(check(p) for p in settings.providers)):
            print(json.dumps(result, ensure_ascii=True))


def main():
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True
    parser = argparse.ArgumentParser(description="Configure credentials, providers and Codex locally")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    key_parser = sub.add_parser("set-key")
    key_parser.add_argument("provider", nargs="?", choices=["provider1", "provider2", "provider3"])
    sub.add_parser("codex-token", help="Credential helper used by Codex; outputs only its local token")
    sub.add_parser("configure-codex")
    sub.add_parser("status")
    sub.add_parser("check-running", help="Exit successfully when the authenticated local proxy is already running")
    sub.add_parser("import-secrets-stdin", help="Import a JSON object from stdin without echoing secrets")
    checks = sub.add_parser("check-upstreams")
    checks.add_argument("--generate", action="store_true", help="Make one small real generation per enabled provider")
    restore = sub.add_parser("restore-codex")
    restore.add_argument("backup", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize()
        elif args.command == "check-running":
            return 0 if proxy_running() else 1
        elif args.command == "set-key":
            for name in ([args.provider] if args.provider else ["provider1", "provider2", "provider3"]):
                value = getpass.getpass(f"API key for {name} (hidden; Enter keeps current): ").strip()
                if value:
                    set_secret(name, value)
                    print(name + ": saved in Windows Credential Manager")
            print("Restart the proxy after changing provider keys.")
        elif args.command == "import-secrets-stdin":
            values = json.load(sys.stdin)
            if not isinstance(values, dict) or set(values) - {"provider1", "provider2", "provider3"}:
                raise ConfigurationError("Expected keys provider1, provider2 and provider3")
            for name, value in values.items():
                if not isinstance(value, str):
                    raise ConfigurationError("Keys must be text")
                set_secret(name, value.strip())
                print(name + ": credential saved")
        elif args.command == "codex-token":
            token = get_secret(LOCAL_TOKEN_ID, load_config(CONFIG).proxy_token_env)
            if not token:
                raise CredentialError("Missing local proxy token")
            print(token)
        elif args.command == "configure-codex":
            configure_codex()
        elif args.command == "restore-codex":
            original = decrypt(args.backup.read_bytes())
            tomlkit.parse(original.decode("utf-8-sig"))
            codex_path().write_bytes(original)
            print("Previous Codex configuration restored. Restart Codex.")
        elif args.command == "check-upstreams":
            asyncio.run(check_upstreams(args.generate))
        elif args.command == "status":
            token = get_secret(LOCAL_TOKEN_ID, load_config(CONFIG).proxy_token_env)
            if not token:
                raise CredentialError("Missing local proxy token")
            with httpx.Client(trust_env=False, timeout=5) as client:
                response = client.get("http://127.0.0.1:4100/status", headers={"authorization": "Bearer " + token})
                print(json.dumps(response.json(), indent=2))
                response.raise_for_status()
        return 0
    except (ConfigurationError, CredentialError, OSError, ValueError, httpx.HTTPError, RuntimeError) as exc:
        # Exception text from network clients and vaults may contain sensitive details.
        if isinstance(exc, (ConfigurationError, CredentialError)):
            print(str(exc), file=sys.stderr)
        else:
            print("Operation failed: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
