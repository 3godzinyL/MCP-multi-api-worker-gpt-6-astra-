"""Atomic, validated provider edits. Keys never enter TOML or browser responses."""
from __future__ import annotations

import os
import ipaddress
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import tomlkit

from proxy.config import load_config, provider_token_limits
from proxy.credentials import get_secret, set_secret, delete_secret


# Keep this literal-address policy aligned with src/proxy/network.rs. DNS is
# deliberately checked only by Rust's resolver, on the connection it will use.
_BLOCKED_IPV4 = tuple(ipaddress.ip_network(value) for value in (
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "224.0.0.0/3", "100.64.0.0/10",
    "169.254.0.0/16", "168.63.129.16/32", "172.16.0.0/12", "192.168.0.0/16",
    "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24",
))
_GLOBAL_IPV6 = ipaddress.ip_network("2000::/3")
_BLOCKED_IPV6 = tuple(ipaddress.ip_network(value) for value in (
    "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20",
))


def _public_address(address):
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return _public_address(address.ipv4_mapped)
        return address in _GLOBAL_IPV6 and not any(address in network for network in _BLOCKED_IPV6)
    return not any(address in network for network in _BLOCKED_IPV4)


def _validate_catalog_urls(settings, document):
    allow_loopback = document.unwrap().get("proxy", {}).get("allow_loopback_upstreams", False)
    if not isinstance(allow_loopback, bool):
        raise ValueError("Niepoprawne pole: allow_loopback_upstreams")
    for provider in settings.providers:
        if not provider.enabled:
            continue
        invalid = ValueError("Adres API musi używać publicznego HTTPS. Lokalny mock wymaga allow_loopback_upstreams=true.")
        value = provider.base_url
        if "\\" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise invalid
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
                    or parsed.password is not None or "?" in value or "#" in value
                    or parsed.path.rstrip("/").endswith("/responses") or "%" in host):
                raise invalid
            _ = parsed.port
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                # IDNA normalization also catches Unicode dots and local suffixes.
                host = host.encode("idna").decode("ascii").lower()
                local = host == "localhost"
                if not local:
                    if ("." not in host or host.endswith((".", ".localhost", ".local", ".internal"))
                            or re.fullmatch(r"[a-z0-9_.-]+", host) is None):
                        raise invalid
                    # WHATWG URL parsing recognizes shortened, integer, octal and
                    # hex IPv4 spellings that ipaddress intentionally rejects.
                    # Require canonical literals, never reinterpret these as DNS.
                    last_label = host.rsplit(".", 1)[-1]
                    if last_label.isdigit() or re.fullmatch(r"0x[0-9a-f]*", last_label):
                        raise invalid
            else:
                local = address.is_loopback
                if not local and not _public_address(address):
                    raise invalid
            if (local and not allow_loopback) or (parsed.scheme != "https" and not (local and allow_loopback)):
                raise invalid
        except (UnicodeError, ValueError):
            raise invalid from None


def label(provider):
    host = urlsplit(provider.base_url).hostname or provider.id
    return provider.label or ("Azure " + host.split("-resource")[0].rsplit("-", 1)[-1] if "azure.com" in host else host)


def public_provider(provider):
    return {"id": provider.id, "label": label(provider), "base_url": provider.base_url,
            "model": provider.deployment, "deployment": provider.deployment,
            "enabled": provider.enabled, "auth_type": provider.auth_type,
            "api_version": provider.api_version, "cooldown_seconds": provider.cooldown_seconds,
            "tokens_per_minute": provider.tokens_per_minute,
            "soft_tokens_per_minute": provider.soft_tokens_per_minute,
            "hard_tokens_per_minute": provider.hard_tokens_per_minute,
            "configured": bool(get_secret(provider.id, provider.api_key_env))}


class ProviderCatalog:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def change(self, body):
        with self.lock:
            previous_settings = load_config(self.path)
            document = tomlkit.parse(self.path.read_text(encoding="utf-8"))
            providers = document.get("providers", [])
            provider_id = body.get("id")
            entry = next((p for p in providers if p["id"] == provider_id), None)
            removing = body.get("delete") is True
            if provider_id and entry is None:
                raise ValueError("Nie znaleziono tego API. Odśwież listę.")
            if removing:
                if len(providers) <= 1:
                    raise ValueError("Pozostaw przynajmniej jedno API.")
                document["providers"] = [p for p in providers if p["id"] != provider_id]
            else:
                if entry is None:
                    if len(providers) >= 32:
                        raise ValueError("Panel obsługuje do 32 połączeń API.")
                    provider_id = "api_" + secrets.token_hex(5)
                    entry = tomlkit.table()
                    entry.update(id=provider_id, api_key_env=provider_id.upper() + "_API_KEY")
                    document["providers"].append(entry)
                for name, limit in (("label", 80), ("base_url", 2000), ("deployment", 200), ("api_version", 100)):
                    value = body.get(name, entry.get(name, ""))
                    if not isinstance(value, str) or len(value) > limit or "\n" in value or "\r" in value:
                        raise ValueError("Niepoprawne pole: " + name)
                    entry[name] = value.strip()
                # Accept the project URLs pasted from Azure Foundry's project page.
                parsed = urlsplit(entry["base_url"])
                if parsed.hostname and parsed.hostname.endswith(".services.ai.azure.com") and parsed.path.startswith("/api/projects/"):
                    entry["base_url"] = parsed._replace(path="/openai/v1").geturl()
                for name in ("base_url", "deployment"):
                    if name in body:
                        entry.pop(name + "_env", None)
                entry["enabled"] = body.get("enabled", entry.get("enabled", True))
                entry["auth_type"] = body.get("auth_type", entry.get("auth_type", "api-key"))
                cooldown = body.get("cooldown_seconds", entry.get("cooldown_seconds", 60))
                if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or not 1 <= cooldown <= 3600:
                    raise ValueError("Przerwa po limicie musi wynosić od 1 do 3600 sekund.")
                entry["cooldown_seconds"] = cooldown
                entry.update(provider_token_limits({**entry.unwrap(), **body}))
            key = body.get("key", "")
            if not isinstance(key, str) or len(key) > 4096 or any(c.isspace() for c in key.strip()):
                raise ValueError("Klucz API musi mieścić się w jednej linii.")
            candidate = self.path.with_name(".providers-" + secrets.token_hex(5) + ".toml")
            previous_key = None
            changed_key = False
            try:
                candidate.write_text(tomlkit.dumps(document), encoding="utf-8")
                settings = load_config(candidate)
                _validate_catalog_urls(settings, document)
                old = next((p for p in previous_settings.providers if p.id == provider_id), None)
                new = next((p for p in settings.providers if p.id == provider_id), None)
                if old and new:
                    changed_destination = (old.base_url, old.auth_type, old.api_key_env) != (new.base_url, new.auth_type, new.api_key_env)
                    if changed_destination and get_secret(provider_id, old.api_key_env) and not key.strip():
                        raise ValueError("Zmiana adresu API lub uwierzytelniania wymaga ponownego podania klucza dla nowego połączenia.")
                    environment_key = os.environ.get(new.api_key_env, "").strip()
                    if key.strip() and environment_key and environment_key != key.strip():
                        raise ValueError("Klucz ze zmiennej środowiskowej ma pierwszeństwo. Zaktualizuj ją lub usuń przed zmianą klucza w panelu.")
                if key.strip() and not removing:
                    previous_key = get_secret(provider_id)
                    set_secret(provider_id, key.strip())
                    changed_key = True
                elif not removing and not get_secret(provider_id, entry.get("api_key_env", "")):
                    raise ValueError("Wklej klucz nowego API.")
                backup = self.path.parent / "backups" / "provider-edits"
                backup.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.path, backup / (str(time.time_ns()) + ".toml"))
                os.replace(candidate, self.path)
            except Exception:
                if changed_key:
                    set_secret(provider_id, previous_key) if previous_key else delete_secret(provider_id)
                raise
            finally:
                candidate.unlink(missing_ok=True)
            if removing:
                delete_secret(provider_id)
            return settings, provider_id
