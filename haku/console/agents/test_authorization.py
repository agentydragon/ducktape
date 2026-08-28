"""Real-Postgres tests for the canonical Agent authority application service."""

from __future__ import annotations

import asyncio
import datetime
import re
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from haku.console.agents.authorization import (
    PostgresAgentAuthority,
    StaticAgentDefinition,
    StaticAgentRejectedError,
    fingerprint_static_token,
)
from haku.console.agents.enrollment import (
    AgentAccessProfileManagedByDeploymentError,
    CreateAgentDecision,
    EnrollmentAllowed,
    EnrollmentBrowserBindingError,
    EnrollmentBrowserSession,
    EnrollmentDecisionConflictError,
    ReconnectAgentDecision,
)
from haku.console.agents.launch_authority import StaticAgentAuthorization
from haku.console.agents.models import AgentStatus, CredentialBindingStatus, CredentialKind, EnrollmentPhase
from haku.console.chat_models import RuntimeKind
from haku.console.conftest import console_sessions
from haku.console.database_migrate import apply_migrations
from haku.console.database_schema import (
    Agent,
    AgentNameReservation,
    AuthorizationGrant,
    ClientSoftware,
    Conversation,
    CredentialBinding,
    EnrollmentInteraction,
    Operator,
    StaticCredential,
)
from haku.console.mcp_auth.fastmcp_adapter import (
    AgentGrantAuthorityUnavailableError,
    AuthorizationCorrelation,
    AuthorizationRequest,
    ClientSoftwareSnapshot,
    EnrollmentRejectedError,
    GrantAuthorization,
    GrantRejectedError,
    TokenFamilyEvidence,
)
from haku.console.operator_identity import (
    OperatorIdentityTrust,
    OperatorStatus,
    ResolvedOperatorIdentity,
    VerifiedExternalIdentity,
)
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.session.launch_identity import ChatLaunchAuthorizer, LaunchAgentRejectedError, LaunchIdentity
from haku.console.x.runtime import RuntimeKey
from mcp_infra.authentik_auth.oidc_principal import VerifiedOidcPrincipal
from third_party.containers.rlocations import PGVECTOR_PG18
from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import start_postgres_container


@pytest.fixture(scope="session")
def postgres_container() -> Any:
    """Postgres **with pgvector**: this file migrates a fresh database to head, and the baseline creates
    `vector` columns. The deployed database gets the extension from CNPG's `Database` CR."""
    container = start_postgres_container(PGVECTOR_PG18)
    try:
        yield container
    finally:
        container.stop()


_BROWSER_ISSUER = "https://auth.test/browser/"
_MCP_ISSUER = "https://auth.test/mcp/"
_CLIENT_ID = "claude-test-client"
_REDIRECT_URI = "https://claude.test/oauth/callback"


@contextmanager
def _orm_session(database_url: str) -> Iterator[Session]:
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@dataclass(frozen=True)
class PersistedGrantState:
    interaction: EnrollmentInteraction
    binding: CredentialBinding
    agent: Agent


def _grant_state(session: Session, grant_id: UUID) -> PersistedGrantState:
    grant = session.get(AuthorizationGrant, grant_id)
    assert grant is not None
    interaction = session.get(EnrollmentInteraction, grant.enrollment_interaction_id)
    binding = session.get(CredentialBinding, grant.binding_id)
    assert interaction is not None
    assert binding is not None
    agent = session.get(Agent, binding.agent_id)
    assert agent is not None
    return PersistedGrantState(interaction=interaction, binding=binding, agent=agent)


@dataclass(frozen=True)
class PersistedInteractionState:
    phase: EnrollmentPhase
    browser_binding_digest: bytes | None
    pending_name_count: int


def _interaction_state(session: Session, interaction_id: UUID) -> PersistedInteractionState:
    interaction = session.get(EnrollmentInteraction, interaction_id)
    assert interaction is not None
    pending_name_count = session.execute(
        select(func.count())
        .select_from(AgentNameReservation)
        .where(AgentNameReservation.pending_interaction_id == interaction_id)
    ).scalar_one()
    return PersistedInteractionState(
        phase=interaction.phase,
        browser_binding_digest=interaction.browser_binding_digest,
        pending_name_count=pending_name_count,
    )


@dataclass
class MutableClock:
    now: datetime.datetime

    def __call__(self) -> datetime.datetime:
        return self.now

    def advance(self, delta: datetime.timedelta) -> None:
        self.now += delta


@dataclass
class SecretSequence:
    sequence: int = 0

    def __call__(self) -> str:
        self.sequence += 1
        return f"browser-secret-{self.sequence}"


class DisableAfterPrincipalResolutionIdentityStore(PostgresOperatorIdentityStore):
    async def resolve_verified_identity(self, identity: VerifiedExternalIdentity) -> ResolvedOperatorIdentity:
        resolved = await super().resolve_verified_identity(identity)
        async with self._session_factory.begin() as session:
            operator = await session.get(Operator, resolved.operator_id)
            assert operator is not None
            operator.status = OperatorStatus.DISABLED
            operator.updated_at = datetime.datetime.now(datetime.UTC)
        return resolved


@dataclass(frozen=True)
class Harness:
    authority: PostgresAgentAuthority
    sessions: async_sessionmaker[AsyncSession]
    identities: PostgresOperatorIdentityStore
    browser: EnrollmentBrowserSession
    principal: VerifiedOidcPrincipal
    clock: MutableClock


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: Any) -> str:
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    return f"postgresql+psycopg://postgres:postgres@{host}:{port}/postgres"


@pytest.fixture
def db_url(postgres_admin_url: str, request: pytest.FixtureRequest) -> Any:
    suffix = uuid4().hex[:8]
    base = re.sub(r"[^a-z0-9_]", "_", request.node.name.lower())[:35].rstrip("_")
    db_name = f"{base or 'agent_authority'}_{suffix}"
    db_url = create_database_sync(postgres_admin_url, db_name, extensions=("vector",))
    apply_migrations(db_url)

    yield db_url

    force_drop_database_sync(postgres_admin_url, db_name)


HarnessFactory = Callable[..., Awaitable[Harness]]


@pytest.fixture
def harness_factory(db_url: str) -> HarnessFactory:
    async def build(
        *, subject: str = "operator-user", access_profiles: tuple[str, ...] = ("no_auto_approval",)
    ) -> Harness:
        sessions = console_sessions(db_url)
        identities = PostgresOperatorIdentityStore(
            sessions,
            OperatorIdentityTrust(
                trust_domain="auth.test/authentik-user-id/v1", trusted_issuers=frozenset({_BROWSER_ISSUER, _MCP_ISSUER})
            ),
        )
        browser_identity = await identities.resolve_verified_identity(
            VerifiedExternalIdentity(issuer=_BROWSER_ISSUER, subject=subject)
        )
        # Correlation reservations are deliberately evaluated against PostgreSQL's
        # clock, so ground the mutable test clock in that same time domain.
        with _orm_session(db_url) as session:
            database_now = session.scalar(select(func.clock_timestamp()))
        assert isinstance(database_now, datetime.datetime)
        clock = MutableClock(database_now)
        authority = PostgresAgentAuthority(
            sessions,
            public_base_url="https://haku.test",
            operator_identity_store=identities,
            clock=clock,
            browser_secret_factory=SecretSequence(),
            access_profiles=access_profiles,
            default_access_profile_id=access_profiles[0],
        )
        return Harness(
            authority=authority,
            sessions=sessions,
            identities=identities,
            browser=EnrollmentBrowserSession(
                operator_id=browser_identity.operator_id,
                identity_id=browser_identity.identity_id,
                browser_session_id="browser-session",
                display_name="Test Operator",
            ),
            principal=VerifiedOidcPrincipal(issuer=_MCP_ISSUER, subject=subject),
            clock=clock,
        )

    return build


@pytest.fixture
async def harness(harness_factory: HarnessFactory) -> Harness:
    return await harness_factory()


def _request(label: str) -> AuthorizationRequest:
    return AuthorizationRequest(
        correlation=AuthorizationCorrelation(
            client_id=_CLIENT_ID, redirect_uri=_REDIRECT_URI, code_challenge=f"challenge-{label}-{uuid4()}"
        ),
        client=ClientSoftwareSnapshot(client_id=_CLIENT_ID, display_name="Claude Desktop"),
        requested_scopes=frozenset({"tools:call", "tools:list"}),
    )


def _interaction_from_url(url: str) -> tuple[UUID, str]:
    parsed = urlsplit(url)
    return UUID(parsed.path.rsplit("/", 1)[1]), parse_qs(parsed.query)["browser_nonce"][0]


async def _open(harness: Harness, request: AuthorizationRequest) -> tuple[UUID, str]:
    url = await harness.authority.reserve_authorization(
        request=request, upstream_authorization_url=f"https://auth.test/authorize/{uuid4()}"
    )
    interaction_id, nonce = _interaction_from_url(url)
    page = await harness.authority.open_interaction(
        interaction_id=interaction_id, browser_nonce=nonce, interaction_cookie=None, browser=harness.browser
    )
    return interaction_id, page.form_token


async def _allow_create(harness: Harness, *, label: str, display_name: str) -> tuple[AuthorizationRequest, UUID]:
    request = _request(label)
    interaction_id, form_token = await _open(harness, request)
    result = await harness.authority.decide(
        interaction_id=interaction_id,
        browser=harness.browser,
        interaction_cookie=form_token,
        decision=CreateAgentDecision(
            form_token=form_token,
            display_name=display_name,
            access_profile_id=harness.authority.available_access_profiles()[0],
        ),
    )
    assert isinstance(result, EnrollmentAllowed)
    return request, interaction_id


async def _create_grant(
    harness: Harness, *, label: str, display_name: str, activate: bool = False
) -> GrantAuthorization:
    request, _interaction_id = await _allow_create(harness, label=label, display_name=display_name)
    grant = await harness.authority.begin_exchange(
        correlation=request.correlation,
        client=request.client,
        principal=harness.principal,
        granted_scopes=frozenset({"tools:call"}),
    )
    await harness.authority.record_token_family(
        grant_id=grant.grant_id,
        evidence=TokenFamilyEvidence(access_jti=f"access-{label}", refresh_jti=f"refresh-{label}"),
    )
    if activate:
        await harness.authority.activate_for_tool_call(
            grant_id=grant.grant_id, client_id=_CLIENT_ID, token_scopes=frozenset({"tools:call"})
        )
    return grant


async def test_list_agents_returns_owned_active_and_disabled_agents_with_latest_binding(
    db_url: str, harness: Harness, harness_factory: HarnessFactory
) -> None:
    assert await harness.authority.list_agents(operator_id=harness.browser.operator_id) == ()

    grant = await _create_grant(harness, label="listed", display_name="Listed Agent", activate=True)
    [active] = await harness.authority.list_agents(operator_id=harness.browser.operator_id)
    assert active.agent_id == grant.actor.agent_id
    assert active.display_name == "Listed Agent"
    assert active.status is AgentStatus.ACTIVE
    assert active.credential_kind is CredentialKind.OAUTH
    assert active.credential_status is CredentialBindingStatus.ACTIVE
    assert active.last_seen_at == harness.clock.now

    await harness.authority.revoke_grant(grant_id=grant.grant_id)
    [disabled] = await harness.authority.list_agents(operator_id=harness.browser.operator_id)
    assert disabled.agent_id == active.agent_id
    assert disabled.status is AgentStatus.DISABLED
    assert disabled.credential_status is CredentialBindingStatus.REVOKED

    other = await harness_factory(subject="other-operator")
    assert await other.authority.list_agents(operator_id=other.browser.operator_id) == ()


async def test_enrollment_and_settings_persist_the_selected_access_profile_id(harness_factory: HarnessFactory) -> None:
    harness = await harness_factory(access_profiles=("no_auto_approval", "haku_v1"))
    request = _request("policy-selection")
    interaction_id, form_token = await _open(harness, request)
    await harness.authority.decide(
        interaction_id=interaction_id,
        browser=harness.browser,
        interaction_cookie=form_token,
        decision=CreateAgentDecision(form_token=form_token, display_name="Policy Agent", access_profile_id="haku_v1"),
    )
    grant = await harness.authority.begin_exchange(
        correlation=request.correlation,
        client=request.client,
        principal=harness.principal,
        granted_scopes=frozenset({"tools:call"}),
    )
    assert grant.actor.access_profile_id == "haku_v1"
    await harness.authority.record_token_family(
        grant_id=grant.grant_id, evidence=TokenFamilyEvidence(access_jti="policy-access", refresh_jti="policy-refresh")
    )
    activated = await harness.authority.activate_for_tool_call(
        grant_id=grant.grant_id, client_id=_CLIENT_ID, token_scopes=frozenset({"tools:call"})
    )
    assert activated.actor.access_profile_id == "haku_v1"

    updated = await harness.authority.set_access_profile(
        operator_id=harness.browser.operator_id, agent_id=grant.actor.agent_id, access_profile_id="no_auto_approval"
    )
    assert updated.access_profile_id == "no_auto_approval"
    resolved = await harness.authority.activate_for_tool_call(
        grant_id=grant.grant_id, client_id=_CLIENT_ID, token_scopes=frozenset({"tools:call"})
    )
    assert resolved.actor.access_profile_id == "no_auto_approval"


async def test_reservation_accumulates_exact_fastmcp_validated_redirects(db_url: str, harness: Harness) -> None:
    first = _request("first-redirect")
    second_redirect = "https://claude.test/oauth/alternate-callback"
    second = AuthorizationRequest(
        correlation=AuthorizationCorrelation(
            client_id=_CLIENT_ID, redirect_uri=second_redirect, code_challenge=f"challenge-second-{uuid4()}"
        ),
        client=first.client,
        requested_scopes=first.requested_scopes,
    )

    await harness.authority.reserve_authorization(
        request=first, upstream_authorization_url="https://auth.test/authorize/first"
    )
    await harness.authority.reserve_authorization(
        request=second, upstream_authorization_url="https://auth.test/authorize/second"
    )

    with _orm_session(db_url) as session:
        client = session.scalar(select(ClientSoftware).where(ClientSoftware.oauth_client_id == _CLIENT_ID))
        assert client is not None
        assert client.validated_redirect_uris == [_REDIRECT_URI, second_redirect]


async def test_create_decision_is_idempotent_and_grant_activates_then_revokes(db_url: str, harness: Harness) -> None:
    request = _request("create")
    interaction_id, form_token = await _open(harness, request)
    decision = CreateAgentDecision(
        form_token=form_token, display_name="Kitchen Claude", access_profile_id="no_auto_approval"
    )
    first = await harness.authority.decide(
        interaction_id=interaction_id, browser=harness.browser, interaction_cookie=form_token, decision=decision
    )
    second = await harness.authority.decide(
        interaction_id=interaction_id, browser=harness.browser, interaction_cookie=form_token, decision=decision
    )
    assert first == second
    with pytest.raises(EnrollmentBrowserBindingError):
        await harness.authority.decide(
            interaction_id=interaction_id,
            browser=harness.browser,
            interaction_cookie=form_token,
            decision=CreateAgentDecision(
                form_token="different-form-token",
                display_name=decision.display_name,
                access_profile_id=decision.access_profile_id,
            ),
        )
    with pytest.raises(EnrollmentDecisionConflictError):
        await harness.authority.decide(
            interaction_id=interaction_id,
            browser=harness.browser,
            interaction_cookie=form_token,
            decision=CreateAgentDecision(
                form_token=form_token, display_name="Different Agent", access_profile_id=decision.access_profile_id
            ),
        )

    grant = await harness.authority.begin_exchange(
        correlation=request.correlation,
        client=request.client,
        principal=harness.principal,
        granted_scopes=frozenset({"tools:call"}),
    )
    await harness.authority.record_token_family(
        grant_id=grant.grant_id, evidence=TokenFamilyEvidence(access_jti="access-create", refresh_jti="refresh-create")
    )
    assert (
        await harness.authority.resolve_grant(
            grant_id=grant.grant_id, client_id=_CLIENT_ID, token_scopes=frozenset({"tools:call"})
        )
    ).actor.binding_id == grant.actor.binding_id
    activated = await harness.authority.activate_for_tool_call(
        grant_id=grant.grant_id, client_id=_CLIENT_ID, token_scopes=frozenset({"tools:call"})
    )
    assert activated.actor == grant.actor
    await harness.authority.revoke_grant(grant_id=grant.grant_id)
    with pytest.raises(GrantRejectedError):
        await harness.authority.resolve_grant(
            grant_id=grant.grant_id, client_id=_CLIENT_ID, token_scopes=frozenset({"tools:call"})
        )

    with _orm_session(db_url) as session:
        state = _grant_state(session, grant.grant_id)
        decision_digest = state.interaction.decision_digest
        assert state.interaction.browser_nonce_digest is None
        assert state.interaction.browser_binding_digest is None
        assert state.interaction.phase is EnrollmentPhase.COMPLETED
        assert state.binding.status is CredentialBindingStatus.REVOKED
        assert state.agent.status is AgentStatus.DISABLED

    with _orm_session(db_url) as session:
        disabled_agent = session.get(Agent, activated.actor.agent_id)
        assert disabled_agent is not None
        disabled_agent.status = AgentStatus.ACTIVE
        with pytest.raises(IntegrityError):
            session.commit()

    assert decision_digest not in {form_token.encode(), b"browser-secret-1", b"browser-secret-2"}


async def test_activation_timeout_abandons_new_agent_but_only_expires_reconnect(db_url: str, harness: Harness) -> None:
    initial = await _create_grant(harness, label="initial-timeout", display_name="Initial Timeout")
    harness.clock.advance(datetime.timedelta(minutes=16))
    with pytest.raises(GrantRejectedError):
        await harness.authority.resolve_grant(
            grant_id=initial.grant_id, client_id=_CLIENT_ID, token_scopes=frozenset({"tools:call"})
        )

    active = await _create_grant(harness, label="active", display_name="Reconnectable", activate=True)
    request = _request("reconnect-timeout")
    interaction_id, form_token = await _open(harness, request)
    reconnect_decision = ReconnectAgentDecision(
        form_token=form_token, agent_id=active.actor.agent_id, access_profile_id="no_auto_approval"
    )
    first = await harness.authority.decide(
        interaction_id=interaction_id,
        browser=harness.browser,
        interaction_cookie=form_token,
        decision=reconnect_decision,
    )
    assert first == await harness.authority.decide(
        interaction_id=interaction_id,
        browser=harness.browser,
        interaction_cookie=form_token,
        decision=reconnect_decision,
    )
    replacement = await harness.authority.begin_exchange(
        correlation=request.correlation,
        client=request.client,
        principal=harness.principal,
        granted_scopes=frozenset({"tools:call"}),
    )
    await harness.authority.record_token_family(
        grant_id=replacement.grant_id, evidence=TokenFamilyEvidence(access_jti="replacement-access", refresh_jti=None)
    )
    harness.clock.advance(datetime.timedelta(minutes=16))
    with pytest.raises(GrantRejectedError):
        await harness.authority.activate_for_tool_call(
            grant_id=replacement.grant_id, client_id=_CLIENT_ID, token_scopes=frozenset({"tools:call"})
        )

    with _orm_session(db_url) as session:
        initial_state = _grant_state(session, initial.grant_id)
        active_state = _grant_state(session, active.grant_id)
        replacement_state = _grant_state(session, replacement.grant_id)
        assert initial_state.binding.status is CredentialBindingStatus.EXPIRED
        assert initial_state.agent.status is AgentStatus.ABANDONED
        assert active_state.binding.status is CredentialBindingStatus.ACTIVE
        assert replacement_state.binding.status is CredentialBindingStatus.EXPIRED
        assert active_state.agent.status is AgentStatus.ACTIVE


async def test_reconnect_activation_revokes_predecessor_before_activating_successor(
    db_url: str, harness: Harness
) -> None:
    active = await _create_grant(harness, label="active-reconnect", display_name="Reconnectable", activate=True)
    request = _request("successful-reconnect")
    interaction_id, form_token = await _open(harness, request)
    await harness.authority.decide(
        interaction_id=interaction_id,
        browser=harness.browser,
        interaction_cookie=form_token,
        decision=ReconnectAgentDecision(
            form_token=form_token, agent_id=active.actor.agent_id, access_profile_id="no_auto_approval"
        ),
    )
    replacement = await harness.authority.begin_exchange(
        correlation=request.correlation,
        client=request.client,
        principal=harness.principal,
        granted_scopes=frozenset({"tools:call"}),
    )
    await harness.authority.record_token_family(
        grant_id=replacement.grant_id,
        evidence=TokenFamilyEvidence(access_jti="replacement-access", refresh_jti="replacement-refresh"),
    )

    activated = await harness.authority.activate_for_tool_call(
        grant_id=replacement.grant_id, client_id=_CLIENT_ID, token_scopes=frozenset({"tools:call"})
    )

    assert activated.actor == replacement.actor
    with _orm_session(db_url) as session:
        active_state = _grant_state(session, active.grant_id)
        replacement_state = _grant_state(session, replacement.grant_id)
        assert active_state.binding.status is CredentialBindingStatus.REVOKED
        assert active_state.binding.end_reason == "superseded"
        assert replacement_state.binding.status is CredentialBindingStatus.ACTIVE
        assert replacement_state.binding.supersedes_binding_id == active.actor.binding_id
        assert replacement_state.agent.status is AgentStatus.ACTIVE


async def test_expiry_maintenance_sweeps_allowed_response_loss_and_skips_locked_rows(
    db_url: str, harness: Harness
) -> None:
    _locked_request, locked_interaction_id = await _allow_create(
        harness, label="locked-allowed", display_name="Locked Allowed"
    )
    harness.clock.advance(datetime.timedelta(seconds=1))
    _unlocked_request, unlocked_interaction_id = await _allow_create(
        harness, label="unlocked-allowed", display_name="Unlocked Allowed"
    )
    harness.clock.advance(datetime.timedelta(minutes=11))

    engine = create_engine(db_url)
    owner = Session(engine)
    transaction = owner.begin()
    try:
        owner.execute(
            select(EnrollmentInteraction)
            .where(EnrollmentInteraction.interaction_id == locked_interaction_id)
            .with_for_update()
        ).scalar_one()
        async with harness.authority.expiry_maintenance(interval=datetime.timedelta(milliseconds=10), batch_size=1):
            locked = _interaction_state(owner, locked_interaction_id)
            unlocked = _interaction_state(owner, unlocked_interaction_id)
            assert locked.phase is EnrollmentPhase.ALLOWED
            assert locked.pending_name_count == 1
            assert unlocked.phase is EnrollmentPhase.EXPIRED
            assert unlocked.browser_binding_digest is None
            assert unlocked.pending_name_count == 0

            transaction.commit()
            for _ in range(200):
                with Session(engine) as observer:
                    state = _interaction_state(observer, locked_interaction_id)
                if state.phase is EnrollmentPhase.EXPIRED:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("periodic expiry maintenance did not catch the previously locked row")
            assert state.browser_binding_digest is None
            assert state.pending_name_count == 0
    finally:
        if transaction.is_active:
            transaction.rollback()
        owner.close()
        engine.dispose()


async def test_expiry_sweep_terminates_exchanging_response_loss(db_url: str, harness: Harness) -> None:
    request, interaction_id = await _allow_create(harness, label="lost-exchange", display_name="Lost Exchange Sweep")
    grant = await harness.authority.begin_exchange(
        correlation=request.correlation,
        client=request.client,
        principal=harness.principal,
        granted_scopes=frozenset({"tools:call"}),
    )
    harness.clock.advance(datetime.timedelta(minutes=11))

    assert await harness.authority.sweep_expired_state() == 1
    assert await harness.authority.sweep_expired_state() == 0

    with _orm_session(db_url) as session:
        state = _grant_state(session, grant.grant_id)
        assert state.interaction.interaction_id == interaction_id
        assert state.interaction.phase is EnrollmentPhase.EXPIRED
        assert state.interaction.browser_binding_digest is None
        assert state.interaction.closure_reason == "expiry_sweep"
        assert state.binding.status is CredentialBindingStatus.FAILED
        assert state.binding.end_reason == "expiry_sweep"
        assert state.agent.status is AgentStatus.ABANDONED


async def test_expiry_sweep_terminates_issued_response_loss(db_url: str, harness: Harness) -> None:
    grant = await _create_grant(harness, label="lost-issued", display_name="Lost Issued Sweep")
    harness.clock.advance(datetime.timedelta(minutes=16))

    assert await harness.authority.sweep_expired_state() == 1
    assert await harness.authority.sweep_expired_state() == 0

    with _orm_session(db_url) as session:
        state = _grant_state(session, grant.grant_id)
        assert state.interaction.phase is EnrollmentPhase.COMPLETED
        assert state.interaction.browser_binding_digest is None
        assert state.binding.status is CredentialBindingStatus.EXPIRED
        assert state.binding.end_reason == "activation_timeout"
        assert state.agent.status is AgentStatus.ABANDONED


async def test_exchange_uses_the_live_correlation_reservation_after_tuple_reuse(db_url: str, harness: Harness) -> None:
    request = _request("correlation-reuse")
    with _orm_session(db_url) as session:
        database_now = session.execute(select(func.clock_timestamp())).scalar_one()
        assert isinstance(database_now, datetime.datetime)
    harness.clock.now = database_now - datetime.timedelta(hours=3)
    old_url = await harness.authority.reserve_authorization(
        request=request, upstream_authorization_url="https://auth.test/authorize/old"
    )
    old_interaction_id, _old_nonce = _interaction_from_url(old_url)
    harness.clock.now = database_now
    new_url = await harness.authority.reserve_authorization(
        request=request, upstream_authorization_url="https://auth.test/authorize/new"
    )
    new_interaction_id, new_nonce = _interaction_from_url(new_url)
    assert new_interaction_id != old_interaction_id
    page = await harness.authority.open_interaction(
        interaction_id=new_interaction_id, browser_nonce=new_nonce, interaction_cookie=None, browser=harness.browser
    )
    await harness.authority.decide(
        interaction_id=new_interaction_id,
        browser=harness.browser,
        interaction_cookie=page.form_token,
        decision=CreateAgentDecision(
            form_token=page.form_token, display_name="Reused Correlation", access_profile_id="no_auto_approval"
        ),
    )
    grant = await harness.authority.begin_exchange(
        correlation=request.correlation,
        client=request.client,
        principal=harness.principal,
        granted_scopes=frozenset({"tools:call"}),
    )
    with _orm_session(db_url) as session:
        persisted_grant = session.get(AuthorizationGrant, grant.grant_id)
        assert persisted_grant is not None
        claimed_interaction_id = persisted_grant.enrollment_interaction_id
    assert claimed_interaction_id == new_interaction_id


async def test_exchange_rejects_principal_from_different_operator_without_mutating_approval(
    db_url: str, harness: Harness
) -> None:
    request, interaction_id = await _allow_create(
        harness, label="wrong-operator", display_name="Wrong Operator Rejected"
    )
    other_subject = "different-operator"
    other_identity = await harness.identities.resolve_verified_identity(
        VerifiedExternalIdentity(issuer=_MCP_ISSUER, subject=other_subject)
    )
    assert other_identity.operator_id != harness.browser.operator_id

    with pytest.raises(EnrollmentRejectedError):
        await harness.authority.begin_exchange(
            correlation=request.correlation,
            client=request.client,
            principal=VerifiedOidcPrincipal(issuer=_MCP_ISSUER, subject=other_subject),
            granted_scopes=frozenset({"tools:call"}),
        )

    with _orm_session(db_url) as session:
        interaction_state = _interaction_state(session, interaction_id)
        grant_count = session.execute(
            select(func.count())
            .select_from(AuthorizationGrant)
            .where(AuthorizationGrant.enrollment_interaction_id == interaction_id)
        ).scalar_one()
        binding_count = session.execute(select(func.count()).select_from(CredentialBinding)).scalar_one()
        agent_count = session.execute(select(func.count()).select_from(Agent)).scalar_one()

    assert interaction_state.phase is EnrollmentPhase.ALLOWED
    assert interaction_state.browser_binding_digest is not None
    assert interaction_state.pending_name_count == 1
    assert grant_count == 0
    assert binding_count == 0
    assert agent_count == 0


async def test_exchange_timeout_and_preissuance_revoke_abandon_new_agents(db_url: str, harness: Harness) -> None:
    request = _request("exchange-expiry")
    interaction_id, form_token = await _open(harness, request)
    await harness.authority.decide(
        interaction_id=interaction_id,
        browser=harness.browser,
        interaction_cookie=form_token,
        decision=CreateAgentDecision(
            form_token=form_token, display_name="Lost Exchange", access_profile_id="no_auto_approval"
        ),
    )
    grant = await harness.authority.begin_exchange(
        correlation=request.correlation,
        client=request.client,
        principal=harness.principal,
        granted_scopes=frozenset({"tools:call"}),
    )
    harness.clock.advance(datetime.timedelta(minutes=11))
    with pytest.raises(EnrollmentRejectedError):
        await harness.authority.record_token_family(
            grant_id=grant.grant_id, evidence=TokenFamilyEvidence(access_jti="too-late", refresh_jti=None)
        )

    request_two = _request("preissuance-revoke")
    interaction_two, token_two = await _open(harness, request_two)
    await harness.authority.decide(
        interaction_id=interaction_two,
        browser=harness.browser,
        interaction_cookie=token_two,
        decision=CreateAgentDecision(
            form_token=token_two, display_name="Revoked Before Issue", access_profile_id="no_auto_approval"
        ),
    )
    grant_two = await harness.authority.begin_exchange(
        correlation=request_two.correlation,
        client=request_two.client,
        principal=harness.principal,
        granted_scopes=frozenset({"tools:call"}),
    )
    await harness.authority.revoke_grant(grant_id=grant_two.grant_id)

    with _orm_session(db_url) as session:
        expired = _grant_state(session, grant.grant_id)
        revoked = _grant_state(session, grant_two.grant_id)
        assert expired.interaction.phase is EnrollmentPhase.EXPIRED
        assert expired.interaction.browser_binding_digest is None
        assert expired.binding.status is CredentialBindingStatus.FAILED
        assert expired.agent.status is AgentStatus.ABANDONED
        assert revoked.interaction.phase is EnrollmentPhase.FAILED
        assert revoked.interaction.browser_binding_digest is None
        assert revoked.binding.status is CredentialBindingStatus.FAILED
        assert revoked.agent.status is AgentStatus.ABANDONED


async def _launch_definition(harness: Harness, *, access_profile_id: str = "no_auto_approval") -> StaticAgentDefinition:
    definition = StaticAgentDefinition(
        agent_id=uuid4(),
        display_name="Launch Test Agent",
        operator_id=harness.browser.operator_id,
        secret_reference="env:LAUNCH_TEST_AGENT_TOKEN",
        token_fingerprint=fingerprint_static_token(f"launch-token-{uuid4()}"),
        access_profile_id=access_profile_id,
    )
    await harness.authority.reconcile_static_agents([definition])
    return definition


def _launch_authorizer(
    harness: Harness,
    agent_id: UUID,
    *,
    launchable: bool = True,
    registered: tuple[RuntimeKind, ...] = (RuntimeKind.CLAUDE_CODE,),
    profile_runtime_kinds: dict[str, tuple[RuntimeKind, ...]] | None = None,
) -> ChatLaunchAuthorizer:
    return ChatLaunchAuthorizer(
        harness.authority,
        launchable_agent_ids={agent_id} if launchable else set(),
        registered_runtime_identities={RuntimeKey(agent_id, kind) for kind in registered},
        profile_runtime_kinds=profile_runtime_kinds
        or {
            "no_auto_approval": (RuntimeKind.CLAUDE_CODE,),
            "chat": (RuntimeKind.CLAUDE_CODE,),
            "review": (RuntimeKind.CLAUDE_CODE,),
            "disallowed": (),
        },
    )


async def _authorize_launch(harness: Harness, authorizer: ChatLaunchAuthorizer, agent_id: UUID) -> LaunchIdentity:
    async with harness.sessions.begin() as db:
        return await authorizer(db, harness.browser.operator_id, agent_id, RuntimeKind.CLAUDE_CODE)


async def test_chat_launch_authorizer_derives_current_profile_for_new_launch(
    db_url: str, harness_factory: HarnessFactory
) -> None:
    harness = await harness_factory(access_profiles=("chat", "review"))
    definition = await _launch_definition(harness, access_profile_id="chat")
    authorizer = _launch_authorizer(harness, definition.agent_id)

    first = await _authorize_launch(harness, authorizer, definition.agent_id)
    assert first.access_profile_id == "chat"

    with _orm_session(db_url) as session:
        agent = session.get(Agent, definition.agent_id)
        assert agent is not None
        agent.access_profile_id = "review"
        session.commit()

    second = await _authorize_launch(harness, authorizer, definition.agent_id)
    assert first.access_profile_id == "chat"
    assert second.agent_id == definition.agent_id
    assert second.access_profile_id == "review"
    assert second.runtime_kind is RuntimeKind.CLAUDE_CODE


async def test_launch_authorization_requires_caller_owned_transaction(harness: Harness) -> None:
    definition = await _launch_definition(harness)
    async with harness.sessions() as db:
        with pytest.raises(RuntimeError, match="active caller transaction"):
            await harness.authority.launch_authorization(
                db, operator_id=harness.browser.operator_id, agent_id=definition.agent_id
            )


async def test_launch_authorization_commits_against_a_concurrent_operator_referencing_write(harness: Harness) -> None:
    """``launch_authorization``'s guard locks coexist with a concurrent write referencing the
    operator.

    Regression for the production deadlock (haku-console-db, SQLSTATE 40P01). Two ordinary
    same-operator transactions used to lock ``operators`` and ``agents`` in opposite orders:
    ``launch_authorization`` took ``operators`` then ``agents``, both FOR UPDATE, while a concurrent
    path held the ``agents`` row (activation, ``record_seen``, revocation) and then INSERTed a row
    referencing the operator — a session or conversation — whose foreign-key check takes an implicit
    FOR KEY SHARE on ``operators``. FOR KEY SHARE conflicts with FOR UPDATE, so the two formed a
    cycle and Postgres killed one. The guard locks are now FOR NO KEY UPDATE, which still serializes
    against a disable/rotation (a non-key UPDATE) but not against foreign-key checks.

    Deterministic on both sides of the fix: the writer takes ``agents`` first, then waits until the
    launcher — the real ``launch_authorization`` — holds its ``operators`` guard lock and is blocked
    on ``agents``, and only then performs the operator-referencing INSERT. Under FOR UPDATE guards
    that interleaving is a guaranteed lock cycle the detector breaks within ``deadlock_timeout``;
    under FOR NO KEY UPDATE guards both transactions must commit.
    """
    definition = await _launch_definition(harness)
    operator_id = harness.browser.operator_id
    agent_id = definition.agent_id
    agents_locked = asyncio.Event()

    async def launcher_blocked_on_a_lock() -> bool:
        # The launcher is this test database's only possible lock waiter: the writer holds its locks
        # without waiting, and this poll's own connection only reads a system view.
        async with harness.sessions() as db:
            return bool(
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity"
                        " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            )

    async def operator_referencing_writer() -> None:
        # Holds ``agents`` first, as activation/record_seen/revocation do, then INSERTs a row whose
        # ``operator_id`` foreign key takes FOR KEY SHARE on ``operators``.
        async with harness.sessions.begin() as db:
            await db.execute(select(Agent).where(Agent.agent_id == agent_id).with_for_update())
            agents_locked.set()
            for _ in range(500):
                if await launcher_blocked_on_a_lock():
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("the launcher never blocked on the agents lock")
            db.add(
                Conversation(
                    conversation_id=uuid4(),
                    operator_id=operator_id,
                    runtime_kind=RuntimeKind.CLAUDE_CODE,
                    created_at=harness.clock.now,
                )
            )

    async def launcher() -> StaticAgentAuthorization:
        await agents_locked.wait()
        async with harness.sessions.begin() as db:
            return await harness.authority.launch_authorization(db, operator_id=operator_id, agent_id=agent_id)

    _, authorization = await asyncio.gather(operator_referencing_writer(), launcher())
    assert authorization.agent_id == agent_id
    assert authorization.operator_id == operator_id


async def test_chat_launch_authorizer_fails_closed_for_missing_agent(harness: Harness) -> None:
    missing_agent_id = uuid4()
    authorizer = _launch_authorizer(harness, missing_agent_id)
    with pytest.raises(LaunchAgentRejectedError):
        await _authorize_launch(harness, authorizer, missing_agent_id)


async def test_chat_launch_authorizer_fails_closed_for_inactive_operator(db_url: str, harness: Harness) -> None:
    definition = await _launch_definition(harness)
    with _orm_session(db_url) as session:
        operator = session.get(Operator, harness.browser.operator_id)
        assert operator is not None
        operator.status = OperatorStatus.DISABLED
        session.commit()

    with pytest.raises(LaunchAgentRejectedError):
        await _authorize_launch(harness, _launch_authorizer(harness, definition.agent_id), definition.agent_id)


async def test_chat_launch_authorizer_fails_closed_for_inactive_agent(harness: Harness) -> None:
    definition = await _launch_definition(harness)
    await harness.authority.reconcile_static_agents([])

    with pytest.raises(LaunchAgentRejectedError):
        await _authorize_launch(harness, _launch_authorizer(harness, definition.agent_id), definition.agent_id)


async def test_chat_launch_authorizer_fails_closed_for_unprofiled_agent(db_url: str, harness: Harness) -> None:
    definition = await _launch_definition(harness)
    with _orm_session(db_url) as session:
        agent = session.get(Agent, definition.agent_id)
        assert agent is not None
        agent.access_profile_id = None
        session.commit()

    with pytest.raises(LaunchAgentRejectedError):
        await _authorize_launch(harness, _launch_authorizer(harness, definition.agent_id), definition.agent_id)


async def test_chat_launch_authorizer_fails_closed_for_unlaunchable_agent(harness: Harness) -> None:
    definition = await _launch_definition(harness)
    with pytest.raises(LaunchAgentRejectedError):
        await _authorize_launch(
            harness, _launch_authorizer(harness, definition.agent_id, launchable=False), definition.agent_id
        )


async def test_chat_launch_authorizer_fails_closed_for_disallowed_profile(
    db_url: str, harness_factory: HarnessFactory
) -> None:
    harness = await harness_factory(access_profiles=("chat", "disallowed"))
    definition = await _launch_definition(harness, access_profile_id="disallowed")
    with pytest.raises(LaunchAgentRejectedError):
        await _authorize_launch(harness, _launch_authorizer(harness, definition.agent_id), definition.agent_id)


async def test_chat_launch_authorizer_fails_closed_for_unregistered_runtime(harness: Harness) -> None:
    definition = await _launch_definition(harness)
    with pytest.raises(LaunchAgentRejectedError):
        await _authorize_launch(
            harness, _launch_authorizer(harness, definition.agent_id, registered=()), definition.agent_id
        )


async def test_chat_launch_authorizer_rejects_an_unregistered_agent_runtime_pair(harness: Harness) -> None:
    first = await _launch_definition(harness)
    second = StaticAgentDefinition(
        agent_id=uuid4(),
        display_name="Second Launch Test Agent",
        operator_id=harness.browser.operator_id,
        secret_reference="env:SECOND_LAUNCH_TEST_AGENT_TOKEN",
        token_fingerprint=fingerprint_static_token(f"launch-token-{uuid4()}"),
        access_profile_id="no_auto_approval",
    )
    await harness.authority.reconcile_static_agents([first, second])
    authorizer = ChatLaunchAuthorizer(
        harness.authority,
        launchable_agent_ids={first.agent_id, second.agent_id},
        registered_runtime_identities={RuntimeKey(first.agent_id, RuntimeKind.CLAUDE_CODE)},
        profile_runtime_kinds={"no_auto_approval": {RuntimeKind.CLAUDE_CODE}},
    )

    assert (await _authorize_launch(harness, authorizer, first.agent_id)).agent_id == first.agent_id
    with pytest.raises(LaunchAgentRejectedError, match="pair is not registered"):
        await _authorize_launch(harness, authorizer, second.agent_id)


async def test_static_reconcile_is_idempotent_rotates_and_revalidates(db_url: str, harness: Harness) -> None:
    definition = StaticAgentDefinition(
        agent_id=uuid4(),
        display_name="Configured Agent",
        operator_id=harness.browser.operator_id,
        secret_reference="env:CONFIGURED_AGENT_TOKEN",
        token_fingerprint=fingerprint_static_token("first-token"),
        access_profile_id="no_auto_approval",
    )
    first, second = await asyncio.gather(
        harness.authority.reconcile_static_agents([definition]), harness.authority.reconcile_static_agents([definition])
    )
    assert first == second
    initial = first[0]
    assert await harness.authority.static_authorization_for_binding(binding_id=initial.binding_id) == initial

    with _orm_session(db_url) as session:
        agent = session.get(Agent, definition.agent_id)
        assert agent is not None
        initial_updated_at = agent.updated_at
        assert agent.last_seen_at is None

    assert (
        await harness.authority.static_authorization_for_fingerprint(fingerprint=definition.token_fingerprint)
        == initial
    )
    with _orm_session(db_url) as session:
        agent = session.get(Agent, definition.agent_id)
        assert agent is not None
        assert agent.updated_at == initial_updated_at
        assert agent.last_seen_at is None

    harness.clock.advance(datetime.timedelta(seconds=1))
    assert (
        await harness.authority.static_authorization_for_fingerprint(
            fingerprint=definition.token_fingerprint, record_seen=True
        )
        == initial
    )
    with _orm_session(db_url) as session:
        agent = session.get(Agent, definition.agent_id)
        assert agent is not None
        assert agent.updated_at == harness.clock.now
        assert agent.last_seen_at == harness.clock.now

    rotated_definition = StaticAgentDefinition(
        agent_id=definition.agent_id,
        display_name=definition.display_name,
        operator_id=definition.operator_id,
        secret_reference=definition.secret_reference,
        token_fingerprint=fingerprint_static_token("second-token"),
        access_profile_id=definition.access_profile_id,
    )
    rotated = (await harness.authority.reconcile_static_agents([rotated_definition]))[0]
    assert rotated.agent_id == initial.agent_id
    assert rotated.binding_id != initial.binding_id
    with pytest.raises(StaticAgentRejectedError):
        await harness.authority.static_authorization_for_binding(binding_id=initial.binding_id)

    with _orm_session(db_url) as session:
        bindings = (
            session.execute(
                select(CredentialBinding)
                .where(CredentialBinding.agent_id == definition.agent_id)
                .order_by(CredentialBinding.generation)
            )
            .scalars()
            .all()
        )
        binding_rows = [
            (binding.binding_id, binding.generation, binding.supersedes_binding_id, binding.status)
            for binding in bindings
        ]
        stored_fingerprints = session.execute(select(StaticCredential.credential_fingerprint)).scalars().all()
    assert binding_rows == [
        (initial.binding_id, 1, None, CredentialBindingStatus.REVOKED),
        (rotated.binding_id, 2, initial.binding_id, CredentialBindingStatus.ACTIVE),
    ]
    assert b"first-token" not in stored_fingerprints
    assert b"second-token" not in stored_fingerprints


async def test_static_agent_access_profile_is_deployment_managed(harness_factory: HarnessFactory) -> None:
    harness = await harness_factory(access_profiles=("no_auto_approval", "haku_v1"))
    definition = StaticAgentDefinition(
        agent_id=uuid4(),
        display_name="Deployment Profile Agent",
        operator_id=harness.browser.operator_id,
        secret_reference="env:DEPLOYMENT_POLICY_AGENT_TOKEN",
        token_fingerprint=fingerprint_static_token("deployment-policy-token"),
        access_profile_id="haku_v1",
    )
    authorization = (await harness.authority.reconcile_static_agents([definition]))[0]
    assert authorization.access_profile_id == "haku_v1"

    with pytest.raises(AgentAccessProfileManagedByDeploymentError):
        await harness.authority.set_access_profile(
            operator_id=harness.browser.operator_id, agent_id=definition.agent_id, access_profile_id="no_auto_approval"
        )


async def test_static_reconcile_revokes_removed_definition_and_restores_stable_slot(
    db_url: str, harness: Harness
) -> None:
    definition = StaticAgentDefinition(
        agent_id=uuid4(),
        display_name="Removable Configured Agent",
        operator_id=harness.browser.operator_id,
        secret_reference="env:REMOVABLE_AGENT_TOKEN",
        token_fingerprint=fingerprint_static_token("first-removable-token"),
        access_profile_id="no_auto_approval",
    )
    initial = (await harness.authority.reconcile_static_agents([definition]))[0]

    assert await harness.authority.reconcile_static_agents([]) == ()
    with pytest.raises(StaticAgentRejectedError):
        await harness.authority.static_authorization_for_binding(binding_id=initial.binding_id)
    with pytest.raises(StaticAgentRejectedError):
        await harness.authority.static_authorization_for_fingerprint(fingerprint=definition.token_fingerprint)

    restored_definition = StaticAgentDefinition(
        agent_id=definition.agent_id,
        display_name=definition.display_name,
        operator_id=definition.operator_id,
        secret_reference=definition.secret_reference,
        token_fingerprint=fingerprint_static_token("rotated-after-removal"),
        access_profile_id=definition.access_profile_id,
    )
    restored = (await harness.authority.reconcile_static_agents([restored_definition]))[0]
    assert restored.agent_id == initial.agent_id
    with _orm_session(db_url) as session:
        bindings = (
            session.execute(
                select(CredentialBinding)
                .where(CredentialBinding.agent_id == definition.agent_id)
                .order_by(CredentialBinding.generation)
            )
            .scalars()
            .all()
        )
        agent = session.get(Agent, definition.agent_id)
        assert agent is not None
        binding_rows = [
            (binding.binding_id, binding.generation, binding.supersedes_binding_id, binding.status, binding.end_reason)
            for binding in bindings
        ]
        agent_status = agent.status
    assert binding_rows == [
        (initial.binding_id, 1, None, CredentialBindingStatus.REVOKED, "static_configuration_removed"),
        (restored.binding_id, 2, initial.binding_id, CredentialBindingStatus.ACTIVE, None),
    ]
    assert agent_status is AgentStatus.ACTIVE


async def test_revoke_waits_for_interaction_before_locking_grant_graph(db_url: str, harness: Harness) -> None:
    grant = await _create_grant(harness, label="lock-order", display_name="Lock Ordered Agent")
    authority_engine = create_async_engine(make_url(db_url).set(drivername="postgresql+asyncpg"), pool_pre_ping=True)
    authority = PostgresAgentAuthority(
        async_sessionmaker(authority_engine, expire_on_commit=False),
        public_base_url="https://haku.test",
        operator_identity_store=harness.identities,
        clock=harness.clock,
        access_profiles=("no_auto_approval",),
        default_access_profile_id="no_auto_approval",
    )
    interaction_lock_attempted = threading.Event()

    def observe_interaction_lock(
        _connection: object, _cursor: object, statement: str, _parameters: object, _context: object, _executemany: bool
    ) -> None:
        normalized = statement.casefold()
        if "enrollment_interactions" in normalized and "for update" in normalized:
            interaction_lock_attempted.set()

    event.listen(authority_engine.sync_engine, "before_cursor_execute", observe_interaction_lock)
    engine = create_engine(db_url)
    owner = Session(engine)
    transaction = owner.begin()
    task: asyncio.Task[None] | None = None
    try:
        persisted_grant = owner.get(AuthorizationGrant, grant.grant_id)
        assert persisted_grant is not None
        interaction_id = persisted_grant.enrollment_interaction_id
        binding_id = persisted_grant.binding_id
        owner.execute(
            select(EnrollmentInteraction)
            .where(EnrollmentInteraction.interaction_id == interaction_id)
            .with_for_update()
        ).scalar_one()
        task = asyncio.create_task(authority.revoke_grant(grant_id=grant.grant_id))
        assert await asyncio.to_thread(interaction_lock_attempted.wait, 5)
        assert not task.done()
        owner.execute(
            select(CredentialBinding).where(CredentialBinding.binding_id == binding_id).with_for_update(nowait=True)
        ).scalar_one()
        transaction.commit()
        await task
    finally:
        if transaction.is_active:
            transaction.rollback()
        owner.close()
        if task is not None and not task.done():
            await task
        event.remove(authority_engine.sync_engine, "before_cursor_execute", observe_interaction_lock)
        engine.dispose()
        await authority_engine.dispose()


async def test_database_outage_maps_to_authority_unavailable() -> None:
    database_url = "postgresql+psycopg://postgres:postgres@127.0.0.1:1/unavailable"
    sessions = console_sessions(database_url)
    identities = PostgresOperatorIdentityStore(
        sessions, OperatorIdentityTrust(trust_domain="test", trusted_issuers=frozenset({_BROWSER_ISSUER}))
    )
    authority = PostgresAgentAuthority(
        sessions,
        public_base_url="https://haku.test",
        operator_identity_store=identities,
        access_profiles=("no_auto_approval",),
        default_access_profile_id="no_auto_approval",
    )
    with pytest.raises(AgentGrantAuthorityUnavailableError):
        await authority.reserve_authorization(
            request=_request("outage"), upstream_authorization_url="https://auth.test/authorize"
        )


async def test_sqlalchemy_pool_timeout_maps_to_authority_unavailable(db_url: str, harness: Harness) -> None:

    async def time_out() -> None:
        raise SQLAlchemyTimeoutError("connection pool exhausted")

    with pytest.raises(AgentGrantAuthorityUnavailableError):
        await harness.authority._database_call(time_out)


async def test_exchange_revalidates_operator_after_principal_resolution(db_url: str, harness: Harness) -> None:
    request = _request("operator-race")
    interaction_id, form_token = await _open(harness, request)
    await harness.authority.decide(
        interaction_id=interaction_id,
        browser=harness.browser,
        interaction_cookie=form_token,
        decision=CreateAgentDecision(
            form_token=form_token, display_name="Disabled During Exchange", access_profile_id="no_auto_approval"
        ),
    )
    trust = OperatorIdentityTrust(
        trust_domain="auth.test/authentik-user-id/v1", trusted_issuers=frozenset({_BROWSER_ISSUER, _MCP_ISSUER})
    )
    authority = PostgresAgentAuthority(
        console_sessions(db_url),
        public_base_url="https://haku.test",
        operator_identity_store=DisableAfterPrincipalResolutionIdentityStore(console_sessions(db_url), trust),
        clock=harness.clock,
        access_profiles=("no_auto_approval",),
        default_access_profile_id="no_auto_approval",
    )

    with pytest.raises(EnrollmentRejectedError):
        await authority.begin_exchange(
            correlation=request.correlation,
            client=request.client,
            principal=harness.principal,
            granted_scopes=frozenset({"tools:call"}),
        )


if __name__ == "__main__":
    pytest_bazel.main()
