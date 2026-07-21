"""FastAPI app for the Haku console JSON API.

The console is the trusted outer shell: it frames Haku's own UI (haku-state's ``ui/``)
full-page as a sandboxed cross-origin iframe and owns the one privileged surface — the
**capability tier** (``haku.console.capabilities``), which uses console-only secrets and
acts on the world (launching the routine); it is same-origin gated and audited (see
``haku/docs/security.md`` → enforcement inventory #11). ``app.py`` wires that router and serves
the config endpoint. It can also mount the built SPA when ``static_dir`` is explicitly
configured for a direct local/dev fallback.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from haku.console import (
    capabilities,
    console_events,
    mcp_agent_auth,
    mcp_approval,
    mcp_mount,
    mcp_operator_oauth,
    mcp_server,
    node_daemons,
    oauth_association_maintenance,
    oauth_connection_result,
    oauth_token_state,
    operator_auth,
    provider_connection,
    tool_call_service,
)
from haku.console.agents import enrollment_routes
from haku.console.agents.authorization import PostgresAgentAuthority, StaticAgentDefinition, fingerprint_static_token
from haku.console.authentik_operator_token import PostgresAuthentikOperatorTokenStore
from haku.console.config import MCP_PATH, Settings
from haku.console.database_migrate import apply_migrations
from haku.console.deployment import DeploymentInfo, build_deployment_info
from haku.console.in_process_servers import HostexecServerConfig, InProcessServerDependencies, build_in_process_servers
from haku.console.mcp_auth.fastmcp_adapter import HakuMcpActorResolver, install_operator_session_route_guard
from haku.console.mcp_config import (
    InProcessBackend,
    InProcessServers,
    LoadedStaticAgent,
    OperatorConnectionCredential,
    _server_entry,
    load_console_config,
    load_static_agents,
    validate_in_process_server_bindings,
)
from haku.console.models import ConfigResponse
from haku.console.operator_identity import OperatorIdentityTrust
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.tools import gmail as gmail_tools, routine as routine_tools
from mcp_infra.authentik_auth.config import authentik_token_endpoint_for_issuer

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
    if path.startswith("/_console/assets/") and status_code in {200, 206, 304}:
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
    static_agent_definitions: tuple[StaticAgentDefinition, ...] | None = None,
    tool_call_executor: mcp_approval.McpServerClient | None = None,
    tool_call_metadata_provider: mcp_approval.McpServerClient | None = None,
    gmail_client: gmail_tools.GmailToolsClient | None = None,
    in_process_servers: InProcessServers | None = None,
) -> FastAPI:
    # Deploy-time console config file (non-secret): the MCP server catalog, static agents, and the
    # hostexec host map. `hostexec is not None` gates the hostexec in-process server, the login-time
    # offline_access request, and operator-Authentik-token persistence — computed once here.
    console_config = load_console_config(settings)
    hostexec_config = console_config.hostexec
    # Postgres is required: it backs the approval ledger and the operator OAuth store, both always
    # constructed. Construction is lazy (no connect); migrations run once at startup (app.main /
    # the test fixture), not here. Cross-replica fan-out (Postgres LISTEN/NOTIFY) is started by the
    # lifespan below, since the listen loop needs a running event loop.
    database_url = settings.database_url.get_secret_value()
    # One engine/sessionmaker for the whole console, injected into every SQLAlchemy store, so the
    # process holds a single connection pool rather than one per store. ConsoleEventHub is not a
    # SQLAlchemy store — it drives Postgres LISTEN/NOTIFY over its own raw psycopg connection.
    db_engine = create_engine(database_url, pool_pre_ping=True)
    db_sessions = sessionmaker(db_engine, expire_on_commit=False)
    operator_identity_store = PostgresOperatorIdentityStore(db_sessions, _operator_identity_trust(settings))
    oauth_token_states = oauth_token_state.PostgresOAuthTokenStateStore(
        db_sessions, operator_identity_store=operator_identity_store
    )
    console_event_hub = console_events.ConsoleEventHub(database_url, operator_identity_store=operator_identity_store)
    tool_call_ledger = mcp_approval.PostgresToolCallLedger(db_sessions)
    mcp_operator_oauth_store = mcp_operator_oauth.PostgresMcpOperatorOAuthStore(
        db_sessions,
        operator_identity_store=operator_identity_store,
        token_states=oauth_token_states,
        token_timeout_seconds=settings.mcp_operator_oauth_token_timeout_seconds,
    )
    # Per-Operator external provider connections (Google today), replacing Airlock's brokered
    # token. Only deploy-named providers whose client env vars are present are offered.
    provider_clients = provider_connection.load_provider_clients(console_config)
    provider_connection_store = provider_connection.PostgresProviderConnectionStore(
        db_sessions,
        operator_identity_store=operator_identity_store,
        token_states=oauth_token_states,
        provider_definitions=console_config.operator_connection_providers,
        provider_clients=provider_clients,
        operator_connections=console_config.operator_connections,
    )
    oauth_connection_result_store = oauth_connection_result.PostgresOAuthConnectionResultStore(
        db_sessions, operator_identity_store=operator_identity_store
    )
    # The operator's own Authentik token (captured at login via offline_access), self-refreshed with
    # the operator-OIDC client — hostexec exchanges it for a per-host token. The store derives the
    # Authentik token endpoint lazily (on refresh), so a non-Authentik operator OIDC that never
    # refreshes (e.g. a hermetic test IdP with hostexec off) constructs it fine.
    authentik_operator_token_store = PostgresAuthentikOperatorTokenStore(
        db_sessions,
        operator_identity_store=operator_identity_store,
        token_states=oauth_token_states,
        client_id=settings.operator_oidc.client_id,
        client_secret=settings.operator_oidc.client_secret.get_secret_value(),
        issuer=settings.operator_oidc.issuer,
    )
    oauth_maintenance = oauth_association_maintenance.OAuthAssociationMaintenance(
        db_engine,
        db_sessions,
        servers=console_config.mcp.servers,
        oauth_store=mcp_operator_oauth_store,
        provider_store=provider_connection_store,
        authentik_store=authentik_operator_token_store,
        refresh_authentik_tokens=hostexec_config is not None,
    )
    node_daemon_service = (
        node_daemons.NodeDaemonService(db_sessions, console_config.node_daemons)
        if console_config.node_daemons is not None
        else None
    )
    agent_authority = PostgresAgentAuthority(
        db_sessions, public_base_url=settings.public_base_url, operator_identity_store=operator_identity_store
    )
    # Tests/new databases may let create_app read env-backed static credentials here. Schema
    # generation may inject already-canonical definitions because it deliberately has no database;
    # this is the same authority input reconciled at startup, not a request-time identity shortcut.
    if loaded_static_agents is not None and static_agent_definitions is not None:
        raise ValueError("loaded_static_agents and static_agent_definitions are mutually exclusive")
    if static_agent_definitions is None:
        loaded_static_agents = (
            loaded_static_agents if loaded_static_agents is not None else load_static_agents(settings)
        )
        static_agent_definitions = tuple(
            StaticAgentDefinition(
                agent_id=agent.agent_id,
                display_name=agent.display_name,
                operator_id=operator_identity_store.resolve_configured_external_user_key(
                    agent.operator_external_user_key
                ),
                secret_reference=agent.secret_reference,
                token_fingerprint=fingerprint_static_token(agent.token.get_secret_value()),
            )
            for agent in loaded_static_agents
        )
    static_credential_registry = mcp_agent_auth.StaticAgentCredentialRegistry(
        fingerprints=tuple(definition.token_fingerprint for definition in static_agent_definitions)
    )

    # The gmail/google_calendar in-process servers are built per call from the acting Operator's
    # Google access token, resolved from the provider-connection store. Auto-approval label lookups
    # use the same per-Operator Gmail client; a test may inject a fixed `gmail_client` instead.
    async def gmail_client_provider(operator_id: UUID) -> gmail_tools.GmailToolsClient | None:
        if gmail_client is not None:
            return gmail_client
        backend = _server_entry(settings, gmail_tools.GMAIL_SERVER_ID).backend
        if not isinstance(backend, InProcessBackend) or not isinstance(
            backend.credential, OperatorConnectionCredential
        ):
            raise RuntimeError("gmail must bind an operator connection credential")
        token = await provider_connection_store.access_token_for(
            connection=backend.credential.connection, operator_id=operator_id
        )
        return gmail_tools.build_gmail_client_from_token(token) if token is not None else None

    # `haku_routine` fires the Haku claude-code-web routine as an approval-gated MCP tool (the
    # standard queue), superseding the bespoke launch-routine capability tier. Same
    # `launch_routine` config/secret; independent of the Google connection above.
    routine_launcher = routine_tools.RoutineLauncher(settings.launch_routine) if settings.launch_routine else None
    if in_process_servers is None:
        # hostexec being configured implies a real Authentik operator OIDC, so deriving the token
        # endpoint here (only in this branch) is safe.
        hostexec_server = None
        if hostexec_config is not None:
            assert node_daemon_service is not None
            hostexec_server = HostexecServerConfig(
                config=hostexec_config,
                token_endpoint=authentik_token_endpoint_for_issuer(settings.operator_oidc.issuer),
                broker=node_daemon_service,
            )
        in_process_servers = build_in_process_servers(
            InProcessServerDependencies(routine_launcher=routine_launcher, hostexec=hostexec_server)
        )
    validate_in_process_server_bindings(console_config, in_process_servers)
    if tool_call_executor is None:
        tool_call_executor = mcp_approval.McpServerClient(in_process_servers)
    if tool_call_metadata_provider is None:
        tool_call_metadata_provider = mcp_approval.McpServerClient(in_process_servers)
    tool_calls = tool_call_service.ToolCallApplicationService(
        settings=settings,
        repository=tool_call_ledger,
        invalidation_publisher=console_event_hub,
        executor=tool_call_executor,
        oauth_store=mcp_operator_oauth_store,
        in_process_servers=in_process_servers,
        gmail_client_provider=gmail_client_provider,
        provider_store=provider_connection_store,
        authentik_token_store=authentik_operator_token_store,
    )

    # The console's own Agent-and-Operator MCP server, mounted at /mcp — its reason to run.
    # Its tools re-expose the connected servers through the same application service. Always built;
    # `build_auth` fails loud if nothing can authenticate to it (no static agent, no OAuth).
    console_mcp_context = mcp_server.ConsoleMcpContext(
        settings=settings,
        tool_calls=tool_calls,
        oauth_store=mcp_operator_oauth_store,
        provider_store=provider_connection_store,
        metadata_provider=tool_call_metadata_provider,
        node_daemons=node_daemon_service,
    )

    mcp_auth = mcp_agent_auth.build_auth(
        settings,
        agent_authority=agent_authority,
        static_credentials=static_credential_registry,
        operator_identity_store=operator_identity_store,
    )
    actor_resolver = HakuMcpActorResolver(agent_authority, static_actor_resolver=mcp_auth.static_actor_resolver)
    console_mcp = mcp_server.build_console_mcp(
        console_mcp_context, auth=mcp_auth.provider, actor_resolver=actor_resolver
    )
    # The console runs multiple interchangeable replicas. FastMCP's default stateful HTTP
    # transport keeps its session map in-process, so a subsequent request routed to another pod
    # receives "Session not found". Haku's tools keep durable state in Postgres and do not need
    # transport-local sessions; give every MCP request a fresh transport instead.
    mcp_asgi = console_mcp.http_app(path=MCP_PATH, stateless_http=True)
    install_operator_session_route_guard(mcp_asgi, path=MCP_PATH)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        await agent_authority.reconcile_static_agents(static_agent_definitions)
        async with agent_authority.expiry_maintenance(), oauth_maintenance.run():
            await console_event_hub.start()
            try:
                # Pre-warm the OIDCProxy client-state store so the first OAuth request isn't slowed by a
                # cold connect (see mcp_infra/oauth_facade/server.py). The OAuth variant always carries
                # a concrete shared store; the static-only variant has no OAuth subsystem to initialize.
                if isinstance(mcp_auth, mcp_agent_auth.OAuthMcpAuth):
                    await mcp_auth.storage.setup()
                async with mcp_asgi.lifespan(app):
                    yield
            finally:
                # Cancel in-flight approved-call executions (each marks its row cancelled) before the
                # event hub they publish through is torn down.
                await tool_calls.aclose()
                await console_event_hub.aclose()

    # OAuth protected-resource and authorization-server discovery are origin-level RFC routes even
    # though the operational MCP/OAuth handlers remain isolated under /mcp. FastMCP cannot infer an
    # outer ASGI mount, so explicitly expose only its well-known routes here; the static-bearer-only
    # provider returns no routes.
    app = FastAPI(title="Haku console", lifespan=_lifespan)
    app.router.routes.extend(mcp_auth.provider.get_well_known_routes(mcp_path=MCP_PATH))
    # The capability router reads settings off app.state (see haku.console.capabilities).
    app.state.settings = settings
    # The operator-login callback persists the operator's Authentik token only when hostexec is
    # configured (offline_access is requested for the same reason). Read at request time from here.
    app.state.hostexec_enabled = hostexec_config is not None
    app.state.agent_enrollment_service = agent_authority
    app.state.operator_identity_store = operator_identity_store
    app.state.tool_call_service = tool_calls
    app.state.mcp_operator_oauth_store = mcp_operator_oauth_store
    app.state.provider_connection_store = provider_connection_store
    app.state.oauth_connection_result_store = oauth_connection_result_store
    app.state.authentik_operator_token_store = authentik_operator_token_store
    app.state.console_event_hub = console_event_hub
    app.state.in_process_servers = in_process_servers
    app.state.tool_call_metadata_provider = tool_call_metadata_provider
    app.state.node_daemon_service = node_daemon_service

    # Content-Security-Policy: let the console frame Haku's own UI origin (the sandboxed
    # cross-origin iframe) and Authentik's origin for the SSO redirect, and forbid the
    # console itself from being framed. Only frame-* is set, so the SPA's own scripts/styles
    # are unaffected. See haku/console/docs/containment.md.
    csp = f"frame-src 'self' {settings.haku_ui_url} {settings.auth_origin}; frame-ancestors 'none'"

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        # The operator-login failure response carries its own stricter, nonce-bound policy.
        # Preserve it; this default governs the SPA/API surfaces.
        response.headers.setdefault("Content-Security-Policy", csp)
        response.headers["Cache-Control"] = _cache_control_for_path(request.url.path, response.status_code)
        response.headers.setdefault("Referrer-Policy", REFERRER_POLICY)
        response.headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # The browser API is operator-only. Agents use /mcp; static bearer support there does not grant
    # access to any /api/* route. The same endpoint separately recognizes the Operator session.
    operator_only = [Depends(operator_auth.require_operator)]
    app.include_router(capabilities.router, dependencies=operator_only)
    app.include_router(console_events.router, dependencies=operator_only)
    app.include_router(mcp_approval.router, dependencies=operator_only)
    app.include_router(mcp_operator_oauth.router, dependencies=operator_only)
    app.include_router(provider_connection.router, dependencies=operator_only)
    app.include_router(oauth_connection_result.router, dependencies=operator_only)
    app.include_router(enrollment_routes.operator_router, dependencies=operator_only)
    app.include_router(node_daemons.operator_router, dependencies=operator_only)
    # Machine endpoints use their own per-daemon bearer and deliberately do not accept an Operator
    # browser session or CSRF token.
    app.include_router(node_daemons.machine_router)
    app.include_router(enrollment_routes.entry_router)

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
    app.state.operator_oauth = operator_auth.build_oauth(
        settings.operator_oidc, offline_access=hostexec_config is not None
    )
    app.include_router(operator_auth.router)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.operator_oidc.session_secret.get_secret_value(),
        https_only=settings.public_base_url.startswith("https://"),
        same_site="lax",
        max_age=operator_auth.OPERATOR_SESSION_MAX_AGE_SECONDS,
    )

    # MCP server (streamable HTTP), mounted after the API routers and before the SPA.
    mcp_mount.mount_mcp_app(app, path=MCP_PATH, mcp_app=mcp_asgi)

    # Optional direct local/dev fallback. Production serves the SPA from the
    # haku-console-static nginx image and leaves static_dir unset on this process.
    # Mounted last so the API routes above take precedence.
    if settings.static_dir is not None and settings.static_dir.is_dir():
        index_file = settings.static_dir / "index.html"

        # The production image exposes fingerprinted browser assets under the reserved console
        # namespace even though they remain in dist/assets on disk. Mirror that mapping here;
        # mounting it before the SPA fallback also keeps a missing asset a real 404.
        assets_dir = settings.static_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/_console/assets", _ConsoleStaticFiles(directory=assets_dir), name="console-assets")

        # Every other browser path is a client-side route: /_console/* selects trusted console
        # pages, while all other paths are mirrored Haku UI routes.
        @app.get("/{spa_path:path}")
        async def _spa_route(spa_path: str) -> Response:
            return Response(content=index_file.read_bytes(), media_type="text/html")

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = Settings()
    loaded_static_agents = load_static_agents(settings)
    # Apply DB migrations once before serving — the console owns its schema at startup, decoupled from
    # constructing any ledger/store (advisory-locked, so concurrent replicas don't race).
    apply_migrations(settings.database_url.get_secret_value())
    app = create_app(settings, loaded_static_agents=loaded_static_agents)
    # host/port are fixed, not env-driven: under the HAKU_CONSOLE_ prefix a `port`
    # setting would read the kubelet's HAKU_CONSOLE_PORT service-link var (a URL),
    # not an int. The deployment also disables service links (enableServiceLinks: false).
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
