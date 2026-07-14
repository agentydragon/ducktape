"""Acceptance tests for the one-way canonical Operator identity cutover."""

from __future__ import annotations

import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import pytest_bazel
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from haku.console.config import OperatorIdentityConfig
from haku.console.database_migrate import apply_migrations
from haku.console.database_schema import metadata
from haku.console.mcp_approval import PostgresToolCallLedger
from haku.console.mcp_config import McpServerEntry
from haku.console.operator_identity import (
    InactiveOperatorError,
    OperatorIdentityTrust,
    UntrustedOidcIssuerError,
    VerifiedExternalIdentity,
)
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.tool_call_actor import AgentActor
from haku.console.tool_calls import SubmitToolCallRequest

_TRUST_DOMAIN = "auth.test/authentik-user-id/v1"
_BROWSER_ISSUER = "https://auth.test/application/o/haku-console/"
_MCP_ISSUER = "https://auth.test/application/o/haku-console-mcp/"
_FASTMCP_STATE_TABLE = "canonical_operator_cutover_oauth_state"
_FASTMCP_COLLECTIONS = (
    "mcp-upstream-tokens",
    "mcp-oauth-proxy-clients",
    "mcp-oauth-transactions",
    "mcp-authorization-codes",
    "mcp-jti-mappings",
    "mcp-refresh-tokens",
    "future-fastmcp-collection",
)


def _store(database_url: str) -> PostgresOperatorIdentityStore:
    return PostgresOperatorIdentityStore(
        database_url,
        OperatorIdentityTrust(trust_domain=_TRUST_DOMAIN, trusted_issuers=frozenset({_BROWSER_ISSUER, _MCP_ISSUER})),
    )


def _alembic_config(conn: object, *, seeds: tuple[tuple[str, str], ...] = ()) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.attributes["connection"] = conn
    cfg.attributes["target_metadata"] = metadata
    cfg.attributes["operator_identity_seeds"] = seeds
    return cfg


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
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM operators")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM identity_anchors")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM oidc_identities")).scalar_one() == 2
    finally:
        engine.dispose()


def test_disabled_operator_invalidates_session_static_and_resolution_paths(migrated_db_url: str) -> None:
    store = _store(migrated_db_url)
    identity = store.resolve_verified_identity(
        VerifiedExternalIdentity(issuer=_BROWSER_ISSUER, subject="disabled-user")
    )
    engine = create_engine(migrated_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE operators SET status = 'disabled' WHERE operator_id = :operator_id"),
                {"operator_id": identity.operator_id},
            )
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


def test_duplicate_static_agent_owner_seed_creates_one_operator(db_url: str) -> None:
    apply_migrations(
        db_url,
        operator_identity_seeds=[(_TRUST_DOMAIN, "shared-owner"), (_TRUST_DOMAIN, "shared-owner")],
        # FastMCP creates this table lazily after migrations on a fresh database. A configured but
        # not-yet-existing table is therefore a normal startup state, not a migration failure.
        fastmcp_oauth_state_table="not_created_yet",
    )
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM operators")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM identity_anchors")).scalar_one() == 1
    finally:
        engine.dispose()


def test_0008_preserves_only_exact_seeded_durable_rows_and_drops_ephemeral_state(db_url: str) -> None:
    engine = create_engine(db_url)
    exact_key = "  opaque-authentik-id  "
    try:
        with engine.begin() as conn:
            alembic_command.upgrade(_alembic_config(conn), "0007")
            for server_id, owner in (("kept", exact_key), ("dropped", "somebody-else")):
                conn.execute(
                    text(
                        """
                        INSERT INTO mcp_operator_oauth_associations (
                            server_id, operator_subject, created_at, updated_at, client_id,
                            token_endpoint, access_token, token_type
                        ) VALUES (
                            :server_id, :owner, now(), now(), 'client',
                            'https://auth.test/token', 'access', 'Bearer'
                        )
                        """
                    ),
                    {"server_id": server_id, "owner": owner},
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO mcp_agent_operator (
                            agent_dcr_client_id, operator_subject, created_at, updated_at
                        ) VALUES (:client_id, :owner, now(), now())
                        """
                    ),
                    {"client_id": f"dcr-{server_id}", "owner": owner},
                )
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_operator_oauth_flows (
                        state, server_id, operator_subject, created_at, expires_at, redirect_uri,
                        code_verifier, client_id, token_endpoint
                    ) VALUES (
                        'flow', 'kept', :owner, now(), now() + interval '10 minutes',
                        'https://haku.test/callback', 'verifier', 'client', 'https://auth.test/token'
                    )
                    """
                ),
                {"owner": exact_key},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_calls (
                        tool_call_id, operator_subject, server_id, tool_name, caller_principal,
                        status, created_at, updated_at, arguments_json, rationale
                    ) VALUES (
                        'legacy-call', :owner, 'kept', 'tool', 'agent',
                        'pending_approval', now(), now(), '{}'::jsonb, 'legacy'
                    )
                    """
                ),
                {"owner": exact_key},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_call_events (
                        event_type, operator_subject, tool_call_id, status, created_at
                    ) VALUES (
                        'tool_call_submitted', :owner, 'legacy-call', 'pending_approval', now()
                    )
                    """
                ),
                {"owner": exact_key},
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE canonical_operator_cutover_oauth_state (
                        collection TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value JSONB NOT NULL,
                        ttl DOUBLE PRECISION,
                        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMPTZ,
                        PRIMARY KEY (collection, key)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE INDEX idx_canonical_operator_cutover_oauth_state_expires_at
                    ON canonical_operator_cutover_oauth_state (expires_at)
                    WHERE expires_at IS NOT NULL
                    """
                )
            )
            for collection in _FASTMCP_COLLECTIONS:
                conn.execute(
                    text(
                        """
                        INSERT INTO canonical_operator_cutover_oauth_state (collection, key, value)
                        VALUES (:collection, :key, '{}'::jsonb)
                        """
                    ),
                    {"collection": collection, "key": f"{collection}-key"},
                )

        apply_migrations(
            db_url,
            operator_identity_seeds=((_TRUST_DOMAIN, exact_key),),
            fastmcp_oauth_state_table=_FASTMCP_STATE_TABLE,
        )

        with engine.connect() as conn:
            anchors = (
                conn.execute(
                    text(
                        """
                    SELECT anchor.operator_id, anchor.stable_external_user_key
                    FROM identity_anchors AS anchor
                    """
                    )
                )
                .mappings()
                .all()
            )
            associations = (
                conn.execute(
                    text(
                        """
                    SELECT server_id, operator_id, association_id, token_revision
                    FROM mcp_operator_oauth_associations
                    """
                    )
                )
                .mappings()
                .all()
            )
            links = (
                conn.execute(text("SELECT agent_dcr_client_id, operator_id FROM mcp_agent_operator")).mappings().all()
            )
            fastmcp_state_count = conn.execute(
                text("SELECT count(*) FROM canonical_operator_cutover_oauth_state")
            ).scalar_one()
            counts = (
                conn.execute(
                    text(
                        """
                    SELECT
                        (SELECT count(*) FROM oidc_identities) AS identities,
                        (SELECT count(*) FROM mcp_operator_oauth_flows) AS flows,
                        (SELECT count(*) FROM mcp_tool_calls) AS calls,
                        (SELECT count(*) FROM mcp_tool_call_events) AS events
                    """
                    )
                )
                .mappings()
                .one()
            )

        assert len(anchors) == 1
        assert anchors[0]["stable_external_user_key"] == exact_key
        assert len(associations) == 1
        assert associations[0]["server_id"] == "kept"
        assert associations[0]["operator_id"] == anchors[0]["operator_id"]
        assert associations[0]["association_id"] is not None
        assert associations[0]["token_revision"] == 0
        assert links == []
        assert fastmcp_state_count == 0
        assert counts == {"identities": 0, "flows": 0, "calls": 0, "events": 0}
    finally:
        engine.dispose()


def test_tool_call_event_owner_must_match_owning_call(migrated_db_url: str) -> None:
    store = _store(migrated_db_url)
    owner = store.resolve_configured_external_user_key("call-owner")
    other = store.resolve_configured_external_user_key("other-owner")
    ledger = PostgresToolCallLedger(migrated_db_url)
    record, _, _ = ledger.submit(
        server=McpServerEntry(id="server"),
        req=SubmitToolCallRequest(
            server_id="server", tool_name="tool", arguments={}, rationale="constraint test", wait_for_ms=0
        ),
        actor=AgentActor(principal="agent", operator_id=owner),
    )
    engine = create_engine(migrated_db_url)
    try:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_call_events (
                        event_type, operator_id, tool_call_id, status, created_at
                    ) VALUES (
                        'tool_call_updated', :operator_id, :tool_call_id, 'pending_approval', :created_at
                    )
                    """
                ),
                {
                    "operator_id": other,
                    "tool_call_id": record.tool_call_id,
                    "created_at": datetime.datetime.now(datetime.UTC),
                },
            )
    finally:
        engine.dispose()


def test_identity_config_rejects_empty_trust_domain() -> None:
    with pytest.raises(ValueError, match="trust_domain"):
        OperatorIdentityConfig(trust_domain="   ")


if __name__ == "__main__":
    pytest_bazel.main()
