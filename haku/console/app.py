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
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi_csrf_protect import CsrfProtect
from fastapi_csrf_protect.exceptions import CsrfProtectError
from starlette.middleware.sessions import SessionMiddleware

from haku.console import (
    capabilities,
    console_events,
    mcp_agent_auth,
    mcp_approval,
    mcp_operator_oauth,
    mcp_server,
    operator_auth,
    tool_call_service,
)
from haku.console.config import MCP_PATH, Settings
from haku.console.database_migrate import apply_migrations
from haku.console.deployment import DeploymentInfo, build_deployment_info
from haku.console.in_process_servers import InProcessServerDependencies, build_in_process_servers
from haku.console.mcp_config import (
    InProcessServers,
    LoadedStaticAgent,
    ResolvedStaticAgent,
    load_static_agents,
    resolve_static_agents,
)
from haku.console.models import ConfigResponse
from haku.console.operator_identity import OperatorIdentityTrust
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.tools import (
    gmail as gmail_tools,
    google_calendar as google_calendar_tools,
    grocy as grocy_tools,
    routine as routine_tools,
    tana as tana_tools,
)

APP_SHELL_CACHE_CONTROL = "no-store"
IMMUTABLE_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
NO_STORE_CACHE_CONTROL = "no-store"
REFERRER_POLICY = "no-referrer"
# The shell (top-level) may read geolocation for the `requestGeolocation` bridge action, and
# capture the screen for the `requestScreenshot` bridge action; `(self)` scopes both to the
# shell origin so neither is ever delegated to the framed haku-ui origin — the frame stays
# unable to read location or capture the screen on its own (docs/containment.md).
PERMISSIONS_POLICY = "geolocation=(self), display-capture=(self)"


class _ConsoleStaticFiles(StaticFiles):
    """Direct dev static serving that never conditionally reuses the SPA shell.

    Bazel-normalized mtimes and same-sized shells can otherwise produce a false 304 after the
    embedded fingerprint changes. Production nginx has the same contract via ``etag off`` and
    ``if_modified_since off``.
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: MutableMapping[str, Any],
        status_code: int = 200,
    ) -> Response:
        path = Path(full_path)
        if path.name == "index.html":
            return Response(
                content=path.read_bytes(),
                headers={"Cache-Control": APP_SHELL_CACHE_CONTROL},
                media_type="text/html",
                status_code=status_code,
            )
        return super().file_response(full_path, stat_result, scope, status_code=status_code)


def _cache_control_for_path(path: str, status_code: int) -> str:
    if path.startswith("/assets/") and status_code in {200, 206, 304}:
        return IMMUTABLE_ASSET_CACHE_CONTROL
    # The app is authoritative for the cache policy of every backend surface it serves — nginx no
    # longer sets Cache-Control on proxied responses (haku/console/default.conf.template), so these
    # prefixes must be listed here, not there. Keep in sync with the proxied `location`s.
    if path.startswith(("/api/", "/mcp", "/auth/", "/.well-known/")) or path == "/healthz":
        return NO_STORE_CACHE_CONTROL
    return APP_SHELL_CACHE_CONTROL


def _operator_identity_trust(settings: Settings) -> OperatorIdentityTrust:
    trusted_issuers = {settings.operator_oidc.issuer}
    if settings.mcp_oauth is not None:
        trusted_issuers.add(settings.mcp_oauth.oidc_issuer)
    return OperatorIdentityTrust(
        trust_domain=settings.operator_identity.trust_domain, trusted_issuers=frozenset(trusted_issuers)
    )


def create_app(
    settings: Settings,
    *,
    loaded_static_agents: list[LoadedStaticAgent] | None = None,
    resolved_static_agents: list[ResolvedStaticAgent] | None = None,
    tool_call_executor: mcp_approval.McpToolExecutor | None = None,
    tool_call_metadata_provider: mcp_approval.McpMetadataProvider | None = None,
    gmail_client: gmail_tools.GmailToolsClient | None = None,
    calendar_client: google_calendar_tools.CalendarToolsClient | None = None,
    in_process_servers: InProcessServers | None = None,
) -> FastAPI:
    # Postgres is required: it backs the approval ledger and the operator OAuth store, both always
    # constructed. Construction is lazy (no connect); migrations run once at startup (app.main /
    # the test fixture), not here. Cross-replica fan-out (Postgres LISTEN/NOTIFY) is started by the
    # lifespan below, since the listen loop needs a running event loop.
    database_url = settings.database_url.get_secret_value()
    operator_identity_store = PostgresOperatorIdentityStore(database_url, _operator_identity_trust(settings))
    console_event_hub = console_events.ConsoleEventHub(database_url, operator_identity_store=operator_identity_store)
    tool_call_ledger = mcp_approval.PostgresToolCallLedger(database_url)
    mcp_operator_oauth_store = mcp_operator_oauth.PostgresMcpOperatorOAuthStore(
        database_url, operator_identity_store=operator_identity_store
    )
    # Read env-backed static credentials before migrations in ``main`` so the forward-only cutover
    # can preserve exact legacy owner keys. Tests/new databases may let create_app read them here.
    if loaded_static_agents is not None and resolved_static_agents is not None:
        raise ValueError("pass at most one static-agent startup representation")
    if resolved_static_agents is None:
        loaded_static_agents = (
            loaded_static_agents if loaded_static_agents is not None else load_static_agents(settings)
        )
        static_agents = resolve_static_agents(loaded_static_agents, operator_identity_store)
    else:
        static_agents = resolved_static_agents
    # Google clients back the two in-process MCP servers (gmail, google_calendar) and their
    # approval-render read endpoints (gmail thread previews, calendar-name resolution).
    if gmail_client is None and settings.google_token_dir is not None:
        gmail_client = gmail_tools.build_gmail_client(settings.google_token_dir)
    if calendar_client is None and settings.google_token_dir is not None:
        calendar_client = google_calendar_tools.build_calendar_client(settings.google_token_dir)
    # `haku_routine` fires the Haku claude-code-web routine as an approval-gated MCP tool (the
    # standard queue), superseding the bespoke launch-routine capability tier. Same
    # `launch_routine` config/secret; independent of the Google grant above.
    routine_launcher = routine_tools.RoutineLauncher(settings.launch_routine) if settings.launch_routine else None
    if in_process_servers is None:
        in_process_servers = build_in_process_servers(
            InProcessServerDependencies(gmail=gmail_client, calendar=calendar_client, routine_launcher=routine_launcher)
        )
    if tool_call_executor is None:
        tool_call_executor = mcp_approval.McpToolExecutor(in_process_servers)
    if tool_call_metadata_provider is None:
        tool_call_metadata_provider = mcp_approval.McpMetadataProvider(in_process_servers)
    tool_calls = tool_call_service.ToolCallApplicationService(
        settings=settings,
        repository=tool_call_ledger,
        event_publisher=console_event_hub,
        executor=tool_call_executor,
        oauth_store=mcp_operator_oauth_store,
        in_process_servers=in_process_servers,
        gmail_client=gmail_client,
    )

    # The console's own agent-facing MCP server, mounted at /mcp — the console's whole reason to run.
    # Its tools re-expose the connected servers through the same application service. Always built;
    # `build_auth` fails loud if nothing can authenticate to it (no static agent, no OAuth).
    console_mcp_context = mcp_server.ConsoleMcpContext(
        settings=settings,
        static_agents=static_agents,
        tool_calls=tool_calls,
        oauth_store=mcp_operator_oauth_store,
        identity_store=operator_identity_store,
        metadata_provider=tool_call_metadata_provider,
    )

    mcp_auth = mcp_agent_auth.build_auth(
        settings,
        static_agents,
        operator_oauth_store=mcp_operator_oauth_store,
        operator_identity_store=operator_identity_store,
    )
    console_mcp = mcp_server.build_console_mcp(console_mcp_context, auth=mcp_auth.provider)
    mcp_asgi = console_mcp.http_app(path="/")

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        await console_event_hub.start()
        try:
            # Pre-warm the OIDCProxy client-state store so the first OAuth request isn't slowed by a
            # cold connect (see mcp_infra/oauth_facade/server.py). The OAuth variant always carries
            # a concrete shared store; the static-only variant has no OAuth subsystem to initialize.
            if isinstance(mcp_auth, mcp_agent_auth.OAuthMcpAuth):
                await mcp_auth.storage.setup()
            # FastMCP's streamable-http session manager runs under mcp_asgi.lifespan; reflect the
            # connected servers into the tool surface once it is up.
            async with mcp_asgi.lifespan(app):
                await mcp_server.register_proxy_tools(console_mcp, console_mcp_context)
                yield
        finally:
            await console_event_hub.aclose()

    # OAuth protected-resource and authorization-server discovery are origin-level RFC routes even
    # though the operational MCP/OAuth handlers remain isolated under /mcp. FastMCP cannot infer an
    # outer ASGI mount, so explicitly expose only its well-known routes here; the static-bearer-only
    # provider returns no routes.
    app = FastAPI(title="Haku console", lifespan=_lifespan)
    app.router.routes.extend(mcp_auth.provider.get_well_known_routes(mcp_path="/"))
    # The capability router reads settings off app.state (see haku.console.capabilities).
    app.state.settings = settings
    app.state.static_agents = static_agents
    app.state.operator_identity_store = operator_identity_store
    app.state.tool_call_service = tool_calls
    app.state.mcp_operator_oauth_store = mcp_operator_oauth_store
    app.state.gmail_client = gmail_client
    app.state.calendar_client = calendar_client
    app.state.console_event_hub = console_event_hub
    app.state.in_process_servers = in_process_servers
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

    # Two router-level guards enforce the /api/* auth split. Operator-only routers require an
    # operator session; the agent-facing tool-call router (submit + read/sweep) also accepts a static
    # agent's bearer. A route added to a guarded router is protected by default — no path list.
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

    # Operator browser auth is mandatory. SessionMiddleware establishes request.session, which the
    # router guards read; https_only follows the canonical public origin.
    app.state.operator_oauth = operator_auth.build_oauth(settings.operator_oidc)
    app.include_router(operator_auth.router)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.operator_oidc.session_secret.get_secret_value(),
        https_only=settings.public_base_url.startswith("https://"),
        same_site="lax",
        max_age=operator_auth.OPERATOR_SESSION_MAX_AGE_SECONDS,
    )

    # Agent-facing MCP server (streamable HTTP), mounted after the API routers and before the SPA.
    app.mount(MCP_PATH, mcp_asgi)

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
        async def _spa_route() -> Response:
            return Response(content=index_file.read_bytes(), media_type="text/html")

        app.mount("/", _ConsoleStaticFiles(directory=settings.static_dir, html=True), name="spa")

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = Settings()
    loaded_static_agents = load_static_agents(settings)
    # Apply DB migrations once before serving — the console owns its schema at startup, decoupled from
    # constructing any ledger/store (advisory-locked, so concurrent replicas don't race).
    apply_migrations(
        settings.database_url.get_secret_value(),
        operator_identity_seeds=[
            (settings.operator_identity.trust_domain, agent.operator_external_user_key)
            for agent in loaded_static_agents
        ],
        fastmcp_oauth_state_table=(
            settings.mcp_oauth.persistence.table_name if settings.mcp_oauth is not None else None
        ),
    )
    app = create_app(settings, loaded_static_agents=loaded_static_agents)
    # host/port are fixed, not env-driven: under the HAKU_CONSOLE_ prefix a `port`
    # setting would read the kubelet's HAKU_CONSOLE_PORT service-link var (a URL),
    # not an int. The deployment also disables service links (enableServiceLinks: false).
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
