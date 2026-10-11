"""Settling stored history matches live settlement and drops the fold's evidence of the deltas."""

from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.history_service.compaction import Compaction, compact
from agentplane.history_service.fold_shadow import FoldShadow
from agentplane.history_service.storage.db import SessionEvent, SessionHistory
from agentplane.history_service.storage.store import Store
from agentplane.history_service.testing.delta_spans import claude_text
from agentplane.history_service.testing.sessions import WRITER
from agentplane.proto import event_log_pb2
from agentplane.thread_fold.models import ThreadEvidence, ThreadNativeLink

# gazelle:include_dep //agentplane/history_service/storage:conftest
# gazelle:include_dep @pypi//protobuf

pytest_plugins = ("agentplane.history_service.storage.conftest",)

# 1-2 start, 3-6 two chunk pairs, 7-8 completion.
SPAN = claude_text(["Hel", "lo"]).entries


async def stored_before_settlement(engine: AsyncEngine, entries: list[event_log_pb2.EventEntry]) -> UUID:
    """A Session whose every entry was stored before settlement existed."""
    session_id = uuid4()
    await Store(engine).bind_writer(session_id, WRITER)
    async with async_sessionmaker(engine).begin() as session:
        session.add_all(
            SessionEvent(
                session_id=session_id, cursor=entry.cursor, payload=entry.SerializeToString(deterministic=True)
            )
            for entry in entries
        )
        row = await session.get_one(SessionHistory, session_id)
        row.source_id, row.last_cursor = entries[-1].origin.source_id, entries[-1].cursor
    return session_id


async def fold_cursors(engine: AsyncEngine, session_id: UUID) -> set[int]:
    """Cursors the fold cites as evidence or links as native frames."""
    async with async_sessionmaker(engine)() as session:
        evidence = await session.scalars(
            select(ThreadEvidence.observation_cursor).where(ThreadEvidence.thread_id == session_id)
        )
        native = await session.scalars(
            select(ThreadNativeLink.source_sequence).where(ThreadNativeLink.thread_id == session_id)
        )
        return set(evidence) | set(native)


async def test_compaction_settles_folded_history_as_live_ingestion_would(engine: AsyncEngine) -> None:
    store = Store(engine)
    folded = await stored_before_settlement(engine, SPAN)
    await FoldShadow(engine=engine, history=store, concurrency=1, page_size=len(SPAN)).cycle()
    # Not folded yet, so its deltas may still be read once; a later run settles it.
    unfolded = await stored_before_settlement(engine, SPAN)
    assert {3, 4, 5, 6} <= await fold_cursors(engine, folded)

    dry_run = await compact(engine, apply=False, batch_size=3)
    assert (dry_run.items, dry_run.cursors) == (1, 4)
    assert dry_run.payload_bytes > 0
    assert len((await store.read_page(folded)).entries) == len(SPAN)

    assert await compact(engine, apply=True, batch_size=3) == dry_run
    live = Store(engine, settle_deltas=True)
    reference = await stored_before_settlement(engine, SPAN[:1])
    await live.append(reference, SPAN)
    compacted, settled_live = await store.read_page(folded), await store.read_page(reference)
    assert [entry.cursor for entry in compacted.entries] == [1, 2, 7, 8]
    assert list(compacted.settlements) == list(settled_live.settlements)
    assert not {3, 4, 5, 6} & await fold_cursors(engine, folded)
    assert len((await store.read_page(unfolded)).entries) == len(SPAN)

    assert await compact(engine, apply=True, batch_size=3) == Compaction()


if __name__ == "__main__":
    pytest_bazel.main()
