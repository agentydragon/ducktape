"""Real-Postgres tests for the canonical Agent authority application service."""

from __future__ import annotations

import asyncio
import datetime
import errno
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from haku.console.database_schema import (
    Agent,
    AgentNameReservation,
    AuthorizationGrant,
    ClientSoftware,
    CredentialBinding,
    EnrollmentCorrelationReservation,
    EnrollmentInteraction,
    StaticCredential,
)
from haku.console.identity.agent import (
    AgentStatus,
    ClientRegistrationKind,
    CredentialBindingStatus,
    CredentialKind,
    EnrollmentPhase,
)
from haku.console.identity.authorization import AgentGrantAuthorityUnavailableError, PostgresAgentAuthority, _digest
from haku.console.identity.enrollment import (
    AgentAccessProfileManagedByDeploymentError,
    CreateAgentDecision,
    EnrollmentAllowed,
    EnrollmentBrowserBindingError,
    EnrollmentBrowserSession,
    EnrollmentDecisionConflictError,
)
from haku.console.identity.operator_identity import OperatorIdentityTrust, VerifiedExternalIdentity
from haku.console.identity.operator_identity_store import PostgresOperatorIdentityStore
from util.db_url import render_postgresql_url
from util.testing.sqlalchemy_engines import managed_async_engine

_BROWSER_ISSUER = "https://auth.test/browser/"
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
class PersistedInteractionState:
    phase: EnrollmentPhase
    browser_binding_digest: bytes | None
    pending_name_count: int


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


@dataclass(frozen=True)
class Harness:
    authority: PostgresAgentAuthority
    sessions: async_sessionmaker[AsyncSession]
    identities: PostgresOperatorIdentityStore
    browser: EnrollmentBrowserSession
    clock: MutableClock


@pytest.fixture
def db_url(migrated_db_url: str) -> str:
    """Use the shared migrated clone with a sync driver for ORM helpers in this test."""
    return render_postgresql_url(migrated_db_url, "postgresql+psycopg")


HarnessFactory = Callable[..., Awaitable[Harness]]


@pytest.fixture
def harness_factory(db_url: str, migrated_sessions: async_sessionmaker[AsyncSession]) -> HarnessFactory:
    async def build(
        *, subject: str = "operator-user", access_profiles: tuple[str, ...] = ("no_auto_approval",)
    ) -> Harness:
        sessions = migrated_sessions
        identities = PostgresOperatorIdentityStore(
            sessions,
            OperatorIdentityTrust(
                trust_domain="auth.test/authentik-user-id/v1", trusted_issuers=frozenset({_BROWSER_ISSUER})
            ),
        )
        browser_identity = await identities.resolve_verified_identity(
            VerifiedExternalIdentity(issuer=_BROWSER_ISSUER, subject=subject)
        )
        # Ground the mutable test clock in PostgreSQL's time domain.
        with _orm_session(db_url) as session:
            database_now = session.scalar(select(func.clock_timestamp()))
        assert isinstance(database_now, datetime.datetime)
        clock = MutableClock(database_now)
        authority = PostgresAgentAuthority(
            sessions,
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
            clock=clock,
        )

    return build


@pytest.fixture
async def harness(harness_factory: HarnessFactory) -> Harness:
    return await harness_factory()


async def _open(harness: Harness, label: str) -> tuple[UUID, str]:
    now = harness.clock.now
    interaction_id = uuid4()
    nonce = f"browser-nonce-{uuid4()}"
    code_challenge = f"challenge-{label}-{uuid4()}"
    async with harness.sessions.begin() as session:
        client = await session.scalar(select(ClientSoftware).where(ClientSoftware.oauth_client_id == _CLIENT_ID))
        if client is None:
            client = ClientSoftware(
                client_software_id=uuid4(),
                registration_kind=ClientRegistrationKind.OAUTH_PROXY_UNCLASSIFIED,
                oauth_client_id=_CLIENT_ID,
                validated_redirect_uris=[_REDIRECT_URI],
                metadata_hash=b"test-client-metadata",
                observed_name="Claude Desktop",
                observed_icon_uri=None,
                created_at=now,
                updated_at=now,
            )
            session.add(client)
            await session.flush()
        session.add(
            EnrollmentInteraction(
                interaction_id=interaction_id,
                client_software_id=client.client_software_id,
                client_id=_CLIENT_ID,
                redirect_uri=_REDIRECT_URI,
                code_challenge=code_challenge,
                requested_scopes=["tools:call", "tools:list"],
                presentation_snapshot={"display_name": "Claude Desktop"},
                upstream_authorization_url=f"https://auth.test/authorize/{uuid4()}",
                phase=EnrollmentPhase.AWAITING_BROWSER,
                expires_at=now + datetime.timedelta(minutes=10),
                correlation_release_after=now + datetime.timedelta(hours=2),
                browser_nonce_digest=_digest("browser-nonce", nonce),
                browser_identity_id=None,
                browser_binding_digest=None,
                decision_digest=None,
                reconnect_agent_id=None,
                reconnect_predecessor_binding_id=None,
                access_profile_id=None,
                closure_reason=None,
                created_at=now,
                updated_at=now,
                closed_at=None,
            )
        )
        await session.flush()
        session.add(
            EnrollmentCorrelationReservation(
                interaction_id=interaction_id,
                client_id=_CLIENT_ID,
                redirect_uri=_REDIRECT_URI,
                code_challenge=code_challenge,
                release_after=now + datetime.timedelta(hours=2),
            )
        )
    page = await harness.authority.open_interaction(
        interaction_id=interaction_id, browser_nonce=nonce, interaction_cookie=None, browser=harness.browser
    )
    return interaction_id, page.form_token


async def _allow_create(harness: Harness, *, label: str, display_name: str) -> UUID:
    interaction_id, form_token = await _open(harness, label)
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
    return interaction_id


async def _seed_expiring_grant(harness: Harness, *, phase: EnrollmentPhase) -> UUID:
    """Insert a legacy grant row to exercise expiry of persisted state without OAuth issuance."""

    assert phase in {EnrollmentPhase.EXCHANGING, EnrollmentPhase.COMPLETED}
    now = harness.clock.now
    older = now - datetime.timedelta(minutes=16)
    interaction_id = uuid4()
    client_software_id = uuid4()
    client_id = f"legacy-client-{uuid4()}"
    reservation_id = uuid4()
    agent_id = uuid4()
    binding_id = uuid4()
    grant_id = uuid4()
    code_challenge = f"legacy-challenge-{uuid4()}"
    is_exchanging = phase is EnrollmentPhase.EXCHANGING
    expires_at = now - datetime.timedelta(seconds=1) if is_exchanging else now + datetime.timedelta(minutes=10)
    correlation_release_after = expires_at + datetime.timedelta(hours=2)
    async with harness.sessions.begin() as session:
        session.add(
            ClientSoftware(
                client_software_id=client_software_id,
                registration_kind=ClientRegistrationKind.OAUTH_PROXY_UNCLASSIFIED,
                oauth_client_id=client_id,
                validated_redirect_uris=[_REDIRECT_URI],
                metadata_hash=b"legacy-client-metadata",
                observed_name="Legacy client",
                observed_icon_uri=None,
                created_at=older,
                updated_at=older,
            )
        )
        await session.flush()
        interaction = EnrollmentInteraction(
            interaction_id=interaction_id,
            client_software_id=client_software_id,
            client_id=client_id,
            redirect_uri=_REDIRECT_URI,
            code_challenge=code_challenge,
            requested_scopes=["tools:call"],
            presentation_snapshot={"display_name": "Legacy client"},
            upstream_authorization_url="https://auth.test/authorize/legacy",
            phase=EnrollmentPhase.AWAITING_BROWSER,
            expires_at=expires_at,
            correlation_release_after=correlation_release_after,
            browser_nonce_digest=b"legacy-browser-nonce",
            browser_identity_id=None,
            browser_binding_digest=None,
            decision_digest=None,
            reconnect_agent_id=None,
            reconnect_predecessor_binding_id=None,
            access_profile_id=None,
            closure_reason=None,
            created_at=older,
            updated_at=older,
            closed_at=None,
        )
        session.add(interaction)
        await session.flush()
        session.add(
            EnrollmentCorrelationReservation(
                interaction_id=interaction_id,
                client_id=client_id,
                redirect_uri=_REDIRECT_URI,
                code_challenge=code_challenge,
                release_after=correlation_release_after,
            )
        )
        await session.flush()
        interaction.phase = EnrollmentPhase.AWAITING_APPROVAL
        interaction.browser_nonce_digest = None
        interaction.browser_identity_id = harness.browser.identity_id
        interaction.browser_binding_digest = b"legacy-browser-binding"
        await session.flush()
        interaction.phase = EnrollmentPhase.ALLOWED
        interaction.decision_digest = b"legacy-decision"
        interaction.access_profile_id = "no_auto_approval"
        await session.flush()
        name = AgentNameReservation(
            reservation_id=reservation_id,
            display_name=f"Legacy Agent {grant_id}",
            display_name_key=f"legacy-agent-{grant_id}",
            originating_interaction_id=interaction_id,
            pending_interaction_id=interaction_id,
            agent_id=None,
            created_at=older,
            activated_at=None,
        )
        session.add(name)
        await session.flush()
        session.add(
            Agent(
                agent_id=agent_id,
                owner_operator_id=harness.browser.operator_id,
                current_name_reservation_id=reservation_id,
                status=AgentStatus.DRAFT,
                created_at=older,
                updated_at=older,
                activated_at=None,
                last_seen_at=None,
                access_profile_id="no_auto_approval",
            )
        )
        await session.flush()
        name.pending_interaction_id = None
        name.agent_id = agent_id
        name.activated_at = older
        binding = CredentialBinding(
            binding_id=binding_id,
            agent_id=agent_id,
            kind=CredentialKind.OAUTH,
            status=CredentialBindingStatus.ISSUING,
            generation=1,
            supersedes_binding_id=None,
            created_at=older,
            updated_at=older,
            issued_at=None,
            activated_at=None,
            ended_at=None,
            end_reason=None,
        )
        session.add(binding)
        await session.flush()
        interaction.phase = EnrollmentPhase.EXCHANGING
        await session.flush()
        grant = AuthorizationGrant(
            grant_id=grant_id,
            binding_id=binding_id,
            authorizing_identity_id=harness.browser.identity_id,
            client_software_id=client_software_id,
            enrollment_interaction_id=interaction_id,
            allowed_scopes=["tools:call"],
            initial_access_jti=None,
            initial_refresh_jti=None,
            token_family_persisted_at=None,
            created_at=older,
        )
        session.add(grant)
        await session.flush()
    if not is_exchanging:
        async with harness.sessions.begin() as session:
            locked_interaction = await session.get(EnrollmentInteraction, interaction_id, with_for_update=True)
            locked_binding = await session.get(CredentialBinding, binding_id, with_for_update=True)
            locked_grant = await session.get(AuthorizationGrant, grant_id, with_for_update=True)
            assert locked_interaction is not None
            assert locked_binding is not None
            assert locked_grant is not None
            locked_binding.status = CredentialBindingStatus.ISSUED
            locked_binding.issued_at = older
            locked_binding.updated_at = older
            locked_grant.initial_access_jti = f"legacy-access-{grant_id}"
            locked_grant.token_family_persisted_at = older
            locked_interaction.phase = EnrollmentPhase.COMPLETED
            locked_interaction.browser_binding_digest = None
            locked_interaction.closure_reason = "authorization_completed"
            locked_interaction.closed_at = now
            locked_interaction.updated_at = now
    return grant_id


async def _seed_retired_static_agent(harness: Harness, *, display_name: str, access_profile_id: str) -> UUID:
    """Persist a disabled static Agent as retained history, as the retirement migration does."""
    now = harness.clock.now
    agent_id = uuid4()
    reservation_id = uuid4()
    binding_id = uuid4()
    async with harness.sessions.begin() as session:
        session.add(
            Agent(
                agent_id=agent_id,
                owner_operator_id=harness.browser.operator_id,
                current_name_reservation_id=reservation_id,
                status=AgentStatus.DISABLED,
                created_at=now,
                updated_at=now,
                activated_at=now,
                last_seen_at=None,
                access_profile_id=access_profile_id,
            )
        )
        session.add(
            AgentNameReservation(
                reservation_id=reservation_id,
                display_name=display_name,
                display_name_key=display_name.casefold(),
                originating_interaction_id=None,
                pending_interaction_id=None,
                agent_id=agent_id,
                created_at=now,
                activated_at=now,
            )
        )
        session.add(
            CredentialBinding(
                binding_id=binding_id,
                agent_id=agent_id,
                kind=CredentialKind.STATIC,
                status=CredentialBindingStatus.REVOKED,
                generation=1,
                supersedes_binding_id=None,
                created_at=now,
                updated_at=now,
                issued_at=now,
                activated_at=now,
                ended_at=now,
                end_reason="static_agents_retired",
            )
        )
        session.add(
            StaticCredential(
                binding_id=binding_id,
                secret_reference="retired-test-fixture",
                credential_fingerprint=b"retired-test-fingerprint",
                created_at=now,
            )
        )
    return agent_id


async def test_list_agents_returns_owned_disabled_static_history(
    harness: Harness, harness_factory: HarnessFactory
) -> None:
    agent_id = await _seed_retired_static_agent(
        harness, display_name="Retired Agent", access_profile_id="no_auto_approval"
    )

    agents = await harness.authority.list_agents(operator_id=harness.browser.operator_id)
    [agent] = agents
    assert agent.agent_id == agent_id
    assert agent.display_name == "Retired Agent"
    assert agent.status is AgentStatus.DISABLED
    assert agent.credential_kind is CredentialKind.STATIC
    assert agent.credential_status is CredentialBindingStatus.REVOKED

    other = await harness_factory(subject="other-operator")
    assert await other.authority.list_agents(operator_id=other.browser.operator_id) == ()


async def test_enrollment_persists_the_selected_access_profile_id(db_url: str, harness_factory: HarnessFactory) -> None:
    harness = await harness_factory(access_profiles=("no_auto_approval", "haku_v1"))
    interaction_id, form_token = await _open(harness, "policy-selection")
    await harness.authority.decide(
        interaction_id=interaction_id,
        browser=harness.browser,
        interaction_cookie=form_token,
        decision=CreateAgentDecision(form_token=form_token, display_name="Policy Agent", access_profile_id="haku_v1"),
    )
    with _orm_session(db_url) as session:
        interaction = session.get(EnrollmentInteraction, interaction_id)
        assert interaction is not None
        assert interaction.access_profile_id == "haku_v1"


async def test_create_decision_is_idempotent(db_url: str, harness: Harness) -> None:
    interaction_id, form_token = await _open(harness, "create")
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

    with _orm_session(db_url) as session:
        interaction = session.get(EnrollmentInteraction, interaction_id)
        assert interaction is not None
        decision_digest = interaction.decision_digest
        assert interaction.browser_nonce_digest is None
        assert interaction.browser_binding_digest is not None
        assert interaction.phase is EnrollmentPhase.ALLOWED
        assert interaction.access_profile_id == "no_auto_approval"

    assert decision_digest not in {form_token.encode(), b"browser-secret-1", b"browser-secret-2"}


async def test_expiry_maintenance_sweeps_allowed_response_loss_and_skips_locked_rows(
    db_url: str, harness: Harness
) -> None:
    locked_interaction_id = await _allow_create(harness, label="locked-allowed", display_name="Locked Allowed")
    harness.clock.advance(datetime.timedelta(seconds=1))
    unlocked_interaction_id = await _allow_create(harness, label="unlocked-allowed", display_name="Unlocked Allowed")
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
    grant_id = await _seed_expiring_grant(harness, phase=EnrollmentPhase.EXCHANGING)

    assert await harness.authority.sweep_expired_state() == 1
    assert await harness.authority.sweep_expired_state() == 0

    with _orm_session(db_url) as session:
        state = _grant_state(session, grant_id)
        assert state.interaction.phase is EnrollmentPhase.EXPIRED
        assert state.interaction.browser_binding_digest is None
        assert state.interaction.closure_reason == "expiry_sweep"
        assert state.binding.status is CredentialBindingStatus.FAILED
        assert state.binding.end_reason == "expiry_sweep"
        assert state.agent.status is AgentStatus.ABANDONED


async def test_expiry_sweep_terminates_issued_response_loss(db_url: str, harness: Harness) -> None:
    grant_id = await _seed_expiring_grant(harness, phase=EnrollmentPhase.COMPLETED)

    assert await harness.authority.sweep_expired_state() == 1
    assert await harness.authority.sweep_expired_state() == 0

    with _orm_session(db_url) as session:
        state = _grant_state(session, grant_id)
        assert state.interaction.phase is EnrollmentPhase.COMPLETED
        assert state.interaction.browser_binding_digest is None
        assert state.binding.status is CredentialBindingStatus.EXPIRED
        assert state.binding.end_reason == "activation_timeout"
        assert state.agent.status is AgentStatus.ABANDONED


async def test_retained_static_agent_access_profile_remains_immutable(harness_factory: HarnessFactory) -> None:
    harness = await harness_factory(access_profiles=("no_auto_approval", "haku_v1"))
    agent_id = await _seed_retired_static_agent(
        harness, display_name="Retained Profile Agent", access_profile_id="haku_v1"
    )

    with pytest.raises(AgentAccessProfileManagedByDeploymentError):
        await harness.authority.set_access_profile(
            operator_id=harness.browser.operator_id, agent_id=agent_id, access_profile_id="no_auto_approval"
        )


async def test_database_outage_maps_to_authority_unavailable() -> None:
    database_url = "postgresql+asyncpg://postgres:postgres@127.0.0.1:1/unavailable"
    async with managed_async_engine(create_async_engine(database_url, pool_pre_ping=True)) as engine:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        identities = PostgresOperatorIdentityStore(
            sessions, OperatorIdentityTrust(trust_domain="test", trusted_issuers=frozenset({_BROWSER_ISSUER}))
        )
        authority = PostgresAgentAuthority(
            sessions,
            operator_identity_store=identities,
            access_profiles=("no_auto_approval",),
            default_access_profile_id="no_auto_approval",
        )
        with pytest.raises(AgentGrantAuthorityUnavailableError):
            await authority.list_agents(operator_id=uuid4())


async def test_sqlalchemy_pool_timeout_maps_to_authority_unavailable(db_url: str, harness: Harness) -> None:

    async def time_out() -> None:
        raise SQLAlchemyTimeoutError("connection pool exhausted")

    with pytest.raises(AgentGrantAuthorityUnavailableError):
        await harness.authority._database_call(time_out)


async def test_host_unreachable_maps_to_authority_unavailable(db_url: str, harness: Harness) -> None:
    """asyncpg can raise a bare OSError (e.g. EHOSTUNREACH) that isn't a ConnectionError subclass."""

    async def no_route_to_host() -> None:
        raise OSError(errno.EHOSTUNREACH, "No route to host")

    with pytest.raises(AgentGrantAuthorityUnavailableError):
        await harness.authority._database_call(no_route_to_host)


if __name__ == "__main__":
    pytest_bazel.main()
