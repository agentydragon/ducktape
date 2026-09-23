"""Folding an archived batch into the thread's materialized rows, in the transaction that archives it."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.app import thread_fold
from agentplane.app.agent_runtime.events.event_log import EventReplicationError
from agentplane.app.agent_runtime.models import ThreadCheckpoint, ThreadEntity, ThreadEvidence, ThreadNativeLink
from agentplane.app.thread.payloads import write_payloads
from agentplane.app.thread.rows import command_summary, fold_item, ordered_entity_rows
from agentplane.app.thread.views import (
    EntityKind,
    ThreadCommandEntityView,
    ThreadFeedErrorState,
    ThreadItemEntityView,
    ThreadOperationalState,
    ThreadViewState,
)
from agentplane.protocol import event_log_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


THREAD_FOLD_EPOCH = "v1"


class ThreadFoldError(EventReplicationError):
    """A semantic observation could not advance the durable thread fold."""


async def record_thread_fold(
    session: AsyncSession, thread_id: UUID, source_id: str, entries: Sequence[event_log_pb2.EventEntry]
) -> None:
    checkpoint = await session.scalar(
        select(ThreadCheckpoint).where(ThreadCheckpoint.thread_id == thread_id).with_for_update()
    )
    if checkpoint is None:
        state = thread_fold.initial(source_id, THREAD_FOLD_EPOCH)
        operational = None
    else:
        if checkpoint.source_id != source_id:
            raise EventReplicationError(
                f"thread fold source changed at cursor {entries[0].cursor}", cursor=entries[0].cursor
            )
        if checkpoint.projection_epoch != THREAD_FOLD_EPOCH:
            raise ThreadFoldError(
                f"thread fold epoch {checkpoint.projection_epoch!r} must be reset for {THREAD_FOLD_EPOCH!r}",
                cursor=entries[0].cursor,
            )
        state, operational = await _fold_state(session, checkpoint)
    batch = thread_fold.EventBatch(source_id, state.position.through_cursor, tuple(entries))
    result = thread_fold.advance(state, batch, await _prior_entities(session, thread_id, batch))
    await write_payloads(session, thread_id, result.payload_writes)
    # Numbered here rather than in the fold, which reports upserts without saying which are new.
    # A revision takes the conflict path and `set_` omits the index, so `returning` hands back the
    # number the row already had and the counter stays where it is.
    next_index = await _next_entity_index(session, thread_id, result.state.position)
    for values in ordered_entity_rows(thread_id, result, operational):
        stored = await session.execute(
            insert(ThreadEntity)
            .values(**values, entity_index=next_index)
            .on_conflict_do_update(
                index_elements=[
                    ThreadEntity.thread_id,
                    ThreadEntity.projection_epoch,
                    ThreadEntity.entity_kind,
                    ThreadEntity.entity_id,
                ],
                set_=values,
            )
            .returning(ThreadEntity.entity_index)
        )
        next_index = max(next_index, stored.scalar_one() + 1)
    for evidence in result.evidence_upserts:
        values = {
            "thread_id": thread_id,
            "projection_epoch": evidence.projection_epoch,
            "entity_cursor": evidence.entity_cursor,
            "observation_cursor": evidence.observation_cursor,
        }
        await session.execute(insert(ThreadEvidence).values(**values).on_conflict_do_nothing())
        for source_sequence in evidence.source_sequences:
            await session.execute(
                insert(ThreadNativeLink).values(**values, source_sequence=source_sequence).on_conflict_do_nothing()
            )
    checkpoint_values = {
        "source_id": result.state.position.source_id,
        "projection_epoch": result.state.position.projection_epoch,
        "through_cursor": result.state.position.through_cursor,
    }
    await session.execute(
        insert(ThreadCheckpoint)
        .values(thread_id=thread_id, **checkpoint_values)
        .on_conflict_do_update(index_elements=[ThreadCheckpoint.thread_id], set_=checkpoint_values)
    )


async def _fold_state(
    session: AsyncSession, checkpoint: ThreadCheckpoint
) -> tuple[thread_fold.ViewState, ThreadOperationalState]:
    row = await session.scalar(
        select(ThreadEntity).where(
            ThreadEntity.thread_id == checkpoint.thread_id,
            ThreadEntity.projection_epoch == checkpoint.projection_epoch,
            ThreadEntity.entity_kind == EntityKind.VIEW_STATE,
            ThreadEntity.entity_id == "current",
        )
    )
    if row is None:
        raise ValueError("thread checkpoint has no current controls")
    view = ThreadViewState.model_validate(row.state)
    return thread_fold.ViewState(
        thread_fold.Position(checkpoint.source_id, checkpoint.projection_epoch, checkpoint.through_cursor),
        thread_fold.Controls(
            applied_model=view.controls.applied_model,
            active_turn_id=view.controls.active_turn_id,
            harness_state=view.controls.harness_state,
        ),
    ), view.operational


async def set_operational(
    session: AsyncSession,
    thread_id: UUID,
    *,
    status: Literal["active", "ended", "failed"],
    error: str | None,
    error_cursor: int | None = None,
) -> None:
    checkpoint = await session.get(ThreadCheckpoint, thread_id)
    if checkpoint is None:
        return
    row = await session.get(ThreadEntity, (thread_id, checkpoint.projection_epoch, EntityKind.VIEW_STATE, "current"))
    if row is None:
        raise ValueError("thread checkpoint has no current controls")
    view = ThreadViewState.model_validate(row.state)
    row.state = view.model_copy(
        update={
            "operational": ThreadOperationalState(
                status=status,
                last_verified_cursor=str(checkpoint.through_cursor),
                feed_error=(
                    None
                    if error is None
                    else ThreadFeedErrorState(cursor=None if error_cursor is None else str(error_cursor), message=error)
                ),
            )
        }
    ).model_dump(mode="json")


async def _prior_entities(
    session: AsyncSession, thread_id: UUID, batch: thread_fold.EventBatch
) -> thread_fold.PriorEntities:
    required = thread_fold.touched_keys(batch)
    checkpoint = await session.scalar(select(ThreadCheckpoint).where(ThreadCheckpoint.thread_id == thread_id))
    if checkpoint is None:
        return thread_fold.PriorEntities(dict.fromkeys(required.item_ids), dict.fromkeys(required.command_ids))
    rows = await session.scalars(
        select(ThreadEntity).where(
            ThreadEntity.thread_id == thread_id,
            ThreadEntity.projection_epoch == checkpoint.projection_epoch,
            (
                (ThreadEntity.entity_kind == EntityKind.ITEM) & (ThreadEntity.entity_id.in_(required.item_ids))
                | (ThreadEntity.entity_kind == EntityKind.COMMAND) & (ThreadEntity.entity_id.in_(required.command_ids))
            ),
        )
    )
    items: dict[str, thread_fold.Item | None] = dict.fromkeys(required.item_ids)
    commands: dict[str, thread_fold.CommandSummary | None] = dict.fromkeys(required.command_ids)
    for row in rows:
        if row.entity_kind == EntityKind.ITEM:
            items[row.entity_id] = fold_item(ThreadItemEntityView.model_validate(row, from_attributes=True))
        elif row.entity_kind == EntityKind.COMMAND:
            commands[row.entity_id] = command_summary(ThreadCommandEntityView.model_validate(row, from_attributes=True))
    return thread_fold.PriorEntities(items, commands)


async def _next_entity_index(session: AsyncSession, thread_id: UUID, scope: thread_fold.Position) -> int:
    """The scope's next free index, read once so a batch numbers its rows without a query apiece."""
    highest = await session.scalar(
        select(func.max(ThreadEntity.entity_index)).where(
            ThreadEntity.thread_id == thread_id, ThreadEntity.projection_epoch == scope.projection_epoch
        )
    )
    return 0 if highest is None else highest + 1
