import os
from pathlib import Path

import pytest

from proxy.catalog import ProviderCatalog, public_provider
from proxy.config import ConfigurationError, load_config
from proxy.credentials import get_secret
from proxy.dpapi import decrypt, encrypt

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_has_three_disabled_providers():
    settings = load_config(ROOT / "providers.example.toml")
    assert len(settings.providers) == 3
    assert not any(p.enabled for p in settings.providers)
    assert settings.cooldown_wait_seconds == 180
    assert settings.rotate_on_failure
    assert settings.codex_stream_max_retries == 10
    assert settings.codex_request_max_retries == 4
    for provider in settings.providers:
        assert provider.tokens_per_minute == 1_000_000
        assert provider.soft_tokens_per_minute == 900_000
        assert provider.hard_tokens_per_minute == 950_000


def test_provider_token_limits_preserve_defaults_for_existing_config(tmp_path):
    path = tmp_path / "providers.toml"
    path.write_text("[[providers]]\nid='old'\nenabled=false\n")
    provider = load_config(path).providers[0]
    assert provider.tokens_per_minute == 1_000_000
    assert provider.soft_tokens_per_minute == 900_000
    assert provider.hard_tokens_per_minute == 950_000


@pytest.mark.parametrize("limit,soft,hard", [(100, 90, 95), (2, 1, 2), (1_000_000_000, 1, 1_000_000_000)])
def test_provider_token_limits_are_independent_and_accept_boundaries(tmp_path, limit, soft, hard):
    path = tmp_path / "providers.toml"
    path.write_text(
        "[[providers]]\nid='default'\nenabled=false\n"
        "[[providers]]\nid='custom'\nenabled=false\n"
        f"tokens_per_minute={limit}\nsoft_tokens_per_minute={soft}\nhard_tokens_per_minute={hard}\n")
    first, second = load_config(path).providers
    assert (first.tokens_per_minute, first.soft_tokens_per_minute, first.hard_tokens_per_minute) == (
        1_000_000, 900_000, 950_000)
    assert (second.tokens_per_minute, second.soft_tokens_per_minute, second.hard_tokens_per_minute) == (
        limit, soft, hard)


@pytest.mark.parametrize("name", ["tokens_per_minute", "soft_tokens_per_minute", "hard_tokens_per_minute"])
@pytest.mark.parametrize("value", ["true", "false", "1.0", "1.5", '"1000000"', "nan", "inf",
                                  "0", "-1", "1000000001", "9" * 400])
def test_provider_token_limits_reject_non_integer_or_out_of_range_values(tmp_path, name, value):
    path = tmp_path / "providers.toml"
    path.write_text(f"[[providers]]\nid='invalid'\nenabled=false\n{name}={value}\n")
    with pytest.raises(ConfigurationError, match=name):
        load_config(path)


@pytest.mark.parametrize("limit,soft,hard", [(100, 90, 90), (100, 95, 90), (100, 90, 101), (100, 101, 102)])
def test_provider_token_limits_reject_invalid_relations(tmp_path, limit, soft, hard):
    path = tmp_path / "providers.toml"
    path.write_text(
        "[[providers]]\nid='invalid'\nenabled=false\n"
        f"tokens_per_minute={limit}\nsoft_tokens_per_minute={soft}\nhard_tokens_per_minute={hard}\n")
    with pytest.raises(ConfigurationError, match="soft_tokens_per_minute < hard_tokens_per_minute <= tokens_per_minute"):
        load_config(path)


def test_catalog_token_limits_survive_edit_reload_and_public_serialization(tmp_path, monkeypatch):
    monkeypatch.setattr("proxy.catalog.get_secret", lambda *args: "fixture-key")
    path = tmp_path / "providers.toml"
    path.write_text("[[providers]]\nid='first'\nenabled=false\n")
    catalog = ProviderCatalog(path)
    catalog.change({"id": "first", "tokens_per_minute": 100, "soft_tokens_per_minute": 90,
                    "hard_tokens_per_minute": 95})
    catalog.change({"id": "first", "label": "Unrelated edit"})
    provider = load_config(path).providers[0]
    assert (provider.tokens_per_minute, provider.soft_tokens_per_minute, provider.hard_tokens_per_minute) == (
        100, 90, 95)
    data = public_provider(provider)
    assert {name: data[name] for name in (
        "tokens_per_minute", "soft_tokens_per_minute", "hard_tokens_per_minute")} == {
            "tokens_per_minute": 100, "soft_tokens_per_minute": 90, "hard_tokens_per_minute": 95}


def test_catalog_token_limits_default_for_new_provider(tmp_path, monkeypatch):
    monkeypatch.setattr("proxy.catalog.get_secret", lambda *args: "fixture-key")
    path = tmp_path / "providers.toml"
    path.write_text("[[providers]]\nid='first'\nenabled=false\n")
    settings, provider_id = ProviderCatalog(path).change({"enabled": False, "label": "Second"})
    provider = next(provider for provider in settings.providers if provider.id == provider_id)
    assert (provider.tokens_per_minute, provider.soft_tokens_per_minute, provider.hard_tokens_per_minute) == (
        1_000_000, 900_000, 950_000)
    assert load_config(path) == settings


@pytest.mark.parametrize("limits", [
    {"tokens_per_minute": True},
    {"tokens_per_minute": 1_000_000.0},
    {"soft_tokens_per_minute": False},
    {"soft_tokens_per_minute": 900_000.0},
    {"hard_tokens_per_minute": True},
    {"hard_tokens_per_minute": 950_000.0},
    {"tokens_per_minute": 1_000_000_001},
    {"soft_tokens_per_minute": 0},
    {"hard_tokens_per_minute": 900_000},
    {"hard_tokens_per_minute": 1_000_001},
])
def test_catalog_rejects_invalid_token_limits_without_mutation(tmp_path, monkeypatch, limits):
    monkeypatch.setattr("proxy.catalog.get_secret", lambda *args: "fixture-key")
    monkeypatch.setattr("proxy.catalog.set_secret", lambda *args: pytest.fail("Invalid limits changed a secret"))
    monkeypatch.setattr("proxy.catalog.delete_secret", lambda *args: pytest.fail("Invalid limits deleted a secret"))
    path = tmp_path / "providers.toml"
    original = "[[providers]]\nid='first'\nenabled=false\n"
    path.write_text(original)
    with pytest.raises(ConfigurationError, match="tokens_per_minute"):
        ProviderCatalog(path).change({"id": "first", "key": "new-fixture-key", **limits})
    assert path.read_text() == original
    assert not list(tmp_path.glob(".providers-*.toml"))
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("name,value", [
    ("codex_stream_max_retries", "-1"),
    ("codex_stream_max_retries", "2.5"),
    ("codex_stream_max_retries", "true"),
    ("codex_stream_max_retries", "4294967296"),
    ("codex_stream_max_retries", "21"),
    ("codex_stream_max_retries", "2.0"),
    ("codex_stream_max_retries", "9" * 400),
    ("codex_request_max_retries", '"4"'),
    ("rotate_on_failure", '"true"'),
])
def test_invalid_retry_settings_are_rejected(tmp_path, name, value):
    original = {"codex_stream_max_retries": "10", "codex_request_max_retries": "4", "rotate_on_failure": "true"}[name]
    content = (ROOT / "providers.example.toml").read_text().replace(f"{name} = {original}", f"{name} = {value}")
    path = tmp_path / "providers.toml"
    path.write_text(content)
    with pytest.raises(ConfigurationError, match=name):
        load_config(path)


@pytest.mark.parametrize("value", ['-1', 'true', '"180"', 'nan', 'inf', '901'])
def test_invalid_cooldown_wait_is_rejected(tmp_path, value):
    content = (ROOT / "providers.example.toml").read_text().replace(
        'cooldown_wait_seconds = 180', 'cooldown_wait_seconds = ' + value)
    path = tmp_path / "providers.toml"
    path.write_text(content)
    with pytest.raises(ConfigurationError, match="cooldown_wait_seconds"):
        load_config(path)


@pytest.mark.parametrize("name,value", [
    ("connect_timeout_seconds", 301), ("read_timeout_seconds", 3601),
    ("write_timeout_seconds", 301), ("pool_timeout_seconds", 301),
    ("reconnect_cooldown_seconds", 86401), ("max_connections", 1025),
    ("max_request_bytes", 64 * 1024 * 1024 + 1),
])
def test_worker_and_rust_config_limits_match(tmp_path, name, value):
    path = tmp_path / "providers.toml"
    path.write_text(f"[proxy]\n{name}={value}\n[[providers]]\nid='disabled'\nenabled=false\n")
    with pytest.raises(ConfigurationError, match=name):
        load_config(path)


def test_config_env_overrides(tmp_path, monkeypatch):
    content = (ROOT / "providers.example.toml").read_text()
    content = content.replace('enabled = false', 'enabled = true', 1)
    path = tmp_path / "providers.toml"
    path.write_text(content)
    monkeypatch.setenv("AZURE_PROVIDER_1_ENDPOINT", "https://example.com/openai/v1")
    monkeypatch.setenv("AZURE_PROVIDER_1_DEPLOYMENT", "my-deployment")
    settings = load_config(path)
    assert settings.providers[0].url() == "https://example.com/openai/v1/responses"
    assert settings.providers[0].deployment == "my-deployment"


@pytest.mark.parametrize("url", ["http://remote.example/v1", "https://secret@host/v1", "https://host/v1?api-key=bad", "https://host/v1/responses"])
def test_invalid_upstream_urls_do_not_leak_in_error(tmp_path, monkeypatch, url):
    content = (ROOT / "providers.example.toml").read_text().replace('enabled = false', 'enabled = true', 1)
    path = tmp_path / "providers.toml"
    path.write_text(content)
    monkeypatch.setenv("AZURE_PROVIDER_1_ENDPOINT", url)
    monkeypatch.setenv("AZURE_PROVIDER_1_DEPLOYMENT", "model")
    with pytest.raises(ConfigurationError) as exc:
        load_config(path)
    assert url not in str(exc.value)


def test_environment_secret_takes_priority(monkeypatch):
    monkeypatch.setenv("PROXY_TEST_SECRET", "test-only")
    assert get_secret("nonexistent", "PROXY_TEST_SECRET") == "test-only"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_backup_is_encrypted_and_recoverable():
    source = b'test_config = "private-backup-value"\n'
    encrypted = encrypt(source)
    assert b"private-backup-value" not in encrypted
    assert decrypt(encrypted) == source
