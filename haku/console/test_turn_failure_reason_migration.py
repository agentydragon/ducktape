"""0096's conversation drop detaches session-attributed tool-call principals instead of failing.

Prod held `mcp_tool_call_principals` rows bound to live sessions when 0096 first ran, and the
RESTRICT session-binding foreign key vetoed `DELETE FROM conversation` (2026-08-26 deploy outage).
The fresh-database suite never sees that shape — it seeds after migrating to head — so this test
seeds it at 0095 and upgrades through 0096 to head.

Temporary per `AGENTS.md` § "Do not keep tests for old migrations": delete once the chain is
roughly five revisions past 0096 (~0101).
"""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import Connection, create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC)


def _operator(conn: Connection) -> UUID:
    operator_id = uuid4()
    conn.execute(
        text("INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"),
        {"id": operator_id, "n": _NOW},
    )
    return operator_id


def _agent_with_binding(conn: Connection, operator_id: UUID) -> tuple[UUID, UUID]:
    agent_id, reservation_id, binding_id = uuid4(), uuid4(), uuid4()
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
            ) VALUES (:agent_id, :operator_id, :reservation_id, 'active', :n, :n, :n, 'chat')
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
    return agent_id, binding_id


def test_0096_detaches_session_bound_principals_and_keeps_the_ledger(db_url: str) -> None:
    apply_migrations(db_url, "0095")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            operator_id = _operator(conn)
            agent_id, binding_id = _agent_with_binding(conn, operator_id)
            conversation_id, session_id = uuid4(), uuid4()
            conn.execute(
                text(
                    """
                    INSERT INTO conversation (
                        conversation_id, operator_id, agent_id, access_profile_id, runtime_kind, created_at
                    ) VALUES (:conversation_id, :operator_id, :agent_id, 'chat', 'claude_code', :n)
                    """
                ),
                {"conversation_id": conversation_id, "operator_id": operator_id, "agent_id": agent_id, "n": _NOW},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO sessions (
                        session_id, operator_id, conversation_id, agent_binding_id, status,
                        bridge_token_fingerprint, lease_expires_at, created_at, updated_at
                    ) VALUES (
                        :session_id, :operator_id, :conversation_id, :binding_id, 'ready',
                        :fingerprint, :n, :n, :n
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
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_calls (
                        tool_call_id, server_id, tool_name, status,
                        created_at, updated_at, arguments_json, rationale, approved_at
                    ) VALUES (
                        'session-attributed', 'kubernetes', 'pods_list', 'ok',
                        :n, :n, '{}'::jsonb, '0096 cutover test', :n
                    )
                    """
                ),
                {"n": _NOW},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO mcp_tool_call_principals (tool_call_id, binding_id, session_id)
                    VALUES ('session-attributed', :binding_id, :session_id)
                    """
                ),
                {"binding_id": binding_id, "session_id": session_id},
            )

        apply_migrations(db_url)

        with engine.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM conversation")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM sessions")).scalar_one() == 0
            assert conn.execute(
                text(
                    "SELECT operator_id, binding_id, session_id FROM mcp_tool_call_principals "
                    "WHERE tool_call_id = 'session-attributed'"
                )
            ).one() == (None, binding_id, None)
            assert (
                conn.execute(
                    text("SELECT status FROM mcp_tool_calls WHERE tool_call_id = 'session-attributed'")
                ).scalar_one()
                == "ok"
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
