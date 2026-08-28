"""FastAPI app for the Haku console JSON API.

The console is the trusted outer shell: it frames Haku's own UI (haku-state's ``ui/``)
full-page as a sandboxed cross-origin iframe and owns the one privileged surface — the
**capability tier** (``haku.console.capabilities``), which uses console-only secrets and
acts on the world (launching the routine); it is same-origin gated and audited (see
``haku/docs/security.md`` → enforcement inventory, "Console privileged-action tier"). ``app.py``
wires that router and serves the config endpoint. It can also mount the built SPA when
``static_dir`` is explicitly configured for a direct local/dev fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from more_itertools import one
from openai import AsyncOpenAI
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.middleware.sessions import SessionMiddleware

from haku.console import (
    agent_bearer_authority,
    capabilities,
    http_decide_routes,
    http_grant_routes,
    kube_proxy_authorization,
    kubernetes_grant_routes,
    mcp_agent_auth,
    mcp_approval,
    mcp_catalog_reconciler,
    mcp_mount,
    mcp_operator_oauth,
    mcp_server,
    operator_auth,
    operator_login_flow,
    tool_call_service,
)
from haku.console.agents import enrollment_routes
from haku.console.agents.authorization import PostgresAgentAuthority, StaticAgentDefinition, fingerprint_static_token
from haku.console.authentik_operator_token import PostgresAuthentikOperatorTokenStore
from haku.console.auto_approval.github import GitHubRepositoryVisibilityService
from haku.console.chat_models import RuntimeKind
from haku.console.config import MCP_PATH, Settings
from haku.console.database_migrate import main as migration_main, verify_schema
from haku.console.deployment import DeploymentInfo, build_deployment_info
from haku.console.hostexecd import service
from haku.console.http_decide_config import load_egress_decide
from haku.console.http_decide_service import HttpDecideService
from haku.console.http_grant_repository import PostgresHttpGrantRepository
from haku.console.http_grant_service import HttpGrantService
from haku.console.in_process_servers import (
    HostexecServerConfig,
    InProcessServerDependencies,
    SandboxServerConfig,
    build_in_process_servers,
)
from haku.console.kubernetes_authorization import KubernetesAuthorizationService, KubernetesSubjectAccessReviewClient
from haku.console.kubernetes_grant_repository import PostgresKubernetesGrantRepository
from haku.console.kubernetes_grant_service import KubernetesGrantService
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
from haku.console.models import ChatLaunchOption, ConfigResponse
from haku.console.notifications import connection_metrics, console_events, push, push_routes
from haku.console.oauth import association_maintenance, connection_result, provider_connection, token_state
from haku.console.operator_identity import OperatorIdentityTrust
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.recall_index_reader import PostgresIndexSearcher
from haku.console.tools import (
    gmail as gmail_tools,
    http_grants as http_grants_tools,
    kubernetes as kubernetes_tools,
    routine as routine_tools,
    sandbox as sandbox_tools,
)
from haku.console.tools.recall_index import HAKU_INDEX_SERVER_ID
from haku.console.x import (
    conversation_follow,
    conversation_reader,
    conversation_runtime,
    runtime as console_runtime,
    runtime_catalog,
    sandbox_allocation,
    sandbox_claims,
    session_runtime,
    subscription,
)

# Aliased: bare `conversation`, `sync` and `outbox` would each collide with something this module
# already talks about (the console's own conversation record, the index sweeps, the push queue).
from haku.console.x.channels.matrix import (
    conversation as matrix_conversation,
    ingress_ledger as matrix_ingress_ledger,
    outbox as matrix_outbox,
    outbox_wake as matrix_outbox_wake,
    revisions as matrix_revisions,
    sync as matrix_sync,
)
from haku.console.x.channels.matrix.room_copy import RoomCopy
from haku.console.x.conversation_history import ConversationHistory
from haku.console.x.conversation_live_updates import ConversationLiveUpdates
from haku.console.x.conversation_wakes import ConversationWakes
from haku.console.x.launch_identity import ChatLaunchAuthorizer
from haku.console.x.session_store import SessionStore
from haku.console.x.session_wakes import SessionWakes
from haku.console.x.system_prompt import SystemPromptTemplate
from haku.recall_index.config import EmbedderConfig
from haku.recall_index.openai_embedder import OpenAIEmbedder
from haku.runtime.x.bridge.protocol import KUBERNETES_PROXY_URL_ENV, RUNNER_SETUP_ENV
from haku.sandbox.kubernetes_client import InClusterSandboxClient
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
    if path.startswith(("/api/", "/mcp", "/auth/", "/.well-known/", "/metrics")) or path == "/healthz":
        return NO_STORE_CACHE_CONTROL
    return APP_SHELL_CACHE_CONTROL


def _embedder(config: EmbedderConfig, *, timeout: float) -> OpenAIEmbedder:
    return OpenAIEmbedder(
        AsyncOpenAI(base_url=config.base_url, api_key=config.api_key.get_secret_value(), timeout=timeout),
        model=config.model,
        query_instruction=config.query_instruction,
    )


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
    gmail_client: gmail_tools.GmailToolsClient | None = None,
    in_process_servers: InProcessServers | None = None,
) -> FastAPI:
    # Deploy-time console config file (non-secret): the MCP server catalog, static agents, and the
    # hostexec host map. `hostexec is not None` gates the hostexec in-process server, the login-time
    # offline_access request, and operator-Authentik-token persistence — computed once here.
    console_config = load_console_config(settings.config_file)
    hostexec_config = console_config.hostexec
    # Postgres is required: it backs the approval ledger and the operator OAuth store, both always
    # constructed. Construction is lazy (no connect); migrations run once at startup (app.main /
    # the test fixture), not here. Cross-replica fan-out (Postgres LISTEN/NOTIFY) is started by the
    # lifespan below, since the listen loop needs a running event loop.
    # One engine/sessionmaker for the whole console, injected into every SQLAlchemy store, so the
    # process holds a single connection pool rather than one per store. ConsoleEventHub is not a
    # SQLAlchemy store — it drives Postgres LISTEN/NOTIFY over its own raw psycopg connection.
    database_url = settings.database_url.get_secret_value()
    db_engine = create_async_engine(database_url, pool_pre_ping=True)
    db_sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    operator_identity_store = PostgresOperatorIdentityStore(db_sessions, _operator_identity_trust(settings))
    operator_login_flows = operator_login_flow.PostgresOperatorLoginFlowStore(db_sessions)
    oauth_token_states = token_state.PostgresTokenStateStore(
        db_sessions, operator_identity_store=operator_identity_store
    )
    console_event_hub = console_events.ConsoleEventHub(database_url, operator_identity_store=operator_identity_store)
    claude_runtime = console_config.harnesses.claude_code if console_config.harnesses is not None else None
    codex_runtime = console_config.harnesses.codex_app_server if console_config.harnesses is not None else None
    static_by_id = {agent.agent_id: agent for agent in console_config.static_agents}
    profile_runtime_kinds = {
        profile.id: set(profile.allowed_chat_runtimes) for profile in console_config.access_profiles
    }
    launchable_agent_ids = {entry.agent_id for entry in console_config.launchable_agents}
    # Each layer owns its own LISTEN connection on its own channel: a session and a conversation
    # are different layers, so their wakes share no wire, no connection, and no module. Two
    # connections is the accepted cost of that separation.
    session_wakes = SessionWakes(database_url)
    conversation_wakes = ConversationWakes(database_url)
    # Conversation changes reach open tabs over the console socket the shell already holds,
    # coalesced per conversation. Constructed unconditionally: it listens on the conversation
    # channel and sends on the console one, neither of which depends on this replica running a
    # Claude runtime.
    conversation_live_updates = ConversationLiveUpdates(conversation_wakes, console_event_hub, db_sessions)
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
    oauth_connection_result_store = connection_result.PostgresConnectionResultStore(
        db_sessions, operator_identity_store=operator_identity_store
    )
    # Web Push reaches the operator's browsers when none of them has the console open. Without a
    # VAPID identity there is nothing to sign with, so the console simply never notifies.
    push_subscription_store = push.PostgresPushSubscriptionStore(db_sessions)
    push_identity = push.PushIdentity(settings.web_push) if settings.web_push else None
    approval_notifier: push.Notifier | push.NullNotifier = (
        push.Notifier(
            identity=push_identity, subscriptions=push_subscription_store, console_base_url=settings.public_base_url
        )
        if push_identity is not None
        else push.NullNotifier()
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
    oauth_maintenance = association_maintenance.AssociationMaintenance(
        db_engine,
        db_sessions,
        servers=console_config.mcp.servers,
        oauth_store=mcp_operator_oauth_store,
        provider_store=provider_connection_store,
        authentik_store=authentik_operator_token_store,
        refresh_authentik_tokens=hostexec_config is not None,
    )
    hostexecd_service = (
        service.Service(db_sessions, console_config.node_daemons) if console_config.node_daemons is not None else None
    )
    agent_authority = PostgresAgentAuthority(
        db_sessions,
        public_base_url=settings.public_base_url,
        operator_identity_store=operator_identity_store,
        access_profiles=tuple(profile.id for profile in console_config.access_profiles),
        default_access_profile_id=console_config.default_access_profile_id,
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

    runtime_registry: console_runtime.RuntimeRegistry
    registrations: list[runtime_catalog.RuntimeRegistration] = []
    runner_environment = (
        {}
        if settings.runner_kubernetes_proxy_url is None
        else {KUBERNETES_PROXY_URL_ENV: settings.runner_kubernetes_proxy_url}
    )
    # Prompts belong to launchable Agents: each runtime registration loads its Agent's identity
    # template, whose own `{% include %}` pulls in the shared attached-chat fragment. Rendered here
    # at startup for every launchable Agent, so a broken include or name prevents readiness rather
    # than failing the first attached chat session hours later.
    launchable_by_id = {entry.agent_id: entry for entry in console_config.launchable_agents}

    def agent_system_prompt(agent_id: UUID) -> SystemPromptTemplate:
        template = SystemPromptTemplate.from_path(launchable_by_id[agent_id].system_prompt_template)
        template.verify_renders()
        return template

    if claude_runtime is not None:
        try:
            claude_profile_id = static_by_id[claude_runtime.agent_id].access_profile_id
        except KeyError as error:
            raise ValueError("configured Claude Agent must be a static Agent") from error
        registrations.append(
            runtime_catalog.runtime_registration(
                claude_runtime,
                sandbox_claims.KubernetesSandboxClaims(
                    sandbox_claims.SandboxClaimSpec(
                        namespace=claude_runtime.namespace,
                        warm_pool=claude_runtime.warm_pool,
                        claim_prefix=claude_runtime.claim_prefix,
                        runtime_label=claude_runtime.runtime_label,
                        runner_environment={},
                    )
                ),
                system_prompt=agent_system_prompt(claude_runtime.agent_id),
                access_profile_id=claude_profile_id,
                execution_environment={
                    **runner_environment,
                    **(
                        {}
                        if settings.haku_agent_workspace_setup is None
                        else {RUNNER_SETUP_ENV: str(settings.haku_agent_workspace_setup)}
                    ),
                },
            )
        )
    if codex_runtime is not None:
        try:
            codex_profile_id = static_by_id[codex_runtime.agent_id].access_profile_id
        except KeyError as error:
            raise ValueError("configured Codex Agent must be a static Agent") from error
        registrations.append(
            runtime_catalog.runtime_registration(
                codex_runtime,
                sandbox_claims.KubernetesSandboxClaims(
                    sandbox_claims.SandboxClaimSpec(
                        namespace=codex_runtime.namespace,
                        warm_pool=codex_runtime.warm_pool,
                        claim_prefix=codex_runtime.claim_prefix,
                        runtime_label=codex_runtime.runtime_label,
                        runner_environment={},
                    )
                ),
                system_prompt=agent_system_prompt(codex_runtime.agent_id),
                access_profile_id=codex_profile_id,
                # The public-coder SandboxTemplate already owns the explicit empty-workspace
                # setup policy. Registration contributes only Console-selected shared topology.
                execution_environment=runner_environment,
            )
        )
    if registrations:
        runtime_registry = runtime_catalog.execution_registry(*registrations)
    else:
        # Runtime-disabled replicas can still inspect every linked durable runtime kind. This
        # registry has projection only: no claims, credentials, or launcher.
        runtime_registry = runtime_catalog.projection_registry()
    if codex_runtime is not None:
        codex_profile_id = static_by_id[codex_runtime.agent_id].access_profile_id
        profile_agents = {
            agent_id for agent_id, agent in static_by_id.items() if agent.access_profile_id == codex_profile_id
        }
        if profile_agents != {codex_runtime.agent_id}:
            raise ValueError("configured Codex Agent must have a dedicated access profile")
    # All read and write paths share one registry. Projection-only composition may link dormant
    # adapters, while launch-capable production composition includes only deliberately supported
    # adapters and resources; no hidden Claude fallback can reinterpret another runtime's rows.
    session_store = SessionStore(db_sessions, runtime_registry)

    async def _resolve_static_agent_definitions() -> tuple[StaticAgentDefinition, ...]:
        assert loaded_static_agents is not None

        async def resolve_agent(agent: LoadedStaticAgent) -> StaticAgentDefinition:
            return StaticAgentDefinition(
                agent_id=agent.agent_id,
                display_name=agent.display_name,
                operator_id=await operator_identity_store.resolve_configured_external_user_key(
                    agent.operator_external_user_key
                ),
                secret_reference=agent.secret_reference,
                token_fingerprint=fingerprint_static_token(agent.token.get_secret_value()),
                access_profile_id=agent.access_profile_id,
            )

        return tuple([await resolve_agent(agent) for agent in loaded_static_agents])

    # Matrix chat surface, absent when unconfigured: the console serves its approval queue
    # without it and simply does not run the sync loop. Neutral runtime supervision is composed
    # after the configured runtime catalog because it provisions sessions through that service.
    matrix_sync_service: matrix_sync.MatrixSyncService | None = None
    matrix_conversation_store: matrix_conversation.MatrixConversationStore | None = None
    if (matrix_config := settings.matrix) is not None and matrix_config.password is not None:
        matrix_conversation_store = matrix_conversation.MatrixConversationStore(db_sessions)
        matrix_ledger = matrix_ingress_ledger.IngressLedger(db_sessions)
        # The sync service hosts one reconciler per live attachment — each room's subscriber to the
        # conversation record and the drain of its reply outbox — so the record readers, the
        # correspondence store and the outbox are all composed into it.
        matrix_sync_service = matrix_sync.MatrixSyncService(
            matrix_config,
            matrix_config.password,
            db_engine,
            matrix_sync.MatrixSyncStore(db_sessions),
            matrix_conversation_store,
            operator_identity_store,
            matrix_conversation.MatrixTurns(matrix_config, session_store, operator_identity_store, matrix_ledger),
            matrix_outbox.RoomOutbox(db_sessions),
            matrix_revisions.RevisionLog(db_sessions),
            matrix_ledger,
            RoomCopy(db_sessions),
            # The channel's own wake wire; the sync leader starts and stops it with its reconcilers.
            matrix_outbox_wake.OutboxWakes(database_url),
            db_sessions,
            subscription.ConversationStream(db_sessions),
            conversation_wakes,
        )
    # Execution exists only when a launch-capable adapter was configured. Read-only replicas keep
    # the same registry in their store above but expose no session-creation runtime service.
    default_runtime_kind: RuntimeKind | None = None
    if runtime_registry.configured_kinds:
        authorize_chat_launch = ChatLaunchAuthorizer(
            agent_authority,
            launchable_agent_ids=launchable_agent_ids,
            registered_runtime_identities=runtime_registry.configured_identities,
            profile_runtime_kinds=profile_runtime_kinds,
        )

        default_chat_agent_id = console_config.default_chat_agent_id
        assert default_chat_agent_id is not None
        default_profile_id = static_by_id[default_chat_agent_id].access_profile_id
        default_candidates = {
            identity.runtime_kind
            for identity in runtime_registry.configured_identities
            if identity.agent_id == default_chat_agent_id
            and identity.runtime_kind in profile_runtime_kinds[default_profile_id]
        }
        if RuntimeKind.CLAUDE_CODE in default_candidates:
            default_runtime_kind = RuntimeKind.CLAUDE_CODE
        else:
            try:
                default_runtime_kind = one(default_candidates)
            except ValueError:
                raise ValueError("default chat Agent must select one configured runtime") from None

        session_service = session_runtime.SessionService(
            runtime_registry,
            session_store,
            session_wakes,
            conversation_history=ConversationHistory(db_sessions),
            launch_authorizer=authorize_chat_launch,
            default_agent_id=default_chat_agent_id,
            default_runtime_kind=default_runtime_kind,
        )
        if matrix_conversation_store is not None:
            matrix_conversation_store.configure_launch_identity(
                authorize_chat_launch, default_agent_id=default_chat_agent_id, default_runtime_kind=default_runtime_kind
            )
    else:
        session_service = None
    sandbox_allocator = (
        sandbox_allocation.SandboxAllocator(session_service, session_store, session_wakes, db_engine)
        if session_service is not None
        else None
    )
    runtime_supervisor = (
        conversation_runtime.ConversationRuntime(session_service, session_store, conversation_wakes, db_engine)
        if session_service is not None
        else None
    )
    # A followed conversation's own socket. Keep it behind executable runtime composition because
    # a follower opens on the same read `GET /api/conversations/{id}` serves; a projection-only
    # replica answers neither.
    follow = (
        None
        if session_service is None
        else conversation_follow.ConversationFollow(session_store, session_service, conversation_wakes)
    )
    if static_agent_definitions is not None:
        static_agent_fingerprints = tuple(definition.token_fingerprint for definition in static_agent_definitions)
    else:
        assert loaded_static_agents is not None
        static_agent_fingerprints = tuple(
            fingerprint_static_token(agent.token.get_secret_value()) for agent in loaded_static_agents
        )
    static_credential_registry = agent_bearer_authority.StaticAgentCredentialRegistry(
        fingerprints=static_agent_fingerprints
    )
    bearer_authority = agent_bearer_authority.build_agent_bearer_authority(
        agent_authority=agent_authority, static_credentials=static_credential_registry, session_tokens=db_sessions
    )

    mcp_auth = mcp_agent_auth.build_auth(
        settings,
        agent_authority=agent_authority,
        static_credentials=static_credential_registry,
        operator_identity_store=operator_identity_store,
        agent_bearer_authority=bearer_authority,
    )
    actor_resolver = HakuMcpActorResolver(agent_authority, static_actor_resolver=mcp_auth.static_actor_resolver)

    kubernetes_grants = KubernetesGrantService(
        PostgresKubernetesGrantRepository(db_sessions),
        max_lifetime=datetime.timedelta(seconds=console_config.kubernetes_grant_max_lifetime_seconds),
    )
    http_grants = HttpGrantService(
        PostgresHttpGrantRepository(db_sessions),
        max_lifetime=datetime.timedelta(seconds=console_config.http_grant_max_lifetime_seconds),
    )
    http_decide = (
        HttpDecideService(
            grants=http_grants,
            credentials=load_egress_decide(console_config.egress_decide),
            prohibited_cidrs=console_config.egress_decide.prohibited_cidrs,
        )
        if console_config.egress_decide is not None
        else None
    )
    kubernetes_authorization = (
        KubernetesAuthorizationService(
            config=console_config.kubernetes_authorization,
            agent_bearer_authority=bearer_authority,
            grants=kubernetes_grants,
            sar_client=KubernetesSubjectAccessReviewClient(),
        )
        if console_config.kubernetes_authorization is not None
        else None
    )
    github_repository_visibility = GitHubRepositoryVisibilityService()

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
    sandbox_server: SandboxServerConfig | None = None
    if in_process_servers is None:
        # hostexec being configured implies a real Authentik operator OIDC, so deriving the token
        # endpoint here (only in this branch) is safe.
        hostexec_server = None
        if hostexec_config is not None:
            assert hostexecd_service is not None
            hostexec_server = HostexecServerConfig(
                config=hostexec_config,
                token_endpoint=authentik_token_endpoint_for_issuer(settings.operator_oidc.issuer),
                broker=hostexecd_service,
            )
        # Configured rather than switched on separately: `config.yaml` is where the server is
        # listed and where the policy that lets an agent call it lives, and a boolean elsewhere
        # could only ever disagree with it — a listed server with no builder fails the binding
        # validation below.
        configured_server_ids = {server.id for server in console_config.mcp.servers}
        index_searcher = None
        if HAKU_INDEX_SERVER_ID in configured_server_ids:
            if settings.embedder is None:
                raise ValueError(
                    f"MCP server {HAKU_INDEX_SERVER_ID!r} is configured but no embedder is: "
                    "search embeds its query, so it cannot run without one"
                )
            # A database reader only: the source and embedding maintenance stages run in the
            # separately deployed haku-indexer worker (haku/console/indexer.py), so search keeps
            # serving the last committed index state while maintenance fails or rolls. The
            # request-path timeout applies: a search embeds one query and should fail rather
            # than hang.
            index_searcher = PostgresIndexSearcher(
                db_sessions,
                _embedder(settings.embedder, timeout=settings.embedder.timeout_seconds),
                indexes=console_config.recall_indexes,
                budget=settings.recall_index.chunk_budget,
            )
        # Claims are created lazily on first use, so this holds no Kubernetes connection until an
        # Agent provisions; `aclose` below is what releases it.
        sandbox_server = (
            SandboxServerConfig(
                client=InClusterSandboxClient(console_config.agent_sandbox), environment=console_config.agent_sandbox
            )
            if console_config.agent_sandbox is not None and sandbox_tools.SANDBOX_SERVER_ID in configured_server_ids
            else None
        )
        in_process_servers = build_in_process_servers(
            InProcessServerDependencies(
                routine_launcher=routine_launcher,
                hostexec=hostexec_server,
                index=index_searcher,
                recall_access_profiles=tuple(console_config.access_profiles),
                configured_recall_index_ids=tuple(index.index_id for index in console_config.recall_indexes),
                # Only with an executable runtime: otherwise nothing writes sessions, so the read
                # tools would reflect an always-empty corpus.
                conversations=(
                    conversation_reader.ConversationReads(session_store) if runtime_registry.configured_kinds else None
                ),
                sandbox=sandbox_server,
                kubernetes=(
                    kubernetes_tools.KubernetesToolsService(
                        grants=kubernetes_grants, authorization=kubernetes_authorization
                    )
                    if kubernetes_authorization is not None
                    and kubernetes_tools.KUBERNETES_SERVER_ID in configured_server_ids
                    else None
                ),
                http_grants=(
                    http_grants_tools.HttpToolsService(grants=http_grants, agents=agent_authority)
                    if http_grants_tools.HTTP_GRANTS_SERVER_ID in configured_server_ids
                    else None
                ),
            )
        )
    validate_in_process_server_bindings(console_config, in_process_servers)
    # The console's one path out to its configured MCP servers. Executing a tool and reflecting a
    # catalog are the same dispatch over the same transports, so they are one object: executing and
    # reflecting are not separate roles with separate wiring.
    dispatcher = mcp_approval.McpServerDispatcher(
        in_process_servers, catalog_cache_ttl_seconds=settings.mcp_catalog_refresh_interval_seconds
    )
    catalogs = mcp_catalog_reconciler.OperatorCatalogReconciler(
        servers=console_config.mcp.servers,
        dispatcher=dispatcher,
        oauth_store=mcp_operator_oauth_store,
        provider_store=provider_connection_store,
        operator_ids=operator_identity_store.list_active_ids,
        refresh_interval_seconds=settings.mcp_catalog_refresh_interval_seconds,
    )
    console_event_hub.add_listener(catalogs.connection_changed)
    tool_calls = tool_call_service.ToolCallApplicationService(
        settings=settings,
        repository=tool_call_ledger,
        invalidation_publisher=console_event_hub,
        executor=dispatcher,
        oauth_store=mcp_operator_oauth_store,
        in_process_servers=in_process_servers,
        gmail_client_provider=gmail_client_provider,
        provider_store=provider_connection_store,
        authentik_token_store=authentik_operator_token_store,
        approval_notifier=approval_notifier,
        kubernetes_authorization=kubernetes_authorization,
        github_repository_visibility=github_repository_visibility,
    )

    # The console's own Agent-and-Operator MCP server, mounted at /mcp — its reason to run.
    # Its tools re-expose the connected servers through the same application service. Always built;
    # `build_auth` fails loud if nothing can authenticate to it (no static agent, no OAuth).
    console_mcp_context = mcp_server.ConsoleMcpContext(
        settings=settings,
        tool_calls=tool_calls,
        oauth_store=mcp_operator_oauth_store,
        provider_store=provider_connection_store,
        dispatcher=dispatcher,
        catalogs=catalogs,
        node_daemons=hostexecd_service,
    )

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
        static_definitions = (
            static_agent_definitions
            if static_agent_definitions is not None
            else await _resolve_static_agent_definitions()
        )
        await agent_authority.reconcile_static_agents(static_definitions)
        await mcp_operator_oauth_store.forget_unconfigured_servers(console_config.mcp.servers)
        if session_service is not None:
            await session_service.reconcile_terminal_claims()
        matrix_running = matrix_sync_service.run() if matrix_sync_service is not None else contextlib.nullcontext()
        # Conversation demand owns session creation and replacement. It is a sibling of every
        # channel and of sandbox allocation, so browser-only and unattached conversations receive
        # the same maintenance as Matrix-bound ones.
        supervising = runtime_supervisor.run() if runtime_supervisor is not None else contextlib.nullcontext()
        # Prompt demand is channel-neutral and durable. Start its elected reconciler only after
        # the notification listener is live; the first sweep is also the restart backstop.
        allocating = sandbox_allocator.run() if sandbox_allocator is not None else contextlib.nullcontext()
        async with agent_authority.expiry_maintenance(), oauth_maintenance.run(), catalogs.run(), matrix_running:
            await console_event_hub.start()
            await session_wakes.start()
            await conversation_wakes.start()
            try:
                # Pre-warm the OIDCProxy client-state store so the first OAuth request isn't slowed by a
                # cold connect (see mcp_infra/oauth_facade/server.py). The OAuth variant always carries
                # a concrete shared store; the static-only variant has no OAuth subsystem to initialize.
                if isinstance(mcp_auth, mcp_agent_auth.OAuthMcpAuth):
                    await mcp_auth.storage.setup()
                async with conversation_live_updates.run(), supervising, allocating, mcp_asgi.lifespan(app):
                    yield
            finally:
                # Cancel in-flight approved-call executions (each marks its row cancelled) before the
                # event hub they publish through is torn down.
                await tool_calls.aclose()
                if kubernetes_authorization is not None:
                    await kubernetes_authorization.aclose()
                if sandbox_server is not None:
                    await sandbox_server.client.aclose()
                await github_repository_visibility.aclose()
                if session_service is not None:
                    await session_service.aclose()
                await session_wakes.aclose()
                await conversation_wakes.aclose()
                await console_event_hub.aclose()
                await approval_notifier.aclose()

    # OAuth protected-resource and authorization-server discovery are origin-level RFC routes even
    # though the operational MCP/OAuth handlers remain isolated under /mcp. FastMCP cannot infer an
    # outer ASGI mount, so explicitly expose only its well-known routes here; the static-bearer-only
    # provider returns no routes.
    app = FastAPI(title="Haku console", lifespan=_lifespan)
    app.router.routes.extend(mcp_auth.provider.get_well_known_routes(mcp_path=MCP_PATH))
    # The capability router reads settings off app.state (see haku.console.capabilities).
    app.state.settings = settings
    # Expose the shared database resources to internal dependencies and diagnostics; every store
    # above uses these same objects rather than creating a second pool.
    app.state.db_engine = db_engine
    app.state.db_sessions = db_sessions
    # The operator-login callback persists the operator's Authentik token only when hostexec is
    # configured (offline_access is requested for the same reason). Read at request time from here.
    app.state.hostexec_enabled = hostexec_config is not None
    app.state.agent_enrollment_service = agent_authority
    app.state.operator_identity_store = operator_identity_store
    app.state.operator_login_flows = operator_login_flows
    app.state.tool_call_service = tool_calls
    app.state.mcp_operator_oauth_store = mcp_operator_oauth_store
    app.state.provider_connection_store = provider_connection_store
    app.state.oauth_connection_result_store = oauth_connection_result_store
    app.state.authentik_operator_token_store = authentik_operator_token_store
    app.state.console_event_hub = console_event_hub
    app.state.session_store = session_store
    app.state.session_wakes = session_wakes
    app.state.conversation_follow = follow
    app.state.session_service = session_service
    app.state.in_process_servers = in_process_servers
    app.state.mcp_dispatcher = dispatcher
    app.state.mcp_catalogs = catalogs
    app.state.hostexecd_service = hostexecd_service
    app.state.push_subscription_store = push_subscription_store
    app.state.push_identity = push_identity
    app.state.kubernetes_authorization = kubernetes_authorization
    app.state.kubernetes_grants = kubernetes_grants
    app.state.http_grants = http_grants
    app.state.http_decide = http_decide

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

    # Prometheus scrape target. Deliberately absent from default.conf.template's proxied
    # `location`s: nginx serves the public origin, so an unproxied /metrics stays reachable only
    # on the pod's own API port, which is what the ServiceMonitor's `metrics` port targets.
    # A route, not app.mount(): a Mount only matches paths *under* its prefix, so bare /metrics
    # would fall through to the SPA catch-all and return the app shell.
    @app.get("/metrics")
    async def metrics() -> Response:
        # Re-sampled per scrape rather than pushed on failure: correct after a restart, and it keeps
        # reporting "still broken" without waiting for the next retry to fail again.
        await connection_metrics.refresh_connection_metrics(db_sessions)
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # The browser API is operator-only. Agents use /mcp; static bearer support there does not grant
    # access to any /api/* route. The same endpoint separately recognizes the Operator session.
    operator_only = [Depends(operator_auth.require_operator), Depends(operator_auth.require_operator_mutation_origin)]
    app.include_router(capabilities.router, dependencies=operator_only)
    app.include_router(session_runtime.router, dependencies=operator_only)
    app.include_router(conversation_follow.router, dependencies=operator_only)
    app.include_router(console_events.router, dependencies=operator_only)
    app.include_router(mcp_approval.router, dependencies=operator_only)
    app.include_router(kubernetes_grant_routes.router, dependencies=operator_only)
    app.include_router(http_grant_routes.router, dependencies=operator_only)
    app.include_router(mcp_operator_oauth.router, dependencies=operator_only)
    app.include_router(provider_connection.router, dependencies=operator_only)
    app.include_router(connection_result.router, dependencies=operator_only)
    app.include_router(enrollment_routes.operator_router, dependencies=operator_only)
    app.include_router(service.operator_router, dependencies=operator_only)
    app.include_router(push_routes.router, dependencies=operator_only)
    # Machine endpoints use their own per-daemon bearer and deliberately do not accept an Operator
    # browser session.
    app.include_router(service.machine_router)
    app.include_router(enrollment_routes.entry_router)
    app.include_router(session_runtime.internal_router)
    # Machine-to-machine, bearer-forwarding contract for the separate Kubernetes proxy. The
    # endpoint remains fail-closed unless standing SAR policy is configured.
    app.include_router(kube_proxy_authorization.router)
    # The colocated egress proxy's decision endpoint is deliberately NOT on this network app.
    # It is the oracle that turns placeholders into real credentials, so it must never be routable
    # from a sandbox workload — and every sandbox can reach this app through the haku-console
    # Service (the force-proxy CCNP admits `toEntities: cluster`). `main()` serves it instead on a
    # loopback-only listener (`build_internal_decide_app`) that no Service exposes, so sandbox
    # unreachability is structural, not a NetworkPolicy (#4670 § Topology, acceptance criterion 14).

    @app.get("/api/deployment", dependencies=operator_only)
    async def deployment() -> DeploymentInfo:
        # The static shell is an independent Deployment. Its Flux-selected tag is
        # read from a projected ConfigMap on every request so a frontend-only roll
        # does not need to restart this API pod merely to update Settings metadata.
        return build_deployment_info(static_image_tag_file=settings.static_image_tag_file)

    @app.get("/api/config", dependencies=operator_only)
    async def config() -> ConfigResponse:
        """Static config for the SPA, including deploy-authorized Web chat launch pairs."""
        launch = settings.launch_routine
        default_agent_id = console_config.default_chat_agent_id
        launch_options = [
            ChatLaunchOption(
                agent_id=identity.agent_id,
                agent_display_name=static_by_id[identity.agent_id].display_name,
                runtime=identity.runtime_kind,
                runtime_display_name=runtime_registry[identity.runtime_kind].display_name,
                is_default=identity.agent_id == default_agent_id and identity.runtime_kind is default_runtime_kind,
            )
            for identity in runtime_registry.configured_identities
            if identity.agent_id in launchable_agent_ids
            and identity.runtime_kind in profile_runtime_kinds[static_by_id[identity.agent_id].access_profile_id]
        ]
        launch_options.sort(key=lambda option: (not option.is_default, option.agent_display_name, option.runtime.value))
        return ConfigResponse(
            launch_routine_url=launch.page_url if launch else None,
            haku_ui_url=settings.haku_ui_url,
            chat_launch_options=launch_options,
        )

    # Operator browser auth is mandatory. SessionMiddleware establishes request.session, which the
    # router guards read; https_only follows the canonical public origin.
    app.state.operator_oauth = operator_auth.build_oauth(
        settings.operator_oidc, login_flows=operator_login_flows, offline_access=hostexec_config is not None
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


# The colocated egress proxy reaches the decision oracle here (#4942). Loopback and a fixed port,
# matching the sidecar's HAKU_EGRESS_DECIDE_URL: no Service targets it, so it is unreachable from
# any other pod — the structural half of acceptance criterion 14 (the proxy-identity bearer is the
# other). Keep both in step with the console deployment's proxy sidecar env.
INTERNAL_DECIDE_HOST = "127.0.0.1"
INTERNAL_DECIDE_PORT = 8079


class _SecondaryServer(uvicorn.Server):
    """A uvicorn server sharing the process with the network server, which owns the signals.

    The network server installs the SIGTERM/SIGINT handlers that reach the lifespan shutdown; a
    second installer would overwrite them in the loop's signal registry, so this one installs none
    and is asked to exit once the network server has.
    """

    def install_signal_handlers(self) -> None:
        return None


def build_internal_decide_app(http_decide: HttpDecideService) -> FastAPI:
    """Loopback-only ASGI app carrying just the egress decision endpoint (#4942).

    Colocation binds the oracle here rather than on the network app ``create_app`` builds: the
    colocated proxy sidecar reaches it over the shared pod loopback, while a sandbox — which can
    reach Console only through its Service — has no route to this listener at all (#4670 §
    Topology). The proxy-identity bearer still authenticates every call; the localhost bind is
    defense in depth, not a replacement for authentication.
    """
    internal = FastAPI(title="Haku console egress oracle")
    internal.state.http_decide = http_decide
    internal.include_router(http_decide_routes.router)
    return internal


async def _serve(app: FastAPI) -> None:
    """Serve the network app, plus the loopback decision oracle when ``egress_decide`` is wired.

    Both run in this one process and event loop so they share the single ``HttpDecideService`` and
    its Postgres-backed grant lookups. The network server owns the process signal handlers and the
    graceful-shutdown bound; the oracle installs none of its own and is asked to exit once the
    network server has.
    """
    # host/port are fixed, not env-driven: under the HAKU_CONSOLE_ prefix a `port`
    # setting would read the kubelet's HAKU_CONSOLE_PORT service-link var (a URL),
    # not an int. The cluster-wide Kyverno default keeps that legacy variable out.
    #
    # `timeout_graceful_shutdown` is load-bearing, not tuning. A Claude runner websocket stays
    # open for the life of a chat session, so with the default (None) uvicorn waits *forever* on
    # SIGTERM for it to drain, never cancels the handler, never runs the lifespan shutdown, and is
    # SIGKILLed at the pod's grace deadline — running no finalizer, so the session's lease is never
    # handed back and the sweep fails it.
    # Bounding the wait makes uvicorn cancel the handlers and reach the lifespan, where the chat
    # service hands its leases back. Keep it below the deployment's terminationGracePeriodSeconds.
    network = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info", timeout_graceful_shutdown=10)
    )
    http_decide = app.state.http_decide
    if http_decide is None:
        await network.serve()
        return
    oracle = _SecondaryServer(
        uvicorn.Config(
            build_internal_decide_app(http_decide),
            host=INTERNAL_DECIDE_HOST,
            port=INTERNAL_DECIDE_PORT,
            log_level="warning",
            timeout_graceful_shutdown=10,
        )
    )
    oracle_task = asyncio.create_task(oracle.serve())
    try:
        await network.serve()
    finally:
        oracle.should_exit = True
        await oracle_task


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = Settings()
    loaded_static_agents = load_static_agents(settings)
    # DDL belongs to the image-coupled release Job. Before binding a port, prove this image can
    # read the already-migrated schema so an incompatible rollout never becomes Ready.
    verify_schema(settings.database_url.get_secret_value())
    app = create_app(settings, loaded_static_agents=loaded_static_agents)
    asyncio.run(_serve(app))


def run_command(argv: list[str]) -> None:
    """Dispatch the image's two process modes without constructing application settings for migrations."""
    if argv == ["migrate"]:
        migration_main()
        return
    if argv:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} [migrate]")
    main()


if __name__ == "__main__":
    run_command(sys.argv[1:])
