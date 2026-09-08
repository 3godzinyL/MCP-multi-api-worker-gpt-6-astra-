import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import psutil
import rust_launcher

from stop_proxy import stop_proxy

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows startup scripts")


@pytest.mark.parametrize("choice,stop_exit,expected,exit_code", [
    ("1", 0, [], 0),
    ("2", 0, ["stop", "start"], 0),
    ("2", 1, ["stop"], 1),
])
def test_start_menu_restarts_only_after_successful_stop(tmp_path, choice, stop_exit, expected, exit_code):
    # Exercise the actual batch file in a path with spaces, with isolated
    # launch/stop stubs so the user's active proxy is never interrupted.
    fixture = tmp_path / "startup fixture"
    scripts = fixture / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / ".venv" / "Scripts" / "python.exe", scripts / "python.exe")
    shutil.copy2(ROOT / ".venv" / "pyvenv.cfg", scripts.parent / "pyvenv.cfg")
    shutil.copy2(ROOT / "legacy" / "start.bat", fixture / "start.bat")
    (fixture / "bootstrap.bat").write_text("@exit /b 0\n")
    (fixture / "manage.py").write_text("raise SystemExit(0)\n")
    (fixture / "run_proxy.py").write_text(
        "import sys\nfrom pathlib import Path\nassert Path(sys.argv[0]).is_absolute()\n"
        "with Path('events.txt').open('a') as f: f.write('start\\n')\n")
    (fixture / "stop_proxy.py").write_text(
        "from pathlib import Path\nwith Path('events.txt').open('a') as f: f.write('stop\\n')\n"
        f"raise SystemExit({stop_exit})\n")
    result = subprocess.run(["cmd.exe", "/d", "/c", "start.bat"], cwd=fixture,
                            input=choice + "\n", capture_output=True, text=True, timeout=15)
    assert result.returncode == exit_code, result.stdout + result.stderr
    events = fixture / "events.txt"
    assert (events.read_text().splitlines() if events.exists() else []) == expected


@pytest.mark.parametrize("scenario,allowed", [
    ("project_venv", True),
    ("venv_child", True),
    ("other_script", False),
    ("script_mentioned_in_arguments", False),
    ("other_interpreter", False),
    ("relative_script", False),
])
def test_stop_script_checks_the_listener_before_stopping(tmp_path, scenario, allowed):
    script = str(ROOT / "run_proxy.py")
    python = str(ROOT / ".venv" / "Scripts" / "python.exe")
    command = [python, script]
    process = {"ProcessId": 123450, "ParentProcessId": 123451,
               "ExecutablePath": python, "CommandLine": command}
    parent = {"ProcessId": 123451, "ExecutablePath": python, "CommandLine": command}
    if scenario in {"venv_child", "other_interpreter"}:
        process["ExecutablePath"] = r"C:\Python\python.exe"
    if scenario == "other_script":
        process["CommandLine"] = [python, r"C:\other\run_proxy.py"]
    if scenario == "script_mentioned_in_arguments":
        process["CommandLine"] = [python, r"C:\other\app.py", script]
    if scenario == "other_interpreter":
        parent["ExecutablePath"] = r"C:\OtherPython\python.exe"
    if scenario == "relative_script":
        process["CommandLine"] = [python, "run_proxy.py"]
    stopped = []
    class Process:
        def __init__(self, value):
            self.value = value
            self.pid = value["ProcessId"]
        def exe(self):
            return self.value["ExecutablePath"]
        def cmdline(self):
            return self.value["CommandLine"]
        def parent(self):
            return Process(parent)
        def terminate(self):
            stopped.append(self.pid)
        def wait(self, timeout):
            assert timeout == 10
    connection = SimpleNamespace(pid=123450, status=psutil.CONN_LISTEN, laddr=SimpleNamespace(ip="127.0.0.1", port=4000))
    options = {"root": ROOT, "connections": lambda **kw: [] if stopped else [connection], "process_factory": lambda pid: Process(process)}
    if allowed:
        stop_proxy(**options, check_only=True)
        assert not stopped
        stop_proxy(**options)
    else:
        with pytest.raises(RuntimeError, match="inny program"):
            stop_proxy(**options)
    assert stopped == ([123450] if allowed else [])


def test_stop_ignores_other_ports():
    process_calls = []
    foreign = SimpleNamespace(pid=123, status=psutil.CONN_LISTEN, laddr=SimpleNamespace(ip="127.0.0.1", port=3000))
    stop_proxy(connections=lambda **kw: [foreign], process_factory=lambda pid: process_calls.append(pid))
    assert process_calls == []


def test_packaged_launcher_uses_adjacent_exe_without_cargo(tmp_path, monkeypatch):
    executable = tmp_path / "3api.exe"
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(rust_launcher.shutil, "which", lambda _: pytest.fail("A packaged launch must not search for Cargo"))
    assert rust_launcher.ensure_binary(tmp_path) == executable


def test_source_launcher_builds_once_and_preserves_argument_boundaries(tmp_path, monkeypatch):
    root = tmp_path / "source with spaces"
    root.mkdir()
    (root / "Cargo.toml").write_text("[package]\n")
    executable = root / "target" / "release" / "3api.exe"
    monkeypatch.setattr(rust_launcher, "ROOT", root)
    monkeypatch.setattr(rust_launcher.shutil, "which", lambda name: "fixture-cargo")
    calls = []

    def run(arguments, **options):
        calls.append((arguments, options))
        if arguments[0] == "fixture-cargo":
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(rust_launcher.subprocess, "run", run)
    assert rust_launcher.launch("proxy", ["--config", "providers with spaces.toml"]) == 0
    assert calls[0] == (["fixture-cargo", "build", "--locked", "--release"], {"cwd": root, "check": True})
    assert calls[1][0] == [str(executable), "proxy", "--config", "providers with spaces.toml"]
    assert rust_launcher.ensure_binary(root) == executable
    assert len(calls) == 2


def test_serve_launcher_prepares_worker_and_passes_release_root(tmp_path, monkeypatch):
    import bootstrap

    executable = tmp_path / "3api.exe"
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(rust_launcher, "ROOT", tmp_path)
    prepared = []
    calls = []
    monkeypatch.setattr(bootstrap, "main", lambda: prepared.append(True) or 0)
    monkeypatch.setattr(rust_launcher.subprocess, "run", lambda arguments, **options:
                        calls.append((arguments, options)) or SimpleNamespace(returncode=0))
    assert rust_launcher.launch("serve", ["--panel-port", "49991"]) == 0
    assert prepared == [True]
    assert calls[0][0] == [str(executable), "serve", "--panel-port", "49991", "--project-dir", str(tmp_path)]
    assert calls[0][1]["cwd"] == tmp_path


def test_source_launcher_rebuilds_newer_rust_but_keeps_current_binary(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    source = tmp_path / "src" / "main.rs"
    source.parent.mkdir()
    source.write_text("fn main() {}\n")
    executable = tmp_path / "target" / "release" / "3api.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"old build")
    timestamp = executable.stat().st_mtime_ns + 2_000_000_000
    os.utime(source, ns=(timestamp, timestamp))
    calls = []
    monkeypatch.setattr(rust_launcher.shutil, "which", lambda _: "fixture-cargo")

    def build(arguments, **options):
        assert arguments == ["fixture-cargo", "build", "--locked", "--release"]
        assert options == {"cwd": tmp_path, "check": True}
        calls.append(arguments)
        executable.write_bytes(b"new build")
        os.utime(executable, ns=(timestamp + 1, timestamp + 1))

    monkeypatch.setattr(rust_launcher.subprocess, "run", build)
    assert rust_launcher.ensure_binary(tmp_path) == executable
    assert executable.read_bytes() == b"new build"
    assert rust_launcher.ensure_binary(tmp_path) == executable
    assert len(calls) == 1


def test_failed_worker_bootstrap_does_not_start_binary(tmp_path, monkeypatch):
    import bootstrap

    (tmp_path / "3api.exe").write_bytes(b"fixture")
    monkeypatch.setattr(rust_launcher, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "main", lambda: 1)
    monkeypatch.setattr(rust_launcher.subprocess, "run", lambda *_args, **_kwargs:
                        pytest.fail("Rust must not start with an unusable Python environment"))
    assert rust_launcher.launch("serve", []) == 1


def test_powershell_starts_packaged_exe_on_isolated_ports_without_source_manifest(tmp_path):
    fixture = tmp_path / "release launcher with spaces"
    scripts = fixture / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / ".venv" / "Scripts" / "python.exe", scripts / "python.exe")
    shutil.copy2(ROOT / ".venv" / "pyvenv.cfg", scripts.parent / "pyvenv.cfg")
    (fixture / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "start.ps1", fixture / "scripts" / "start.ps1")
    (fixture / "bootstrap.bat").write_text("@exit /b 0\n")
    (fixture / "manage.py").write_text("import sys\nassert sys.argv[1:] == ['init']\n")
    compiler = fixture / "compile-fixture.ps1"
    compiler.write_text("""$code = @'
using System;
using System.IO;
public class StartupFixture {
    public static int Main(string[] args) {
        File.AppendAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "events.txt"),
            string.Join("|", args) + "\\n");
        return 0;
    }
}
'@
Add-Type -TypeDefinition $code -OutputAssembly (Join-Path $PSScriptRoot '3api.exe') -OutputType ConsoleApplication
""", encoding="utf-8")
    result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                             "-File", str(compiler)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    listeners = [socket.socket() for _ in range(2)]
    for listener in listeners:
        listener.bind(("127.0.0.1", 0))
    proxy_port, panel_port = [listener.getsockname()[1] for listener in listeners]
    for listener in listeners:
        listener.close()
    result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                             "-File", str(fixture / "scripts" / "start.ps1"), "-ProxyPort", str(proxy_port),
                             "-PanelPort", str(panel_port)], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [line.split("|") for line in (fixture / "events.txt").read_text().splitlines()]
    assert calls == [
        ["check", "--config", str(fixture / "providers.toml")],
        ["serve", "--config", str(fixture / "providers.toml"), "--data-dir", str(fixture / "data" / "rust"),
         "--project-dir", str(fixture), "--proxy-port", str(proxy_port), "--panel-port", str(panel_port)],
    ]
    assert not (fixture / "Cargo.toml").exists()
    assert "Token logowania" not in result.stdout


@pytest.mark.parametrize("readiness", ["malformed", "oversized", "timeout"])
def test_failed_worker_readiness_stops_owned_descendants_and_releases_ports(tmp_path, readiness):
    binary = ROOT / "target" / "debug" / "3api.exe"
    if not binary.is_file():
        pytest.skip("Run cargo build --locked before testing worker containment")
    fixture = tmp_path / "failed worker with spaces"
    dashboard = fixture / "dashboard"
    dashboard.mkdir(parents=True)
    (dashboard / "__init__.py").write_text("")
    record = fixture / "child.json"
    child_source = (
        "import json,os,socket,sys,time\nfrom pathlib import Path\n"
        "listener=socket.socket()\nlistener.bind(('127.0.0.1',0))\nlistener.listen()\n"
        "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'port':listener.getsockname()[1]}))\n"
        "time.sleep(60)\n"
    )
    (dashboard / "sidecar.py").write_text(
        "import os,subprocess,sys,time\nfrom pathlib import Path\n"
        f"record=Path({str(record)!r})\n"
        f"subprocess.Popen([sys.executable,'-c',{child_source!r},str(record)],creationflags=subprocess.CREATE_NO_WINDOW)\n"
        "while not record.exists(): time.sleep(.01)\n"
        + ({"malformed": "print('invalid-readiness',flush=True)\n",
            "oversized": "print('x'*2000000,flush=True)\n", "timeout": ""}[readiness])
        + "time.sleep(60)\n", encoding="utf-8")
    (fixture / "providers.toml").write_text(
        '[[providers]]\nid="startup-containment-fixture"\nbase_url="https://fixture.example/v1"\n'
        'deployment="fixture-model"\napi_key_env="THREE_API_STARTUP_UNUSED_KEY"\nenabled=false\n', encoding="utf-8")
    listeners = [socket.socket() for _ in range(2)]
    for listener in listeners:
        listener.bind(("127.0.0.1", 0))
    proxy_port, panel_port = [listener.getsockname()[1] for listener in listeners]
    for listener in listeners:
        listener.close()
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA", "USERPROFILE"}}
    environment.update(LOCAL_RESPONSES_PROXY_TOKEN="startup-containment-private-token", PYTHONUTF8="1")
    process = subprocess.Popen([str(binary), "serve", "--project-dir", str(fixture),
        "--python", str(ROOT / ".venv" / "Scripts" / "python.exe"), "--proxy-port", str(proxy_port),
        "--panel-port", str(panel_port), "--worker-startup-timeout-seconds", "3"], cwd=tmp_path,
        env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    descendant = None
    try:
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode != 0
        assert b"startup-containment-private-token" not in stdout + stderr
        expected = {"malformed": b"Worker did not report a private port",
                    "oversized": b"Invalid worker readiness message", "timeout": b"Worker startup timed out"}
        assert expected[readiness] in stderr
        assert record.is_file(), "The fixture must spawn a listening descendant before failing readiness"
        descendant = json.loads(record.read_text())
        for _ in range(60):
            if not psutil.pid_exists(descendant["pid"]):
                break
            time.sleep(.05)
        assert not psutil.pid_exists(descendant["pid"]), "Failed startup left its worker descendant alive"
        for port in (proxy_port, panel_port, descendant["port"]):
            with socket.socket() as probe:
                probe.settimeout(.5)
                assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
        if descendant is None and record.is_file():
            descendant = json.loads(record.read_text())
        if descendant is not None:
            try:
                child = psutil.Process(descendant["pid"])
                if str(record) in child.cmdline():
                    child.kill()
                    child.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
