"""FastAPI app for the Haku console JSON API.

The console is the trusted outer shell: it frames Haku's own UI (haku-state's ``ui/``)
full-page as a sandboxed cross-origin iframe and owns the one privileged surface — the
**capability tier** (``haku.console.capabilities``), which uses console-only secrets and
acts on the world (launching the routine); it is CSRF-gated and audited (see
``haku/docs/security.md`` → enforcement inventory #11). ``app.py`` wires that router, configures CSRF, and serves
the config endpoint. It can also mount the built SPA when ``static_dir`` is explicitly
configured for a direct local/dev fallback.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi_csrf_protect import CsrfProtect
from fastapi_csrf_protect.exceptions import CsrfProtectError
from fastmcp import FastMCP

from haku.console import capabilities, mcp_approval, mcp_operator_oauth
from haku.console.config import Settings
from haku.console.models import ConfigResponse
from haku.console.tools import (
    gmail as gmail_tools,
    google_calendar as google_calendar_tools,
    grocy as grocy_tools,
    routine as routine_tools,
    tana as tana_tools,
)

logger = logging.getLogger(__name__)

APP_SHELL_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"
IMMUTABLE_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
NO_STORE_CACHE_CONTROL = "no-store"
REFERRER_POLICY = "no-referrer"
# The shell (top-level) may read geolocation for the `requestGeolocation` bridge action;
# `(self)` scopes it to the shell origin so it is NEVER delegated to the framed haku-ui
# origin — the frame stays unable to read location on its own (docs/containment.md).
PERMISSIONS_POLICY = "geolocation=(self)"


def _cache_control_for_path(path: str) -> str:
    if path.startswith("/assets/"):
        return IMMUTABLE_ASSET_CACHE_CONTROL
    if path.startswith("/api/") or path == "/healthz":
        return NO_STORE_CACHE_CONTROL
    return APP_SHELL_CACHE_CONTROL


def create_app(settings: Settings) -> FastAPI:
    database_url = settings.database_url.get_secret_value() if settings.database_url is not None else None
    # Cross-replica fan-out (Postgres LISTEN/NOTIFY) when a database is configured;
    # started/stopped by the lifespan below rather than at construction time, since
    # starting the listen loop needs a running event loop.
    tool_call_event_hub = mcp_approval.ToolCallEventHub(database_url)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        await tool_call_event_hub.start()
        try:
            yield
        finally:
            await tool_call_event_hub.aclose()

    app = FastAPI(title="Haku console", lifespan=_lifespan)
    # The capability router reads settings off app.state (see haku.console.capabilities).
    app.state.settings = settings
    app.state.tool_call_ledger = mcp_approval.PostgresToolCallLedger(database_url) if database_url is not None else None
    app.state.mcp_operator_oauth_store = (
        mcp_operator_oauth.PostgresMcpOperatorOAuthStore(database_url) if database_url is not None else None
    )
    # Two in-process MCP servers (gmail, google_calendar) built from the one mounted
    # haku_console_google token; each client is also exposed on app.state for its
    # approval-render read endpoint (gmail thread previews, calendar-name resolution).
    in_process_servers: dict[str, FastMCP] = {}
    app.state.gmail_client = None
    app.state.calendar_client = None
    if settings.google_token_dir is not None:
        gmail_client = gmail_tools.build_gmail_client(settings.google_token_dir)
        calendar_client = google_calendar_tools.build_calendar_client(settings.google_token_dir)
        app.state.gmail_client = gmail_client
        app.state.calendar_client = calendar_client
        in_process_servers[gmail_tools.GMAIL_SERVER_ID] = gmail_tools.build_mcp(gmail_client)
        in_process_servers[google_calendar_tools.GOOGLE_CALENDAR_SERVER_ID] = google_calendar_tools.build_mcp(
            calendar_client
        )
    # `haku_routine` fires the Haku claude-code-web routine as an approval-gated MCP tool (the
    # standard queue), superseding the bespoke launch-routine capability tier. Same
    # `launch_routine` config/secret; independent of the Google grant above.
    if settings.launch_routine is not None:
        in_process_servers[routine_tools.HAKU_ROUTINE_SERVER_ID] = routine_tools.build_mcp(
            routine_tools.RoutineLauncher(settings.launch_routine)
        )
    app.state.tool_call_event_hub = tool_call_event_hub
    app.state.in_process_servers = in_process_servers
    app.state.tool_call_executor = mcp_approval.McpToolExecutor(in_process_servers)
    app.state.tool_call_metadata_provider = mcp_approval.McpMetadataProvider(in_process_servers)

    # Content-Security-Policy: let the console frame Haku's own UI origin (the sandboxed
    # cross-origin iframe) and Authentik's origin for the SSO redirect, and forbid the
    # console itself from being framed. Only frame-* is set, so the SPA's own scripts/styles
    # are unaffected. See haku/console/docs/containment.md.
    csp = f"frame-src 'self' {settings.haku_ui_url} {settings.auth_origin}; frame-ancestors 'none'"

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = csp
        response.headers["Cache-Control"] = _cache_control_for_path(request.url.path)
        response.headers["Referrer-Policy"] = REFERRER_POLICY
        response.headers["Permissions-Policy"] = PERMISSIONS_POLICY
        return response

    # CSRF for the capability tier: a header-located double-submit token (the SPA
    # echoes the token from GET /api/capabilities/csrf in X-CSRF-Token). Use the
    # configured secret (shared across every replica — see csrf-secret.sops.yaml),
    # else an ephemeral one; the ephemeral fallback only ever worked by accident
    # with exactly one replica (a token from a different pod's secret would fail
    # validation), so it's a dev/test convenience now, not a real deploy path.
    csrf_secret = settings.csrf_secret.get_secret_value() if settings.csrf_secret else secrets.token_urlsafe(32)
    CsrfProtect.load_config(lambda: [("secret_key", csrf_secret), ("token_location", "header")])

    @app.exception_handler(CsrfProtectError)
    async def _csrf_error(request: Request, exc: CsrfProtectError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    async def config() -> ConfigResponse:
        """Static config for the SPA: launch-routine URL and Haku UI URL."""
        launch = settings.launch_routine
        return ConfigResponse(launch_routine_url=launch.page_url if launch else None, haku_ui_url=settings.haku_ui_url)

    app.include_router(capabilities.router)
    app.include_router(mcp_approval.router)
    app.include_router(mcp_operator_oauth.router)
    app.include_router(gmail_tools.router)
    app.include_router(google_calendar_tools.router)
    app.include_router(grocy_tools.router)
    app.include_router(tana_tools.router)

    # Optional direct local/dev fallback. Production serves the SPA from the
    # haku-console-static nginx image and leaves static_dir unset on this process.
    # Mounted last so the API routes above take precedence.
    if settings.static_dir is not None and settings.static_dir.is_dir():
        index_file = settings.static_dir / "index.html"

        # SPA client-side routes (frontend/routing.ts, e.g. /tool-calls) have no file on
        # disk; production's nginx serves index.html for them (try_files $uri /index.html).
        # Mirror that here — registered before the mount so it wins — so the dev fallback
        # can deep-link a console route instead of 404ing.
        @app.get("/tool-calls")
        async def _spa_route() -> FileResponse:
            return FileResponse(index_file)

        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="spa")

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = create_app(Settings())
    # host/port are fixed, not env-driven: under the HAKU_CONSOLE_ prefix a `port`
    # setting would read the kubelet's HAKU_CONSOLE_PORT service-link var (a URL),
    # not an int. The deployment also disables service links (enableServiceLinks: false).
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
