"""The additive control migration preserves original experiment checkpoints."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest_bazel
from sqlalchemy import text

from agentplane.x.postgres_codex.store import Base, Intent, Store
from util.bazel.runfiles import get_required_path
from util.testing.sqlalchemy_engines import postgres_async_engine


async def test_controls_migration_preserves_original_rows(database_url: str) -> None:
    conversation_id, intent_id = uuid4(), uuid4()
    items = [{"type": "message", "role": "user", "content": "preserve this prompt"}]
    async with postgres_async_engine(database_url) as engine:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                CREATE TABLE spike_conversation (
                    id uuid PRIMARY KEY, generation integer NOT NULL, owner uuid,
                    lease_until timestamptz, items jsonb NOT NULL, pending jsonb
                )
            """)
            )
            await connection.run_sync(Base.metadata.tables["spike_intent"].create)
            await connection.execute(
                text("""
                INSERT INTO spike_conversation (id, generation, items)
                VALUES (:id, 0, CAST(:items AS jsonb))
            """),
                {"id": conversation_id, "items": json.dumps(items)},
            )
            await connection.execute(
                Base.metadata.tables["spike_intent"]
                .insert()
                .values(
                    id=intent_id,
                    conversation_id=conversation_id,
                    generation=0,
                    request={"kind": "sampling"},
                    result={"saved": True},
                )
            )
            compaction = get_required_path(
                "_main/agentplane/x/postgres_codex/migrations/0001_compaction.sql"
            ).read_text()
            for statement in compaction.split(";"):
                if statement.strip():
                    await connection.execute(text(statement))
            migration = get_required_path(
                "_main/agentplane/x/postgres_codex/migrations/0002_process_controls.sql"
            ).read_text()
            for statement in migration.split(";"):
                if statement.strip():
                    await connection.execute(text(statement))
            migration = get_required_path(
                "_main/agentplane/x/postgres_codex/migrations/0003_process_output.sql"
            ).read_text()
            for statement in migration.split(";"):
                if statement.strip():
                    await connection.execute(text(statement))
        store = Store(engine)
        owner = await store.claim(conversation_id, lease=timedelta(seconds=30))
        row = await store.read(owner)
        assert row.items == items
        assert not row.stopped
        assert row.executor_session is None
        assert row.context() == items
        assert row.context_items is None
        assert row.instructions is None
        assert row.context_tokens is None
        assert await store.execution_state(owner) == {"cells": [], "processes": []}
        async with store.sessions() as session:
            original = await session.get(Intent, intent_id)
            assert original is not None
            assert original.result == {"saved": True}
        request_id = await store.request_interrupt(conversation_id, owner.generation)
        assert await store.pending_interrupts(owner) == [request_id]


if __name__ == "__main__":
    pytest_bazel.main()
