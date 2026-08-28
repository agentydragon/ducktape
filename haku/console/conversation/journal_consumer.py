"""Commit the runner's neutral-operation journal into the durable conversation model (#4667).

The Console half of <../../runner/neutral_operations.py>, behind whatever transport the
generation cut wires up: given a session's parsed `RunnerHello` or `OperationBatch`, this consumer
owns validation, atomic idempotent commit, and the ACK/resume answers. The runner owns everything
before that — native frames never reach here, and an unknown operation kind never reaches here
either: it fails the union parse upstream, which is where a must-understand change belongs on this
negotiated seam (README § Vocabularies across a roll).

**One batch, one transaction, one cursor.** A batch's operations and its `runner_batch_seq` commit
together against `sessions.acked_batch_seq`, taken under the session's row lock. A batch at or
below the cursor is a replay: it re-ACKs the cursor and applies nothing, which is what makes the
runner's retained-batch replay after a reconnect safe. A batch beyond `cursor + 1` is a hole in a
journal whose numbering is dense — evidence of loss, rejected. Rejection is `JournalViolationError`,
raised before anything of the offending batch is committed; the caller's transaction rolls back
whatever the batch had half-done.

**Materialisation goes through the one log writer.** Items and turns land as ordinary
`conversation_log` rows, addressed by the runner identities the journal names
(`runner_item_id`/`runner_turn_id`); a `prompt.admitted` takes text and origin from the session
conversation's `submitted_prompt` row — never from the wire — and materialises the authored prompt
item at the admitted position (<prompt_inbox.py>).

**Frames are beside the journal, never under it.** Operations commit whether or not any
`session_frames` row exists; provenance ranges may dangle ahead of frame persistence, and nothing
here reads or writes a frame.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.conversation import conversation_event, log
from haku.console.conversation.log import LogWriter, UnknownItemError
from haku.console.database_schema import ConversationItem, ConversationTurn, Session, SubmittedPrompt
from haku.console.notifications.conversation_wakes import notify_update
from haku.runner import neutral_operations
from haku.runner.neutral_operations import (
    GENERATION,
    SUPPORTED_NEUTRAL_VERSIONS,
    BatchAck,
    ConsoleResume,
    OperationBatch,
    RunnerHello,
)

logger = logging.getLogger(__name__)


class JournalViolationError(ValueError):
    """The peer broke the journal contract; nothing of the offending message was committed.

    Terminal for the connection, not the session: the runner's journal state is wrong or hostile,
    so the transport drops it and an operator reads the message. The next connection resumes from
    the durable cursor as if the rejected message had never arrived.
    """


class JournalConsumer:
    """The per-session consumer of runner operation batches."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def resume(self, session_id: UUID, hello: RunnerHello) -> ConsoleResume:
        """Answer a runner's hello: the settled contract, and where this session's journal stands.

        Sent on every connection, reconnects included; the answer comes from the durable cursor,
        never from connection state, so any replica answers the same.
        """
        if hello.generation != GENERATION:
            raise JournalViolationError(f"transport generation mismatch: {hello.generation=} != {GENERATION!r}")
        settled = max(set(hello.supported) & set(SUPPORTED_NEUTRAL_VERSIONS), default=None)
        if settled is None:
            raise JournalViolationError(
                f"no common neutral protocol version: {hello.supported=} {SUPPORTED_NEUTRAL_VERSIONS=}"
            )
        async with self._sessions() as db:
            chat = await db.get(Session, session_id)
            if chat is None:
                raise KeyError(session_id)
            if chat.ended_at is not None:
                raise JournalViolationError(f"the session has ended and its journal is closed: {session_id=}")
            return ConsoleResume(neutral_protocol_version=settled, acked_batch_seq=chat.acked_batch_seq or None)

    async def commit(self, session_id: UUID, batch: OperationBatch) -> BatchAck:
        """Commit one batch atomically and answer the cumulative ACK.

        Idempotent by `runner_batch_seq` against the session's cursor: at or below it re-ACKs and
        applies nothing, exactly one past it applies and advances, anything further rejects.
        """
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            chat = await db.get(Session, session_id, with_for_update=True)
            if chat is None:
                raise KeyError(session_id)
            if chat.ended_at is not None:
                raise JournalViolationError(f"the session has ended and its journal is closed: {session_id=}")
            cursor = chat.acked_batch_seq
            if batch.runner_batch_seq <= cursor:
                return BatchAck(acked_batch_seq=cursor)
            if batch.runner_batch_seq != cursor + 1:
                raise JournalViolationError(
                    f"the journal is dense and this batch leaves a hole: {batch.runner_batch_seq=} {cursor=}"
                )
            if batch.diagnostics.unprojected:
                logger.warning(
                    "session %s batch %s left native frames unprojected: %s",
                    session_id,
                    batch.runner_batch_seq,
                    dict(batch.diagnostics.unprojected),
                )
            if batch.operations:
                writer = await log.writer_for(db, chat.conversation_id, session_id=session_id, turn_id=None, now=now)
                try:
                    for operation in batch.operations:
                        await _apply(db, writer, chat, operation, batch_seq=batch.runner_batch_seq)
                except UnknownItemError as error:
                    raise JournalViolationError(
                        f"batch {batch.runner_batch_seq} names what the log cannot resolve: {error}"
                    ) from error
                await notify_update(
                    db,
                    session_id=session_id,
                    conversation_id=chat.conversation_id,
                    position=writer.conversation.next_event_seq - 1,
                )
            chat.acked_batch_seq = batch.runner_batch_seq
            chat.updated_at = now
            return BatchAck(acked_batch_seq=batch.runner_batch_seq)


async def _apply(
    db: AsyncSession, writer: LogWriter, chat: Session, operation: neutral_operations.Operation, *, batch_seq: int
) -> None:
    match operation:
        case neutral_operations.TurnOpened():
            if await _open_turn(db, chat.conversation_id) is not None:
                raise JournalViolationError(f"turn.opened while the previous turn is still open: {operation.turn_id=}")
            if await _runner_turn(db, chat.session_id, operation.turn_id) is not None:
                raise JournalViolationError(f"turn.opened reuses a runner turn id: {operation.turn_id=}")
            opened = await log.opened_turn(
                writer,
                runner_turn_id=operation.turn_id,
                first_frame_seq=operation.provenance.first_frame_seq if operation.provenance is not None else None,
            )
            if isinstance(operation.cause, neutral_operations.PromptsCause):
                for prompt_id in operation.cause.prompt_ids:
                    await _answered_by(db, writer, chat, prompt_id, opened)
        case neutral_operations.TurnEnded():
            ending = await _runner_turn(db, chat.session_id, operation.turn_id)
            if ending is None:
                raise JournalViolationError(f"turn.ended names a turn never opened: {operation.turn_id=}")
            if ending.ended_at is not None:
                raise JournalViolationError(f"turn.ended names a turn already ended: {operation.turn_id=}")
            body = _ended(operation.end)
            ending.last_seq = writer.conversation.next_event_seq
            ending.last_frame_seq = operation.provenance.last_frame_seq if operation.provenance is not None else None
            ending.ended_at = writer.now
            ending.outcome = body.outcome
            ending.failure = body.failure if isinstance(body, conversation_event.TurnFailed) else None
            writer.authored(body, turn_id=ending.turn_id)
        case neutral_operations.PromptAdmitted():
            # Dense in-order commits put the cursor at `batch_seq - 1` here, so the frontier is
            # covered exactly when it lies below this batch; at or above it names output the
            # runner cannot have observed yet.
            if operation.after_batch_seq is not None and operation.after_batch_seq >= batch_seq:
                raise JournalViolationError(
                    f"admission frontier is not committed: {operation.after_batch_seq=} {batch_seq=}"
                )
            row = await db.get(SubmittedPrompt, operation.prompt_id, with_for_update=True)
            if row is None or row.conversation_id != chat.conversation_id:
                raise JournalViolationError(
                    f"prompt.admitted names no submitted prompt of this conversation: {operation.prompt_id=}"
                )
            if row.withdrawn_at is not None:
                raise JournalViolationError(f"prompt.admitted names a withdrawn prompt: {operation.prompt_id=}")
            if row.admitted_at is not None:
                raise JournalViolationError(f"prompt.admitted repeats an admission: {operation.prompt_id=}")
            item_id = await writer.authored_prompt(row.text, row.origin)
            row.admitted_at = writer.now
            row.admitted_item_id = item_id
            if (running := await _open_turn(db, chat.conversation_id)) is not None:
                # Injected into the running exchange, so the transcript attributes it there;
                # admitted between turns it waits for the opening cause that names it.
                item = await db.get(ConversationItem, item_id)
                assert item is not None
                item.turn_id = running.turn_id
        case neutral_operations.ItemOpened():
            if operation.turn_id is None:
                # The wire permits an item outside any turn; the durable log does not
                # (`ck_conversation_event_provenance_frames` pins frame-derived rows to a turn).
                # No current runner emits one — widening the log is the move if one ever does.
                raise JournalViolationError(f"an item outside any turn is not storable: {operation.item_id=}")
            into = await _runner_turn(db, chat.session_id, operation.turn_id)
            if into is None:
                raise JournalViolationError(
                    f"item.opened names a turn never opened: {operation.turn_id=} {operation.item_id=}"
                )
            if (
                await db.scalar(
                    select(ConversationItem.item_id).where(
                        ConversationItem.session_id == chat.session_id,
                        ConversationItem.runner_item_id == operation.item_id,
                    )
                )
                is not None
            ):
                raise JournalViolationError(f"item.opened reuses a runner item id: {operation.item_id=}")
            await writer.runner_opened(operation, turn_id=into.turn_id)
        case neutral_operations.ItemSegment():
            await writer.runner_segment(operation)
        case neutral_operations.ItemCompleted():
            await writer.runner_completed(operation)


async def _answered_by(
    db: AsyncSession, writer: LogWriter, chat: Session, prompt_id: UUID, turn: ConversationTurn
) -> None:
    """Attribute an admitted prompt's item to the turn whose cause names it."""
    row = await db.get(SubmittedPrompt, prompt_id)
    if row is None or row.conversation_id != chat.conversation_id or row.admitted_at is None:
        raise JournalViolationError(f"a turn's cause names a prompt this conversation has not admitted: {prompt_id=}")
    item = await db.get(ConversationItem, row.admitted_item_id)
    assert item is not None  # `ck_submitted_prompt_admission_pair` plus the foreign key
    if item.turn_id is None:
        item.turn_id = turn.turn_id
        item.updated_at = writer.now
    elif item.turn_id != turn.turn_id:
        raise JournalViolationError(f"a turn's cause claims a prompt another turn already answered: {prompt_id=}")


def _ended(end: neutral_operations.TurnEnd) -> conversation_event.TurnEnd:
    match end:
        case neutral_operations.TurnAnswered():
            return conversation_event.TurnAnswered()
        case neutral_operations.TurnAborted():
            return conversation_event.TurnAborted()
        case neutral_operations.TurnFailed():
            return conversation_event.TurnFailed(failure=end.failure)


async def _open_turn(db: AsyncSession, conversation_id: UUID) -> ConversationTurn | None:
    turn: ConversationTurn | None = await db.scalar(
        select(ConversationTurn).where(
            ConversationTurn.conversation_id == conversation_id, ConversationTurn.ended_at.is_(None)
        )
    )
    return turn


async def _runner_turn(db: AsyncSession, session_id: UUID, runner_turn_id: UUID) -> ConversationTurn | None:
    turn: ConversationTurn | None = await db.scalar(
        select(ConversationTurn).where(
            ConversationTurn.session_id == session_id, ConversationTurn.runner_turn_id == runner_turn_id
        )
    )
    return turn
