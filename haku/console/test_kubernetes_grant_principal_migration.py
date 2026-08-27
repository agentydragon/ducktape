"""The 0097 Kubernetes grant principal cutover preserves leases and fails closed."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 25, tzinfo=datetime.UTC)
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _downgrade(database_url: str, revision: str) -> None:
    engine = create_engine(sync_database_url(database_url))
    try:
        with engine.begin() as conn:
            config = AlembicConfig()
            config.set_main_option("script_location", str(_MIGRATIONS_DIR))
            config.attributes["connection"] = conn
            alembic_command.downgrade(config, revision)
    finally:
        engine.dispose()


def _operator(conn: Connection) -> UUID:
    operator_id = uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    return operator_id


def _agent(conn: Connection, operator_id: UUID) -> tuple[UUID, UUID]:
    agent_id, reservation_id, binding_id = uuid4(), uuid4(), uuid4()
    conn.execute(
        text(
            """
            INSERT INTO agent_name_reservations (
                reservation_id, display_name, display_name_key, agent_id, created_at, activated_at
            ) VALUES (:reservation_id, :name, :name, :agent_id, :n, :n)
            """
        ),
        {"reservation_id": reservation_id, "name": str(agent_id), "agent_id": agent_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO agents (
                agent_id, owner_operator_id, current_name_reservation_id, status,
                created_at, updated_at, activated_at, access_profile_id
            ) VALUES (:agent_id, :operator_id, :reservation_id, 'active', :n, :n, :n, 'public-coder')
            """
        ),
        {"agent_id": agent_id, "operator_id": operator_id, "reservation_id": reservation_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO credential_bindings (
                binding_id, agent_id, kind, status, generation, created_at, updated_at,
                issued_at, activated_at
            ) VALUES (:binding_id, :agent_id, 'static', 'active', 1, :n, :n, :n, :n)
            """
        ),
        {"binding_id": binding_id, "agent_id": agent_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO static_credentials (
                binding_id, secret_reference, credential_fingerprint, created_at
            ) VALUES (:binding_id, :reference, :fingerprint, :n)
            """
        ),
        {"binding_id": binding_id, "reference": f"env:TEST_{agent_id}", "fingerprint": binding_id.bytes, "n": _NOW},
    )
    return agent_id, binding_id


def _conversation_session(conn: Connection, operator_id: UUID, binding_id: UUID) -> UUID:
    conversation_id, session_id = uuid4(), uuid4()
    conn.execute(
        text(
            "INSERT INTO conversation (conversation_id, operator_id, runtime_kind, created_at) "
            "VALUES (:id, :operator_id, 'claude_code', :n)"
        ),
        {"id": conversation_id, "operator_id": operator_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, operator_id, conversation_id, agent_binding_id, status,
                bridge_token_fingerprint, lease_expires_at, created_at, updated_at
            ) VALUES (
                :session_id, :operator_id, :conversation_id, :binding_id, 'ready',
                :fingerprint, :lease, :n, :n
            )
            """
        ),
        {
            "session_id": session_id,
            "operator_id": operator_id,
            "conversation_id": conversation_id,
            "binding_id": binding_id,
            "fingerprint": session_id.bytes,
            "lease": datetime.datetime(2999, 1, 1, tzinfo=datetime.UTC),
            "n": _NOW,
        },
    )
    return session_id


def _source(conn: Connection, binding_id: UUID, *, session_id: UUID | None = None) -> str:
    tool_call_id = f"tc_{uuid4().hex}"
    conn.execute(
        text(
            """
            INSERT INTO mcp_tool_calls (
                tool_call_id, server_id, tool_name, status, created_at, updated_at,
                arguments_json, rationale, approved_at
            ) VALUES (
                :id, 'kubernetes', 'create_grant', 'running', :n, :n,
                '{}'::jsonb, 'grant principal migration test', :n
            )
            """
        ),
        {"id": tool_call_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO mcp_tool_call_principals (tool_call_id, binding_id, session_id)
            VALUES (:id, :binding_id, :session_id)
            """
        ),
        {"id": tool_call_id, "binding_id": binding_id, "session_id": session_id},
    )
    return tool_call_id


def _grant_values(
    *,
    agent_id: UUID,
    source: str,
    principal_kind: str | None = None,
    principal_agent_id: UUID | None = None,
    principal_session_id: UUID | None = None,
) -> dict[str, object]:
    values: dict[str, object] = {
        "grant_id": uuid4(),
        "agent_id": agent_id,
        "source": source,
        "scope": json.dumps({"kind": "namespaces", "namespaces": ["public-coder-agent"]}),
        "rules": json.dumps(
            [
                {
                    "api_groups": [""],
                    "resources": ["pods/log"],
                    "verbs": ["get"],
                    "resource_names": [],
                    "non_resource_urls": [],
                }
            ]
        ),
        "created": _NOW,
        "expires": _NOW + datetime.timedelta(minutes=30),
    }
    if principal_kind is not None:
        values["principal_kind"] = principal_kind
        values["principal_agent_id"] = principal_agent_id
        values["principal_session_id"] = principal_session_id
    return values


_LEGACY_INSERT = text(
    """
    INSERT INTO kubernetes_grants (
        grant_id, agent_id, source_tool_call_id, scope, rules, status, created_at, expires_at
    ) VALUES (
        :grant_id, :agent_id, :source, CAST(:scope AS jsonb), CAST(:rules AS jsonb),
        'active', :created, :expires
    )
    """
)

_NEW_INSERT = text(
    """
    INSERT INTO kubernetes_grants (
        grant_id, owner_agent_id, principal_kind, principal_agent_id, principal_session_id,
        source_tool_call_id, scope, rules, status, created_at, expires_at
    ) VALUES (
        :grant_id, :agent_id, :principal_kind, :principal_agent_id, :principal_session_id,
        :source, CAST(:scope AS jsonb), CAST(:rules AS jsonb), 'active', :created, :expires
    )
    """
)


def test_kubernetes_grant_principal_cutover_preserves_rows_and_enforces_provenance(db_url: str) -> None:
    apply_migrations(db_url, "0096")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            operator_id = _operator(conn)
            agent_id, binding_id = _agent(conn, operator_id)
            legacy_source = _source(conn, binding_id)
            legacy = _grant_values(agent_id=agent_id, source=legacy_source)
            legacy_sibling = _grant_values(agent_id=agent_id, source=legacy_source)
            legacy_sibling["scope"] = json.dumps({"kind": "namespaces", "namespaces": ["public-coder-agent", "haku"]})
            legacy_sibling["created"] = _NOW + datetime.timedelta(seconds=1)
            legacy_sibling["expires"] = _NOW + datetime.timedelta(minutes=20)
            conn.execute(_LEGACY_INSERT, legacy)
            conn.execute(_LEGACY_INSERT, legacy_sibling)

        apply_migrations(db_url, "0097")

        with engine.begin() as conn:
            row = (
                conn.execute(
                    text(
                        """
                        SELECT grant_id, owner_agent_id, principal_kind, principal_agent_id, principal_session_id,
                               source_tool_call_id, scope, rules, status, created_at, expires_at
                        FROM kubernetes_grants WHERE grant_id = :grant_id
                        """
                    ),
                    {"grant_id": legacy["grant_id"]},
                )
                .mappings()
                .one()
            )
            assert row["owner_agent_id"] == agent_id
            assert row["principal_kind"] == "agent"
            assert row["principal_agent_id"] == agent_id
            assert row["principal_session_id"] is None
            assert row["source_tool_call_id"] == legacy_source
            assert row["scope"] == json.loads(str(legacy["scope"]))
            assert row["rules"] == json.loads(str(legacy["rules"]))
            assert row["status"] == "active"
            assert row["created_at"] == _NOW
            assert row["expires_at"] == _NOW + datetime.timedelta(minutes=30)

            source_rows = (
                conn.execute(
                    text(
                        """
                        SELECT grant_id, principal_kind, principal_agent_id, principal_session_id,
                               scope, created_at, expires_at
                        FROM kubernetes_grants
                        WHERE source_tool_call_id = :source
                        """
                    ),
                    {"source": legacy_source},
                )
                .mappings()
                .all()
            )
            assert {source_row["grant_id"] for source_row in source_rows} == {
                legacy["grant_id"],
                legacy_sibling["grant_id"],
            }
            assert all(source_row["principal_kind"] == "agent" for source_row in source_rows)
            assert all(source_row["principal_agent_id"] == agent_id for source_row in source_rows)
            assert all(source_row["principal_session_id"] is None for source_row in source_rows)
            sibling_row = next(
                source_row for source_row in source_rows if source_row["grant_id"] == legacy_sibling["grant_id"]
            )
            assert sibling_row["scope"] == json.loads(str(legacy_sibling["scope"]))
            assert sibling_row["created_at"] == legacy_sibling["created"]
            assert sibling_row["expires_at"] == legacy_sibling["expires"]

            session_id = _conversation_session(conn, operator_id, binding_id)
            session_source = _source(conn, binding_id, session_id=session_id)
            matching = _grant_values(
                agent_id=agent_id, source=session_source, principal_kind="session", principal_session_id=session_id
            )
            conn.execute(_NEW_INSERT, matching)

        def insert_new(values: dict[str, object]) -> None:
            with engine.begin() as conn:
                conn.execute(_NEW_INSERT, values)

        wrong_session = _grant_values(
            agent_id=agent_id, source=session_source, principal_kind="session", principal_session_id=uuid4()
        )
        with pytest.raises(IntegrityError, match="invalid Kubernetes grant source provenance or principal"):
            insert_new(wrong_session)

        malformed = [
            _grant_values(agent_id=agent_id, source=session_source, principal_kind="agent"),
            _grant_values(agent_id=agent_id, source=session_source, principal_kind="agent", principal_agent_id=uuid4()),
            _grant_values(
                agent_id=agent_id,
                source=session_source,
                principal_kind="agent",
                principal_agent_id=agent_id,
                principal_session_id=session_id,
            ),
            _grant_values(agent_id=agent_id, source=session_source, principal_kind="session"),
            _grant_values(agent_id=agent_id, source=session_source, principal_kind="unknown"),
        ]
        for values in malformed:
            with pytest.raises(IntegrityError, match="ck_kubernetes_grants_principal_shape"):
                insert_new(values)

        with engine.begin() as conn:
            static_source = _source(conn, binding_id)
        static_session = _grant_values(
            agent_id=agent_id, source=static_source, principal_kind="session", principal_session_id=session_id
        )
        with pytest.raises(IntegrityError, match="invalid Kubernetes grant source provenance or principal"):
            insert_new(static_session)

        with engine.begin() as conn:
            conn.execute(
                text("UPDATE sessions SET status = 'failed' WHERE session_id = :session_id"), {"session_id": session_id}
            )
            ended_session_source = _source(conn, binding_id, session_id=session_id)
        ended_session = _grant_values(
            agent_id=agent_id, source=ended_session_source, principal_kind="session", principal_session_id=session_id
        )
        with pytest.raises(IntegrityError, match="invalid Kubernetes grant source provenance or principal"):
            insert_new(ended_session)

        with pytest.raises(
            DBAPIError, match="cannot downgrade Kubernetes grants containing session or non-owner Agent principals"
        ):
            _downgrade(db_url, "0096")
    finally:
        engine.dispose()


def test_agent_source_set_survives_upgrade_downgrade_round_trip(db_url: str) -> None:
    apply_migrations(db_url, "0096")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            operator_id = _operator(conn)
            agent_id, binding_id = _agent(conn, operator_id)
            source = _source(conn, binding_id)
            grants = [_grant_values(agent_id=agent_id, source=source) for _ in range(2)]
            grants[1]["scope"] = json.dumps({"kind": "namespaces", "namespaces": ["haku"]})
            grants[1]["created"] = _NOW + datetime.timedelta(seconds=1)
            grants[1]["expires"] = _NOW + datetime.timedelta(minutes=20)
            for grant in grants:
                conn.execute(_LEGACY_INSERT, grant)

        expected_ids = {grant["grant_id"] for grant in grants}
        apply_migrations(db_url, "0097")
        _downgrade(db_url, "0096")
        with engine.connect() as conn:
            downgraded = conn.execute(
                text(
                    "SELECT grant_id, agent_id, source_tool_call_id FROM kubernetes_grants "
                    "WHERE source_tool_call_id = :source"
                ),
                {"source": source},
            ).mappings()
            assert {row["grant_id"] for row in downgraded} == expected_ids

        apply_migrations(db_url, "0097")
        with engine.connect() as conn:
            upgraded = conn.execute(
                text(
                    "SELECT grant_id, owner_agent_id, principal_kind, principal_agent_id, principal_session_id "
                    "FROM kubernetes_grants WHERE source_tool_call_id = :source"
                ),
                {"source": source},
            ).mappings()
            rows = list(upgraded)
            assert {row["grant_id"] for row in rows} == expected_ids
            assert all(row["owner_agent_id"] == agent_id for row in rows)
            assert all(row["principal_kind"] == "agent" for row in rows)
            assert all(row["principal_agent_id"] == agent_id for row in rows)
            assert all(row["principal_session_id"] is None for row in rows)
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
