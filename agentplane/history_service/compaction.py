"""Settle history stored before settlement, with the code live ingestion settles with.

A Session is walked a batch of entries at a time, each batch in one transaction holding the row
lock `Store.append` takes, so it serializes with ingestion. Only completions the History Service
fold has already folded are settled; the fold never rereads those cursors, so the same transaction
drops the fold's evidence of the settled deltas. A later run picks up the rest. Settled items
already carry a settlement row and are skipped, so a run can stop and resume anywhere.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.history_service.fold_shadow import FOLD_EPOCH
from agentplane.history_service.storage.db import SessionEvent, SessionEventSettlement, SessionHistory
from agentplane.history_service.storage.settlement import completion_target
from agentplane.history_service.storage.store import record_settlement, settlement_for
from agentplane.proto import event_log_pb2
from agentplane.thread_fold.models import ThreadCheckpoint
from agentplane.thread_fold.recording import forget_settled

# gazelle:include_dep @pypi//protobuf

logger = logging.getLogger(__name__)


@dataclass
class Compaction:
    """What settling removes, or would remove in a dry run."""

    items: int = 0
    cursors: int = 0
    payload_bytes: int = 0


async def compact_session(engine: AsyncEngine, session_id: UUID, *, apply: bool, batch_size: int) -> Compaction:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    result = Compaction()
    after = 0
    while True:
        async with sessions.begin() as session:
            await session.scalar(select(SessionHistory.id).where(SessionHistory.id == session_id).with_for_update())
            folded = await session.scalar(
                select(ThreadCheckpoint.through_cursor).where(
                    ThreadCheckpoint.thread_id == session_id, ThreadCheckpoint.projection_epoch == FOLD_EPOCH
                )
            )
            if folded is None:
                return result
            rows = (
                await session.execute(
                    select(SessionEvent.cursor, SessionEvent.payload)
                    .where(
                        SessionEvent.session_id == session_id,
                        SessionEvent.cursor > after,
                        SessionEvent.cursor <= folded,
                    )
                    .order_by(SessionEvent.cursor)
                    .limit(batch_size)
                )
            ).all()
            if not rows:
                return result
            already = set(
                await session.scalars(
                    select(SessionEventSettlement.completion_cursor).where(
                        SessionEventSettlement.session_id == session_id,
                        SessionEventSettlement.completion_cursor.between(rows[0].cursor, rows[-1].cursor),
                    )
                )
            )
            for cursor, payload in rows:
                if cursor in already:
                    continue
                entry = event_log_pb2.EventEntry.FromString(payload)
                if completion_target(entry) is None:
                    continue
                settled = await settlement_for(session, session_id, entry)
                if settled is None:
                    continue
                for cursor_range in settled.ranges:
                    result.cursors += cursor_range.last - cursor_range.first + 1
                    result.payload_bytes += (
                        await session.execute(
                            select(func.coalesce(func.sum(func.length(SessionEvent.payload)), 0)).where(
                                SessionEvent.session_id == session_id,
                                SessionEvent.cursor.between(cursor_range.first, cursor_range.last),
                            )
                        )
                    ).scalar_one()
                result.items += 1
                if apply:
                    await record_settlement(session, session_id, cursor, settled)
                    await forget_settled(session, session_id, FOLD_EPOCH, [settled])
            after = rows[-1].cursor


async def compact(engine: AsyncEngine, *, apply: bool, batch_size: int) -> Compaction:
    async with async_sessionmaker(engine)() as session:
        session_ids = list(await session.scalars(select(SessionHistory.id).order_by(SessionHistory.id)))
    total = Compaction()
    touched = 0
    for session_id in session_ids:
        result = await compact_session(engine, session_id, apply=apply, batch_size=batch_size)
        if result.items:
            touched += 1
            logger.info(
                "session %s: %s items, %s cursors, %s payload bytes",
                session_id,
                result.items,
                result.cursors,
                result.payload_bytes,
            )
        total.items += result.items
        total.cursors += result.cursors
        total.payload_bytes += result.payload_bytes
    logger.info(
        "%s %s items, %s cursors, %s payload bytes across %s of %s sessions",
        "settled" if apply else "would settle",
        total.items,
        total.cursors,
        total.payload_bytes,
        touched,
        len(session_ids),
    )
    return total
