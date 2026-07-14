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

from haku.console.agents.authorization import (
    PostgresAgentAuthority,
    StaticAgentDefinition,
    StaticAgentRejectedError,
    fingerprint_static_token,
)
from haku.console.agents.enrollment import (
    CreateAgentDecision,
    EnrollmentAllowed,
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
from haku.console.operator_identity import OperatorIdentityTrust, VerifiedExternalIdentity
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


async def _create_grant(
    harness: Harness, *, label: str, display_name: str, activate: bool = False
) -> GrantAuthorization:
    request = _request(label)
    interaction_id, form_token = await _open(harness, request)
    result = await harness.authority.decide(
        interaction_id=interaction_id,
        browser=harness.browser,
        interaction_cookie=form_token,
        decision=CreateAgentDecision(form_token=form_token, display_name=display_name),
    )
    assert isinstance(result, EnrollmentAllowed)
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
    assert row.agent_status == "active"
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


if __name__ == "__main__":
    pytest_bazel.main()
