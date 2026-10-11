"""Acceptance tests for canonical Operator identity resolution."""

from __future__ import annotations

import asyncio

import pytest
import pytest_bazel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.config import OperatorIdentityConfig
from haku.console.database_schema import IdentityAnchor, OidcIdentity, Operator
from haku.console.identity.operator_identity import (
    InactiveOperatorError,
    OperatorIdentityTrust,
    OperatorStatus,
    UntrustedOidcIssuerError,
    VerifiedExternalIdentity,
)
from haku.console.identity.operator_identity_store import PostgresOperatorIdentityStore

_TRUST_DOMAIN = "auth.test/authentik-user-id/v1"
_BROWSER_ISSUER = "https://auth.test/application/o/haku-console/"
_MCP_ISSUER = "https://auth.test/application/o/haku-console-mcp/"


def _store(sessions: async_sessionmaker[AsyncSession]) -> PostgresOperatorIdentityStore:
    return PostgresOperatorIdentityStore(
        sessions,
        OperatorIdentityTrust(trust_domain=_TRUST_DOMAIN, trusted_issuers=frozenset({_BROWSER_ISSUER, _MCP_ISSUER})),
    )


async def test_exact_trusted_issuers_converge_and_equal_untrusted_subject_is_rejected(
    migrated_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = _store(migrated_sessions)
    browser = await store.resolve_verified_identity(
        VerifiedExternalIdentity(issuer=_BROWSER_ISSUER, subject="authentik-user-42")
    )
    mcp = await store.resolve_verified_identity(
        VerifiedExternalIdentity(issuer=_MCP_ISSUER, subject="authentik-user-42")
    )

    assert browser.operator_id == mcp.operator_id
    assert browser.identity_id != mcp.identity_id
    with pytest.raises(UntrustedOidcIssuerError):
        await store.resolve_verified_identity(
            VerifiedExternalIdentity(
                issuer="https://attacker.invalid/application/o/lookalike/", subject="authentik-user-42"
            )
        )


async def test_concurrent_first_contact_creates_one_operator_and_anchor(
    migrated_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async def resolve(issuer: str) -> tuple[object, object]:
        identity = await _store(migrated_sessions).resolve_verified_identity(
            VerifiedExternalIdentity(issuer=issuer, subject="concurrent-user")
        )
        return identity.operator_id, identity.identity_id

    issuers = [_BROWSER_ISSUER, _MCP_ISSUER] * 8
    results = await asyncio.gather(*(resolve(issuer) for issuer in issuers))

    assert len({operator_id for operator_id, _ in results}) == 1
    assert len({identity_id for _, identity_id in results}) == 2
    async with migrated_sessions() as session:
        assert (await session.execute(select(func.count()).select_from(Operator))).scalar_one() == 1
        assert (await session.execute(select(func.count()).select_from(IdentityAnchor))).scalar_one() == 1
        assert (await session.execute(select(func.count()).select_from(OidcIdentity))).scalar_one() == 2


async def test_disabled_operator_invalidates_session_static_and_resolution_paths(
    migrated_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = _store(migrated_sessions)
    identity = await store.resolve_verified_identity(
        VerifiedExternalIdentity(issuer=_BROWSER_ISSUER, subject="disabled-user")
    )
    async with migrated_sessions.begin() as session:
        operator = await session.get(Operator, identity.operator_id)
        assert operator is not None
        operator.status = OperatorStatus.DISABLED

    assert (
        await store.resolve_active_session(operator_id=identity.operator_id, identity_id=identity.identity_id) is None
    )
    assert not await store.is_active(identity.operator_id)
    with pytest.raises(InactiveOperatorError):
        await store.resolve_configured_external_user_key("disabled-user")


async def test_list_active_ids_is_scoped_to_current_trust_domain(
    migrated_sessions: async_sessionmaker[AsyncSession],
) -> None:
    current_store = _store(migrated_sessions)
    current = await current_store.resolve_configured_external_user_key("current-user")
    other_store = PostgresOperatorIdentityStore(
        migrated_sessions,
        OperatorIdentityTrust(trust_domain="auth.test/other/v1", trusted_issuers=frozenset({_BROWSER_ISSUER})),
    )
    await other_store.resolve_configured_external_user_key("other-user")

    assert await current_store.list_active_ids() == [current]


async def test_existing_authority_fails_closed_when_current_trust_changes(
    migrated_sessions: async_sessionmaker[AsyncSession],
) -> None:
    identity = await _store(migrated_sessions).resolve_verified_identity(
        VerifiedExternalIdentity(issuer=_BROWSER_ISSUER, subject="trust-rotation-user")
    )
    changed_issuer_store = PostgresOperatorIdentityStore(
        migrated_sessions, OperatorIdentityTrust(trust_domain=_TRUST_DOMAIN, trusted_issuers=frozenset({_MCP_ISSUER}))
    )
    assert (
        await changed_issuer_store.resolve_active_session(
            operator_id=identity.operator_id, identity_id=identity.identity_id
        )
        is None
    )
    # Static/DCR ownership is anchored to the deployment trust domain, not a particular OIDC
    # adapter, so removing the browser issuer revokes the browser session without orphaning it.
    assert await changed_issuer_store.is_active(identity.operator_id)

    changed_domain_store = PostgresOperatorIdentityStore(
        migrated_sessions,
        OperatorIdentityTrust(
            trust_domain="auth.test/authentik-user-id/v2", trusted_issuers=frozenset({_BROWSER_ISSUER, _MCP_ISSUER})
        ),
    )
    assert (
        await changed_domain_store.resolve_active_session(
            operator_id=identity.operator_id, identity_id=identity.identity_id
        )
        is None
    )
    assert not await changed_domain_store.is_active(identity.operator_id)


def test_identity_config_rejects_empty_trust_domain() -> None:
    with pytest.raises(ValueError, match="trust_domain"):
        OperatorIdentityConfig(trust_domain="   ")


if __name__ == "__main__":
    pytest_bazel.main()
