"""Consent isolation, durable retry and one-time issuance against migrated PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select, update
from sqlalchemy.engine import Connection as SqlConnection
from sqlalchemy.ext.asyncio import AsyncEngine

from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import ConfiguredOperatorBearerAuthenticator
from x.agentplane.action_service.catalog import ActionCatalog
from x.agentplane.action_service.connections import (
    ConnectionAuthority,
    ConnectionConflictError,
    GrantBinding,
    GrantRejectedError,
    GrantStatus,
    Identity,
    NewConnection,
    ReconnectConnection,
)
from x.agentplane.action_service.db import ActionStore, Base, EnrollmentRow, make_sessionmaker
from x.agentplane.action_service.enrollments import (
    ConfirmedReconnectConnection,
    EnrollmentAllow,
    EnrollmentAuthority,
    EnrollmentConflictError,
    EnrollmentDecisionResult,
    EnrollmentDeny,
    EnrollmentExpiredError,
    EnrollmentInput,
    EnrollmentPreviewInput,
    EnrollmentRejectedError,
)
from x.agentplane.action_service.models import Principal, PrincipalRole, Verdict
from x.agentplane.action_service.service import ActionService
from x.agentplane.action_service.updates import ActionUpdates
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator
from x.agentplane.sandbox_auth.principal import SandboxPrincipalResolver


@dataclass
class Consent:
    authority: EnrollmentAuthority
    connections: ConnectionAuthority
    request: EnrollmentInput
    handle: str
    browser: EnrollmentPreviewInput
    operator: Principal
    allow: EnrollmentAllow


@pytest.fixture
async def consent(engine: AsyncEngine) -> Consent:
    connections = ConnectionAuthority(
        make_sessionmaker(engine),
        {"test-personal": Identity(), "test-other": Identity(), "test-off": Identity(enabled=False)},
    )
    authority = EnrollmentAuthority(make_sessionmaker(engine), connections)
    request = EnrollmentInput(
        issuer="https://test-actions.example",
        client_id="test-client",
        client_name="Test client",
        redirect_uri="https://test-client.example/callback",
        code_challenge="test-pkce-challenge",
        upstream_url="https://test-idp.example/authorize?state=test-held-state",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    created = await authority.create(request)
    browser = EnrollmentPreviewInput(browser_binding="test-browser-" + "a" * 32)
    operator = Principal(issuer="configured-operator", subject="test-operator", role=PrincipalRole.OPERATOR)
    preview = await authority.preview(created.handle, browser, operator)
    allow = EnrollmentAllow(
        browser_binding=browser.browser_binding,
        expected_version=preview.version,
        idempotency_key="test-decision",
        connection=NewConnection(display_name="My test connection"),
        identity_id="test-personal",
    )
    return Consent(authority, connections, request, created.handle, browser, operator, allow)


async def test_consent_survives_replacement_and_creates_only_one_grant(consent: Consent, engine: AsyncEngine) -> None:
    assert await consent.connections.list() == []
    results = await asyncio.gather(
        *(consent.authority.decide(consent.handle, consent.allow, consent.operator) for _ in range(4))
    )
    assert all(
        result == EnrollmentDecisionResult(verdict=Verdict.ALLOW, redirect_url=consent.request.upstream_url)
        for result in results
    )
    assert await consent.connections.list() == []
    replacement = EnrollmentAuthority(make_sessionmaker(engine), consent.connections)
    assert await replacement.decide(consent.handle, consent.allow, consent.operator) == results[0]
    binding = await replacement.approved(
        client_id=consent.request.client_id,
        redirect_uri=consent.request.redirect_uri,
        code_challenge=consent.request.code_challenge,
        operator=consent.operator,
    )
    grant = await consent.connections.bind(binding)
    assert await consent.connections.bind(binding) == grant
    assert grant.identity_id == "test-personal"
    assert grant.client_id == consent.request.client_id
    assert grant.issuer == consent.request.issuer
    claims = await asyncio.gather(*(replacement.claim_exchange(grant.id) for _ in range(4)), return_exceptions=True)
    assert claims.count(None) == 1
    assert sum(isinstance(result, EnrollmentRejectedError) for result in claims) == 3
    with pytest.raises(EnrollmentRejectedError):
        await replacement.approved(
            client_id=consent.request.client_id,
            redirect_uri=consent.request.redirect_uri,
            code_challenge=consent.request.code_challenge,
            operator=consent.operator,
        )


async def test_browser_operator_and_caller_boundaries(consent: Consent) -> None:
    other_browser = EnrollmentPreviewInput(browser_binding="test-browser-" + "b" * 32)
    for operator, browser in [
        (consent.operator, other_browser),
        (consent.operator.model_copy(update={"subject": "test-other-operator"}), consent.browser),
        (consent.operator.model_copy(update={"role": PrincipalRole.CALLER}), consent.browser),
    ]:
        with pytest.raises(EnrollmentRejectedError):
            await consent.authority.preview(consent.handle, browser, operator)
        with pytest.raises(EnrollmentRejectedError):
            await consent.authority.decide(
                consent.handle, consent.allow.model_copy(update={"browser_binding": browser.browser_binding}), operator
            )
    await consent.authority.decide(consent.handle, consent.allow, consent.operator)
    with pytest.raises(EnrollmentRejectedError):
        await consent.authority.approved(
            client_id=consent.request.client_id,
            redirect_uri=consent.request.redirect_uri,
            code_challenge=consent.request.code_challenge,
            operator=consent.operator.model_copy(update={"issuer": "https://test-different-issuer.example"}),
        )


async def test_deny_and_conflicting_replay_never_release_upstream_url(consent: Consent) -> None:
    denied = EnrollmentDeny(
        browser_binding=consent.browser.browser_binding,
        expected_version=consent.allow.expected_version,
        idempotency_key="test-denial",
    )
    result = await consent.authority.decide(consent.handle, denied, consent.operator)
    assert result == EnrollmentDecisionResult(verdict=Verdict.DENY, redirect_url=None)
    assert await consent.authority.decide(consent.handle, denied, consent.operator) == result
    with pytest.raises(EnrollmentConflictError):
        await consent.authority.decide(consent.handle, consent.allow, consent.operator)
    with pytest.raises(EnrollmentRejectedError):
        await consent.authority.approved(
            client_id=consent.request.client_id,
            redirect_uri=consent.request.redirect_uri,
            code_challenge=consent.request.code_challenge,
            operator=consent.operator,
        )
    assert await consent.connections.list() == []


async def test_picker_rejects_disabled_and_missing_identities(consent: Consent, engine: AsyncEngine) -> None:
    for identity in ["test-off", "test-missing"]:
        with pytest.raises(EnrollmentRejectedError):
            await consent.authority.decide(
                consent.handle, consent.allow.model_copy(update={"identity_id": identity}), consent.operator
            )
    await consent.authority.decide(consent.handle, consent.allow, consent.operator)
    disabled = EnrollmentAuthority(
        make_sessionmaker(engine),
        ConnectionAuthority(make_sessionmaker(engine), {"test-personal": Identity(enabled=False)}),
    )
    with pytest.raises(EnrollmentRejectedError):
        await disabled.approved(
            client_id=consent.request.client_id,
            redirect_uri=consent.request.redirect_uri,
            code_challenge=consent.request.code_challenge,
            operator=consent.operator,
        )


async def test_correlations_do_not_merge_concurrent_tabs_or_revive_expired_consent(
    consent: Consent, engine: AsyncEngine
) -> None:
    with pytest.raises(EnrollmentConflictError):
        await consent.authority.create(consent.request)
    second = await consent.authority.create(consent.request.model_copy(update={"code_challenge": "test-other-pkce"}))
    assert second.handle != consent.handle
    await consent.authority.preview(
        second.handle, EnrollmentPreviewInput(browser_binding="test-other-browser-" + "c" * 32), consent.operator
    )
    async with make_sessionmaker(engine).begin() as db:
        await db.execute(update(EnrollmentRow).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    with pytest.raises(EnrollmentExpiredError):
        await consent.authority.preview(consent.handle, consent.browser, consent.operator)
    with pytest.raises(EnrollmentExpiredError):
        await consent.authority.decide(consent.handle, consent.allow, consent.operator)
    with pytest.raises(EnrollmentConflictError):
        await consent.authority.create(consent.request)


async def test_unapproved_or_stale_consent_cannot_exchange(consent: Consent) -> None:
    with pytest.raises(EnrollmentRejectedError):
        await consent.authority.approved(
            client_id=consent.request.client_id,
            redirect_uri=consent.request.redirect_uri,
            code_challenge=consent.request.code_challenge,
            operator=consent.operator,
        )
    with pytest.raises(EnrollmentConflictError):
        await consent.authority.decide(
            consent.handle, consent.allow.model_copy(update={"expected_version": 999}), consent.operator
        )
    assert await consent.connections.list() == []


async def test_preview_does_not_return_oauth_secrets_and_handles_are_hashed(
    consent: Consent, engine: AsyncEngine
) -> None:
    preview = await consent.authority.preview(consent.handle, consent.browser, consent.operator)
    payload = preview.model_dump_json()
    for secret in [
        consent.request.upstream_url,
        consent.request.code_challenge,
        consent.browser.browser_binding,
        consent.handle,
    ]:
        assert secret not in payload
    async with make_sessionmaker(engine)() as db:
        row = (await db.scalars(select(EnrollmentRow))).one()
        assert row.handle_hash == hashlib.sha256(consent.handle.encode()).hexdigest()
        assert row.browser_hash == hashlib.sha256(consent.browser.browser_binding.encode()).hexdigest()


async def test_operator_routes_require_auth_and_reject_redirect_injection(
    consent: Consent, engine: AsyncEngine, db_url: str
) -> None:
    token = "test-operator-token"
    catalog = ActionCatalog(groups={})
    actions = ActionService(ActionStore(make_sessionmaker(engine)), catalog, {})
    app = create_app(
        actions,
        SandboxPrincipalAuthenticator(Mock(spec=SandboxPrincipalResolver)),
        ConfiguredOperatorBearerAuthenticator(
            token_digest=hashlib.sha256(token.encode()).digest(), subject="test-operator"
        ),
        catalog,
        updates=ActionUpdates(db_url),
        connections=consent.connections,
        enrollments=consent.authority,
    )
    path = f"/v1/operator/connection-enrollments/{consent.handle}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test-actions") as http:
        assert (await http.post(f"{path}/preview", json=consent.browser.model_dump())).status_code == 401
        http.headers["Authorization"] = f"Bearer {token}"
        assert (await http.post(f"{path}/preview", json=consent.browser.model_dump())).status_code == 200
        injected = {**consent.allow.model_dump(), "redirect_url": "https://test-attacker.example"}
        assert (await http.post(f"{path}/decision", json=injected)).status_code == 422
        result = await http.post(f"{path}/decision", json=consent.allow.model_dump())
        assert result.status_code == 200
        assert EnrollmentDecisionResult.model_validate(result.json()).redirect_url == consent.request.upstream_url
        assert (await http.post("/v1/operator/connection-enrollments", json={})).status_code == 404


def _schema_matches(connection: SqlConnection) -> None:
    context = MigrationContext.configure(
        connection,
        opts={
            "include_object": lambda obj, name, type_, reflected, compare_to: (
                type_ != "table" or name == "connection_enrollment"
            )
        },
    )
    assert compare_metadata(context, Base.metadata) == []


async def test_enrollment_migration_matches_metadata(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_schema_matches)


@pytest.mark.parametrize("identity", ["test-personal", "test-other"])
async def test_reconnect_consent_preserves_selection_and_revokes_before_activation(
    consent: Consent, engine: AsyncEngine, identity: str
) -> None:
    old = await consent.connections.bind(
        GrantBinding(
            grant_id=uuid4(),
            identity_id="test-personal",
            issuer=consent.request.issuer,
            client_id="test-old-registration",
            activation_deadline=consent.request.expires_at,
            connection=NewConnection(display_name="Existing test Connection"),
        )
    )
    old = await consent.connections.activate(old.id)
    connection = await consent.connections.get(old.connection_id)
    selection = ConfirmedReconnectConnection(
        connection_id=connection.id, expected_version=connection.version, authority_change_confirmed=True
    )
    allow = consent.allow.model_copy(update={"identity_id": identity, "connection": selection})
    result = await consent.authority.decide(consent.handle, allow, consent.operator)
    assert await consent.connections.resolve(old.id, issuer=old.issuer, client_id=old.client_id) == old
    replacement = EnrollmentAuthority(make_sessionmaker(engine), consent.connections)
    assert await replacement.decide(consent.handle, allow, consent.operator) == result
    binding = await replacement.approved(
        client_id=consent.request.client_id,
        redirect_uri=consent.request.redirect_uri,
        code_challenge=consent.request.code_challenge,
        operator=consent.operator,
    )
    assert binding.connection == ReconnectConnection(connection_id=connection.id, expected_version=connection.version)
    new = await consent.connections.bind(binding)
    assert new.status == GrantStatus.PENDING
    assert new.identity_id == identity
    assert new.connection_id == old.connection_id
    assert new.revision == old.revision + 1
    assert await consent.connections.bind(binding) == new
    with pytest.raises(GrantRejectedError):
        await consent.connections.resolve(old.id, issuer=old.issuer, client_id=old.client_id)
    # A failed issuance never restores the old authorization, even after restart/retry.
    await consent.connections.revoke(new.id)
    with pytest.raises(GrantRejectedError):
        await consent.connections.activate(new.id)
    assert await replacement.decide(consent.handle, allow, consent.operator) == result
    final = await consent.connections.get(connection.id)
    assert final.display_name == connection.display_name
    assert all(grant.status == GrantStatus.REVOKED for grant in final.grants)
    assert final.grants[0].provenance() == old.provenance()


async def test_reconnect_rechecks_version_after_consent_and_serializes_competing_grants(consent: Consent) -> None:
    old = await consent.connections.bind(
        GrantBinding(
            grant_id=uuid4(),
            identity_id="test-personal",
            issuer=consent.request.issuer,
            client_id="test-old-registration",
            activation_deadline=consent.request.expires_at,
            connection=NewConnection(display_name="Existing test Connection"),
        )
    )
    old = await consent.connections.activate(old.id)
    connection = await consent.connections.get(old.connection_id)
    allow = consent.allow.model_copy(
        update={
            "connection": ConfirmedReconnectConnection(
                connection_id=connection.id, expected_version=connection.version, authority_change_confirmed=True
            )
        }
    )
    stale = allow.model_copy(update={"connection": allow.connection.model_copy(update={"expected_version": 999})})
    with pytest.raises(ConnectionConflictError):
        await consent.authority.decide(consent.handle, stale, consent.operator)
    await consent.authority.decide(consent.handle, allow, consent.operator)
    binding = await consent.authority.approved(
        client_id=consent.request.client_id,
        redirect_uri=consent.request.redirect_uri,
        code_challenge=consent.request.code_challenge,
        operator=consent.operator,
    )
    renamed = await consent.connections.rename(
        connection.id, expected_version=connection.version, display_name="Reviewed again"
    )
    with pytest.raises(ConnectionConflictError):
        await consent.connections.bind(binding)
    assert await consent.connections.resolve(old.id, issuer=old.issuer, client_id=old.client_id) == old
    # Independent, freshly reviewed consents may race; the Connection version admits only one.
    fresh = binding.model_copy(
        update={"connection": ReconnectConnection(connection_id=connection.id, expected_version=renamed.version)}
    )
    competing = fresh.model_copy(update={"grant_id": uuid4()})
    results = await asyncio.gather(
        consent.connections.bind(fresh), consent.connections.bind(competing), return_exceptions=True
    )
    assert sum(isinstance(result, ConnectionConflictError) for result in results) == 1
    assert len((await consent.connections.get(connection.id)).grants) == 2


if __name__ == "__main__":
    pytest_bazel.main()
