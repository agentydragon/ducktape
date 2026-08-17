"""Postgres acceptance tests for the canonical Agent authority schema.

Raw SQL is intentional here: this suite exercises migration states, physical PostgreSQL
schema objects, deferred constraints, triggers, and invalid rows rather than domain setup.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from haku.console.database_migrate import apply_migrations
from haku.console.database_schema import UNMAPPED_COLUMNS_PENDING_DROP, UNMAPPED_TABLES_PENDING_DROP, metadata
from third_party.containers.rlocations import PGVECTOR_PG18
from util.testing.postgres import create_database_sync, force_drop_database_sync
from util.testing.postgres_fixtures import start_postgres_container


@pytest.fixture(scope="session")
def postgres_container() -> Any:
    """Postgres **with pgvector**: migration 0037 creates `vector` columns, and this file migrates
    a fresh database to head. The deployed database gets the extension from CNPG's `Database` CR."""
    container = start_postgres_container(PGVECTOR_PG18)
    try:
        yield container
    finally:
        container.stop()


_UTC = datetime.UTC


@dataclass(frozen=True)
class IdentityIds:
    operator_id: UUID
    anchor_id: UUID
    browser_identity_id: UUID
    mcp_identity_id: UUID


@dataclass(frozen=True)
class AgentGraph:
    interaction_id: UUID
    client_software_id: UUID
    reservation_id: UUID
    agent_id: UUID
    binding_id: UUID
    grant_id: UUID
    identity: IdentityIds


@dataclass(frozen=True)
class StaticAgent:
    reservation_id: UUID
    agent_id: UUID
    binding_id: UUID


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: Any) -> str:
    host = postgres_container.get_container_host_ip()
    port = int(postgres_container.get_exposed_port(5432))
    return f"postgresql+psycopg://postgres:postgres@{host}:{port}/postgres"


@pytest.fixture
def db_url(postgres_admin_url: str, request: pytest.FixtureRequest) -> Any:
    suffix = uuid4().hex[:8]
    base = re.sub(r"[^a-z0-9_]", "_", request.node.name.lower())[:35].rstrip("_")
    db_name = f"{base or 'agent_schema'}_{suffix}"
    db_url = create_database_sync(postgres_admin_url, db_name, extensions=("vector",))

    yield db_url

    force_drop_database_sync(postgres_admin_url, db_name)


def _now() -> datetime.datetime:
    return datetime.datetime.now(_UTC)


def _seed_identity(conn: Connection, label: str) -> IdentityIds:
    ids = IdentityIds(operator_id=uuid4(), anchor_id=uuid4(), browser_identity_id=uuid4(), mcp_identity_id=uuid4())
    now = _now()
    conn.execute(
        text(
            """
            INSERT INTO operators (operator_id, status, created_at, updated_at)
            VALUES (:operator_id, 'active', :now, :now)
            """
        ),
        {"operator_id": ids.operator_id, "now": now},
    )
    conn.execute(
        text(
            """
            INSERT INTO identity_anchors (
                anchor_id, operator_id, trust_domain, stable_external_user_key,
                created_at, updated_at
            ) VALUES (
                :anchor_id, :operator_id, 'test/authentik-user-id/v1', :external_key,
                :now, :now
            )
            """
        ),
        {
            "anchor_id": ids.anchor_id,
            "operator_id": ids.operator_id,
            "external_key": f"external-{label}-{uuid4().hex}",
            "now": now,
        },
    )
    for identity_id, issuer in (
        (ids.browser_identity_id, "https://auth.test/browser/"),
        (ids.mcp_identity_id, "https://auth.test/mcp/"),
    ):
        conn.execute(
            text(
                """
                INSERT INTO oidc_identities (
                    identity_id, anchor_id, issuer, subject, first_seen_at, last_seen_at
                ) VALUES (:identity_id, :anchor_id, :issuer, :subject, :now, :now)
                """
            ),
            {
                "identity_id": identity_id,
                "anchor_id": ids.anchor_id,
                "issuer": issuer,
                "subject": f"subject-{label}-{identity_id}",
                "now": now,
            },
        )
    return ids


def _seed_client(conn: Connection, label: str) -> tuple[UUID, str, str]:
    client_software_id = uuid4()
    client_id = f"client-{label}-{uuid4()}"
    redirect_uri = f"https://{label}.client.test/oauth/callback"
    now = _now()
    conn.execute(
        text(
            """
            INSERT INTO client_software (
                client_software_id, registration_kind, oauth_client_id,
                validated_redirect_uris, metadata_hash, observed_name, observed_icon_uri,
                created_at, updated_at
            ) VALUES (
                :client_software_id, 'dcr', :client_id,
                :redirect_uris, :metadata_hash, :observed_name, NULL, :now, :now
            )
            """
        ),
        {
            "client_software_id": client_software_id,
            "client_id": client_id,
            "redirect_uris": [redirect_uri],
            "metadata_hash": b"metadata-hash",
            "observed_name": f"Client {label}",
            "now": now,
        },
    )
    return client_software_id, client_id, redirect_uri


def _start_interaction(
    engine: Engine, *, label: str, client_software_id: UUID, client_id: str, redirect_uri: str
) -> UUID:
    interaction_id = uuid4()
    now = _now()
    expires_at = now + datetime.timedelta(minutes=10)
    release_after = now + datetime.timedelta(hours=2)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO enrollment_interactions (
                    interaction_id, client_software_id, client_id, redirect_uri,
                    code_challenge, requested_scopes, presentation_snapshot,
                    upstream_authorization_url, phase, expires_at,
                    correlation_release_after, browser_nonce_digest, created_at, updated_at
                ) VALUES (
                    :interaction_id, :client_software_id, :client_id, :redirect_uri,
                    :code_challenge, :requested_scopes, '{}'::jsonb,
                    :upstream_url, 'awaiting_browser', :expires_at,
                    :release_after, :browser_nonce, :now, :now
                )
                """
            ),
            {
                "interaction_id": interaction_id,
                "client_software_id": client_software_id,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": f"challenge-{label}-{uuid4()}",
                "requested_scopes": ["tools:call", "tools:list"],
                "upstream_url": f"https://auth.test/authorize/{label}",
                "expires_at": expires_at,
                "release_after": release_after,
                "browser_nonce": b"browser-nonce",
                "now": now,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO enrollment_correlation_reservations (
                    interaction_id, client_id, redirect_uri, code_challenge, release_after
                )
                SELECT interaction_id, client_id, redirect_uri, code_challenge,
                       correlation_release_after
                FROM enrollment_interactions WHERE interaction_id = :interaction_id
                """
            ),
            {"interaction_id": interaction_id},
        )
    return interaction_id


def _bind_browser(engine: Engine, interaction_id: UUID, browser_identity_id: UUID) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE enrollment_interactions
                SET phase = 'awaiting_approval', browser_nonce_digest = NULL,
                    browser_identity_id = :browser_identity_id,
                    browser_binding_digest = :browser_binding_digest,
                    updated_at = :now
                WHERE interaction_id = :interaction_id
                """
            ),
            {
                "browser_identity_id": browser_identity_id,
                "browser_binding_digest": b"browser-binding",
                "interaction_id": interaction_id,
                "now": _now(),
            },
        )


def _allow_create(engine: Engine, interaction_id: UUID, *, display_name: str, display_name_key: str) -> UUID:
    reservation_id = uuid4()
    now = _now()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE enrollment_interactions
                SET phase = 'allowed', decision_digest = :decision_digest, updated_at = :now
                WHERE interaction_id = :interaction_id
                """
            ),
            {"decision_digest": b"allow-decision", "interaction_id": interaction_id, "now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO agent_name_reservations (
                    reservation_id, display_name, display_name_key,
                    originating_interaction_id, pending_interaction_id, created_at
                ) VALUES (
                    :reservation_id, :display_name, :display_name_key,
                    :interaction_id, :interaction_id, :now
                )
                """
            ),
            {
                "reservation_id": reservation_id,
                "display_name": display_name,
                "display_name_key": display_name_key,
                "interaction_id": interaction_id,
                "now": now,
            },
        )
    return reservation_id


def _exchange_create(
    engine: Engine,
    *,
    interaction_id: UUID,
    client_software_id: UUID,
    reservation_id: UUID,
    identity: IdentityIds,
    allowed_scopes: list[str] | None = None,
) -> AgentGraph:
    agent_id = uuid4()
    binding_id = uuid4()
    grant_id = uuid4()
    now = _now()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE enrollment_interactions
                SET phase = 'exchanging', updated_at = :now
                WHERE interaction_id = :interaction_id
                """
            ),
            {"interaction_id": interaction_id, "now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO agents (
                    agent_id, owner_operator_id, current_name_reservation_id,
                    status, created_at, updated_at
                ) VALUES (
                    :agent_id, :operator_id, :reservation_id,
                    'draft', :now, :now
                )
                """
            ),
            {"agent_id": agent_id, "operator_id": identity.operator_id, "reservation_id": reservation_id, "now": now},
        )
        conn.execute(
            text(
                """
                UPDATE agent_name_reservations
                SET pending_interaction_id = NULL, agent_id = :agent_id, activated_at = :now
                WHERE reservation_id = :reservation_id
                """
            ),
            {"agent_id": agent_id, "reservation_id": reservation_id, "now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO credential_bindings (
                    binding_id, agent_id, kind, status, generation, created_at, updated_at
                ) VALUES (
                    :binding_id, :agent_id, 'oauth', 'issuing', 1, :now, :now
                )
                """
            ),
            {"binding_id": binding_id, "agent_id": agent_id, "now": now},
        )
        conn.execute(
            text(
                """
                INSERT INTO authorization_grants (
                    grant_id, binding_id, authorizing_identity_id, client_software_id,
                    enrollment_interaction_id, allowed_scopes, created_at
                ) VALUES (
                    :grant_id, :binding_id, :identity_id, :client_software_id,
                    :interaction_id, :allowed_scopes, :now
                )
                """
            ),
            {
                "grant_id": grant_id,
                "binding_id": binding_id,
                "identity_id": identity.mcp_identity_id,
                "client_software_id": client_software_id,
                "interaction_id": interaction_id,
                "allowed_scopes": allowed_scopes or ["tools:call"],
                "now": now,
            },
        )
    return AgentGraph(
        interaction_id=interaction_id,
        client_software_id=client_software_id,
        reservation_id=reservation_id,
        agent_id=agent_id,
        binding_id=binding_id,
        grant_id=grant_id,
        identity=identity,
    )


def _record_issuance_and_complete(engine: Engine, graph: AgentGraph) -> None:
    now = _now()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE credential_bindings
                SET status = 'issued', issued_at = :now, updated_at = :now
                WHERE binding_id = :binding_id
                """
            ),
            {"binding_id": graph.binding_id, "now": now},
        )
        conn.execute(
            text(
                """
                UPDATE authorization_grants
                SET initial_access_jti = :access_jti, initial_refresh_jti = :refresh_jti,
                    token_family_persisted_at = :now
                WHERE grant_id = :grant_id
                """
            ),
            {
                "grant_id": graph.grant_id,
                "access_jti": f"access-{graph.grant_id}",
                "refresh_jti": f"refresh-{graph.grant_id}",
                "now": now,
            },
        )
        conn.execute(
            text(
                """
                UPDATE enrollment_interactions
                SET phase = 'completed', browser_binding_digest = NULL, closed_at = :now,
                    closure_reason = 'token_family_persisted', updated_at = :now
                WHERE interaction_id = :interaction_id
                """
            ),
            {"interaction_id": graph.interaction_id, "now": now},
        )


def _activate_agent(engine: Engine, graph: AgentGraph) -> None:
    now = _now()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE agents
                SET status = 'active', activated_at = :now, last_seen_at = :now, updated_at = :now
                WHERE agent_id = :agent_id
                """
            ),
            {"agent_id": graph.agent_id, "now": now},
        )
        conn.execute(
            text(
                """
                UPDATE credential_bindings
                SET status = 'active', activated_at = :now, updated_at = :now
                WHERE binding_id = :binding_id
                """
            ),
            {"binding_id": graph.binding_id, "now": now},
        )


def _create_oauth_graph(
    engine: Engine, label: str, *, display_name: str | None = None, display_name_key: str | None = None
) -> AgentGraph:
    with engine.begin() as conn:
        identity = _seed_identity(conn, label)
        client_software_id, client_id, redirect_uri = _seed_client(conn, label)
    interaction_id = _start_interaction(
        engine, label=label, client_software_id=client_software_id, client_id=client_id, redirect_uri=redirect_uri
    )
    _bind_browser(engine, interaction_id, identity.browser_identity_id)
    reservation_id = _allow_create(
        engine,
        interaction_id,
        display_name=display_name or f"Agent {label}",
        display_name_key=display_name_key or f"agent {label}",
    )
    graph = _exchange_create(
        engine,
        interaction_id=interaction_id,
        client_software_id=client_software_id,
        reservation_id=reservation_id,
        identity=identity,
    )
    _record_issuance_and_complete(engine, graph)
    return graph


def _create_static_agent(conn: Connection, *, identity: IdentityIds, label: str, status: str = "active") -> StaticAgent:
    reservation_id = uuid4()
    agent_id = uuid4()
    binding_id = uuid4()
    now = _now()
    activated_at = now if status == "active" else None
    conn.execute(
        text(
            """
            INSERT INTO agents (
                agent_id, owner_operator_id, current_name_reservation_id,
                status, created_at, updated_at, activated_at
            ) VALUES (
                :agent_id, :operator_id, :reservation_id,
                :status, :now, :now, :activated_at
            )
            """
        ),
        {
            "agent_id": agent_id,
            "operator_id": identity.operator_id,
            "reservation_id": reservation_id,
            "status": status,
            "now": now,
            "activated_at": activated_at,
        },
    )
    conn.execute(
        text(
            """
            INSERT INTO agent_name_reservations (
                reservation_id, display_name, display_name_key,
                agent_id, created_at, activated_at
            ) VALUES (
                :reservation_id, :display_name, :display_name_key,
                :agent_id, :now, :now
            )
            """
        ),
        {
            "reservation_id": reservation_id,
            "display_name": f"Static {label}",
            "display_name_key": f"static {label}",
            "agent_id": agent_id,
            "now": now,
        },
    )
    conn.execute(
        text(
            """
            INSERT INTO credential_bindings (
                binding_id, agent_id, kind, status, generation,
                created_at, updated_at, issued_at, activated_at
            ) VALUES (
                :binding_id, :agent_id, 'static', :binding_status, 1,
                :now, :now, :credential_time, :credential_time
            )
            """
        ),
        {
            "binding_id": binding_id,
            "agent_id": agent_id,
            "binding_status": "active" if status == "active" else "issuing",
            "credential_time": activated_at,
            "now": now,
        },
    )
    conn.execute(
        text(
            """
            INSERT INTO static_credentials (
                binding_id, secret_reference, credential_fingerprint, created_at
            ) VALUES (
                :binding_id, :secret_reference, :fingerprint, :now
            )
            """
        ),
        {
            "binding_id": binding_id,
            "secret_reference": f"env:STATIC_{label.upper()}",
            "fingerprint": f"fingerprint-{label}-{binding_id}".encode(),
            "now": now,
        },
    )
    return StaticAgent(reservation_id=reservation_id, agent_id=agent_id, binding_id=binding_id)


def _not_awaiting_its_drop(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    """Hide the tables and columns an expand/contract has unmapped but not yet dropped.

    Each exists in the database and in no ORM class, which is otherwise exactly the difference this
    comparison is for — so each is named in `UNMAPPED_{TABLES,COLUMNS}_PENDING_DROP` beside the
    tombstone that says when it goes, and everything else still has to match exactly.
    """
    match type_:
        case "table":
            return name not in UNMAPPED_TABLES_PENDING_DROP
        case "column":
            return (parent_names["table_name"], name) not in UNMAPPED_COLUMNS_PENDING_DROP
        case _:
            return True


def test_fresh_baseline_matches_sqlalchemy_metadata(db_url: str) -> None:
    """Exact in both directions: every name the migrations create is mapped, and every name the ORM
    maps exists. Only the names awaiting their drop are excluded, so a column left behind by a
    half-finished expand/contract fails here rather than living on unmapped."""
    apply_migrations(db_url)
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"compare_type": True, "include_name": _not_awaiting_its_drop}
            )
            assert compare_metadata(context, metadata) == []
    finally:
        engine.dispose()


def test_oauth_token_state_migration_preserves_all_association_tokens(db_url: str) -> None:
    apply_migrations(db_url, "0015")
    engine = create_engine(db_url)
    operator_id = uuid4()
    now = _now()
    expires_at = now + datetime.timedelta(hours=1)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO operators (operator_id, status, created_at, updated_at)
                    VALUES (:operator_id, 'active', :now, :now)
                    """
                ),
                {"operator_id": operator_id, "now": now},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_operator_oauth_associations (
                        server_id, operator_id, association_id, token_revision, created_at, updated_at,
                        client_id, token_endpoint, access_token, refresh_token, token_type, scope,
                        token_expires_at
                    ) VALUES (
                        'remote', :operator_id, :association_id, 3, :now, :now,
                        'client', 'https://issuer.test/token', 'mcp-access', 'mcp-refresh', 'Bearer',
                        'mcp-scope', :expires_at
                    )
                    """
                ),
                {"operator_id": operator_id, "association_id": uuid4(), "now": now, "expires_at": expires_at},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO provider_connections (
                        operator_id, connection_name, provider_name, provider, connection_id,
                        token_revision, created_at, updated_at, access_token, refresh_token,
                        token_type, scope, token_expires_at
                    ) VALUES (
                        :operator_id, 'google_mail', 'google', 'google', :connection_id,
                        4, :now, :now, 'provider-access', 'provider-refresh', 'Bearer',
                        'provider-scope', :expires_at
                    )
                    """
                ),
                {"operator_id": operator_id, "connection_id": uuid4(), "now": now, "expires_at": expires_at},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO operator_authentik_tokens (
                        operator_id, token_revision, created_at, updated_at, access_token,
                        refresh_token, token_type, scope, token_expires_at
                    ) VALUES (
                        :operator_id, 5, :now, :now, 'login-access', 'login-refresh',
                        'Bearer', 'login-scope', :expires_at
                    )
                    """
                ),
                {"operator_id": operator_id, "now": now, "expires_at": expires_at},
            )

        apply_migrations(db_url)

        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT access_token, refresh_token, token_revision, scope, token_expires_at
                    FROM oauth_token_states
                    WHERE operator_id = :operator_id
                    ORDER BY access_token
                    """
                ),
                {"operator_id": operator_id},
            ).tuples()
            assert list(rows) == [
                ("login-access", "login-refresh", 5, "login-scope", expires_at),
                ("mcp-access", "mcp-refresh", 3, "mcp-scope", expires_at),
                ("provider-access", "provider-refresh", 4, "provider-scope", expires_at),
            ]
            assert (
                conn.execute(
                    text(
                        """
                    SELECT count(*)
                    FROM (
                        SELECT token_state_id FROM mcp_operator_oauth_associations
                        UNION ALL SELECT token_state_id FROM provider_connections
                        UNION ALL SELECT token_state_id FROM operator_authentik_tokens
                    ) AS owners
                    JOIN oauth_token_states USING (token_state_id)
                    """
                    )
                ).scalar_one()
                == 3
            )
    finally:
        engine.dispose()


def test_operator_connection_key_migration_discards_ambiguous_provider_grants(db_url: str) -> None:
    apply_migrations(db_url, "0012")
    engine = create_engine(db_url)
    operator_id = uuid4()
    now = _now()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO operators (operator_id, status, created_at, updated_at)
                    VALUES (:operator_id, 'active', :now, :now)
                    """
                ),
                {"operator_id": operator_id, "now": now},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO provider_connections (
                        operator_id, provider, connection_id, token_revision, created_at, updated_at,
                        access_token, refresh_token, token_type, scope, token_expires_at
                    ) VALUES (
                        :operator_id, 'google', :connection_id, 0, :now, :now,
                        'old-access', 'old-refresh', 'Bearer', 'old-broad-scope', NULL
                    )
                    """
                ),
                {"operator_id": operator_id, "connection_id": uuid4(), "now": now},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO provider_connection_flows (
                        state, operator_id, provider, created_at, expires_at,
                        redirect_uri, code_verifier, scope
                    ) VALUES (
                        'old-flow', :operator_id, 'google', :now, :now,
                        'https://haku.test/callback', 'verifier', 'old-broad-scope'
                    )
                    """
                ),
                {"operator_id": operator_id, "now": now},
            )

        apply_migrations(db_url, "0013")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0013"
            assert "provider_name" not in {
                row.column_name
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = 'provider_connections'
                        """
                    )
                )
            }

        apply_migrations(db_url)

        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM provider_connections")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM provider_connection_flows")).scalar_one() == 0
    finally:
        engine.dispose()


def test_database_at_head_with_missing_orm_column_fails_validation(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE provider_connections DROP COLUMN provider_name CASCADE"))

        with pytest.raises(ProgrammingError, match=r"provider_connections\.provider_name"):
            apply_migrations(db_url)
    finally:
        engine.dispose()


def test_database_already_at_head_is_unchanged(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(db_url)
    operator_id = uuid4()
    try:
        with engine.connect() as conn:
            head_version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO operators (operator_id, status, created_at, updated_at)
                    VALUES (:operator_id, 'active', :now, :now)
                    """
                ),
                {"operator_id": operator_id, "now": _now()},
            )

        apply_migrations(db_url)

        with engine.connect() as conn:
            # Re-applying when already at head is a no-op: the stamp is unchanged (asserting a
            # specific revision literal would just re-check the current head — a change detector)
            # and pre-existing rows survive rather than being recreated.
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == head_version
            assert (
                conn.execute(
                    text("SELECT count(*) FROM operators WHERE operator_id = :operator_id"),
                    {"operator_id": operator_id},
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()


def test_create_issue_complete_and_first_use_activation_form_one_graph(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(db_url)
    try:
        # NFKC + Unicode casefold need not equal PostgreSQL lower(display_name). The database owns
        # nonempty/global uniqueness and immutability; application naming code owns normalization.
        graph = _create_oauth_graph(engine, "unicode", display_name="Straße ⑨", display_name_key="strasse 9")
        with engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        """
                        SELECT agent.status::TEXT AS agent_status,
                               binding.status::TEXT AS binding_status,
                               interaction.phase::TEXT AS phase,
                               name.display_name,
                               name.display_name_key,
                               agent.owner_operator_id,
                               auth_grant.allowed_scopes,
                               auth_grant.initial_access_jti
                        FROM authorization_grants AS auth_grant
                        JOIN credential_bindings AS binding
                          ON binding.binding_id = auth_grant.binding_id
                        JOIN agents AS agent ON agent.agent_id = binding.agent_id
                        JOIN agent_name_reservations AS name
                          ON name.reservation_id = agent.current_name_reservation_id
                        JOIN enrollment_interactions AS interaction
                          ON interaction.interaction_id = auth_grant.enrollment_interaction_id
                        WHERE auth_grant.grant_id = :grant_id
                        """
                    ),
                    {"grant_id": graph.grant_id},
                )
                .mappings()
                .one()
            )
            assert row["agent_status"] == "draft"
            assert row["binding_status"] == "issued"
            assert row["phase"] == "completed"
            assert row["display_name"] == "Straße ⑨"
            assert row["display_name_key"] == "strasse 9"
            assert row["owner_operator_id"] == graph.identity.operator_id
            assert row["allowed_scopes"] == ["tools:call"]
            assert row["initial_access_jti"] == f"access-{graph.grant_id}"

        _activate_agent(engine, graph)
        with engine.connect() as conn:
            statuses = conn.execute(
                text(
                    """
                    SELECT agent.status::TEXT, binding.status::TEXT
                    FROM agents AS agent
                    JOIN credential_bindings AS binding ON binding.agent_id = agent.agent_id
                    WHERE agent.agent_id = :agent_id
                    """
                ),
                {"agent_id": graph.agent_id},
            ).one()
            assert statuses == ("active", "active")
    finally:
        engine.dispose()


def test_agent_names_are_required_globally_unique_and_owned_by_current_agent(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(db_url)
    try:
        graph = _create_oauth_graph(engine, "name-owner", display_name="Straße ⑨", display_name_key="strasse 9")
        _activate_agent(engine, graph)
        with engine.begin() as conn:
            other_identity = _seed_identity(conn, "other-name-owner")
            other = _create_static_agent(conn, identity=other_identity, label="other-name")

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO agent_name_reservations (
                        reservation_id, display_name, display_name_key,
                        agent_id, created_at, activated_at
                    ) VALUES (
                        :reservation_id, 'STRASSE 9', 'strasse 9',
                        :agent_id, :now, :now
                    )
                    """
                ),
                {"reservation_id": uuid4(), "agent_id": graph.agent_id, "now": _now()},
            )

        for display_name, display_name_key in (("   ", "empty-display"), ("Valid", "   ")):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO agent_name_reservations (
                            reservation_id, display_name, display_name_key,
                            agent_id, created_at, activated_at
                        ) VALUES (
                            :reservation_id, :display_name, :display_name_key,
                            :agent_id, :now, :now
                        )
                        """
                    ),
                    {
                        "reservation_id": uuid4(),
                        "display_name": display_name,
                        "display_name_key": display_name_key,
                        "agent_id": graph.agent_id,
                        "now": _now(),
                    },
                )

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE agents SET current_name_reservation_id = :other_reservation, updated_at = :now
                    WHERE agent_id = :agent_id
                    """
                ),
                {"other_reservation": other.reservation_id, "agent_id": graph.agent_id, "now": _now()},
            )

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE agent_name_reservations SET display_name = 'Renamed in place'
                    WHERE reservation_id = :reservation_id
                    """
                ),
                {"reservation_id": graph.reservation_id},
            )
    finally:
        engine.dispose()


def test_interaction_phase_identity_and_exact_tuple_are_one_time(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            identity = _seed_identity(conn, "interaction")
            other_identity = _seed_identity(conn, "interaction-attacker")
            client_software_id, client_id, redirect_uri = _seed_client(conn, "interaction")
        interaction_id = _start_interaction(
            engine,
            label="interaction",
            client_software_id=client_software_id,
            client_id=client_id,
            redirect_uri=redirect_uri,
        )

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE enrollment_interactions
                    SET phase = 'allowed', browser_nonce_digest = NULL,
                        browser_identity_id = :identity_id,
                        browser_binding_digest = 'binding'::bytea,
                        decision_digest = 'decision'::bytea, updated_at = :now
                    WHERE interaction_id = :interaction_id
                    """
                ),
                {"identity_id": identity.browser_identity_id, "interaction_id": interaction_id, "now": _now()},
            )

        _bind_browser(engine, interaction_id, identity.browser_identity_id)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE enrollment_interactions
                    SET browser_identity_id = :identity_id, updated_at = :now
                    WHERE interaction_id = :interaction_id
                    """
                ),
                {"identity_id": other_identity.browser_identity_id, "interaction_id": interaction_id, "now": _now()},
            )

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE enrollment_interactions
                    SET requested_scopes = ARRAY['tools:call', 'admin'], updated_at = :now
                    WHERE interaction_id = :interaction_id
                    """
                ),
                {"interaction_id": interaction_id, "now": _now()},
            )

        _allow_create(engine, interaction_id, display_name="Interaction Agent", display_name_key="interaction agent")
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE enrollment_interactions
                    SET phase = 'awaiting_approval', decision_digest = NULL, updated_at = :now
                    WHERE interaction_id = :interaction_id
                    """
                ),
                {"interaction_id": interaction_id, "now": _now()},
            )

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text("DELETE FROM enrollment_interactions WHERE interaction_id = :interaction_id"),
                {"interaction_id": interaction_id},
            )

        duplicate_interaction_id = uuid4()
        now = _now()
        with engine.connect() as conn:
            values = (
                conn.execute(
                    text(
                        """
                        SELECT client_id, redirect_uri, code_challenge
                        FROM enrollment_interactions WHERE interaction_id = :interaction_id
                        """
                    ),
                    {"interaction_id": interaction_id},
                )
                .mappings()
                .one()
            )
        release_after = now + datetime.timedelta(hours=3)

        def insert_duplicate_tuple() -> None:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO enrollment_interactions (
                            interaction_id, client_software_id, client_id, redirect_uri,
                            code_challenge, requested_scopes, presentation_snapshot,
                            upstream_authorization_url, phase, expires_at,
                            correlation_release_after, browser_nonce_digest, created_at, updated_at
                        ) VALUES (
                            :new_id, :client_software_id, :client_id, :redirect_uri,
                            :code_challenge, ARRAY['tools:call'], '{}'::jsonb,
                            'https://auth.test/duplicate', 'awaiting_browser',
                            :expires_at, :release_after, 'nonce'::bytea, :now, :now
                        )
                        """
                    ),
                    {
                        "new_id": duplicate_interaction_id,
                        "client_software_id": client_software_id,
                        "client_id": values["client_id"],
                        "redirect_uri": values["redirect_uri"],
                        "code_challenge": values["code_challenge"],
                        "expires_at": now + datetime.timedelta(minutes=10),
                        "release_after": release_after,
                        "now": now,
                    },
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO enrollment_correlation_reservations (
                            interaction_id, client_id, redirect_uri, code_challenge, release_after
                        ) VALUES (
                            :new_id, :client_id, :redirect_uri, :code_challenge, :release_after
                        )
                        """
                    ),
                    {
                        "new_id": duplicate_interaction_id,
                        "client_id": values["client_id"],
                        "redirect_uri": values["redirect_uri"],
                        "code_challenge": values["code_challenge"],
                        "release_after": release_after,
                    },
                )

        with pytest.raises(IntegrityError):
            insert_duplicate_tuple()

        released_interaction_id = uuid4()
        released_challenge = f"released-{uuid4()}"
        old_created_at = now - datetime.timedelta(hours=4)
        old_expires_at = now - datetime.timedelta(hours=2)
        old_release_after = now - datetime.timedelta(hours=1)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO enrollment_interactions (
                        interaction_id, client_software_id, client_id, redirect_uri,
                        code_challenge, requested_scopes, presentation_snapshot,
                        upstream_authorization_url, phase, expires_at,
                        correlation_release_after, browser_nonce_digest, created_at, updated_at
                    ) VALUES (
                        :interaction_id, :client_software_id, :client_id, :redirect_uri,
                        :code_challenge, ARRAY['tools:call'], '{}'::jsonb,
                        'https://auth.test/released', 'awaiting_browser', :expires_at,
                        :release_after, 'nonce'::bytea, :created_at, :created_at
                    )
                    """
                ),
                {
                    "interaction_id": released_interaction_id,
                    "client_software_id": client_software_id,
                    "client_id": values["client_id"],
                    "redirect_uri": values["redirect_uri"],
                    "code_challenge": released_challenge,
                    "expires_at": old_expires_at,
                    "release_after": old_release_after,
                    "created_at": old_created_at,
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO enrollment_correlation_reservations (
                        interaction_id, client_id, redirect_uri, code_challenge, release_after
                    ) VALUES (
                        :interaction_id, :client_id, :redirect_uri, :code_challenge, :release_after
                    )
                    """
                ),
                {
                    "interaction_id": released_interaction_id,
                    "client_id": values["client_id"],
                    "redirect_uri": values["redirect_uri"],
                    "code_challenge": released_challenge,
                    "release_after": old_release_after,
                },
            )
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    DELETE FROM enrollment_correlation_reservations
                    WHERE interaction_id = :interaction_id
                    """
                ),
                {"interaction_id": released_interaction_id},
            )
        with engine.connect() as conn:
            audit_counts = (
                conn.execute(
                    text(
                        """
                        SELECT
                            (SELECT count(*) FROM enrollment_interactions
                             WHERE interaction_id = :interaction_id) AS interactions,
                            (SELECT count(*) FROM enrollment_correlation_reservations
                             WHERE interaction_id = :interaction_id) AS reservations
                        """
                    ),
                    {"interaction_id": released_interaction_id},
                )
                .mappings()
                .one()
            )
            assert audit_counts == {"interactions": 1, "reservations": 0}
    finally:
        engine.dispose()


def test_grant_owner_scope_subtype_and_provenance_fail_closed(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            owner = _seed_identity(conn, "grant-owner")
            attacker = _seed_identity(conn, "grant-attacker")
            client_software_id, client_id, redirect_uri = _seed_client(conn, "grant")
        interaction_id = _start_interaction(
            engine, label="grant", client_software_id=client_software_id, client_id=client_id, redirect_uri=redirect_uri
        )
        _bind_browser(engine, interaction_id, owner.browser_identity_id)
        reservation_id = _allow_create(
            engine, interaction_id, display_name="Grant Agent", display_name_key="grant agent"
        )

        with pytest.raises(IntegrityError):
            _exchange_create(
                engine,
                interaction_id=interaction_id,
                client_software_id=client_software_id,
                reservation_id=reservation_id,
                identity=attacker,
            )
        with pytest.raises(IntegrityError):
            _exchange_create(
                engine,
                interaction_id=interaction_id,
                client_software_id=client_software_id,
                reservation_id=reservation_id,
                identity=owner,
                allowed_scopes=["tools:call", "admin:everything"],
            )

        graph = _exchange_create(
            engine,
            interaction_id=interaction_id,
            client_software_id=client_software_id,
            reservation_id=reservation_id,
            identity=owner,
        )
        _record_issuance_and_complete(engine, graph)

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text("UPDATE authorization_grants SET allowed_scopes = ARRAY['tools:list'] WHERE grant_id = :grant_id"),
                {"grant_id": graph.grant_id},
            )

        # A binding cannot exist as an untyped base row, even while it is only issuing.
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO credential_bindings (
                        binding_id, agent_id, kind, status, generation,
                        created_at, updated_at
                    ) VALUES (
                        :binding_id, :agent_id, 'oauth', 'issuing', 2,
                        :now, :now
                    )
                    """
                ),
                {"binding_id": uuid4(), "agent_id": graph.agent_id, "now": _now()},
            )

        # P3 links are now immutable too; ordinary updates cannot rewrite historical provenance.
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text("UPDATE oidc_identities SET anchor_id = :anchor_id WHERE identity_id = :identity_id"),
                {"anchor_id": attacker.anchor_id, "identity_id": owner.mcp_identity_id},
            )
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text("UPDATE identity_anchors SET operator_id = :operator_id WHERE anchor_id = :anchor_id"),
                {"operator_id": attacker.operator_id, "anchor_id": owner.anchor_id},
            )
    finally:
        engine.dispose()


def _insert_static_replacement(
    conn: Connection, *, agent_id: UUID, predecessor_id: UUID, generation: int, label: str
) -> UUID:
    binding_id = uuid4()
    now = _now()
    conn.execute(
        text(
            """
            INSERT INTO credential_bindings (
                binding_id, agent_id, kind, status, generation,
                supersedes_binding_id, created_at, updated_at
            ) VALUES (
                :binding_id, :agent_id, 'static', 'issuing', :generation,
                :predecessor_id, :now, :now
            )
            """
        ),
        {
            "binding_id": binding_id,
            "agent_id": agent_id,
            "generation": generation,
            "predecessor_id": predecessor_id,
            "now": now,
        },
    )
    conn.execute(
        text(
            """
            INSERT INTO static_credentials (
                binding_id, secret_reference, credential_fingerprint, created_at
            ) VALUES (:binding_id, :secret_reference, :fingerprint, :now)
            """
        ),
        {
            "binding_id": binding_id,
            "secret_reference": "env:ROTATING_STATIC_TOKEN",
            "fingerprint": f"rotation-{label}-{binding_id}".encode(),
            "now": now,
        },
    )
    return binding_id


def test_binding_generation_predecessor_and_activation_are_compare_and_set(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            identity = _seed_identity(conn, "binding")
            static_agent = _create_static_agent(conn, identity=identity, label="binding")
        with engine.begin() as conn:
            generation_two = _insert_static_replacement(
                conn, agent_id=static_agent.agent_id, predecessor_id=static_agent.binding_id, generation=2, label="two"
            )
            generation_three = _insert_static_replacement(
                conn,
                agent_id=static_agent.agent_id,
                predecessor_id=static_agent.binding_id,
                generation=3,
                label="three",
            )
            other = _create_static_agent(conn, identity=identity, label="other-binding")

        now = _now()

        def issue_and_activate(conn: Connection, binding_id: UUID) -> None:
            conn.execute(
                text(
                    """
                    UPDATE credential_bindings
                    SET status = 'issued', issued_at = :now, updated_at = :now
                    WHERE binding_id = :binding_id
                    """
                ),
                {"binding_id": binding_id, "now": now},
            )
            conn.execute(
                text(
                    """
                    UPDATE credential_bindings
                    SET status = 'active', activated_at = :now, updated_at = :now
                    WHERE binding_id = :binding_id
                    """
                ),
                {"binding_id": binding_id, "now": now},
            )

        def activate_stale_generation() -> None:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        UPDATE credential_bindings
                        SET status = 'revoked', ended_at = :now,
                            end_reason = 'superseded', updated_at = :now
                        WHERE binding_id = :predecessor
                        """
                    ),
                    {"predecessor": static_agent.binding_id, "now": now},
                )
                issue_and_activate(conn, generation_two)

        with pytest.raises(IntegrityError):
            activate_stale_generation()

        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE credential_bindings
                    SET status = 'revoked', ended_at = :now, end_reason = 'superseded', updated_at = :now
                    WHERE binding_id = :predecessor
                    """
                ),
                {"predecessor": static_agent.binding_id, "now": now},
            )
            issue_and_activate(conn, generation_three)

        with pytest.raises(IntegrityError), engine.begin() as conn:
            _insert_static_replacement(
                conn, agent_id=static_agent.agent_id, predecessor_id=other.binding_id, generation=4, label="wrong-owner"
            )

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE agents SET status = 'disabled', updated_at = :now
                    WHERE agent_id = :agent_id
                    """
                ),
                {"agent_id": static_agent.agent_id, "now": _now()},
            )

        disable_time = _now()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE agents SET status = 'disabled', updated_at = :now
                    WHERE agent_id = :agent_id
                    """
                ),
                {"agent_id": static_agent.agent_id, "now": disable_time},
            )
            conn.execute(
                text(
                    """
                    UPDATE credential_bindings
                    SET status = 'revoked', ended_at = :now,
                        end_reason = 'Agent disabled', updated_at = :now
                    WHERE binding_id = :binding_id
                    """
                ),
                {"binding_id": generation_three, "now": disable_time},
            )

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE credential_bindings
                    SET status = 'active', ended_at = NULL, end_reason = NULL, updated_at = :now
                    WHERE binding_id = :binding_id
                    """
                ),
                {"binding_id": generation_three, "now": _now()},
            )
    finally:
        engine.dispose()


def _insert_tool_call(conn: Connection, tool_call_id: str) -> None:
    now = _now()
    conn.execute(
        text(
            """
            INSERT INTO mcp_tool_calls (
                tool_call_id, server_id, tool_name, status,
                created_at, updated_at, arguments_json, rationale
            ) VALUES (
                :tool_call_id, 'server', 'tool', 'pending_approval',
                :now, :now, '{}'::jsonb, 'schema test'
            )
            """
        ),
        {"tool_call_id": tool_call_id, "now": now},
    )


def test_tool_call_principal_is_an_exact_immutable_union_and_events_derive_it(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            identity = _seed_identity(conn, "principal")
            agent = _create_static_agent(conn, identity=identity, label="principal")

        with pytest.raises(IntegrityError), engine.begin() as conn:
            _insert_tool_call(conn, "missing-principal")

        def insert_ambiguous_principal() -> None:
            with engine.begin() as conn:
                _insert_tool_call(conn, "ambiguous-principal")
                conn.execute(
                    text(
                        """
                        INSERT INTO mcp_tool_call_principals (
                            tool_call_id, operator_id, binding_id
                        ) VALUES (
                            'ambiguous-principal', :operator_id, :binding_id
                        )
                        """
                    ),
                    {"operator_id": identity.operator_id, "binding_id": agent.binding_id},
                )

        with pytest.raises(IntegrityError):
            insert_ambiguous_principal()

        with engine.begin() as conn:
            _insert_tool_call(conn, "agent-call")
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_call_principals (tool_call_id, binding_id)
                    VALUES ('agent-call', :binding_id)
                    """
                ),
                {"binding_id": agent.binding_id},
            )

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE mcp_tool_call_principals
                    SET binding_id = NULL, operator_id = :operator_id
                    WHERE tool_call_id = 'agent-call'
                    """
                ),
                {"operator_id": identity.operator_id},
            )

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM mcp_tool_calls WHERE tool_call_id = 'agent-call'"))
        with engine.connect() as conn:
            principals = conn.scalar(
                text(
                    """
                    SELECT count(*) FROM mcp_tool_call_principals
                    WHERE tool_call_id = 'agent-call'
                    """
                )
            )
            assert principals == 0

        with engine.begin() as conn:
            _insert_tool_call(conn, "operator-call")
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_call_principals (tool_call_id, operator_id)
                    VALUES ('operator-call', :operator_id)
                    """
                ),
                {"operator_id": identity.operator_id},
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()


def test_lease_backfill_reclaims_a_session_no_replica_is_holding(db_url: str) -> None:
    """A session written before 0027 has no lease, so the sweep cannot see it.

    That is exactly how the Matrix room stayed on "responding" after the lease shipped: the
    wedged session predated the column, and `expire_stale_leases` only looks at leases that
    exist and have passed. A live row must come out of this migration holding a lease that will
    expire unless somebody renews it, and a terminal row must not be resurrected.
    """
    apply_migrations(db_url, "0027")
    engine = create_engine(db_url)
    operator_id = uuid4()
    orphan, healthy, finished = uuid4(), uuid4(), uuid4()
    now = _now()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO operators (operator_id, status, created_at, updated_at)
                    VALUES (:operator_id, 'active', :now, :now)
                    """
                ),
                {"operator_id": operator_id, "now": now},
            )
            for session_id, status in ((orphan, "responding"), (healthy, "ready"), (finished, "closed")):
                conn.execute(
                    text(
                        """
                        -- The historical name on purpose: this writes at revision 0027, where
                        -- `sessions` does not exist yet (0040 is what renames it).
                        INSERT INTO claude_chat_sessions (
                            session_id, operator_id, status, bridge_token_fingerprint,
                            bridge_connected_at, error, lease_expires_at, created_at, updated_at
                        ) VALUES (
                            :session_id, :operator_id, :status, :fingerprint,
                            NULL, NULL, NULL, :now, :now
                        )
                        """
                    ),
                    {
                        "session_id": session_id,
                        "operator_id": operator_id,
                        "status": status,
                        "fingerprint": b"fingerprint",
                        "now": now,
                    },
                )

        # `0028`, the revision under test, rather than head: a row this old has no `surface`, and
        # `0058` requires one now that the purge has deleted the rows that had none.
        apply_migrations(db_url, "0028")

        with engine.connect() as conn:
            leases: dict[UUID, datetime.datetime | None] = {
                row.session_id: row.lease_expires_at
                for row in conn.execute(text("SELECT session_id, lease_expires_at FROM claude_chat_sessions"))
            }
        # Grace, not an expired lease: a replica that is genuinely alive renews inside the TTL,
        # so the backfill must not declare every healthy session dead the moment it runs.
        for live in (orphan, healthy):
            lease = leases[live]
            assert lease is not None, "0027 rows must not stay leaseless"
            assert lease > now, "a live session must get grace to prove its holder exists"
        assert leases[finished] == now, "a terminal session's lease ended when the row last changed"
    finally:
        engine.dispose()


def test_a_chat_session_cannot_be_written_without_a_lease(db_url: str) -> None:
    """The point of 0029: "live but unreclaimable" stops being a state you can reach.

    0028 repaired the rows already in it, but repair alone leaves the next forgotten insert
    free to recreate it, and the failure is invisible — the session simply never recovers.
    """
    apply_migrations(db_url)
    engine = create_engine(db_url)
    operator_id = uuid4()
    now = _now()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO operators (operator_id, status, created_at, updated_at)
                    VALUES (:operator_id, 'active', :now, :now)
                    """
                ),
                {"operator_id": operator_id, "now": now},
            )
        with pytest.raises(IntegrityError, match="lease_expires_at"), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO sessions (
                        session_id, operator_id, surface, status, bridge_token_fingerprint,
                        bridge_connected_at, error, lease_expires_at, created_at, updated_at
                    ) VALUES (
                        :session_id, :operator_id, 'spa', 'responding', :fingerprint,
                        NULL, NULL, NULL, :now, :now
                    )
                    """
                ),
                {"session_id": uuid4(), "operator_id": operator_id, "fingerprint": b"fp", "now": now},
            )
    finally:
        engine.dispose()
