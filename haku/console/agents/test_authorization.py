"""Real-Postgres tests for the canonical Agent authority application service."""

from __future__ import annotations

import asyncio
import datetime
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from haku.console.agents.authorization import (
    PostgresAgentAuthority,
    StaticAgentDefinition,
    StaticAgentRejectedError,
    fingerprint_static_token,
)
from haku.console.agents.enrollment import (
    CreateAgentDecision,
    EnrollmentAllowed,
    EnrollmentBrowserBindingError,
    EnrollmentBrowserSession,
    EnrollmentDecisionConflictError,
    ReconnectAgentDecision,
)
from haku.console.database_migrate import apply_migrations
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
from haku.console.operator_identity import OperatorIdentityTrust, ResolvedOperatorIdentity, VerifiedExternalIdentity
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from mcp_infra.authentik_auth.oidc_principal import VerifiedOidcPrincipal
from util.testing.postgres import force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container as _postgres_container

postgres_container = _postgres_container

_BROWSER_ISSUER = "https://auth.test/browser/"
_MCP_ISSUER = "https://auth.test/mcp/"
_CLIENT_ID = "claude-test-client"
_REDIRECT_URI = "https://claude.test/oauth/callback"


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
    def __init__(self, database_url: str, trust: OperatorIdentityTrust) -> None:
        super().__init__(database_url, trust)
        self._test_database_url = database_url

    def resolve_verified_identity(self, identity: VerifiedExternalIdentity) -> ResolvedOperatorIdentity:
        resolved = super().resolve_verified_identity(identity)
        engine = create_engine(self._test_database_url)
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE operators SET status = 'disabled', updated_at = now() WHERE operator_id = :operator_id"),
                {"operator_id": resolved.operator_id},
            )
        engine.dispose()
        return resolved


@dataclass(frozen=True)
class Harness:
    authority: PostgresAgentAuthority
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
    admin_engine = create_engine(postgres_admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()
    url = postgres_admin_url.rsplit("/", 1)[0] + f"/{db_name}"
    apply_migrations(url)

    yield url

    force_drop_database_sync(postgres_admin_url, db_name)


def _harness(db_url: str, *, subject: str = "operator-user") -> Harness:
    identities = PostgresOperatorIdentityStore(
        db_url,
        OperatorIdentityTrust(
            trust_domain="auth.test/authentik-user-id/v1", trusted_issuers=frozenset({_BROWSER_ISSUER, _MCP_ISSUER})
        ),
    )
    browser_identity = identities.resolve_verified_identity(
        VerifiedExternalIdentity(issuer=_BROWSER_ISSUER, subject=subject)
    )
    clock = MutableClock(datetime.datetime(2026, 7, 14, 20, 0, tzinfo=datetime.UTC))
    authority = PostgresAgentAuthority(
        db_url,
        public_base_url="https://haku.test",
        operator_identity_store=identities,
        clock=clock,
        browser_secret_factory=SecretSequence(),
    )
    return Harness(
        authority=authority,
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


def _request(label: str) -> AuthorizationRequest:
    return AuthorizationRequest(
        correlation=AuthorizationCorrelation(
            client_id=_CLIENT_ID, redirect_uri=_REDIRECT_URI, code_challenge=f"challenge-{label}-{uuid4()}"
        ),
        client=ClientSoftwareSnapshot(
            client_id=_CLIENT_ID, display_name="Claude Desktop", redirect_uris=(_REDIRECT_URI,)
        ),
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
        decision=CreateAgentDecision(form_token=form_token, display_name=display_name),
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


async def _wait_until_connection_is_lock_blocked(
    engine: Engine, *, application_name: str, task: asyncio.Task[None]
) -> None:
    with engine.connect() as conn:
        for _ in range(200):
            waiting = conn.execute(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND application_name = :application_name
                          AND state = 'active'
                          AND wait_event_type = 'Lock'
                    )
                    """
                ),
                {"application_name": application_name},
            ).scalar_one()
            if waiting:
                return
            if task.done():
                await task
                pytest.fail("authority operation completed before reaching its expected row lock")
            await asyncio.sleep(0.01)
    pytest.fail("authority operation did not reach its expected row lock")


async def test_create_decision_is_idempotent_and_grant_activates_then_revokes(db_url: str) -> None:
    harness = _harness(db_url)
    request = _request("create")
    interaction_id, form_token = await _open(harness, request)
    decision = CreateAgentDecision(form_token=form_token, display_name="Kitchen Claude")
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
            decision=CreateAgentDecision(form_token="different-form-token", display_name=decision.display_name),
        )
    with pytest.raises(EnrollmentDecisionConflictError):
        await harness.authority.decide(
            interaction_id=interaction_id,
            browser=harness.browser,
            interaction_cookie=form_token,
            decision=CreateAgentDecision(form_token=form_token, display_name="Different Agent"),
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
        await harness.authority.grant_for_access(
            grant_id=grant.grant_id, client_id=_CLIENT_ID, token_scopes=frozenset({"tools:call"})
        )
    ).actor.binding_id == grant.actor.binding_id
    activated = await harness.authority.activate_for_tool_call(
        grant_id=grant.grant_id, client_id=_CLIENT_ID, token_scopes=frozenset({"tools:call"})
    )
    assert activated.actor == grant.actor
    await harness.authority.revoke_grant(grant_id=grant.grant_id)
    with pytest.raises(GrantRejectedError):
        await harness.authority.grant_for_refresh(
            grant_id=grant.grant_id, client_id=_CLIENT_ID, requested_scopes=frozenset({"tools:call"})
        )

    engine = create_engine(db_url)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT interaction.browser_nonce_digest, interaction.browser_binding_digest,
                       interaction.decision_digest, interaction.phase::TEXT,
                       binding.status::TEXT AS binding_status,
                       agent.status::TEXT AS agent_status
                FROM authorization_grants AS auth_grant
                JOIN enrollment_interactions AS interaction
                  ON interaction.interaction_id = auth_grant.enrollment_interaction_id
                JOIN credential_bindings AS binding ON binding.binding_id = auth_grant.binding_id
                JOIN agents AS agent ON agent.agent_id = binding.agent_id
                WHERE auth_grant.grant_id = :grant_id
                """
            ),
            {"grant_id": grant.grant_id},
        ).one()
    engine.dispose()
    assert row.browser_nonce_digest is None
    assert row.browser_binding_digest is None
    assert row.phase == "completed"
    assert row.binding_status == "revoked"
    assert row.agent_status == "disabled"
    assert row.decision_digest not in {form_token.encode(), b"browser-secret-1", b"browser-secret-2"}


async def test_activation_timeout_abandons_new_agent_but_only_expires_reconnect(db_url: str) -> None:
    harness = _harness(db_url)
    initial = await _create_grant(harness, label="initial-timeout", display_name="Initial Timeout")
    harness.clock.advance(datetime.timedelta(minutes=16))
    with pytest.raises(GrantRejectedError):
        await harness.authority.grant_for_access(
            grant_id=initial.grant_id, client_id=_CLIENT_ID, token_scopes=frozenset({"tools:call"})
        )

    active = await _create_grant(harness, label="active", display_name="Reconnectable", activate=True)
    request = _request("reconnect-timeout")
    interaction_id, form_token = await _open(harness, request)
    engine = create_engine(db_url)
    with engine.connect() as conn:
        agent_id = conn.execute(
            text("SELECT agent_id FROM credential_bindings WHERE binding_id = :binding_id"),
            {"binding_id": active.actor.binding_id},
        ).scalar_one()
    reconnect_decision = ReconnectAgentDecision(form_token=form_token, agent_id=agent_id)
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

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT binding_id, status::TEXT FROM credential_bindings
                WHERE binding_id IN (:initial, :active, :replacement)
                ORDER BY binding_id
                """
            ),
            {
                "initial": initial.actor.binding_id,
                "active": active.actor.binding_id,
                "replacement": replacement.actor.binding_id,
            },
        ).all()
        statuses = {row.binding_id: row.status for row in rows}
        agent_statuses: dict[UUID, str] = {
            row.binding_id: row.agent_status
            for row in conn.execute(
                text(
                    """
                    SELECT binding.binding_id, agent.status::TEXT AS agent_status
                    FROM credential_bindings AS binding
                    JOIN agents AS agent ON agent.agent_id = binding.agent_id
                    WHERE binding.binding_id IN (:initial, :active)
                    """
                ),
                {"initial": initial.actor.binding_id, "active": active.actor.binding_id},
            )
        }
    engine.dispose()
    assert statuses[initial.actor.binding_id] == "expired"
    assert agent_statuses[initial.actor.binding_id] == "abandoned"
    assert statuses[active.actor.binding_id] == "active"
    assert statuses[replacement.actor.binding_id] == "expired"
    assert agent_statuses[active.actor.binding_id] == "active"


async def test_expiry_maintenance_sweeps_allowed_response_loss_and_skips_locked_rows(db_url: str) -> None:
    harness = _harness(db_url)
    _locked_request, locked_interaction_id = await _allow_create(
        harness, label="locked-allowed", display_name="Locked Allowed"
    )
    harness.clock.advance(datetime.timedelta(seconds=1))
    _unlocked_request, unlocked_interaction_id = await _allow_create(
        harness, label="unlocked-allowed", display_name="Unlocked Allowed"
    )
    harness.clock.advance(datetime.timedelta(minutes=11))

    engine = create_engine(db_url)
    owner = engine.connect()
    transaction = owner.begin()
    try:
        owner.execute(
            text(
                "SELECT interaction_id FROM enrollment_interactions WHERE interaction_id = :interaction_id FOR UPDATE"
            ),
            {"interaction_id": locked_interaction_id},
        )
        async with harness.authority.expiry_maintenance(interval=datetime.timedelta(milliseconds=10), batch_size=1):
            rows = {
                row.interaction_id: row
                for row in owner.execute(
                    text(
                        """
                        SELECT interaction.interaction_id, interaction.phase::TEXT,
                               interaction.browser_binding_digest,
                               EXISTS (
                                   SELECT 1 FROM agent_name_reservations AS name
                                   WHERE name.pending_interaction_id = interaction.interaction_id
                               ) AS has_pending_name
                        FROM enrollment_interactions AS interaction
                        WHERE interaction.interaction_id IN (:locked, :unlocked)
                        """
                    ),
                    {"locked": locked_interaction_id, "unlocked": unlocked_interaction_id},
                )
            }
            assert rows[locked_interaction_id].phase == "allowed"
            assert rows[locked_interaction_id].has_pending_name is True
            assert rows[unlocked_interaction_id].phase == "expired"
            assert rows[unlocked_interaction_id].browser_binding_digest is None
            assert rows[unlocked_interaction_id].has_pending_name is False

            transaction.commit()
            for _ in range(200):
                with engine.connect() as observer:
                    row = observer.execute(
                        text(
                            """
                            SELECT interaction.phase::TEXT, interaction.browser_binding_digest,
                                   EXISTS (
                                       SELECT 1 FROM agent_name_reservations AS name
                                       WHERE name.pending_interaction_id = interaction.interaction_id
                                   ) AS has_pending_name
                            FROM enrollment_interactions AS interaction
                            WHERE interaction.interaction_id = :interaction_id
                            """
                        ),
                        {"interaction_id": locked_interaction_id},
                    ).one()
                if row.phase == "expired":
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("periodic expiry maintenance did not catch the previously locked row")
            assert row.browser_binding_digest is None
            assert row.has_pending_name is False
    finally:
        if transaction.is_active:
            transaction.rollback()
        owner.close()
        engine.dispose()


async def test_expiry_sweep_terminates_exchanging_response_loss(db_url: str) -> None:
    harness = _harness(db_url)
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

    engine = create_engine(db_url)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT interaction.phase::TEXT, interaction.browser_binding_digest,
                       interaction.closure_reason, binding.status::TEXT AS binding_status,
                       binding.end_reason, agent.status::TEXT AS agent_status
                FROM authorization_grants AS auth_grant
                JOIN enrollment_interactions AS interaction
                  ON interaction.interaction_id = auth_grant.enrollment_interaction_id
                JOIN credential_bindings AS binding ON binding.binding_id = auth_grant.binding_id
                JOIN agents AS agent ON agent.agent_id = binding.agent_id
                WHERE auth_grant.grant_id = :grant_id
                  AND interaction.interaction_id = :interaction_id
                """
            ),
            {"grant_id": grant.grant_id, "interaction_id": interaction_id},
        ).one()
    engine.dispose()
    assert row.phase == "expired"
    assert row.browser_binding_digest is None
    assert row.closure_reason == "expiry_sweep"
    assert row.binding_status == "failed"
    assert row.end_reason == "expiry_sweep"
    assert row.agent_status == "abandoned"


async def test_expiry_sweep_terminates_issued_response_loss(db_url: str) -> None:
    harness = _harness(db_url)
    grant = await _create_grant(harness, label="lost-issued", display_name="Lost Issued Sweep")
    harness.clock.advance(datetime.timedelta(minutes=16))

    assert await harness.authority.sweep_expired_state() == 1
    assert await harness.authority.sweep_expired_state() == 0

    engine = create_engine(db_url)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT interaction.phase::TEXT, interaction.browser_binding_digest,
                       binding.status::TEXT AS binding_status, binding.end_reason,
                       agent.status::TEXT AS agent_status
                FROM authorization_grants AS auth_grant
                JOIN enrollment_interactions AS interaction
                  ON interaction.interaction_id = auth_grant.enrollment_interaction_id
                JOIN credential_bindings AS binding ON binding.binding_id = auth_grant.binding_id
                JOIN agents AS agent ON agent.agent_id = binding.agent_id
                WHERE auth_grant.grant_id = :grant_id
                """
            ),
            {"grant_id": grant.grant_id},
        ).one()
    engine.dispose()
    assert row.phase == "completed"
    assert row.browser_binding_digest is None
    assert row.binding_status == "expired"
    assert row.end_reason == "activation_timeout"
    assert row.agent_status == "abandoned"


async def test_exchange_uses_the_live_correlation_reservation_after_tuple_reuse(db_url: str) -> None:
    harness = _harness(db_url)
    request = _request("correlation-reuse")
    old_url = await harness.authority.reserve_authorization(
        request=request, upstream_authorization_url="https://auth.test/authorize/old"
    )
    old_interaction_id, _old_nonce = _interaction_from_url(old_url)
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM enrollment_correlation_reservations WHERE interaction_id = :interaction_id"),
            {"interaction_id": old_interaction_id},
        )
    harness.clock.advance(datetime.timedelta(seconds=1))
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
        decision=CreateAgentDecision(form_token=page.form_token, display_name="Reused Correlation"),
    )
    grant = await harness.authority.begin_exchange(
        correlation=request.correlation,
        client=request.client,
        principal=harness.principal,
        granted_scopes=frozenset({"tools:call"}),
    )
    with engine.connect() as conn:
        claimed_interaction_id = conn.execute(
            text("SELECT enrollment_interaction_id FROM authorization_grants WHERE grant_id = :grant_id"),
            {"grant_id": grant.grant_id},
        ).scalar_one()
    engine.dispose()
    assert claimed_interaction_id == new_interaction_id


async def test_exchange_timeout_and_preissuance_revoke_abandon_new_agents(db_url: str) -> None:
    harness = _harness(db_url)
    request = _request("exchange-expiry")
    interaction_id, form_token = await _open(harness, request)
    await harness.authority.decide(
        interaction_id=interaction_id,
        browser=harness.browser,
        interaction_cookie=form_token,
        decision=CreateAgentDecision(form_token=form_token, display_name="Lost Exchange"),
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
        decision=CreateAgentDecision(form_token=token_two, display_name="Revoked Before Issue"),
    )
    grant_two = await harness.authority.begin_exchange(
        correlation=request_two.correlation,
        client=request_two.client,
        principal=harness.principal,
        granted_scopes=frozenset({"tools:call"}),
    )
    await harness.authority.revoke_grant(grant_id=grant_two.grant_id)

    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT auth_grant.grant_id, interaction.phase::TEXT,
                       interaction.browser_binding_digest, binding.status::TEXT, agent.status::TEXT
                FROM authorization_grants AS auth_grant
                JOIN enrollment_interactions AS interaction
                  ON interaction.interaction_id = auth_grant.enrollment_interaction_id
                JOIN credential_bindings AS binding ON binding.binding_id = auth_grant.binding_id
                JOIN agents AS agent ON agent.agent_id = binding.agent_id
                WHERE auth_grant.grant_id IN (:expired, :revoked)
                """
            ),
            {"expired": grant.grant_id, "revoked": grant_two.grant_id},
        ).all()
    engine.dispose()
    by_grant = {row.grant_id: row for row in rows}
    assert by_grant[grant.grant_id][1:] == ("expired", None, "failed", "abandoned")
    assert by_grant[grant_two.grant_id][1:] == ("failed", None, "failed", "abandoned")


async def test_static_reconcile_is_idempotent_rotates_and_revalidates(db_url: str) -> None:
    harness = _harness(db_url)
    definition = StaticAgentDefinition(
        agent_id=uuid4(),
        display_name="Configured Agent",
        operator_id=harness.browser.operator_id,
        secret_reference="env:CONFIGURED_AGENT_TOKEN",
        token_fingerprint=fingerprint_static_token("first-token"),
    )
    first, second = await asyncio.gather(
        harness.authority.reconcile_static_agents([definition]), harness.authority.reconcile_static_agents([definition])
    )
    assert first == second
    initial = first[0]
    assert await harness.authority.static_authorization_for_binding(binding_id=initial.binding_id) == initial
    assert (
        await harness.authority.static_authorization_for_fingerprint(fingerprint=definition.token_fingerprint)
        == initial
    )

    rotated_definition = StaticAgentDefinition(
        agent_id=definition.agent_id,
        display_name=definition.display_name,
        operator_id=definition.operator_id,
        secret_reference=definition.secret_reference,
        token_fingerprint=fingerprint_static_token("second-token"),
    )
    rotated = (await harness.authority.reconcile_static_agents([rotated_definition]))[0]
    assert rotated.agent_id == initial.agent_id
    assert rotated.binding_id != initial.binding_id
    with pytest.raises(StaticAgentRejectedError):
        await harness.authority.static_authorization_for_binding(binding_id=initial.binding_id)

    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT binding_id, generation, supersedes_binding_id, status::TEXT
                FROM credential_bindings WHERE agent_id = :agent_id ORDER BY generation
                """
            ),
            {"agent_id": definition.agent_id},
        ).all()
        stored_fingerprints = (
            conn.execute(text("SELECT credential_fingerprint FROM static_credentials")).scalars().all()
        )
    engine.dispose()
    assert rows == [(initial.binding_id, 1, None, "revoked"), (rotated.binding_id, 2, initial.binding_id, "active")]
    assert b"first-token" not in stored_fingerprints
    assert b"second-token" not in stored_fingerprints


async def test_static_reconcile_revokes_removed_definition_and_restores_stable_slot(db_url: str) -> None:
    harness = _harness(db_url)
    definition = StaticAgentDefinition(
        agent_id=uuid4(),
        display_name="Removable Configured Agent",
        operator_id=harness.browser.operator_id,
        secret_reference="env:REMOVABLE_AGENT_TOKEN",
        token_fingerprint=fingerprint_static_token("first-removable-token"),
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
    )
    restored = (await harness.authority.reconcile_static_agents([restored_definition]))[0]
    assert restored.agent_id == initial.agent_id
    engine = create_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT binding_id, generation, supersedes_binding_id, status::TEXT, end_reason
                FROM credential_bindings WHERE agent_id = :agent_id ORDER BY generation
                """
            ),
            {"agent_id": definition.agent_id},
        ).all()
        agent_status = conn.execute(
            text("SELECT status::TEXT FROM agents WHERE agent_id = :agent_id"), {"agent_id": definition.agent_id}
        ).scalar_one()
    engine.dispose()
    assert rows == [
        (initial.binding_id, 1, None, "revoked", "static_configuration_removed"),
        (restored.binding_id, 2, initial.binding_id, "active", None),
    ]
    assert agent_status == "active"


async def test_revoke_waits_for_interaction_before_locking_grant_graph(db_url: str) -> None:
    harness = _harness(db_url)
    grant = await _create_grant(harness, label="lock-order", display_name="Lock Ordered Agent")
    application_name = f"authority-lock-order-{uuid4()}"
    authority = PostgresAgentAuthority(
        f"{db_url}?application_name={application_name}",
        public_base_url="https://haku.test",
        operator_identity_store=harness.identities,
        clock=harness.clock,
    )
    engine = create_engine(db_url)
    owner = engine.connect()
    transaction = owner.begin()
    task: asyncio.Task[None] | None = None
    try:
        interaction_id, binding_id = owner.execute(
            text(
                """
                SELECT enrollment_interaction_id, binding_id
                FROM authorization_grants WHERE grant_id = :grant_id
                """
            ),
            {"grant_id": grant.grant_id},
        ).one()
        owner.execute(
            text(
                "SELECT interaction_id FROM enrollment_interactions WHERE interaction_id = :interaction_id FOR UPDATE"
            ),
            {"interaction_id": interaction_id},
        )
        task = asyncio.create_task(authority.revoke_grant(grant_id=grant.grant_id))
        await _wait_until_connection_is_lock_blocked(engine, application_name=application_name, task=task)
        owner.execute(
            text("SELECT binding_id FROM credential_bindings WHERE binding_id = :binding_id FOR UPDATE NOWAIT"),
            {"binding_id": binding_id},
        )
        transaction.commit()
        await task
    finally:
        if transaction.is_active:
            transaction.rollback()
        owner.close()
        if task is not None and not task.done():
            await task
        engine.dispose()


async def test_database_outage_maps_to_authority_unavailable() -> None:
    database_url = "postgresql+psycopg://postgres:postgres@127.0.0.1:1/unavailable"
    identities = PostgresOperatorIdentityStore(
        database_url, OperatorIdentityTrust(trust_domain="test", trusted_issuers=frozenset({_BROWSER_ISSUER}))
    )
    authority = PostgresAgentAuthority(
        database_url, public_base_url="https://haku.test", operator_identity_store=identities
    )
    with pytest.raises(AgentGrantAuthorityUnavailableError):
        await authority.reserve_authorization(
            request=_request("outage"), upstream_authorization_url="https://auth.test/authorize"
        )


async def test_sqlalchemy_pool_timeout_maps_to_authority_unavailable(db_url: str) -> None:
    harness = _harness(db_url)

    def time_out() -> None:
        raise SQLAlchemyTimeoutError("connection pool exhausted")

    with pytest.raises(AgentGrantAuthorityUnavailableError):
        await harness.authority._database_call(time_out)


async def test_exchange_revalidates_operator_after_principal_resolution(db_url: str) -> None:
    harness = _harness(db_url)
    request = _request("operator-race")
    interaction_id, form_token = await _open(harness, request)
    await harness.authority.decide(
        interaction_id=interaction_id,
        browser=harness.browser,
        interaction_cookie=form_token,
        decision=CreateAgentDecision(form_token=form_token, display_name="Disabled During Exchange"),
    )
    trust = OperatorIdentityTrust(
        trust_domain="auth.test/authentik-user-id/v1", trusted_issuers=frozenset({_BROWSER_ISSUER, _MCP_ISSUER})
    )
    authority = PostgresAgentAuthority(
        db_url,
        public_base_url="https://haku.test",
        operator_identity_store=DisableAfterPrincipalResolutionIdentityStore(db_url, trust),
        clock=harness.clock,
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
