"""Conversation identity is fail-closed on the current schema.

The 0092/0093 cutover's own backfill test is gone: it migrated to head and asserted against rows
0096 is entitled to delete. See `AGENTS.md` § "Do not keep tests for old migrations".
"""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import IntegrityError

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC)


def _operator(conn: Connection, *, active: bool = True) -> UUID:
    operator_id = uuid4()
    status = "active" if active else "disabled"
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, :status, :n, :n)"),
        {"id": operator_id, "status": status, "n": _NOW},
    )
    return operator_id


def _agent(conn: Connection, operator_id: UUID, *, profile: str = "chat") -> UUID:
    agent_id, reservation_id = uuid4(), uuid4()
    conn.execute(
        text(
            """
            INSERT INTO agent_name_reservations (
                reservation_id, display_name, display_name_key, agent_id, created_at, activated_at
            ) VALUES (:reservation_id, :name, :name_key, :agent_id, :n, :n)
            """
        ),
        {
            "reservation_id": reservation_id,
            "name": str(agent_id),
            "name_key": str(agent_id),
            "agent_id": agent_id,
            "n": _NOW,
        },
    )
    conn.execute(
        text(
            """
            INSERT INTO agents (
                agent_id, owner_operator_id, current_name_reservation_id, status,
                created_at, updated_at, activated_at, access_profile_id
            ) VALUES (:agent_id, :operator_id, :reservation_id, 'active', :n, :n, :n, :profile)
            """
        ),
        {
            "agent_id": agent_id,
            "operator_id": operator_id,
            "reservation_id": reservation_id,
            "profile": profile,
            "n": _NOW,
        },
    )
    binding_id = uuid4()
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
            ) VALUES (:binding_id, :secret_reference, :fingerprint, :n)
            """
        ),
        {
            "binding_id": binding_id,
            "secret_reference": f"env:TEST_{agent_id}",
            "fingerprint": binding_id.bytes,
            "n": _NOW,
        },
    )
    return agent_id


def _binding_for_agent(conn: Connection, agent_id: UUID) -> UUID:
    binding_id = conn.execute(
        text("SELECT binding_id FROM credential_bindings WHERE agent_id = :agent_id"), {"agent_id": agent_id}
    ).scalar_one()
    return UUID(str(binding_id))


def _insert_tool_call(conn: Connection, tool_call_id: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mcp_tool_calls (
                tool_call_id, server_id, tool_name, status,
                created_at, updated_at, arguments_json, rationale
            ) VALUES (
                :tool_call_id, 'server', 'tool', 'pending_approval',
                :n, :n, '{}'::jsonb, 'session attribution migration test'
            )
            """
        ),
        {"tool_call_id": tool_call_id, "n": _NOW},
    )


def test_session_bearers_and_tool_attribution_are_exactly_session_scoped(db_url: str) -> None:
    apply_migrations(db_url)
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            operator_id = _operator(conn)
            agent_id = _agent(conn, operator_id)
            other_agent_id = _agent(conn, operator_id)
            binding_id = _binding_for_agent(conn, agent_id)
            other_binding_id = _binding_for_agent(conn, other_agent_id)
            conversation_id = uuid4()
            conn.execute(
                text(
                    """
                    INSERT INTO conversation (
                        conversation_id, operator_id, agent_id, access_profile_id,
                        harness_kind, runtime_kind, created_at
                    ) VALUES (
                        :conversation_id, :operator_id, :agent_id, 'chat',
                        'claude_code', 'claude_code', :n
                    )
                    """
                ),
                {"conversation_id": conversation_id, "operator_id": operator_id, "agent_id": agent_id, "n": _NOW},
            )
            session_id = uuid4()
            conn.execute(
                text(
                    """
                    INSERT INTO sessions (
                        session_id, operator_id, conversation_id, agent_binding_id,
                        bridge_token_fingerprint, bridge_connected_at, lease_expires_at, created_at, updated_at
                    ) VALUES (
                        :session_id, :operator_id, :conversation_id, :binding_id,
                        :fingerprint, :n, :n, :n, :n
                    )
                    """
                ),
                {
                    "session_id": session_id,
                    "operator_id": operator_id,
                    "conversation_id": conversation_id,
                    "binding_id": binding_id,
                    "fingerprint": b"session-one",
                    "n": _NOW,
                },
            )
            _insert_tool_call(conn, "session-attributed")
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_call_principals (tool_call_id, binding_id, session_id)
                    VALUES ('session-attributed', :binding_id, :session_id)
                    """
                ),
                {"binding_id": binding_id, "session_id": session_id},
            )
            assert conn.execute(
                text(
                    "SELECT binding_id, session_id FROM mcp_tool_call_principals "
                    "WHERE tool_call_id = 'session-attributed'"
                )
            ).one() == (binding_id, session_id)

        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO sessions (
                        session_id, operator_id, conversation_id, agent_binding_id,
                        bridge_token_fingerprint, bridge_connected_at, lease_expires_at, created_at, updated_at
                    ) VALUES (
                        :session_id, :operator_id, :conversation_id, :binding_id,
                        :fingerprint, :n, :n, :n, :n
                    )
                    """
                ),
                {
                    "session_id": uuid4(),
                    "operator_id": operator_id,
                    "conversation_id": conversation_id,
                    "binding_id": binding_id,
                    "fingerprint": b"session-one",
                    "n": _NOW,
                },
            )

        def insert_mismatched_session_binding() -> None:
            with engine.begin() as conn:
                _insert_tool_call(conn, "mismatched-session-binding")
                conn.execute(
                    text(
                        """
                        INSERT INTO mcp_tool_call_principals (tool_call_id, binding_id, session_id)
                        VALUES ('mismatched-session-binding', :binding_id, :session_id)
                        """
                    ),
                    {"binding_id": other_binding_id, "session_id": session_id},
                )

        with pytest.raises(IntegrityError):
            insert_mismatched_session_binding()
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
