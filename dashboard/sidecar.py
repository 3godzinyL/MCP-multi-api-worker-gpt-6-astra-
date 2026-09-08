"""Private Python compatibility worker, owned and authenticated by Rust 3API."""
from __future__ import annotations

import argparse
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from dashboard.server import SIDECAR_TOKEN_ENV, create_app
from proxy.server import configure_logging


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private 3API panel sidecar")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:4100")
    args = parser.parse_args(argv)
    token = os.environ.get(SIDECAR_TOKEN_ENV, "")
    if not token.isascii() or len(token) < 32 or any(c.isspace() for c in token):
        parser.error("THREE_API_SIDECAR_TOKEN must contain a private, random ASCII token (at least 32 characters)")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        endpoint = urlsplit(args.proxy_url)
        valid = endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1" and endpoint.port is not None
        valid = valid and not (endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path.rstrip("/"))
    except ValueError:
        valid = False
    if not valid:
        parser.error("--proxy-url must point directly to an IPv4 loopback HTTP port")
    configure_logging("WARNING")
    app = create_app(config_path=args.config.resolve(), data_dir=args.data_dir.resolve(), proxy_url=args.proxy_url.rstrip("/"))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False,
        proxy_headers=False, server_header=False, log_level="warning", timeout_keep_alive=5,
        timeout_graceful_shutdown=15, limit_concurrency=64, h11_max_incomplete_event_size=16384))
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", args.port))
        listener.setblocking(False)
        print(json.dumps({"port": listener.getsockname()[1]}), flush=True)
        server.run(sockets=[listener])
    return 0 if server.started else 1


if __name__ == "__main__":
    raise SystemExit(main())
