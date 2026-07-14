"""Acceptance tests for canonical Operator identity resolution."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
import pytest_bazel
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from haku.console.config import OperatorIdentityConfig
from haku.console.database_schema import IdentityAnchor, OidcIdentity, Operator
from haku.console.operator_identity import (
    InactiveOperatorError,
    OperatorIdentityTrust,
    OperatorStatus,
    UntrustedOidcIssuerError,
    VerifiedExternalIdentity,
)
from haku.console.operator_identity_store import PostgresOperatorIdentityStore

_TRUST_DOMAIN = "auth.test/authentik-user-id/v1"
_BROWSER_ISSUER = "https://auth.test/application/o/haku-console/"
_MCP_ISSUER = "https://auth.test/application/o/haku-console-mcp/"


def _store(database_url: str) -> PostgresOperatorIdentityStore:
    return PostgresOperatorIdentityStore(
        database_url,
        OperatorIdentityTrust(trust_domain=_TRUST_DOMAIN, trusted_issuers=frozenset({_BROWSER_ISSUER, _MCP_ISSUER})),
    )


def test_exact_trusted_issuers_converge_and_equal_untrusted_subject_is_rejected(migrated_db_url: str) -> None:
    store = _store(migrated_db_url)
    browser = store.resolve_verified_identity(
        VerifiedExternalIdentity(issuer=_BROWSER_ISSUER, subject="authentik-user-42")
    )
    mcp = store.resolve_verified_identity(VerifiedExternalIdentity(issuer=_MCP_ISSUER, subject="authentik-user-42"))

    assert browser.operator_id == mcp.operator_id
    assert browser.identity_id != mcp.identity_id
    with pytest.raises(UntrustedOidcIssuerError):
        store.resolve_verified_identity(
            VerifiedExternalIdentity(
                issuer="https://attacker.invalid/application/o/lookalike/", subject="authentik-user-42"
            )
        )


def test_concurrent_first_contact_creates_one_operator_and_anchor(migrated_db_url: str) -> None:
    def resolve(issuer: str) -> tuple[object, object]:
        identity = _store(migrated_db_url).resolve_verified_identity(
            VerifiedExternalIdentity(issuer=issuer, subject="concurrent-user")
        )
        return identity.operator_id, identity.identity_id

    issuers = [_BROWSER_ISSUER, _MCP_ISSUER] * 8
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(resolve, issuers))

    assert len({operator_id for operator_id, _ in results}) == 1
    assert len({identity_id for _, identity_id in results}) == 2
    engine = create_engine(migrated_db_url)
    try:
        with Session(engine) as session:
            assert session.execute(select(func.count()).select_from(Operator)).scalar_one() == 1
            assert session.execute(select(func.count()).select_from(IdentityAnchor)).scalar_one() == 1
            assert session.execute(select(func.count()).select_from(OidcIdentity)).scalar_one() == 2
    finally:
        engine.dispose()


def test_disabled_operator_invalidates_session_static_and_resolution_paths(migrated_db_url: str) -> None:
    store = _store(migrated_db_url)
    identity = store.resolve_verified_identity(
        VerifiedExternalIdentity(issuer=_BROWSER_ISSUER, subject="disabled-user")
    )
    engine = create_engine(migrated_db_url)
    try:
        with Session(engine) as session, session.begin():
            operator = session.get(Operator, identity.operator_id)
            assert operator is not None
            operator.status = OperatorStatus.DISABLED
    finally:
        engine.dispose()

    assert store.resolve_active_session(operator_id=identity.operator_id, identity_id=identity.identity_id) is None
    assert not store.is_active(identity.operator_id)
    with pytest.raises(InactiveOperatorError):
        store.resolve_configured_external_user_key("disabled-user")


def test_existing_authority_fails_closed_when_current_trust_changes(migrated_db_url: str) -> None:
    identity = _store(migrated_db_url).resolve_verified_identity(
        VerifiedExternalIdentity(issuer=_BROWSER_ISSUER, subject="trust-rotation-user")
    )
    changed_issuer_store = PostgresOperatorIdentityStore(
        migrated_db_url, OperatorIdentityTrust(trust_domain=_TRUST_DOMAIN, trusted_issuers=frozenset({_MCP_ISSUER}))
    )
    assert (
        changed_issuer_store.resolve_active_session(operator_id=identity.operator_id, identity_id=identity.identity_id)
        is None
    )
    # Static/DCR ownership is anchored to the deployment trust domain, not a particular OIDC
    # adapter, so removing the browser issuer revokes the browser session without orphaning it.
    assert changed_issuer_store.is_active(identity.operator_id)

    changed_domain_store = PostgresOperatorIdentityStore(
        migrated_db_url,
        OperatorIdentityTrust(
            trust_domain="auth.test/authentik-user-id/v2", trusted_issuers=frozenset({_BROWSER_ISSUER, _MCP_ISSUER})
        ),
    )
    assert (
        changed_domain_store.resolve_active_session(operator_id=identity.operator_id, identity_id=identity.identity_id)
        is None
    )
    assert not changed_domain_store.is_active(identity.operator_id)
    with pytest.raises(InactiveOperatorError):
        changed_domain_store.require_active(identity.operator_id)


def test_identity_config_rejects_empty_trust_domain() -> None:
    with pytest.raises(ValueError, match="trust_domain"):
        OperatorIdentityConfig(trust_domain="   ")


if __name__ == "__main__":
    pytest_bazel.main()
