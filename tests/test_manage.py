import os
from types import SimpleNamespace

import httpx
import psutil
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


@pytest.fixture
def rust_installation(tmp_path, monkeypatch):
    root = tmp_path / "installation with spaces"
    (root / "dashboard").mkdir(parents=True)
    (root / "dashboard" / "sidecar.py").touch()
    executable = root / "target" / "release" / "3api.exe"
    arguments = [str(executable), "serve", "--project-dir", str(root),
                 "--config", str(root / "providers.toml"), "--data-dir", str(root / "data" / "rust")]
    waited = []
    process = SimpleNamespace(pid=87654, exe=lambda: str(executable), cmdline=lambda: arguments,
                              cwd=lambda: str(root), is_running=lambda: True,
                              wait=lambda timeout: waited.append(timeout) or 0)
    listeners = [SimpleNamespace(pid=process.pid, status=psutil.CONN_LISTEN,
                                 laddr=SimpleNamespace(ip="127.0.0.1", port=port)) for port in (14100, 14101)]
    monkeypatch.setattr(manage.psutil, "net_connections", lambda **_: listeners)
    monkeypatch.setattr(manage.psutil, "Process", lambda pid: process)
    return root, process, arguments, listeners, waited


@pytest.mark.parametrize("variant", ["source", "packaged", "equals_arguments", "relative_arguments"])
def test_running_application_recognizes_only_this_installation(rust_installation, variant):
    root, process, arguments, _, _ = rust_installation
    if variant == "packaged":
        process.exe = lambda: str(root / "3api.exe")
        process.cwd = lambda: str(root.parent)
        arguments[:] = [str(root / "3api.exe"), "serve"]
    elif variant == "equals_arguments":
        arguments[:] = [arguments[0], "serve", "--project-dir=" + str(root),
                        "--config=" + str(root / "providers.toml"), "--data-dir=" + str(root / "data" / "rust")]
    elif variant == "relative_arguments":
        arguments[:] = [arguments[0], "serve", "--config", "providers.toml", "--data-dir", "data/rust"]
    assert manage.running_application(root, 14100, 14101) is process


@pytest.mark.parametrize("variant", ["foreign_exe", "foreign_data", "foreign_config", "foreign_project", "proxy_only",
                                     "different_owners", "missing_port", "public_binding"])
def test_stop_refuses_foreign_or_ambiguous_installations(rust_installation, variant):
    root, process, arguments, listeners, waited = rust_installation
    if variant == "foreign_exe":
        process.exe = lambda: str(root.parent / "other" / "3api.exe")
    elif variant in {"foreign_data", "foreign_config", "foreign_project"}:
        option = {"foreign_data": "--data-dir", "foreign_config": "--config", "foreign_project": "--project-dir"}[variant]
        arguments[arguments.index(option) + 1] = str(root.parent / "other")
    elif variant == "proxy_only":
        arguments[1] = "proxy"
    elif variant == "different_owners":
        listeners[1].pid += 1
    elif variant == "missing_port":
        listeners.pop()
    elif variant == "public_binding":
        listeners[0].laddr.ip = "0.0.0.0"
    with pytest.raises(manage.LifecycleError, match="inna instalacja"):
        manage.stop_application(root, 14100, 14101, transport=httpx.MockTransport(lambda _: pytest.fail("No HTTP to foreign processes")))
    assert not waited


@pytest.mark.parametrize("active_state", [None, "running", "awaiting_input", "finalizing"])
def test_stop_uses_session_and_stops_active_work_before_shutdown(rust_installation, active_state):
    root, _, _, _, waited = rust_installation
    note = root / "notes.txt"
    note.write_text("Keep my notes", encoding="utf-8")
    calls = []
    active = bool(active_state)

    def handler(request):
        nonlocal active
        path = request.url.path
        calls.append((request.method, path))
        assert "authorization" not in request.headers
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "application": "3api-rust-panel"})
        if path == "/ui/":
            return httpx.Response(200, text='<meta name="csrf-token" content="fixture-csrf">',
                                  headers={"set-cookie": "fixture-session=present; HttpOnly; SameSite=Strict; Path=/"})
        assert "fixture-session=present" in request.headers["cookie"]
        if request.method == "POST":
            assert request.headers["origin"] == "http://127.0.0.1:14101"
            assert request.headers["x-panel-csrf"] == "fixture-csrf"
        if path == "/ui/api/shutdown":
            return httpx.Response(409 if active else 200, json={"ok": not active})
        if path == "/ui/api/state":
            return httpx.Response(200, json={"tasks": [{"id": "saved-task", "run_id": "saved-run", "state": active_state}]})
        if path == "/ui/api/tasks/saved-task/stop":
            active = False
            return httpx.Response(202, json={"state": "stopping"})
        pytest.fail("Unexpected management request")

    result = manage.stop_application(root, 14100, 14101, transport=httpx.MockTransport(handler))
    assert "3api zatrzymane" in result
    assert len(waited) == 1
    assert calls.count(("POST", "/ui/api/shutdown")) == (2 if active_state else 1)
    assert calls.count(("POST", "/ui/api/tasks/saved-task/stop")) == bool(active_state)
    assert note.read_text(encoding="utf-8") == "Keep my notes"


def test_repeated_stop_does_not_contact_any_server(rust_installation):
    root, _, _, listeners, waited = rust_installation
    listeners.clear()
    result = manage.stop_application(root, 14100, 14101, transport=httpx.MockTransport(lambda _: pytest.fail("Already stopped")))
    assert "juz zatrzymane" in result
    assert not waited


@pytest.mark.parametrize("status", [302, 401, 403, 500])
def test_stop_never_reports_success_for_rejected_shutdown(rust_installation, status):
    root, _, _, _, waited = rust_installation
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"application": "3api-rust-panel"})
        if request.url.path == "/ui/":
            return httpx.Response(200, text='<meta name="csrf-token" content="fixture-csrf">',
                                  headers={"set-cookie": "session=present; Path=/"})
        return httpx.Response(status, text="Private upstream diagnostic must not be displayed",
                              headers={"location": "https://foreign.example/"})
    with pytest.raises(manage.LifecycleError) as error:
        manage.stop_application(root, 14100, 14101, transport=httpx.MockTransport(handler))
    assert "Private upstream" not in str(error.value)
    assert not waited
