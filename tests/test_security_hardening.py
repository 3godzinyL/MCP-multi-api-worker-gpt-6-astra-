"""Adversarial, offline checks for the private Python compatibility boundary."""
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import httpx
import pytest
import psutil
from starlette.testclient import TestClient

from dashboard.runner import CodexRunner, WORKER_ENV_ALLOWLIST, codex_command
from dashboard.server import SidecarSecurityMiddleware, create_app
from dashboard.sidecar import main as sidecar_main
from proxy.catalog import ProviderCatalog
from tests.test_dashboard import make_runner, wait_for

SIDECAR_TOKEN = "fixture-private-sidecar-token-0123456789"
LOCAL_TOKEN = "fixture-local-proxy-token-0123456789"
PROVIDER_KEY = "fixture-provider-key-0123456789"


@pytest.fixture(autouse=True)
def isolated_secrets(monkeypatch, tmp_path):
    values = {"fixture": PROVIDER_KEY, "local-proxy-token": LOCAL_TOKEN}
    monkeypatch.delenv("THREE_API_SIDECAR_TOKEN", raising=False)
    monkeypatch.delenv("THREE_API_TEST_PROVIDER_KEY", raising=False)
    monkeypatch.setenv("LOCAL_RESPONSES_PROXY_TOKEN", LOCAL_TOKEN)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    def get_secret(name, env_name=""):
        if env_name in {"THREE_API_TEST_PROVIDER_KEY", "LOCAL_RESPONSES_PROXY_TOKEN"}:
            return os.environ.get(env_name) or values.get(name)
        return values.get(name)

    for module in ("dashboard.runner", "dashboard.server", "proxy.catalog"):
        monkeypatch.setattr(module + ".get_secret", get_secret)
    monkeypatch.setattr("proxy.catalog.set_secret", lambda name, value: values.__setitem__(name, value))
    monkeypatch.setattr("proxy.catalog.delete_secret", lambda name: values.pop(name, None))
    return values


def configuration(tmp_path):
    path = tmp_path / "providers.toml"
    path.write_text('[[providers]]\nid="fixture"\nbase_url="https://fixture.example/v1"\n'
                    'deployment="fixture-model"\napi_key_env="THREE_API_TEST_PROVIDER_KEY"\n', encoding="utf-8")
    return path


def panel(tmp_path):
    return create_app(config_path=configuration(tmp_path), data_dir=tmp_path / "panel-data",
                      transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))


@pytest.mark.parametrize("path", ["/", "/health", "/ui/", "/ui/assets/app.js", "/ui/api/state", "/missing"])
def test_sidecar_requires_private_header_on_every_route(tmp_path, monkeypatch, path):
    monkeypatch.setenv("THREE_API_SIDECAR_TOKEN", SIDECAR_TOKEN)
    with TestClient(panel(tmp_path), base_url="http://127.0.0.1:4101") as client:
        denied = client.get(path)
        assert denied.status_code == 403
        assert SIDECAR_TOKEN not in denied.text
        assert client.get(path, headers={"x-3api-sidecar-token": SIDECAR_TOKEN[:-1] + "x"}).status_code == 403
        assert client.get(path, headers=[("x-3api-sidecar-token", SIDECAR_TOKEN)] * 2).status_code == 403


def test_sidecar_preserves_session_origin_and_csrf_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("THREE_API_SIDECAR_TOKEN", SIDECAR_TOKEN)
    with TestClient(panel(tmp_path), base_url="http://127.0.0.1:4101",
                    headers={"x-3api-sidecar-token": SIDECAR_TOKEN}) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ui/api/state").status_code == 401
        page = client.get("/ui/")
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
        assert SIDECAR_TOKEN not in page.text and LOCAL_TOKEN not in page.text
        headers = {"origin": "http://127.0.0.1:4101", "x-panel-csrf": csrf}
        assert client.post("/ui/api/settings", json={}, headers=headers).status_code == 200
        assert client.post("/ui/api/settings", json={}).status_code == 403
        assert client.post("/ui/api/settings", json={}, headers={**headers, "origin": "https://evil.example"}).status_code == 403
        assert client.get("/ui/", headers={"host": "evil.example"}).status_code == 403


async def exchange(middleware, receive, headers=()):
    messages = []

    async def send(message):
        messages.append(message)

    await middleware({"type": "http", "method": "POST", "path": "/anything", "headers": list(headers)}, receive, send)
    return messages


async def test_oversized_chunk_is_rejected_before_handler_or_further_reads():
    called = False
    reads = 0

    async def endpoint(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * 20, "more_body": True}

    result = await exchange(SidecarSecurityMiddleware(endpoint, max_body_bytes=10), receive)
    assert result[0]["status"] == 413 and reads == 1 and not called


async def test_unauthorized_sidecar_request_never_reads_body():
    async def forbidden(*args):
        pytest.fail("An unauthorized request reached the body reader or handler")

    result = await exchange(SidecarSecurityMiddleware(forbidden, token=SIDECAR_TOKEN), forbidden)
    assert result[0]["status"] == 403


async def test_body_deadline_and_bad_content_length():
    async def forbidden(*args):
        pytest.fail("Invalid request reached the handler")

    async def slow():
        await asyncio.sleep(10)

    guard = SidecarSecurityMiddleware(forbidden, body_timeout=0.01)
    result = await exchange(guard, slow)
    assert result[0]["status"] == 408
    for headers in [[(b"content-length", b"-1")], [(b"content-length", b"1")] * 2,
                    [(b"content-length", b"200000")]]:
        result = await exchange(guard, forbidden, headers)
        assert result[0]["status"] in {400, 413}


async def test_unexpected_errors_are_sanitized(caplog):
    async def broken(scope, receive, send):
        raise RuntimeError("secret fixture " + SIDECAR_TOKEN)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    result = await exchange(SidecarSecurityMiddleware(broken), receive)
    assert result[0]["status"] == 500
    assert SIDECAR_TOKEN not in str(result) and SIDECAR_TOKEN not in caplog.text


def test_json_depth_constants_and_compressed_body_are_rejected(tmp_path):
    with TestClient(panel(tmp_path), base_url="http://127.0.0.1:4101") as client:
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/ui/").text)[1]
        headers = {"origin": "http://127.0.0.1:4101", "x-panel-csrf": csrf}
        for body in ['{"value": NaN}', '[' * 2000 + '0' + ']' * 2000]:
            assert client.post("/ui/api/settings", content=body, headers=headers).status_code == 400
        assert client.post("/ui/api/settings", content=b"x" * 150001, headers=headers).status_code == 413
        assert client.post("/ui/api/settings", content=b"{}", headers={**headers, "content-encoding": "gzip"}).status_code == 415


async def test_default_approval_reaches_codex_and_waits_for_user(tmp_path):
    store, runner, project = make_runner(tmp_path)
    seen = []
    request = runner.request

    async def capture(method, params, **kwargs):
        seen.append((method, params))
        return await request(method, params, **kwargs)

    runner.request = capture
    try:
        result = await runner.start_task(project["id"], "approve", "low")
        task = runner.tasks[result["id"]]
        await wait_for(lambda: task["state"] == "awaiting_input")
        start = next(params for method, params in seen if method == "thread/start")
        turn = next(params for method, params in seen if method == "turn/start")
        assert start["sandbox"] == "workspace-write" and start["approvalPolicy"] == "on-request"
        assert turn["sandboxPolicy"]["type"] == "workspaceWrite"
        assert not turn["sandboxPolicy"]["networkAccess"]
        assert not (Path(project["path"]) / "created-by-task.txt").exists()
        await runner.answer(task["id"], task["approvals"][0]["id"], "decline")
        await wait_for(lambda: task["state"] == "completed")
    finally:
        await runner.close()
        store.close()


def test_worker_has_only_local_delegation_and_complete_provider(tmp_path, monkeypatch):
    for name in ("THREE_API_SIDECAR_TOKEN", "THREE_API_TEST_PROVIDER_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "NODE_OPTIONS"):
        monkeypatch.setenv(name, "fixture-private-" + name)
    store, runner, _ = make_runner(tmp_path)
    try:
        runner.proxy_url = "http://127.0.0.1:4100"
        runner.credential_env_names = {"THREE_API_TEST_PROVIDER_KEY"}
        environment = runner.worker_environment()
        assert environment["LOCAL_RESPONSES_PROXY_TOKEN"] == LOCAL_TOKEN
        assert all(name not in environment for name in ("THREE_API_SIDECAR_TOKEN", "THREE_API_TEST_PROVIDER_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "NODE_OPTIONS"))
        config = runner.task_config({}, "fixture-route")["model_providers." + runner.model_provider]
        assert config["base_url"] == "http://127.0.0.1:4100/r/fixture-route/v1"
        assert config["env_key"] == "LOCAL_RESPONSES_PROXY_TOKEN" and config["wire_api"] == "responses"
        assert "auth" not in config and "experimental_bearer_token" not in config
        overrides = runner._arguments()
        source = next(value for value in overrides if value.startswith("model_providers." + runner.model_provider + "="))
        startup = tomllib.loads(source)["model_providers"][runner.model_provider]
        assert startup["base_url"] == "http://127.0.0.1:4100/v1" and startup["env_key"] == config["env_key"]
        assert LOCAL_TOKEN not in str(overrides)
    finally:
        store.close()


@pytest.mark.skipif(codex_command() is None, reason="Codex CLI is not installed")
async def test_real_codex_thread_ignores_inherited_provider_auth(tmp_path):
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir()
    home.joinpath("config.toml").write_text('model_provider="local_proxy"\n'
        '[model_providers.local_proxy]\nname="Inherited provider"\nbase_url="http://127.0.0.1:47999/v1"\n'
        '[model_providers.local_proxy.auth]\ncommand="fixture-auth-must-not-run"\n', encoding="utf-8")
    store, fixture_runner, project = make_runner(tmp_path)
    runner = CodexRunner(store, tmp_path / "data", "fixture-model", command=codex_command())
    runner.proxy_url = "http://127.0.0.1:47999"
    try:
        await runner.ensure_started()
        # Opening a thread validates provider configuration; no model turn is sent.
        result = await runner.request("thread/start", {"cwd": project["path"], "model": "fixture-model",
            "modelProvider": runner.model_provider, "approvalPolicy": "on-request", "sandbox": "workspace-write",
            "config": runner.task_config({}), "ephemeral": True}, timeout=20)
        assert result["thread"]["id"]
        assert runner.model_provider != "local_proxy"
    finally:
        await runner.close()
        await fixture_runner.close()
        store.close()


@pytest.mark.parametrize("change", [{"base_url": "https://changed.example/v1"}, {"auth_type": "bearer"}])
def test_destination_change_cannot_reuse_saved_key(tmp_path, isolated_secrets, change):
    path = configuration(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="ponownego podania klucza"):
        ProviderCatalog(path).change({"id": "fixture", **change})
    assert path.read_bytes() == before and isolated_secrets["fixture"] == PROVIDER_KEY
    ProviderCatalog(path).change({"id": "fixture", **change, "key": "new-fixture-key"})
    assert isolated_secrets["fixture"] == "new-fixture-key"


def test_environment_key_cannot_silently_override_replacement(tmp_path, monkeypatch, isolated_secrets):
    path = configuration(tmp_path)
    before = path.read_bytes()
    monkeypatch.setenv("THREE_API_TEST_PROVIDER_KEY", PROVIDER_KEY)
    with pytest.raises(ValueError, match="pierwszeństwo"):
        ProviderCatalog(path).change({"id": "fixture", "base_url": "https://changed.example/v1", "key": "new-fixture-key"})
    assert path.read_bytes() == before and isolated_secrets["fixture"] == PROVIDER_KEY


@pytest.mark.parametrize("endpoint", [
    "https://169.254.169.254/v1", "https://10.0.0.1/v1", "https://localhost/v1",
    "https://127.0.0.1/v1", "https://[::1]/v1", "https://[::ffff:127.0.0.1]/v1",
    "https://100.64.0.1/v1", "https://168.63.129.16/v1", "https://192.0.2.1/v1",
    "https://198.18.0.1/v1", "https://224.0.0.1/v1", "https://[fc00::1]/v1",
    "https://[2001:db8::1]/v1", "https://[2002:7f00:1::]/v1", "https://[3fff::1]/v1",
    "https://service.local/v1", "https://service.internal/v1", "https://service.localhost/v1",
    "https://singlelabel/v1", "https://api.example.com./v1", "https://127.1/v1",
    "https://0177.0.0.1/v1", "https://0x7f000001/v1", "https://2130706433/v1",
    "https://%31%32%37.0.0.1/v1", "https://127.0.0.1\\public.example/v1",
])
def test_catalog_rejects_private_or_ambiguous_urls_before_mutating_secrets(tmp_path, isolated_secrets, endpoint):
    path = configuration(tmp_path)
    original = path.read_bytes()
    keys = dict(isolated_secrets)
    with pytest.raises(ValueError, match="publicznego HTTPS"):
        ProviderCatalog(path).change({"id": "fixture", "base_url": endpoint, "key": "replacement-fixture-key"})
    assert path.read_bytes() == original and isolated_secrets == keys
    assert not list(tmp_path.glob(".providers-*.toml"))
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("endpoint", ["https://api.example.com/v1", "https://8.8.8.8/v1", "https://[2606:4700:4700::1111]/v1"])
def test_catalog_accepts_public_https_without_dns(tmp_path, monkeypatch, isolated_secrets, endpoint):
    def no_dns(*args, **kwargs):
        pytest.fail("The Python catalog must not perform DNS lookups")

    monkeypatch.setattr("socket.getaddrinfo", no_dns)
    path = configuration(tmp_path)
    ProviderCatalog(path).change({"id": "fixture", "base_url": endpoint, "key": "replacement-fixture-key"})
    assert endpoint in path.read_text() and isolated_secrets["fixture"] == "replacement-fixture-key"


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:8123/v1", "http://localhost:8123/v1", "https://[::1]:8123/v1"])
def test_catalog_preserves_existing_explicit_loopback_mock(tmp_path, isolated_secrets, endpoint):
    path = configuration(tmp_path)
    path.write_text('[proxy]\nallow_loopback_upstreams=true\n' + path.read_text().replace("https://fixture.example/v1", endpoint), encoding="utf-8")
    ProviderCatalog(path).change({"id": "fixture", "label": "Local fixture"})
    assert endpoint in path.read_text() and isolated_secrets["fixture"] == PROVIDER_KEY


@pytest.mark.parametrize("flag", ["true", '"true"', "1"])
def test_catalog_loopback_flag_never_allows_private_networks(tmp_path, isolated_secrets, flag):
    path = configuration(tmp_path)
    path.write_text('[proxy]\nallow_loopback_upstreams=' + flag + '\n' + path.read_text(), encoding="utf-8")
    original = path.read_bytes()
    keys = dict(isolated_secrets)
    with pytest.raises(ValueError):
        ProviderCatalog(path).change({"id": "fixture", "base_url": "https://10.0.0.1/v1", "key": "replacement-fixture-key"})
    assert path.read_bytes() == original and isolated_secrets == keys


def test_catalog_validates_effective_endpoint_from_environment(tmp_path, monkeypatch, isolated_secrets):
    path = configuration(tmp_path)
    path.write_text(path.read_text() + 'base_url_env="THREE_API_TEST_BASE_URL"\n', encoding="utf-8")
    monkeypatch.setenv("THREE_API_TEST_BASE_URL", "https://169.254.169.254/v1")
    original = path.read_bytes()
    keys = dict(isolated_secrets)
    with pytest.raises(ValueError, match="publicznego HTTPS"):
        ProviderCatalog(path).change({"id": "fixture", "label": "Still unsafe"})
    assert path.read_bytes() == original and isolated_secrets == keys


def test_sidecar_refuses_to_start_without_private_token(tmp_path):
    with pytest.raises(SystemExit) as result:
        sidecar_main(["--port", "0", "--config", str(configuration(tmp_path)), "--data-dir", str(tmp_path / "data")])
    assert result.value.code == 2


def test_internal_shutdown_requires_private_token_without_browser_session(tmp_path, monkeypatch):
    monkeypatch.setenv("THREE_API_SIDECAR_TOKEN", SIDECAR_TOKEN)
    app = panel(tmp_path)
    stopped = []
    app.state.shutdown = lambda: stopped.append(True)
    with TestClient(app, base_url="http://127.0.0.1:4101") as client:
        assert client.post("/internal/shutdown", json={}).status_code == 403
        assert not stopped
        result = client.post("/internal/shutdown", json={}, headers={"x-3api-sidecar-token": SIDECAR_TOKEN})
        assert result.status_code == 200 and result.json() == {"ok": True}
        assert stopped == [True]


def test_internal_shutdown_is_absent_from_legacy_app(tmp_path):
    with TestClient(panel(tmp_path), base_url="http://127.0.0.1:4101") as client:
        assert client.post("/internal/shutdown", json={}).status_code == 404


async def test_real_sidecar_binds_random_port_and_authenticates_health(tmp_path):
    config = configuration(tmp_path)
    environment = {key: value for key, value in os.environ.items() if key.upper() in WORKER_ENV_ALLOWLIST}
    environment.update(THREE_API_SIDECAR_TOKEN=SIDECAR_TOKEN, LOCAL_RESPONSES_PROXY_TOKEN=LOCAL_TOKEN,
                       THREE_API_TEST_PROVIDER_KEY=PROVIDER_KEY, PYTHONUTF8="1")
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "dashboard.sidecar", "--port", "0",
        "--config", str(config), "--data-dir", str(tmp_path / "sidecar-data"),
        cwd=Path(__file__).resolve().parents[1], env=environment, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **flags)
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 15)
        assert 0 < len(line) < 1024
        port = json.loads(line)["port"]
        assert 1 <= port <= 65535
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=1) as client:
            async with asyncio.timeout(10):
                while True:
                    try:
                        result = await client.get("/health", headers={"x-3api-sidecar-token": SIDECAR_TOKEN})
                        if result.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(.05)
            assert result.json()["application"] == "3api-panel"
            assert (await client.get("/health")).status_code == 403
            client.headers["x-3api-sidecar-token"] = SIDECAR_TOKEN
            page = await client.get("/ui/")
            assert 'name="csrf-token"' in page.text
            client.cookies.clear()
            result = await client.post("/internal/shutdown", json={})
            assert result.status_code == 200
        await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        stdout, stderr = await process.communicate()
        assert SIDECAR_TOKEN.encode() not in stdout + stderr
        assert LOCAL_TOKEN.encode() not in stdout + stderr
        assert PROVIDER_KEY.encode() not in stdout + stderr


@pytest.mark.parametrize("worker_crash", [False, True])
async def test_packaged_gateway_without_token_form_and_private_worker_shutdown(tmp_path, worker_crash):
    source = Path(__file__).resolve().parents[1]
    binary = source / "target" / "debug" / ("3api.exe" if os.name == "nt" else "3api")
    if not binary.is_file():
        pytest.skip("Run cargo build --locked before testing the packaged gateway")
    release = tmp_path / "release with spaces"
    release.mkdir()
    for package in ("dashboard", "proxy"):
        shutil.copytree(source / package, release / package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    executable = release / binary.name
    shutil.copy2(binary, executable)
    configuration(release)
    sockets = [socket.socket() for _ in range(2)]
    for listener in sockets:
        listener.bind(("127.0.0.1", 0))
    proxy_port, panel_port = [listener.getsockname()[1] for listener in sockets]
    for listener in sockets:
        listener.close()
    environment = {key: value for key, value in os.environ.items() if key.upper() in WORKER_ENV_ALLOWLIST}
    environment.update(LOCAL_RESPONSES_PROXY_TOKEN=LOCAL_TOKEN, THREE_API_TEST_PROVIDER_KEY=PROVIDER_KEY,
                       CODEX_HOME=str(tmp_path / "isolated-codex"), PYTHONUTF8="1")
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    # No Cargo, --project-dir or project cwd: the EXE must find its adjacent worker.
    process = await asyncio.create_subprocess_exec(str(executable), "serve", "--python", sys.executable,
        "--proxy-port", str(proxy_port), "--panel-port", str(panel_port), cwd=tmp_path,
        env=environment, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, **flags)
    base_url = f"http://127.0.0.1:{panel_port}"
    owned_children = []
    try:
        async with httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=2) as client:
            async with asyncio.timeout(20):
                while True:
                    assert process.returncode is None, "The packaged gateway exited before readiness"
                    try:
                        if (await client.get("/health")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(.05)
            assert (await client.get("/ui/api/state")).status_code == 401
            page = await client.get("/ui/")
            assert page.status_code == 200 and 'name="csrf-token"' in page.text
            assert 'id="token"' not in page.text and LOCAL_TOKEN not in page.text
            cookies = page.headers.get_list("set-cookie")
            assert len(cookies) == 2 and all("HttpOnly" in value and "SameSite" in value for value in cookies)
            csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
            assert (await client.get("/ui/api/state")).status_code == 200
            assert (await client.get("/ui/", headers={"origin": "https://evil.example"})).status_code == 403
            assert (await client.get("/ui/", headers={"host": "evil.example"})).status_code == 403
            assert (await client.post("/ui/api/settings", json={}, headers={
                "origin": base_url, "x-panel-csrf": "wrong-csrf"})).status_code == 403
            assert (await client.post("/internal/shutdown", json={})).status_code == 404
            owned_children = psutil.Process(process.pid).children(recursive=True)
            worker = None
            worker_port = None
            for child in owned_children:
                for connection in child.net_connections(kind="tcp4"):
                    if connection.status == psutil.CONN_LISTEN:
                        worker, worker_port = child, connection.laddr.port
            assert worker is not None and worker_port not in {proxy_port, panel_port}
            async with httpx.AsyncClient(trust_env=False, timeout=2) as direct:
                assert (await direct.get(f"http://127.0.0.1:{worker_port}/health")).status_code == 403
            if worker_crash:
                worker.terminate()
                await asyncio.wait_for(process.wait(), 15)
                assert process.returncode != 0
            else:
                client.cookies.delete("three_api_gateway")
                assert (await client.get("/ui/api/state")).status_code == 401
                page = await client.get("/ui/", headers={"origin": base_url})
                assert page.status_code == 200
                csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]
                assert (await client.get("/ui/api/state")).status_code == 200
                result = await client.post("/ui/api/shutdown", json={}, headers={"origin": base_url, "x-panel-csrf": csrf})
                assert result.status_code == 200
                await asyncio.wait_for(process.wait(), 15)
                assert process.returncode == 0
            for port in (proxy_port, panel_port, worker_port):
                with socket.socket() as probe:
                    probe.settimeout(.5)
                    assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        if process.returncode is None:
            owned_children.extend(psutil.Process(process.pid).children(recursive=True))
            process.kill()
            await process.wait()
        for child in owned_children:
            try:
                if child.is_running():
                    child.kill()
            except psutil.NoSuchProcess:
                pass
        stdout, stderr = await process.communicate()
        assert LOCAL_TOKEN.encode() not in stdout + stderr
        assert PROVIDER_KEY.encode() not in stdout + stderr
