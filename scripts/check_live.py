"""Opt-in smoke check of configured providers through a temporary Rust proxy.

One capped generation per configured provider, no task execution or vault writes.
Output contains status/usage only, never credentials or upstream payloads.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def run(binary, config, generate):
    # Explicit credential helper output stays in memory, never in the report.
    credential = subprocess.run([str(binary), "token", "--config", str(config)], capture_output=True, timeout=10)
    if credential.returncode:
        raise RuntimeError("Local token unavailable")
    token = credential.stdout.decode().strip()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(path, payload=None):
        headers = {"Authorization": "Bearer " + token}
        raw = json.dumps(payload).encode() if payload is not None else None
        if raw is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=raw, headers=headers)
        try:
            with opener.open(request, timeout=60) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, {}
    with tempfile.TemporaryDirectory(prefix="3api-live-check-") as temporary:
        process = subprocess.Popen([str(binary), "proxy", "--config", str(config), "--data-dir", temporary, "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            status = None
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Rust proxy did not start")
                try:
                    _, status = call("/status")
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(.1)
            if not status:
                raise RuntimeError("Rust proxy startup timed out")
            providers = [{"id": item["id"], "enabled": item["enabled"], "configured": item["configured"]} for item in status["providers"]]
            report = {"configured_providers": providers, "generations": []}
            if generate:
                for item in providers:
                    if not (item["enabled"] and item["configured"]):
                        continue
                    route = "live-check-" + item["id"]
                    code, _ = call("/admin/routes", {"id": route, "providers": [item["id"]], "wait_seconds": 0, "max_output_tokens": 32})
                    if code != 200:
                        report["generations"].append({"provider": item["id"], "route_status": code})
                        continue
                    code, result = call(f"/r/{route}/v1/responses", {"model": "gpt-6-astra", "input": "Reply exactly OK.",
                        "store": False, "max_output_tokens": 32, "reasoning": {"effort": "low"}})
                    usage = result.get("usage") or {}
                    report["generations"].append({"provider": item["id"], "http_status": code, "status": result.get("status"),
                        "total_tokens": usage.get("total_tokens")})
            print(json.dumps(report, indent=2))
            return int(any(item.get("http_status") != 200 for item in report["generations"]))
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "providers.toml")
    parser.add_argument("--binary", type=Path, default=ROOT / "target" / "release" / ("3api.exe" if os.name == "nt" else "3api"))
    parser.add_argument("--generate", action="store_true", help="Make one real, potentially billable 32-output-token request per provider")
    args = parser.parse_args()
    try:
        raise SystemExit(run(args.binary.resolve(), args.config.resolve(), args.generate))
    except Exception as error:
        print("Live check failed: " + type(error).__name__)
        raise SystemExit(1)
