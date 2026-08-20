"""Runtime identity is a lossless conversation backfill, not a chat-data reset."""

from __future__ import annotations

import datetime
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from haku.console.database_migrate import apply_migrations, sync_database_url

_NOW = datetime.datetime(2026, 8, 19, tzinfo=datetime.UTC)


def test_existing_conversations_are_backfilled_without_losing_chat_data(db_url: str) -> None:
    apply_migrations(db_url, "0086")
    engine = create_engine(sync_database_url(db_url))
    operator_id, conversation_id, attachment_id = uuid4(), uuid4(), uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO operators (operator_id, status, created_at, updated_at) "
                    "VALUES (:operator_id, 'active', :now, :now)"
                ),
                {"operator_id": operator_id, "now": _NOW},
            )
            conn.execute(
                text(
                    "INSERT INTO conversation (conversation_id, operator_id, created_at) "
                    "VALUES (:conversation_id, :operator_id, :now)"
                ),
                {"conversation_id": conversation_id, "operator_id": operator_id, "now": _NOW},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO chat_attachment (
                        attachment_id, conversation_id, surface, address, attached_at, detached_at
                    ) VALUES (:attachment_id, :conversation_id, 'matrix', '!room:example.org', :now, NULL)
                    """
                ),
                {"attachment_id": attachment_id, "conversation_id": conversation_id, "now": _NOW},
            )

        apply_migrations(db_url)

        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT runtime_kind FROM conversation WHERE conversation_id = :conversation_id"),
                    {"conversation_id": conversation_id},
                ).scalar_one()
                == "claude_code"
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM chat_attachment WHERE attachment_id = :attachment_id"),
                    {"attachment_id": attachment_id},
                ).scalar_one()
                == 1
            )
            assert conn.execute(
                text(
                    """
                    SELECT data_type, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'conversation'
                      AND column_name = 'runtime_kind'
                    """
                )
            ).one() == ("text", "NO")

            with pytest.raises(DBAPIError):
                conn.execute(
                    text("UPDATE conversation SET runtime_kind = 'codex_app_server' WHERE conversation_id = :id"),
                    {"id": conversation_id},
                )
    finally:
        engine.dispose()


if __name__ == "__main__":
    pytest_bazel.main()
