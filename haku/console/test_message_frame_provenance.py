"""The message provenance migration preserves pointers into the raw session frame log."""

from __future__ import annotations

import datetime
import json
from uuid import uuid4

import pytest_bazel
from sqlalchemy import create_engine, text

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 15, tzinfo=datetime.UTC)


def test_message_provenance_migration_backfills_observed_assistant_frames(db_url: str) -> None:
    """The historical pointer is rescued only when the old row names its wire message."""
    apply_migrations(db_url, "0044")
    engine = create_engine(sync_database_url(db_url))
    operator_id, session_id, message_id = uuid4(), uuid4(), uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO operators (operator_id, status, created_at, updated_at) VALUES (:id, 'active', :n, :n)"
                ),
                {"id": operator_id, "n": _NOW},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO sessions (
                        session_id, operator_id, surface, status, bridge_token_fingerprint,
                        lease_expires_at, created_at, updated_at
                    ) VALUES (:session_id, :operator_id, 'spa', 'ready', :fingerprint, :n, :n, :n)
                    """
                ),
                {"session_id": session_id, "operator_id": operator_id, "fingerprint": b"fingerprint", "n": _NOW},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO session_messages (
                        message_id, session_id, role, status, content, agent_message_id,
                        tool_uses, created_at, updated_at
                    ) VALUES (:message_id, :session_id, 'assistant', 'complete', '', 'msg_01', '[]'::jsonb, :n, :n)
                    """
                ),
                {"message_id": message_id, "session_id": session_id, "n": _NOW},
            )
            for payload in (
                {"type": "assistant", "message": {"id": "msg_01"}},
                {"type": "assistant", "message": {"id": "msg_01"}},
            ):
                conn.execute(
                    text(
                        """
                        INSERT INTO session_frames (
                            session_id, direction, kind, payload, partial, created_at, updated_at
                        ) VALUES (:session_id, 'from_agent', 'assistant', CAST(:payload AS jsonb), false, :n, :n)
                        """
                    ),
                    {"session_id": session_id, "payload": json.dumps(payload), "n": _NOW},
                )

        apply_migrations(db_url)

        with engine.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT source_first_frame_seq, source_last_frame_seq FROM session_messages WHERE message_id = :id"
                ),
                {"id": message_id},
            ).one() == (1, 2)
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
