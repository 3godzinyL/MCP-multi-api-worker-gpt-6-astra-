from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from dashboard.store import DashboardStore, IdempotencyConflict, creation_fingerprint
from dashboard.runner import CodexRunner, RunnerError
from dashboard.identity import PANEL_ID
from dashboard.preferences import preferences, validate_preferences, read_agents, write_agents
from dashboard.workspaces import prepare_workspace
from proxy.catalog import ProviderCatalog, public_provider, label as provider_label
from proxy.config import load_config
from proxy.credentials import LOCAL_TOKEN_ID, get_secret
from proxy.telemetry import TelemetryStore

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(__file__).with_name("assets")
COOKIE = "three_api_panel"
SIDECAR_TOKEN_ENV = "THREE_API_SIDECAR_TOKEN"
MAX_BODY_BYTES = 150000
BODY_TIMEOUT_SECONDS = 10.0
LOG = logging.getLogger("three_api_panel")


class SidecarSecurityMiddleware:
    """Authenticate the Rust parent before buffering a bounded HTTP request."""

    def __init__(self, app, *, token=None, max_body_bytes=MAX_BODY_BYTES, body_timeout=BODY_TIMEOUT_SECONDS):
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.body_timeout = body_timeout
        if token is not None and (not token.isascii() or len(token) < 32 or any(c.isspace() for c in token)):
            raise ValueError("Invalid private sidecar token configuration")
        self.token = token.encode("ascii") if token is not None else None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        async def reject(status, message):
            await JSONResponse({"error": message}, status_code=status, headers={
                "cache-control": "no-store", "x-content-type-options": "nosniff",
            })(scope, receive, send)

        headers = scope.get("headers", [])
        if self.token is not None:
            tokens = [value for name, value in headers if name.lower() == b"x-3api-sidecar-token"]
            if len(tokens) != 1 or not hmac.compare_digest(tokens[0], self.token):
                return await reject(403, "Prywatny panel wymaga połączenia przez 3API.")
        lengths = [value for name, value in headers if name.lower() == b"content-length"]
        if lengths:
            if len(lengths) != 1 or not lengths[0].isdigit() or len(lengths[0]) > 12:
                return await reject(400, "Niepoprawna długość żądania.")
            if int(lengths[0]) > self.max_body_bytes:
                return await reject(413, "Wiadomość jest zbyt długa.")
        encodings = [value.lower() for name, value in headers if name.lower() == b"content-encoding"]
        if encodings and encodings != [b"identity"]:
            return await reject(415, "Wyślij nieskompresowaną treść żądania.")
        body = bytearray()
        try:
            async with asyncio.timeout(self.body_timeout):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        return await reject(400, "Niepoprawne żądanie.")
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.max_body_bytes:
                        return await reject(413, "Wiadomość jest zbyt długa.")
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "Przekroczono czas przesyłania żądania.")

        delivered = False
        response_started = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, replay, tracked_send)
        except Exception as exc:
            # Never write upstream errors, paths, prompts or credentials to logs.
            LOG.error("event=panel_request_failed exception=%s", type(exc).__name__)
            if not response_started:
                await reject(500, "Operacja panelu nie powiodła się. Spróbuj ponownie.")


def create_app(*, data_dir: Path | None = None, proxy_url="http://127.0.0.1:4000", transport=None, runner_factory=CodexRunner, config_path=None):
    sessions = {}
    config_path = Path(config_path or ROOT / "providers.toml")
    sidecar_token = os.environ.get(SIDECAR_TOKEN_ENV)

    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = load_config(config_path)
        app.state.catalog = ProviderCatalog(config_path)
        app.state.edit_lock = asyncio.Lock()
        app.state.store = DashboardStore((data_dir or ROOT / "data") / "dashboard.sqlite3")
        app.state.telemetry = TelemetryStore((data_dir or ROOT / "data") / "telemetry.sqlite3")
        app.state.runner = runner_factory(app.state.store, data_dir or ROOT / "data", app.state.settings.public_model)
        app.state.runner.proxy_token = get_secret(LOCAL_TOKEN_ID, app.state.settings.proxy_token_env)
        app.state.runner.credential_env_names = {app.state.settings.proxy_token_env,
            *(p.api_key_env for p in app.state.settings.providers)}
        if app.state.runner.proxy_token:
            app.state.runner._secrets.append(app.state.runner.proxy_token)
        for provider in app.state.settings.providers:
            value = get_secret(provider.id, provider.api_key_env)
            if value and value not in app.state.runner._secrets:
                app.state.runner._secrets.append(value)
        if not app.state.store.projects(include_archived=True):
            app.state.store.add_project(str(ROOT))
        app.state.client = httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False, transport=transport)
        async def route_setup(value):
            token = get_secret(LOCAL_TOKEN_ID, app.state.settings.proxy_token_env)
            try:
                response = await app.state.client.post(proxy_url + "/admin/routes", json=value,
                    headers={"authorization": "Bearer " + (token or "")}, timeout=8)
            except httpx.HTTPError:
                raise RunnerError("Proxy jest niedostępne. Sprawdź status połączeń przed wysłaniem zadania.") from None
            if response.status_code != 200:
                raise RunnerError("Nie udało się przygotować wybranych API. Sprawdź ich konfigurację i zgodność modeli.")
        app.state.runner.route_setup = route_setup
        async def route_release(value):
            token = get_secret(LOCAL_TOKEN_ID, app.state.settings.proxy_token_env)
            try:
                response = await app.state.client.post(proxy_url + "/admin/routes/release", json=value,
                    headers={"authorization": "Bearer " + (token or "")}, timeout=8)
                response.raise_for_status()
            except httpx.HTTPError:
                raise RunnerError("Nie udało się zwolnić tras API. Ponawiam zwalnianie połączeń.") from None
        app.state.runner.route_release = route_release
        app.state.runner.run_usage = app.state.telemetry.run_usage
        app.state.runner.proxy_url = proxy_url
        try:
            await app.state.runner.recover_runs()
            await app.state.runner.release_stale_routes()
            yield
        finally:
            await app.state.runner.close()
            await app.state.client.aclose()
            app.state.telemetry.close()
            app.state.store.close()

    def error(message, status=400):
        return JSONResponse({"error": message}, status_code=status, headers={"cache-control": "no-store"})

    def safe_error(request, exc):
        if isinstance(exc, IdempotencyConflict):
            return JSONResponse({"error": str(exc), "code": "request_conflict"}, status_code=409,
                                headers={"cache-control": "no-store"})
        if isinstance(exc, sqlite3.OperationalError):
            return JSONResponse({"error": "Baza danych jest chwilowo zajęta. Spróbuj ponownie.", "code": "storage_busy"},
                                status_code=503, headers={"cache-control": "no-store", "retry-after": "1"})
        if isinstance(exc, (OSError, RuntimeError)):
            return error("Operacja nie powiodła się. Sprawdź konfigurację i dostęp do plików.", 500)
        if isinstance(exc, RunnerError) and getattr(exc, "status_code", None):
            return error(request.app.state.runner.clean(str(exc), 1000), exc.status_code)
        if isinstance(exc, RunnerError) and getattr(exc, "task_id", None):
            return JSONResponse({"error": request.app.state.runner.clean(str(exc), 1000), "task_id": exc.task_id},
                                status_code=400, headers={"cache-control": "no-store"})
        return error(request.app.state.runner.clean(str(exc), 1000))

    def local(request):
        return request.url.hostname in {"127.0.0.1", "localhost", "::1"}

    def guard(request, *, write=False):
        if not local(request):
            return error("Panel jest dostępny tylko lokalnie.", 403)
        session = sessions.get(request.cookies.get(COOKIE, ""))
        if not session or session["expires"] < time.monotonic():
            return JSONResponse({"error": "Odnawianie połączenia z panelem.", "code": "session_expired"}, status_code=401)
        if write:
            expected_origin = f"{request.url.scheme}://{request.url.netloc}"
            if request.headers.get("origin") != expected_origin:
                return error("Niedozwolone źródło żądania.", 403)
            supplied = request.headers.get("x-panel-csrf", "")
            if not supplied.isascii() or not hmac.compare_digest(supplied, session["csrf"]):
                return JSONResponse({"error": "Sesja panelu wymaga odnowienia.", "code": "csrf_expired"}, status_code=403)
        return None

    async def index(request):
        if not local(request):
            return error("Panel jest dostępny tylko lokalnie.", 403)
        now = time.monotonic()
        for key in list(sessions):
            if sessions[key]["expires"] < now:
                del sessions[key]
        if len(sessions) >= 64:
            del sessions[next(iter(sessions))]
        session_id = request.cookies.get(COOKIE, "")
        session = sessions.get(session_id)
        if not session:
            session_id = secrets.token_urlsafe(32)
            session = {"csrf": secrets.token_urlsafe(32)}
            sessions[session_id] = session
        session["expires"] = now + 43200
        csrf = session["csrf"]
        content = (ASSETS / "index.html").read_text(encoding="utf-8").replace("__CSRF__", csrf)
        for marker, filename in (("__COMPOSER__", "composer.html"), ("__CONTROLS_DIALOGS__", "dialogs.html")):
            content = content.replace(marker, (ASSETS / filename).read_text(encoding="utf-8"))
        response = HTMLResponse(content, headers={
            "cache-control": "no-store", "x-frame-options": "DENY", "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
            "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        response.set_cookie(COOKIE, session_id, httponly=True, samesite="strict", path="/ui", max_age=43200)
        return response

    async def health(request):
        return JSONResponse({"status": "ok", "application": "3api-panel", "instance": PANEL_ID,
                             "tasks_running": sum(t["state"] in {"starting", "running", "awaiting_input", "stopping", "finalizing"}
                                                  for t in request.app.state.runner.tasks.values())})

    async def shutdown(request):
        if (denied := guard(request, write=True)) is not None:
            return denied
        if any(t["state"] in {"starting", "running", "awaiting_input", "stopping", "finalizing"} for t in request.app.state.runner.tasks.values()):
            return error("Najpierw zatrzymaj trwające zadania w panelu.", 409)
        callback = getattr(request.app.state, "shutdown", None)
        if callback is None:
            return error("Tę instancję uruchomiono w trybie deweloperskim.", 409)
        return JSONResponse({"ok": True}, background=BackgroundTask(callback))

    async def internal_shutdown(request):
        # Rust owns this process and uses this endpoint to finish runner.close()
        # during Ctrl+C. The outer middleware requires its private token.
        callback = getattr(request.app.state, "shutdown", None)
        if callback is None:
            return error("Brak właściciela procesu panelu.", 409)
        return JSONResponse({"ok": True}, headers={"cache-control": "no-store"}, background=BackgroundTask(callback))

    async def state(request):
        if (denied := guard(request)) is not None:
            return denied
        settings = request.app.state.settings
        proxy = None
        try:
            token = get_secret(LOCAL_TOKEN_ID, settings.proxy_token_env)
            response = await request.app.state.client.get(proxy_url + "/status", headers={"authorization": "Bearer " + (token or "")})
            if response.status_code == 200:
                proxy = response.json()
        except (httpx.HTTPError, ValueError, RuntimeError):
            pass
        labels = {p.id: {"label": provider_label(p), "model": p.deployment, "base_url": p.base_url,
                         "cooldown_seconds": p.cooldown_seconds} for p in settings.providers}
        if proxy:
            for provider in proxy.get("providers", []):
                provider.update(labels.get(provider["id"], {}))
        usage = request.app.state.telemetry.snapshot()
        activity = request.app.state.store.activity()
        for event in usage["events"]:
            label = labels.get(event["provider"], {}).get("label", event["provider"])
            peer = labels.get(event["peer"], {}).get("label", event["peer"])
            kind = event["kind"]
            title = {"attempt": label + " rozpoczyna odpowiedź", "completed": label + " · odpowiedź gotowa",
                     "rotation": "Przełączenie " + label + " → " + peer,
                     "cooldown": label + " · czasowa przerwa", "failed": label + " · nieudana próba",
                     "client_disconnected": label + " · klient zakończył połączenie",
                     "upstream_interrupted": label + " · przerwana odpowiedź"}.get(kind, label + " · " + kind)
            subtitle = f"{event['tokens']:,} tokenów".replace(",", " ") if event["tokens"] is not None else {
                "http_429": "Osiągnięto limit API", "sse_rate_limit": "Osiągnięto limit API",
                "stream_closed_before_completion": "Strumień zamknięty przed końcem odpowiedzi"}.get(event["reason"], "")
            activity.append({"time": event["time"], "kind": kind, "title": title, "subtitle": subtitle,
                             "level": "error" if kind in {"failed", "upstream_interrupted"} else "info",
                             "icon": "route" if kind == "rotation" else "clock" if kind == "cooldown" else "tokens"})
        runner = request.app.state.runner
        notice = None
        if not proxy:
            notice = "Brak połączenia z proxy. Uruchom gui.bat, aby włączyć panel i API."
        elif not proxy.get("telemetry_enabled"):
            notice = "Proxy działa. Licznik tokenów czeka na wczytanie aktualizacji monitorowania."
        elif not runner.status()["available"]:
            notice = "Nie znaleziono Codex CLI. Podgląd API działa; uruchamianie zadań wymaga Codex CLI."
        return JSONResponse({
            "schema_version": 2,
            **{key: request.query_params.get(key) for key in ("project_id", "task_id", "run_id")},
            **{"requested_" + key: request.query_params.get(key) for key in ("project_id", "task_id", "run_id")},
            "proxy": proxy, "projects": request.app.state.store.projects(),
            "model": settings.public_model, "tasks": runner.public_tasks(request.query_params.get("task_id")),
            "active_agents": runner.active_agents(), "metrics": usage["metrics"],
            "timeline": usage["timeline"], "activity": sorted(activity, key=lambda e: e["time"], reverse=True)[:35],
            "runner": runner.status(), "notice": notice,
            "defaults": {k: preferences(request.app.state.store)[k] for k in ("permission_mode", "effort", "max_agents")},
            "models": runner._models,
        }, headers={"cache-control": "no-store"})

    async def payload(request):
        # The outer ASGI middleware enforces this while receiving, before allocation.
        raw = await request.body()
        try:
            def reject_constant(_):
                raise ValueError("Non-finite JSON")
            result = json.loads(raw, parse_constant=reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ValueError("Niepoprawna treść żądania.") from None
        if not isinstance(result, dict):
            raise ValueError("Niepoprawna treść żądania.")
        return result

    async def add_project(request):
        if (denied := guard(request, write=True)) is not None:
            return denied
        try:
            body = await payload(request)
            def prepare():
                workspace = prepare_workspace(body, request.app.state.runner.data_dir)
                if workspace["kind"] == "project" and body.get("name") is None:
                    workspace["name"] = None
                return workspace
            return JSONResponse(request.app.state.runner.add_project(workspace_factory=prepare,
                client_request_id=body.get("client_request_id"), request_fingerprint=creation_fingerprint(body)))
        except (ValueError, sqlite3.OperationalError) as exc:
            return safe_error(request, exc)
        except OSError:
            return error("Nie można otworzyć tego folderu. Sprawdź ścieżkę i uprawnienia.")

    async def remove_project(request):
        if (denied := guard(request, write=True)) is not None:
            return denied
        try:
            return JSONResponse(await request.app.state.runner.remove_project(request.path_params["project_id"]))
        except (ValueError, OSError, sqlite3.OperationalError) as exc:
            return safe_error(request, exc)

    async def create_chat(request):
        if (denied := guard(request, write=True)) is not None:
            return denied
        try:
            body = await payload(request)
            if not isinstance(body.get("project_id"), str):
                raise ValueError("Wybierz projekt.")
            title = body.get("title", "")
            if not isinstance(title, str) or len(title) > 200:
                raise ValueError("Podaj tytuł czatu do 200 znaków.")
            chat = request.app.state.runner.create_chat(body["project_id"], title=title,
                client_request_id=body.get("client_request_id"), request_fingerprint=creation_fingerprint(body))
            return JSONResponse(chat, status_code=201, headers={"cache-control": "no-store"})
        except (ValueError, OSError, sqlite3.OperationalError) as exc:
            return safe_error(request, exc)

    def page_arguments(request):
        raw = request.query_params.get("limit", "50")
        if not raw.isascii() or not raw.isdigit() or len(raw) > 3 or not 1 <= int(raw) <= 200:
            raise ValueError("Limit musi być liczbą od 1 do 200.")
        return {"cursor": request.query_params.get("cursor"), "limit": int(raw)}

    def with_usage(request, run):
        measured = request.app.state.telemetry.run_usage(run["id"])
        if measured["attempts"]:
            run["usage"] = measured
        return run

    async def project_chats(request):
        if (denied := guard(request)) is not None:
            return denied
        try:
            store = request.app.state.store
            project_id = request.path_params["project_id"]
            if not store.project(project_id):
                return error("Nie znaleziono projektu.", 404)
            return JSONResponse(store.list_chats(project_id, **page_arguments(request)), headers={"cache-control": "no-store"})
        except (ValueError, sqlite3.OperationalError) as exc:
            return safe_error(request, exc)

    async def project_history(request):
        if (denied := guard(request)) is not None:
            return denied
        try:
            store = request.app.state.store
            project_id = request.path_params["project_id"]
            if not store.project(project_id):
                return error("Nie znaleziono projektu.", 404)
            result = store.list_runs(project_id, task_id=request.query_params.get("task_id"), **page_arguments(request))
            result["items"] = [with_usage(request, run) for run in result["items"]]
            return JSONResponse(result, headers={"cache-control": "no-store"})
        except (ValueError, sqlite3.OperationalError) as exc:
            return safe_error(request, exc)

    async def run_details(request):
        if (denied := guard(request)) is not None:
            return denied
        try:
            store = request.app.state.store
            task_id, run_id = request.path_params["task_id"], request.path_params["run_id"]
            page = page_arguments(request)
            result = store.load_run(task_id, run_id, limit=page["limit"])
            if result is None or not store.project(result["project_id"]):
                return error("Nie znaleziono uruchomienia.", 404)
            for field, method in (("messages", store.list_messages), ("changes", store.list_changes),
                                  ("change_history", store.list_change_history)):
                result[field] = method(task_id, run_id, cursor=request.query_params.get(field + "_cursor", page["cursor"]),
                                       limit=page["limit"])
            event_cursor = request.query_params.get("api_events_cursor", page["cursor"])
            telemetry = request.app.state.telemetry
            result["api_events"] = (telemetry.list_events(run_id, cursor=event_cursor, limit=page["limit"])
                if telemetry.has_run_events(run_id) else store.list_api_events(task_id, run_id,
                    cursor=event_cursor, limit=page["limit"]))
            return JSONResponse(with_usage(request, result), headers={"cache-control": "no-store"})
        except (ValueError, sqlite3.OperationalError) as exc:
            return safe_error(request, exc)

    async def start_task(request):
        if (denied := guard(request, write=True)) is not None:
            return denied
        try:
            body = await payload(request)
            if not isinstance(body.get("project_id"), str) or (body.get("continue_task") is not None and not isinstance(body["continue_task"], str)):
                raise ValueError("Wybierz projekt i poprawne zadanie.")
            if not request.app.state.store.project(body["project_id"]):
                return error("Nie znaleziono projektu.", 404)
            runner = request.app.state.runner
            client_request_id = body.get("client_request_id")
            if client_request_id is not None and (not isinstance(client_request_id, str) or
                    not 1 <= len(client_request_id) <= 128 or not client_request_id.isascii() or
                    any(not (c.isalnum() or c in "_-.") for c in client_request_id)):
                raise ValueError("Niepoprawny identyfikator wysyłanego polecenia.")
            if client_request_id:
                existing = request.app.state.store.find_run_by_request(client_request_id)
                if existing:
                    if existing["project_id"] != body["project_id"] or (body.get("continue_task") and
                            existing["task_id"] != body["continue_task"]):
                        return error("Ten identyfikator polecenia należy do innego czatu.", 409)
                    return JSONResponse({"id": existing["task_id"], "run_id": existing["id"], "state": existing["state"]},
                                        status_code=202, headers={"cache-control": "no-store"})
            previous = runner.tasks.get(body.get("continue_task")) or {}
            ids = body.get("api_ids", previous.get("api_ids"))
            configured = {p.id: p for p in request.app.state.settings.providers if p.enabled and get_secret(p.id, p.api_key_env)}
            if ids is None:
                ids = [p.id for p in configured.values() if p.deployment == request.app.state.settings.public_model]
            if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in configured for i in ids):
                raise ValueError("Wybierz przynajmniej jedno skonfigurowane API.")
            if len({configured[i].deployment for i in ids}) != 1:
                raise ValueError("API przejmujące zadanie muszą korzystać z tego samego modelu.")
            defaults = preferences(request.app.state.store)
            options = {"api_ids": ids, "model": configured[ids[0]].deployment,
                       "mode": body.get("mode", previous.get("mode", "standard")),
                       "permission_mode": body.get("permission_mode", defaults["permission_mode"])}
            return JSONResponse(await runner.start_task(body["project_id"], body.get("prompt"),
                                body.get("effort", defaults["effort"]), body.get("continue_task"), options=options,
                                client_request_id=client_request_id), status_code=202)
        except (ValueError, OSError, sqlite3.OperationalError) as exc:
            return safe_error(request, exc)

    async def stop_task(request):
        if (denied := guard(request, write=True)) is not None:
            return denied
        try:
            return JSONResponse(await request.app.state.runner.stop_task(request.path_params["task_id"]))
        except (ValueError, OSError) as exc:
            return safe_error(request, exc)

    async def approval(request):
        if (denied := guard(request, write=True)) is not None:
            return denied
        try:
            body = await payload(request)
            if not isinstance(body.get("id"), str):
                raise ValueError("Brak identyfikatora prośby.")
            return JSONResponse(await request.app.state.runner.answer(request.path_params["task_id"], body["id"], body.get("decision"), body.get("answers")))
        except (ValueError, OSError) as exc:
            return safe_error(request, exc)

    async def folders(request):
        if (denied := guard(request)) is not None:
            return denied
        try:
            path = Path(request.query_params.get("path") or Path.home() / "Desktop").expanduser().resolve(strict=True)
            items = []
            query = request.query_params.get("q", "").strip().casefold()[:200]
            total = 0
            for item in sorted(path.iterdir(), key=lambda p: p.name.casefold()):
                if item.is_dir() and not item.is_symlink() and not item.name.startswith(".") and query in item.name.casefold():
                    total += 1
                    if len(items) < 150:
                        items.append({"name": item.name, "path": str(item)})
            return JSONResponse({"path": str(path), "parent": str(path.parent), "folders": items, "total": total, "query": query})
        except OSError:
            return error("Nie można wyświetlić zawartości folderu.")

    async def configuration(request):
        if (denied := guard(request, write=request.method == "POST")) is not None:
            return denied
        try:
            store = request.app.state.store
            current = preferences(store)
            if request.method == "POST":
                body = await payload(request)
                current = validate_preferences(body, current)
                store.set_setting("preferences", current)
            return JSONResponse(current, headers={"cache-control": "no-store"})
        except (ValueError, OSError) as exc:
            return safe_error(request, exc)

    async def capabilities(request):
        if (denied := guard(request)) is not None:
            return denied
        try:
            return JSONResponse({"models": await request.app.state.runner.models()})
        except (ValueError, OSError) as exc:
            return safe_error(request, exc)

    async def providers(request):
        if (denied := guard(request, write=request.method == "POST")) is not None:
            return denied
        try:
            if request.method == "POST":
                body = await payload(request)
                for task in request.app.state.runner.tasks.values():
                    if task["state"] in {"running", "starting", "awaiting_input", "stopping"} and body.get("id") in task.get("api_ids", []):
                        raise ValueError("To API należy do trwającego zadania. Możesz dodać nowe API teraz, a to edytować po zakończeniu zadania.")
                async with request.app.state.edit_lock:
                    settings, provider_id = await asyncio.to_thread(request.app.state.catalog.change, body)
                    request.app.state.settings = settings
                    # Refresh redaction before any new key could appear in a tool result.
                    runner = request.app.state.runner
                    runner.credential_env_names = {settings.proxy_token_env, *(p.api_key_env for p in settings.providers)}
                    for provider in settings.providers:
                        value = get_secret(provider.id, provider.api_key_env)
                        if value and value not in runner._secrets:
                            runner._secrets.append(value)
                    token = get_secret(LOCAL_TOKEN_ID, settings.proxy_token_env)
                    synced = False
                    try:
                        response = await request.app.state.client.post(proxy_url + "/admin/reload", json={},
                            headers={"authorization": "Bearer " + (token or "")}, timeout=8)
                        synced = response.status_code == 200
                    except httpx.HTTPError:
                        pass
                    return JSONResponse({"id": provider_id, "ok": True, "synced": synced,
                        "message": "Zapisano i wczytano API." if synced else "Zapisano API. Proxy wczyta zmianę przy uruchomieniu."})
            return JSONResponse({"providers": [public_provider(p) for p in request.app.state.settings.providers]}, headers={"cache-control": "no-store"})
        except (ValueError, OSError, RuntimeError) as exc:
            return safe_error(request, exc)

    async def project_agents(request):
        if (denied := guard(request, write=request.method == "POST")) is not None:
            return denied
        try:
            project = request.app.state.store.project(request.path_params["project_id"])
            if not project:
                raise ValueError("Wybierz projekt.")
            if request.method == "GET":
                result = await asyncio.to_thread(read_agents, project)
            else:
                body = await payload(request)
                async with request.app.state.edit_lock:
                    result = await asyncio.to_thread(write_agents, project, body.get("content"), body.get("version"), data_dir or ROOT / "data")
            return JSONResponse(result, headers={"cache-control": "no-store"})
        except (ValueError, OSError) as exc:
            return safe_error(request, exc)

    async def skills(request):
        if (denied := guard(request, write=request.method == "POST")) is not None:
            return denied
        try:
            project_id = request.query_params.get("project_id")
            body = await payload(request) if request.method == "POST" else {}
            project = request.app.state.store.project(body.get("project_id", project_id))
            if not project:
                raise ValueError("Wybierz projekt, aby odczytać jego skills.")
            inventory = await request.app.state.runner.skills(project["path"])
            current = preferences(request.app.state.store)
            overrides = {p["path"]: p["enabled"] for p in current["skill_overrides"]}
            if request.method == "POST":
                entries = body.get("skills")
                known = {s["path"]: str(Path(s["path"]).parent) for s in inventory.get("skills", [])}
                if not isinstance(entries, list) or len(entries) > 500:
                    raise ValueError("Niepoprawna lista skills.")
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get("path") not in known or not isinstance(entry.get("enabled"), bool):
                        raise ValueError("Wybierz skill obecny na liście.")
                    overrides[known[entry["path"]]] = entry["enabled"]
                current["skill_overrides"] = [{"path": p, "enabled": v} for p, v in overrides.items()]
                request.app.state.store.set_setting("preferences", current)
            result = []
            for item in inventory.get("skills", []):
                result.append({"name": item["name"], "path": item["path"], "description": item.get("description", ""),
                               "enabled": overrides.get(str(Path(item["path"]).parent), item.get("enabled", True))})
            return JSONResponse({"skills": result, "errors": inventory.get("errors", [])}, headers={"cache-control": "no-store"})
        except (ValueError, OSError) as exc:
            return safe_error(request, exc)

    async def switch_route(request):
        if (denied := guard(request, write=True)) is not None:
            return denied
        try:
            body = await payload(request)
            runner = request.app.state.runner
            task = runner.tasks.get(request.path_params["task_id"])
            if not task or task.get("mode") == "experimental":
                raise ValueError("W eksperymencie role API ustala się przed rozpoczęciem pracy.")
            target = body.get("provider_id")
            if target not in task.get("api_ids", []):
                raise ValueError("To API nie należy do wybranego zespołu zadania.")
            task["api_ids"] = [target] + [p for p in task["api_ids"] if p != target]
            if task["state"] not in {"starting", "running", "awaiting_input"}:
                runner.touch(task, save=True)
                return JSONResponse({"ok": True})
            route_id = task.get("route_id") or task["id"]
            strategy = "balanced" if task.get("effort") == "ultra" else "priority"
            await runner.setup_route(task, route_id, task["api_ids"], strategy, thread_id=task.get("thread_id"))
            for thread_id, task_id in tuple(runner.thread_tasks.items()):
                if task_id == task["id"] and thread_id != task.get("thread_id"):
                    await runner.setup_route(task, route_id, task["api_ids"], strategy,
                                             thread_id=thread_id, role="auxiliary")
            runner.touch(task, save=True)
            return JSONResponse({"ok": True})
        except (ValueError, OSError) as exc:
            return safe_error(request, exc)

    return Starlette(lifespan=lifespan, middleware=[
        Middleware(SidecarSecurityMiddleware, token=sidecar_token),
    ], routes=[
        Route("/", lambda request: RedirectResponse("/ui/")),
        Route("/health", health), Route("/ui/", index),
        Route("/ui/api/state", state), Route("/ui/api/projects", add_project, methods=["POST"]),
        Route("/ui/api/projects/{project_id}", remove_project, methods=["DELETE"]),
        Route("/ui/api/chats", create_chat, methods=["POST"]),
        Route("/ui/api/projects/{project_id}/chats", project_chats),
        Route("/ui/api/projects/{project_id}/history", project_history),
        Route("/ui/api/tasks/{task_id}/runs/{run_id}", run_details),
        Route("/ui/api/tasks", start_task, methods=["POST"]),
        Route("/ui/api/tasks/{task_id}/stop", stop_task, methods=["POST"]),
        Route("/ui/api/tasks/{task_id}/approval", approval, methods=["POST"]),
        Route("/ui/api/folders", folders), Mount("/ui/assets", StaticFiles(directory=ASSETS)),
        Route("/ui/api/settings", configuration, methods=["GET", "POST"]),
        Route("/ui/api/capabilities", capabilities),
        Route("/ui/api/providers", providers, methods=["GET", "POST"]),
        Route("/ui/api/projects/{project_id}/agents", project_agents, methods=["GET", "POST"]),
        Route("/ui/api/skills", skills, methods=["GET", "POST"]),
        Route("/ui/api/tasks/{task_id}/route", switch_route, methods=["POST"]),
        Route("/ui/api/shutdown", shutdown, methods=["POST"]),
        *([Route("/internal/shutdown", internal_shutdown, methods=["POST"])] if sidecar_token is not None else []),
    ])
