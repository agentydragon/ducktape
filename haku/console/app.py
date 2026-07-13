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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi_csrf_protect import CsrfProtect
from fastapi_csrf_protect.exceptions import CsrfProtectError
from starlette.middleware.sessions import SessionMiddleware

from haku.console import capabilities, console_events, mcp_approval, mcp_operator_oauth, mcp_server, operator_auth
from haku.console.config import Settings
from haku.console.database_migrate import apply_migrations
from haku.console.deployment import DeploymentInfo, build_deployment_info
from haku.console.in_process_servers import InProcessServerDependencies, build_in_process_servers
from haku.console.mcp_config import resolve_static_agents
from haku.console.models import ConfigResponse
from haku.console.tools import (
    gmail as gmail_tools,
    google_calendar as google_calendar_tools,
    grocy as grocy_tools,
    routine as routine_tools,
    tana as tana_tools,
)
from mcp_infra.persistence import build_client_storage

APP_SHELL_CACHE_CONTROL = "no-store"
IMMUTABLE_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
NO_STORE_CACHE_CONTROL = "no-store"
REFERRER_POLICY = "no-referrer"
# The shell (top-level) may read geolocation for the `requestGeolocation` bridge action, and
# capture the screen for the `requestScreenshot` bridge action; `(self)` scopes both to the
# shell origin so neither is ever delegated to the framed haku-ui origin — the frame stays
# unable to read location or capture the screen on its own (docs/containment.md).
PERMISSIONS_POLICY = "geolocation=(self), display-capture=(self)"


def _cache_control_for_path(path: str, status_code: int) -> str:
    if path.startswith("/assets/") and status_code < 400:
        return IMMUTABLE_ASSET_CACHE_CONTROL
    # The app is authoritative for the cache policy of every backend surface it serves — nginx no
    # longer sets Cache-Control on proxied responses (haku/console/default.conf.template), so these
    # prefixes must be listed here, not there. Keep in sync with the proxied `location`s.
    if path.startswith(("/api/", "/mcp", "/auth/", "/.well-known/")) or path == "/healthz":
        return NO_STORE_CACHE_CONTROL
    return APP_SHELL_CACHE_CONTROL


def create_app(settings: Settings) -> FastAPI:
    # Postgres is required: it backs the approval ledger and the operator OAuth store, both always
    # constructed. Construction is lazy (no connect); migrations run once at startup (app.main /
    # the test fixture), not here. Cross-replica fan-out (Postgres LISTEN/NOTIFY) is started by the
    # lifespan below, since the listen loop needs a running event loop.
    database_url = settings.database_url.get_secret_value()
    console_event_hub = console_events.ConsoleEventHub(database_url)
    tool_call_ledger = mcp_approval.PostgresToolCallLedger(database_url)
    mcp_operator_oauth_store = mcp_operator_oauth.PostgresMcpOperatorOAuthStore(database_url)
    # The static machine agents (fixed bearer → operator subject) from the config file; resolved once
    # here (fails loud if a named env var is missing) and reused by the middleware, the /mcp static
    # verifier, and operator resolution.
    static_agents = resolve_static_agents(settings)
    # Google clients back the two in-process MCP servers (gmail, google_calendar) and their
    # approval-render read endpoints (gmail thread previews, calendar-name resolution).
    gmail_client = None
    calendar_client = None
    if settings.google_token_dir is not None:
        gmail_client = gmail_tools.build_gmail_client(settings.google_token_dir)
        calendar_client = google_calendar_tools.build_calendar_client(settings.google_token_dir)
    # `haku_routine` fires the Haku claude-code-web routine as an approval-gated MCP tool (the
    # standard queue), superseding the bespoke launch-routine capability tier. Same
    # `launch_routine` config/secret; independent of the Google grant above.
    routine_launcher = routine_tools.RoutineLauncher(settings.launch_routine) if settings.launch_routine else None
    in_process_servers = build_in_process_servers(
        InProcessServerDependencies(gmail=gmail_client, calendar=calendar_client, routine_launcher=routine_launcher)
    )
    tool_call_executor = mcp_approval.McpToolExecutor(in_process_servers)
    tool_call_metadata_provider = mcp_approval.McpMetadataProvider(in_process_servers)

    # The console's own agent-facing MCP server, mounted at /mcp — the console's whole reason to run.
    # Its tools re-expose the connected servers through the same approval ledger. Always built;
    # `build_auth` fails loud if nothing can authenticate to it (no static agent, no OAuth).
    console_mcp_context = mcp_server.ConsoleMcpContext(
        settings=settings,
        static_agents=static_agents,
        ledger=tool_call_ledger,
        hub=console_event_hub,
        executor=tool_call_executor,
        oauth_store=mcp_operator_oauth_store,
        metadata_provider=tool_call_metadata_provider,
        in_process_servers=in_process_servers,
        gmail_client=gmail_client,
    )

    # Record the agent→operator link when an OAuth agent (claude.ai / claude CLI) completes the
    # OIDCProxy authorization-code exchange: its DCR client_id → the operator's opaque subject.
    # A missing `sub` is a misconfiguration (both providers run sub_mode=user_id), so fail loud.
    async def _link_agent_operator(client_id: str, idp_tokens: Mapping[str, Any]) -> None:
        subject = mcp_operator_oauth.operator_subject_from_idp_tokens(idp_tokens)
        if subject is None:
            raise RuntimeError(f"MCP OAuth id_token for client {client_id} carried no `sub` claim")
        mcp_operator_oauth_store.upsert_agent_operator(agent_dcr_client_id=client_id, operator_subject=subject)

    # The OIDCProxy client-state store only exists when OAuth does; the static-bearer-only deploy has
    # no dynamic client registrations to persist.
    mcp_oauth_storage = build_client_storage(settings.mcp_oauth_persistence) if settings.mcp_oauth is not None else None
    console_mcp = mcp_server.build_console_mcp(
        console_mcp_context,
        auth=mcp_server.build_auth(
            settings, static_agents, mcp_oauth_storage, on_client_authorized=_link_agent_operator
        ),
    )
    mcp_asgi = console_mcp.http_app(path="/")

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        await console_event_hub.start()
        try:
            # Pre-warm the OIDCProxy client-state store so the first OAuth request isn't slowed by a
            # cold connect (see mcp_infra/oauth_facade/server.py).
            if mcp_oauth_storage is not None and hasattr(mcp_oauth_storage, "setup"):
                await mcp_oauth_storage.setup()
            # FastMCP's streamable-http session manager runs under mcp_asgi.lifespan; reflect the
            # connected servers into the tool surface once it is up.
            async with mcp_asgi.lifespan(app):
                await mcp_server.register_proxy_tools(console_mcp, console_mcp_context)
                yield
        finally:
            await console_event_hub.aclose()

    app = FastAPI(title="Haku console", lifespan=_lifespan)
    # The capability router reads settings off app.state (see haku.console.capabilities).
    app.state.settings = settings
    app.state.static_agents = static_agents
    app.state.tool_call_ledger = tool_call_ledger
    app.state.mcp_operator_oauth_store = mcp_operator_oauth_store
    app.state.gmail_client = gmail_client
    app.state.calendar_client = calendar_client
    app.state.console_event_hub = console_event_hub
    app.state.in_process_servers = in_process_servers
    app.state.tool_call_executor = tool_call_executor
    app.state.tool_call_metadata_provider = tool_call_metadata_provider

    # Content-Security-Policy: let the console frame Haku's own UI origin (the sandboxed
    # cross-origin iframe) and Authentik's origin for the SSO redirect, and forbid the
    # console itself from being framed. Only frame-* is set, so the SPA's own scripts/styles
    # are unaffected. See haku/console/docs/containment.md.
    csp = f"frame-src 'self' {settings.haku_ui_url} {settings.auth_origin}; frame-ancestors 'none'"

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = csp
        response.headers["Cache-Control"] = _cache_control_for_path(request.url.path, response.status_code)
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

    # Two router-level guards enforce the /api/* auth split (both no-op unless operator_oidc is set —
    # the dev/test mode has no in-app guard, as before). Operator-only routers require an operator
    # session; the agent-facing tool-call router (submit + read/sweep) also accepts a static agent's
    # bearer. A route added to a guarded router is protected by default — no path list to maintain.
    operator_only = [Depends(operator_auth.require_operator)]
    app.include_router(
        mcp_approval.agent_router, dependencies=[Depends(operator_auth.require_operator_or_static_agent)]
    )
    app.include_router(capabilities.router, dependencies=operator_only)
    app.include_router(console_events.router, dependencies=operator_only)
    app.include_router(mcp_approval.router, dependencies=operator_only)
    app.include_router(mcp_operator_oauth.router, dependencies=operator_only)
    app.include_router(gmail_tools.router, dependencies=operator_only)
    app.include_router(google_calendar_tools.router, dependencies=operator_only)
    app.include_router(grocy_tools.router, dependencies=operator_only)
    app.include_router(tana_tools.router, dependencies=operator_only)

    deployment_info = build_deployment_info()

    @app.get("/api/deployment", dependencies=operator_only)
    async def deployment() -> DeploymentInfo:
        return deployment_info

    @app.get("/api/config", dependencies=operator_only)
    async def config() -> ConfigResponse:
        """Static config for the SPA: launch-routine URL and Haku UI URL."""
        launch = settings.launch_routine
        return ConfigResponse(launch_routine_url=launch.page_url if launch else None, haku_ui_url=settings.haku_ui_url)

    # Operator browser auth (Authentik OIDC), replacing the proxy outpost. Gated on config so nothing
    # changes when unset (the outpost still guards; tests/dev run without it). SessionMiddleware
    # establishes request.session, which the router guards read; https_only follows the public origin.
    if settings.operator_oidc is not None:
        app.state.operator_oauth = operator_auth.build_oauth(settings.operator_oidc)
        app.include_router(operator_auth.router)
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.operator_oidc.session_secret.get_secret_value(),
            https_only=(settings.public_base_url or "").startswith("https://"),
            same_site="lax",
        )

    # Agent-facing MCP server (streamable HTTP), mounted after the API routers and before the SPA.
    app.mount("/mcp", mcp_asgi)

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
    settings = Settings()
    # Apply DB migrations once before serving — the console owns its schema at startup, decoupled from
    # constructing any ledger/store (advisory-locked, so concurrent replicas don't race).
    apply_migrations(settings.database_url.get_secret_value())
    app = create_app(settings)
    # host/port are fixed, not env-driven: under the HAKU_CONSOLE_ prefix a `port`
    # setting would read the kubelet's HAKU_CONSOLE_PORT service-link var (a URL),
    # not an int. The deployment also disables service links (enableServiceLinks: false).
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
