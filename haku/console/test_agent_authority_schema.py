"""Postgres acceptance tests for the canonical Agent authority schema."""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from alembic import command as alembic_command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from haku.console.database_migrate import apply_migrations
from haku.console.database_schema import metadata
from util.testing.postgres import force_drop_database_sync
from util.testing.postgres_fixtures import postgres_container as _postgres_container

# Import the shared, preloaded Postgres fixture under the exact name pytest exposes to dependents.
postgres_container = _postgres_container

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
    admin_engine = create_engine(postgres_admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    yield postgres_admin_url.rsplit("/", 1)[0] + f"/{db_name}"

    force_drop_database_sync(postgres_admin_url, db_name)


def _now() -> datetime.datetime:
    return datetime.datetime.now(_UTC)


def _alembic_config(conn: Connection) -> AlembicConfig:
    config = AlembicConfig()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.attributes["connection"] = conn
    config.attributes["target_metadata"] = metadata
    config.attributes["operator_identity_seeds"] = ()
    return config


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
                SET phase = 'completed', closed_at = :now,
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


def test_0009_is_a_destructive_authority_cutover_without_fastmcp_store_deletion(db_url: str) -> None:
    engine = create_engine(db_url)
    operator_id = uuid4()
    try:
        with engine.begin() as conn:
            alembic_command.upgrade(_alembic_config(conn), "0008")
            now = _now()
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
                    INSERT INTO mcp_agent_operator (
                        agent_dcr_client_id, operator_id, created_at, updated_at
                    ) VALUES ('legacy-dcr-client', :operator_id, :now, :now)
                    """
                ),
                {"operator_id": operator_id, "now": now},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_calls (
                        tool_call_id, operator_id, server_id, tool_name, caller_principal,
                        status, created_at, updated_at, arguments_json, rationale
                    ) VALUES (
                        'legacy-call', :operator_id, 'server', 'tool', 'legacy-agent',
                        'pending_approval', :now, :now, '{}'::jsonb, 'legacy'
                    )
                    """
                ),
                {"operator_id": operator_id, "now": now},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_call_events (
                        event_type, operator_id, tool_call_id, status, created_at
                    ) VALUES (
                        'tool_call_submitted', :operator_id, 'legacy-call',
                        'pending_approval', :now
                    )
                    """
                ),
                {"operator_id": operator_id, "now": now},
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE haku_fastmcp_oauth_state (
                        collection TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value JSONB NOT NULL,
                        PRIMARY KEY (collection, key)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO haku_fastmcp_oauth_state (collection, key, value)
                    VALUES ('mcp-refresh-tokens', 'must-expire-by-ttl', '{}'::jsonb)
                    """
                )
            )

        with engine.begin() as conn:
            alembic_command.upgrade(_alembic_config(conn), "0009")

        database = inspect(engine)
        tables = set(database.get_table_names())
        assert "mcp_agent_operator" not in tables
        assert {
            "client_software",
            "enrollment_interactions",
            "enrollment_correlation_reservations",
            "agents",
            "agent_name_reservations",
            "credential_bindings",
            "authorization_grants",
            "static_credentials",
            "mcp_tool_call_principals",
        } <= tables
        assert {column["name"] for column in database.get_columns("mcp_tool_calls")}.isdisjoint(
            {"operator_id", "caller_principal"}
        )
        assert "operator_id" not in {column["name"] for column in database.get_columns("mcp_tool_call_events")}

        with engine.connect() as conn:
            counts = (
                conn.execute(
                    text(
                        """
                        SELECT
                            (SELECT count(*) FROM mcp_tool_calls) AS calls,
                            (SELECT count(*) FROM mcp_tool_call_events) AS events,
                            (SELECT count(*) FROM haku_fastmcp_oauth_state) AS fastmcp_rows
                        """
                    )
                )
                .mappings()
                .one()
            )
            assert counts == {"calls": 0, "events": 0, "fastmcp_rows": 1}
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0009"
    finally:
        engine.dispose()


def test_migrated_database_matches_sqlalchemy_metadata(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": True})
            assert compare_metadata(context, metadata) == []
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
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_call_events (
                        event_type, tool_call_id, status, created_at
                    ) VALUES (
                        'tool_call_submitted', 'agent-call', 'pending_approval', :now
                    )
                    """
                ),
                {"now": _now()},
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
            counts = (
                conn.execute(
                    text(
                        """
                        SELECT
                            (SELECT count(*) FROM mcp_tool_call_principals
                             WHERE tool_call_id = 'agent-call') AS principals,
                            (SELECT count(*) FROM mcp_tool_call_events
                             WHERE tool_call_id = 'agent-call') AS events
                        """
                    )
                )
                .mappings()
                .one()
            )
            assert counts == {"principals": 0, "events": 0}

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
