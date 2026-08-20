"""The 0092 conversation identity cutover is bounded and fail-closed."""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

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


def _conversation(conn: Connection, operator_id: UUID) -> UUID:
    conversation_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO conversation (conversation_id, operator_id, runtime_kind, created_at) "
            "VALUES (:id, :operator_id, 'claude_code', :n)"
        ),
        {"id": conversation_id, "operator_id": operator_id, "n": _NOW},
    )
    return conversation_id


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


def _session_derived_rows(conn: Connection, conversation_id: UUID, operator_id: UUID) -> dict[str, UUID]:
    """Seed the representative rows that 0092 must reset, not preserve."""
    session_id, turn_id, item_id, prompt_id = uuid4(), uuid4(), uuid4(), uuid4()
    conn.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, operator_id, conversation_id, status, bridge_token_fingerprint,
                lease_expires_at, created_at, updated_at
            ) VALUES (:s, :o, :c, 'ready', :fingerprint, :n, :n, :n)
            """
        ),
        {"s": session_id, "o": operator_id, "c": conversation_id, "fingerprint": b"session", "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO conversation_turn (turn_id, conversation_id, session_id, first_seq, started_at)
            VALUES (:t, :c, :s, 1, :n)
            """
        ),
        {"t": turn_id, "c": conversation_id, "s": session_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO conversation_item (
                item_id, conversation_id, session_id, turn_id, item_type, status,
                opened_seq, closed_seq, text, created_at, updated_at
            ) VALUES (:i, :c, :s, :t, 'prompt', 'complete', 1, 2, 'before cutover', :n, :n)
            """
        ),
        {"i": item_id, "c": conversation_id, "s": session_id, "t": turn_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO conversation_prompt (prompt_id, conversation_id, item_id, queued_at)
            VALUES (:p, :c, :i, :n)
            """
        ),
        {"p": prompt_id, "c": conversation_id, "i": item_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO conversation_event (
                conversation_id, event_seq, session_id, turn_id, item_id, kind,
                provenance, body, created_at
            ) VALUES (:c, 1, :s, :t, :i, 'item_started', 'authored', '{}'::jsonb, :n)
            """
        ),
        {"c": conversation_id, "s": session_id, "t": turn_id, "i": item_id, "n": _NOW},
    )
    conn.execute(
        text(
            """
            INSERT INTO session_frames (
                session_id, direction, kind, payload, created_at, updated_at
            ) VALUES (:s, 'to_agent', 'harness_frame', '{}'::jsonb, :n, :n)
            """
        ),
        {"s": session_id, "n": _NOW},
    )
    return {"session_id": session_id, "turn_id": turn_id, "item_id": item_id, "prompt_id": prompt_id}


def test_identity_backfill_is_bounded_and_immutable(db_url: str) -> None:
    apply_migrations(db_url, "0089")
    engine = create_engine(sync_database_url(db_url))
    try:
        with engine.begin() as conn:
            zero = _operator(conn)
            one = _operator(conn)
            many = _operator(conn)
            inactive = _operator(conn, active=False)
            one_agent = _agent(conn, one)
            many_agent = _agent(conn, many)
            _agent(conn, many)
            _agent(conn, inactive)
            conversations = {
                "zero": _conversation(conn, zero),
                "one": _conversation(conn, one),
                "many": _conversation(conn, many),
                "inactive": _conversation(conn, inactive),
            }
            derived = _session_derived_rows(conn, conversations["one"], one)
            attachment_id = uuid4()
            conn.execute(
                text(
                    "INSERT INTO chat_attachment "
                    "(attachment_id, conversation_id, surface, address, attached_at) "
                    "VALUES (:id, :conversation, 'matrix', '!room:example.org', :n)"
                ),
                {"id": attachment_id, "conversation": conversations["one"], "n": _NOW},
            )

        apply_migrations(db_url)

        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT agent_id, access_profile_id FROM conversation WHERE conversation_id = :id"),
                {"id": conversations["one"]},
            ).one()
            assert rows == (one_agent, "chat")
            for key in ("zero", "many", "inactive"):
                assert conn.execute(
                    text("SELECT agent_id, access_profile_id FROM conversation WHERE conversation_id = :id"),
                    {"id": conversations[key]},
                ).one() == (None, None)
            assert (
                conn.execute(
                    text("SELECT count(*) FROM chat_attachment WHERE attachment_id = :id"), {"id": attachment_id}
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(text("SELECT count(*) FROM operators WHERE operator_id = :id"), {"id": one}).scalar_one()
                == 1
            )
            assert (
                conn.execute(text("SELECT count(*) FROM agents WHERE agent_id = :id"), {"id": one_agent}).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM static_credentials AS sc "
                        "JOIN credential_bindings AS cb ON cb.binding_id = sc.binding_id "
                        "WHERE cb.agent_id = :agent"
                    ),
                    {"agent": one_agent},
                ).scalar_one()
                == 1
            )
            for table, key in (
                ("sessions", "session_id"),
                ("conversation_turn", "turn_id"),
                ("conversation_item", "item_id"),
                ("conversation_prompt", "prompt_id"),
                ("conversation_event", "event_seq"),
                ("session_frames", "session_id"),
            ):
                column = "event_seq" if table == "conversation_event" else key
                value = 1 if table == "conversation_event" else derived[key]
                assert (
                    conn.execute(
                        text(f"SELECT count(*) FROM {table} WHERE {column} = :value"), {"value": value}
                    ).scalar_one()
                    == 0
                )

        with pytest.raises(DBAPIError), engine.begin() as conn:
            conn.execute(
                text("UPDATE conversation SET runtime_kind = 'codex_app_server' WHERE conversation_id = :id"),
                {"id": conversations["one"]},
            )
        with pytest.raises(DBAPIError), engine.begin() as conn:
            conn.execute(
                text("UPDATE conversation SET access_profile_id = 'chat' WHERE conversation_id = :id"),
                {"id": conversations["zero"]},
            )
        with pytest.raises(DBAPIError), engine.begin() as conn:
            conn.execute(
                text("UPDATE conversation SET agent_id = :agent WHERE conversation_id = :id"),
                {"agent": many_agent, "id": conversations["one"]},
            )
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversation "
                    "(conversation_id, operator_id, agent_id, runtime_kind, created_at) "
                    "VALUES (:id, :operator_id, :agent_id, 'claude_code', :n)"
                ),
                {"id": uuid4(), "operator_id": one, "agent_id": one_agent, "n": _NOW},
            )
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversation "
                    "(conversation_id, operator_id, access_profile_id, runtime_kind, created_at) "
                    "VALUES (:id, :operator_id, 'chat', 'claude_code', :n)"
                ),
                {"id": uuid4(), "operator_id": one, "n": _NOW},
            )
    finally:
        engine.dispose()


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
                        runtime_kind, created_at
                    ) VALUES (
                        :conversation_id, :operator_id, :agent_id, 'chat',
                        'claude_code', :n
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
                        session_id, operator_id, conversation_id, agent_binding_id, status,
                        bridge_token_fingerprint, lease_expires_at, created_at, updated_at
                    ) VALUES (
                        :session_id, :operator_id, :conversation_id, :binding_id, 'ready',
                        :fingerprint, :n, :n, :n
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
