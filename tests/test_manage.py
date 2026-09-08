import os

import pytest
import tomlkit

import manage
from proxy.dpapi import decrypt


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI backup")
@pytest.mark.parametrize("reconnect,stream_retries,http_retries", [(True, 10, 4), (False, 10, 4), (True, 7, 2)])
def test_configure_codex_preserves_other_providers_and_settings_and_is_idempotent(tmp_path, monkeypatch, reconnect, stream_retries, http_retries):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    original = ('model = "old-model"\nmodel_provider = "azure"\nmodel_reasoning_effort = "high"\n'
                'openai_base_url = "https://original.example/v1"\n'
                '[model_providers.azure]\nbase_url = "https://private.example/v1"\n'
                'http_headers = { "api-key" = "sensitive-test-key" }\n'
                '[model_providers.local_proxy]\nexperimental_bearer_token = "old-local-token"\n'
                '[profiles.direct]\nmodel_provider = "azure"\n'
                '[features]\njs_repl = false\n[plugins.test]\nenabled = true\n')
    path = codex_home / "config.toml"
    path.write_text(original, encoding="utf-8")
    original_bytes = path.read_bytes()
    providers_path = tmp_path / "providers.toml"
    template = (manage.ROOT / "providers.example.toml").read_text(encoding="utf-8")
    template = template.replace("reconnect_failover = true", "reconnect_failover = " + str(reconnect).lower())
    template = template.replace("codex_stream_max_retries = 10", f"codex_stream_max_retries = {stream_retries}")
    template = template.replace("codex_request_max_retries = 4", f"codex_request_max_retries = {http_retries}")
    providers_path.write_text(template, encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(manage, "CONFIG", providers_path)
    monkeypatch.setattr(manage, "get_secret", lambda *_: "fake-local-token")
    manage.configure_codex()
    result = path.read_text(encoding="utf-8")
    doc = tomlkit.parse(result)
    assert doc["model_providers"]["azure"]["base_url"] == "https://private.example/v1"
    assert doc["model_providers"]["azure"]["http_headers"]["api-key"] == "sensitive-test-key"
    assert doc["profiles"]["direct"]["model_provider"] == "azure"
    assert doc["openai_base_url"] == "https://original.example/v1"
    assert "old-local-token" not in result
    assert doc["model_provider"] == "local_proxy"
    assert list(doc["model_providers"]) == ["azure", "local_proxy"]
    provider = doc["model_providers"]["local_proxy"]
    assert provider["base_url"] == "http://127.0.0.1:4100/v1"
    assert provider["request_max_retries"] == http_retries
    assert provider["stream_max_retries"] == (stream_retries if reconnect else 0)
    assert provider["auth"]["args"][-1] == "codex-token"
    assert not provider["supports_websockets"]
    assert doc["model_reasoning_effort"] == "high"
    assert doc["plugins"]["test"]["enabled"]
    assert not doc["features"]["js_repl"]
    backups = list((tmp_path / "local").rglob("*.dpapi"))
    assert len(backups) == 1
    assert b"sensitive-test-key" not in backups[0].read_bytes()
    assert decrypt(backups[0].read_bytes()) == original_bytes
    manage.configure_codex()
    assert path.read_text(encoding="utf-8") == result
    assert len(list((tmp_path / "local").rglob("*.dpapi"))) == 1
