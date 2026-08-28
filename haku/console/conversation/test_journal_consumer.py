"""Contracts of the journal consumer: the golden journal committed, and the rejections that keep it.

The `testdata` lines are stage 1's cross-stage contract fixtures — the same bytes the runner-side
projector emits — consumed here into a real migrated database, so the two halves cannot drift
apart while they are built. No `session_frames` row exists in any of these tests: operations are
the authority and commits never gate on frame arrival, which the golden test states outright.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.chat_models import (
    SPA_ORIGIN,
    ItemStatus,
    ItemType,
    MatrixOrigin,
    ReasoningDisclosure,
    ToolOutcome,
    TurnOutcome,
)
from haku.console.conversation import prompt_inbox
from haku.console.conversation.journal_consumer import JournalConsumer, JournalViolationError
from haku.console.database_schema import ConversationEventRow, ConversationTurn, Session, SessionFrame, SubmittedPrompt
from haku.console.session.conftest import session_items
from haku.console.session.store import Store

# The wire's outcome vocabulary collides with the stored one imported above; the alias says whose
# value crosses which boundary.
from haku.runtime.x.bridge.neutral_operations import (
    NEUTRAL_PROTOCOL_VERSION,
    RUNNER_TO_CONSOLE,
    BatchAck,
    FrameRange,
    ItemCompleted,
    ItemOpened,
    ItemSegment,
    MessageCompletion,
    MessageOpen,
    Operation,
    OperationBatch,
    PromptAdmitted,
    RunnerHello,
    ToolCallCompletion,
    ToolOutcome as WireToolOutcome,
    TurnAnswered,
    TurnEnded,
    TurnOpened,
    WakeCause,
)
from util.bazel.runfiles import get_required_path

_TESTDATA = "haku/runtime/x/bridge/testdata"
_PROMPT_ONE = UUID("11111111-1111-4111-8111-111111111101")
_PROMPT_TWO = UUID("11111111-1111-4111-8111-111111111102")
_GOLDEN_TURNS = tuple(UUID(f"22222222-2222-4222-8222-2222222222{n:02}") for n in (1, 2, 3, 4))
_MESSAGE = UUID("33333333-3333-4333-8333-333333333301")
_TOOL_CALL = UUID("33333333-3333-4333-8333-333333333302")
_PROMPT_ONE_TEXT = "Check the build, please."
_PROMPT_TWO_TEXT = "And now the tests."
_ROOM_ORIGIN = MatrixOrigin(address="!ops:haku.test", refs=("$prompt-two",))


def _golden_messages() -> tuple[RunnerHello, list[OperationBatch]]:
    source = Path(f"{_TESTDATA}/neutral_v1_runner_to_console.jsonl")
    path = source if source.exists() else get_required_path(f"ducktape/{_TESTDATA}/neutral_v1_runner_to_console.jsonl")
    hello, *rest = [RUNNER_TO_CONSOLE.validate_json(line) for line in path.read_text().splitlines()]
    assert isinstance(hello, RunnerHello)
    return hello, [message for message in rest if isinstance(message, OperationBatch)]


def _batch(seq: int, *operations: Operation) -> OperationBatch:
    return OperationBatch(
        neutral_protocol_version=NEUTRAL_PROTOCOL_VERSION, runner_batch_seq=seq, operations=operations
    )


def _frame(seq: int) -> FrameRange:
    return FrameRange(first_frame_seq=seq, last_frame_seq=seq)


@pytest.fixture
def consumer(migrated_sessions: async_sessionmaker[AsyncSession]) -> JournalConsumer:
    return JournalConsumer(migrated_sessions)


@pytest.fixture
async def journal_session(session_store: Store, operator_id: UUID) -> tuple[UUID, UUID]:
    """One session and its conversation, as `(session_id, conversation_id)`."""
    view, _token = await session_store.create(operator_id)
    return view.session_id, await session_store.conversation_of(view.session_id)


async def _submit(
    sessions: async_sessionmaker[AsyncSession], conversation_id: UUID, prompt_id: UUID, text: str, origin=SPA_ORIGIN
) -> None:
    async with sessions.begin() as db:
        await prompt_inbox.submit(
            db, conversation_id=conversation_id, text=text, origin=origin, now=datetime.now(UTC), prompt_id=prompt_id
        )


async def _events(sessions: async_sessionmaker[AsyncSession], conversation_id: UUID) -> list[ConversationEventRow]:
    async with sessions() as db:
        rows = await db.scalars(
            select(ConversationEventRow)
            .where(ConversationEventRow.conversation_id == conversation_id)
            .order_by(ConversationEventRow.event_seq)
        )
        return list(rows.all())


async def _turns(sessions: async_sessionmaker[AsyncSession], conversation_id: UUID) -> list[ConversationTurn]:
    async with sessions() as db:
        rows = await db.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.conversation_id == conversation_id)
            .order_by(ConversationTurn.first_seq)
        )
        return list(rows.all())


async def _cursor(sessions: async_sessionmaker[AsyncSession], session_id: UUID) -> int:
    async with sessions() as db:
        chat = await db.get(Session, session_id)
        assert chat is not None
        return chat.acked_batch_seq


async def test_golden_journal_commits_and_acks(
    consumer: JournalConsumer, journal_session: tuple[UUID, UUID], migrated_sessions: async_sessionmaker[AsyncSession]
) -> None:
    session_id, conversation_id = journal_session
    await _submit(migrated_sessions, conversation_id, _PROMPT_ONE, _PROMPT_ONE_TEXT)
    await _submit(migrated_sessions, conversation_id, _PROMPT_TWO, _PROMPT_TWO_TEXT, origin=_ROOM_ORIGIN)
    hello, batches = _golden_messages()

    resume = await consumer.resume(session_id, hello)
    assert resume.neutral_protocol_version == NEUTRAL_PROTOCOL_VERSION
    assert resume.acked_batch_seq is None
    acks = [await consumer.commit(session_id, batch) for batch in batches]
    assert acks == [BatchAck(acked_batch_seq=seq) for seq in (1, 2, 3, 4)]
    assert (await consumer.resume(session_id, hello)).acked_batch_seq == 4
    assert await _cursor(migrated_sessions, session_id) == 4

    turns = await _turns(migrated_sessions, conversation_id)
    assert [(turn.outcome, turn.first_frame_seq, turn.last_frame_seq) for turn in turns] == [
        (TurnOutcome.ANSWERED, None, 15),
        (TurnOutcome.FAILED, 17, 24),
        (TurnOutcome.ABORTED, 25, None),
        (None, 26, None),  # the turn the runner was lost inside stays open
    ]
    assert turns[1].failure == "provider disconnected: overloaded_error"
    assert all(turn.session_id == session_id for turn in turns)
    assert tuple(turn.runner_turn_id for turn in turns) == _GOLDEN_TURNS

    prompt_one, message, prompt_two, tool_call, reasoning = await session_items(migrated_sessions, session_id)
    assert (prompt_one.item_type, prompt_one.item_text, prompt_one.origin) == (
        ItemType.PROMPT,
        _PROMPT_ONE_TEXT,
        SPA_ORIGIN,
    )
    # The wire carried only the prompt id: the text above came from the Console's own inbox row,
    # materialised at the admitted position and attributed to the turn whose cause names it.
    assert prompt_one.turn_id == turns[0].turn_id
    assert (prompt_two.item_text, prompt_two.origin, prompt_two.turn_id) == (
        _PROMPT_TWO_TEXT,
        _ROOM_ORIGIN,
        turns[1].turn_id,
    )
    assert (message.item_type, message.status, message.item_text) == (
        ItemType.MESSAGE,
        ItemStatus.COMPLETE,
        "Checking the build now.",
    )
    assert (message.backend_item_id, message.turn_id, message.runner_item_id) == (
        "msg_0195mgqkkd",
        turns[0].turn_id,
        _MESSAGE,
    )
    assert (tool_call.tool_name, tool_call.arguments, tool_call.call_id) == (
        "Bash",
        {"command": "bazel test //haku/...", "description": "Run the affected tests"},
        str(_TOOL_CALL),
    )
    assert (tool_call.item_text, tool_call.outcome, tool_call.structured) == (
        "3 tests passed.",
        ToolOutcome.SUCCEEDED,
        {"exitCode": 0},
    )
    assert (reasoning.item_type, reasoning.item_text, reasoning.disclosure) == (
        ItemType.REASONING,
        "The exit code settles it.",
        ReasoningDisclosure.SUMMARY,
    )

    events = await _events(migrated_sessions, conversation_id)
    assert [event.event_seq for event in events] == list(range(1, len(events) + 1))  # dense, from 1

    async with migrated_sessions() as db:
        admissions = (await db.scalars(select(SubmittedPrompt).order_by(SubmittedPrompt.submitted_at))).all()
        assert [(row.admitted_item_id, row.withdrawn_at) for row in admissions] == [
            (prompt_one.item_id, None),
            (prompt_two.item_id, None),
        ]
        # Frames independence: the whole journal committed with no frame row ever written.
        assert (await db.scalar(select(SessionFrame.frame_seq).limit(1))) is None


async def test_replayed_batches_reack_without_reapplying(
    consumer: JournalConsumer, journal_session: tuple[UUID, UUID], migrated_sessions: async_sessionmaker[AsyncSession]
) -> None:
    session_id, conversation_id = journal_session
    await _submit(migrated_sessions, conversation_id, _PROMPT_ONE, _PROMPT_ONE_TEXT)
    _hello, batches = _golden_messages()

    first = await consumer.commit(session_id, batches[0])
    replayed = await consumer.commit(session_id, batches[0])
    assert first == replayed == BatchAck(acked_batch_seq=1)
    await consumer.commit(session_id, batches[1])
    applied = await _events(migrated_sessions, conversation_id)

    # A replay after later commits re-ACKs cumulatively and still applies nothing.
    assert await consumer.commit(session_id, batches[0]) == BatchAck(acked_batch_seq=2)
    unchanged = await _events(migrated_sessions, conversation_id)
    assert [(event.event_seq, event.kind, event.body) for event in unchanged] == [
        (event.event_seq, event.kind, event.body) for event in applied
    ]
    (message,) = [
        item for item in await session_items(migrated_sessions, session_id) if item.item_type is ItemType.MESSAGE
    ]
    assert message.item_text == "Checking the build now."  # segments were not appended twice


async def test_a_journal_hole_rejects_and_commits_nothing(
    consumer: JournalConsumer, journal_session: tuple[UUID, UUID], migrated_sessions: async_sessionmaker[AsyncSession]
) -> None:
    session_id, conversation_id = journal_session
    _hello, batches = _golden_messages()
    before = [event.event_seq for event in await _events(migrated_sessions, conversation_id)]
    with pytest.raises(JournalViolationError, match="hole"):
        await consumer.commit(session_id, batches[1])  # seq 2 against an empty journal
    assert await _cursor(migrated_sessions, session_id) == 0
    assert [event.event_seq for event in await _events(migrated_sessions, conversation_id)] == before


async def test_admission_requires_a_pending_inbox_row(
    consumer: JournalConsumer, journal_session: tuple[UUID, UUID], migrated_sessions: async_sessionmaker[AsyncSession]
) -> None:
    session_id, conversation_id = journal_session
    admit = PromptAdmitted(prompt_id=_PROMPT_ONE, after_batch_seq=None, provenance=None)

    with pytest.raises(JournalViolationError, match="no submitted prompt"):
        await consumer.commit(session_id, _batch(1, admit))

    await _submit(migrated_sessions, conversation_id, _PROMPT_ONE, _PROMPT_ONE_TEXT)
    async with migrated_sessions.begin() as db:
        await prompt_inbox.withdraw(db, _PROMPT_ONE, now=datetime.now(UTC))
    with pytest.raises(JournalViolationError, match="withdrawn"):
        await consumer.commit(session_id, _batch(1, admit))

    await _submit(migrated_sessions, conversation_id, _PROMPT_TWO, _PROMPT_TWO_TEXT)
    admit_two = PromptAdmitted(prompt_id=_PROMPT_TWO, after_batch_seq=None, provenance=None)
    await consumer.commit(session_id, _batch(1, admit_two))
    with pytest.raises(JournalViolationError, match="repeats"):
        await consumer.commit(session_id, _batch(2, admit_two))

    # A frontier at or above the batch's own seq names output the runner cannot have observed.
    with pytest.raises(JournalViolationError, match="frontier"):
        await consumer.commit(
            session_id, _batch(2, PromptAdmitted(prompt_id=_PROMPT_ONE, after_batch_seq=2, provenance=None))
        )
    assert await _cursor(migrated_sessions, session_id) == 1


async def test_the_inbox_state_machine_is_pending_then_one_outcome(
    journal_session: tuple[UUID, UUID], migrated_sessions: async_sessionmaker[AsyncSession]
) -> None:
    _session_id, conversation_id = journal_session
    now = datetime.now(UTC)
    async with migrated_sessions.begin() as db:
        await prompt_inbox.submit(
            db, conversation_id=conversation_id, text="hold on", origin=SPA_ORIGIN, now=now, prompt_id=_PROMPT_ONE
        )
    async with migrated_sessions.begin() as db:
        withdrawn = await prompt_inbox.withdraw(db, _PROMPT_ONE, now=now)
        assert withdrawn.withdrawn_at == now
    async with migrated_sessions.begin() as db:
        with pytest.raises(prompt_inbox.PromptNotPendingError, match="already withdrawn"):
            await prompt_inbox.withdraw(db, _PROMPT_ONE, now=now)
        with pytest.raises(prompt_inbox.PromptNotPendingError, match="no submitted prompt"):
            await prompt_inbox.withdraw(db, uuid4(), now=now)


async def test_lifecycle_violations_reject_the_batch_atomically(
    consumer: JournalConsumer, journal_session: tuple[UUID, UUID], migrated_sessions: async_sessionmaker[AsyncSession]
) -> None:
    session_id, _conversation_id = journal_session
    runner_turn, runner_item = uuid4(), uuid4()

    # Nothing is open yet: every reference must name an operation this journal committed.
    with pytest.raises(JournalViolationError, match="cannot resolve"):
        await consumer.commit(session_id, _batch(1, ItemSegment(item_id=runner_item, text="x", provenance=_frame(1))))
    with pytest.raises(JournalViolationError, match="outside any turn"):
        await consumer.commit(
            session_id,
            _batch(1, ItemOpened(item_id=runner_item, turn_id=None, item=MessageOpen(), provenance=_frame(1))),
        )
    with pytest.raises(JournalViolationError, match="never opened"):
        await consumer.commit(
            session_id,
            _batch(1, ItemOpened(item_id=runner_item, turn_id=runner_turn, item=MessageOpen(), provenance=_frame(1))),
        )
    with pytest.raises(JournalViolationError, match="never opened"):
        await consumer.commit(
            session_id, _batch(1, TurnEnded(turn_id=runner_turn, end=TurnAnswered(), provenance=None))
        )

    # A batch is one transaction: the open and complete before the defective segment vanish with it.
    await consumer.commit(
        session_id,
        _batch(
            1,
            TurnOpened(turn_id=runner_turn, cause=WakeCause(), provenance=_frame(1)),
            ItemOpened(item_id=runner_item, turn_id=runner_turn, item=MessageOpen(), provenance=_frame(2)),
        ),
    )
    second_item = uuid4()
    with pytest.raises(JournalViolationError, match="cannot resolve"):
        await consumer.commit(
            session_id,
            _batch(
                2,
                ItemOpened(item_id=second_item, turn_id=runner_turn, item=MessageOpen(), provenance=_frame(3)),
                ItemCompleted(item_id=second_item, completion=MessageCompletion(), provenance=_frame(4)),
                ItemSegment(item_id=second_item, text="after the end", provenance=_frame(5)),
            ),
        )
    assert [item.runner_item_id for item in await session_items(migrated_sessions, session_id)] == [runner_item]

    with pytest.raises(JournalViolationError, match="disagrees"):
        await consumer.commit(
            session_id,
            _batch(
                2,
                ItemCompleted(
                    item_id=runner_item,
                    completion=ToolCallCompletion(outcome=WireToolOutcome.SUCCEEDED, structured=None),
                    provenance=_frame(3),
                ),
            ),
        )
    with pytest.raises(JournalViolationError, match="reuses a runner item id"):
        await consumer.commit(
            session_id,
            _batch(2, ItemOpened(item_id=runner_item, turn_id=runner_turn, item=MessageOpen(), provenance=_frame(3))),
        )
    with pytest.raises(JournalViolationError, match="still open"):
        await consumer.commit(
            session_id, _batch(2, TurnOpened(turn_id=uuid4(), cause=WakeCause(), provenance=_frame(3)))
        )
    await consumer.commit(
        session_id,
        _batch(
            2,
            ItemCompleted(item_id=runner_item, completion=MessageCompletion(), provenance=_frame(3)),
            TurnEnded(turn_id=runner_turn, end=TurnAnswered(), provenance=_frame(4)),
        ),
    )
    with pytest.raises(JournalViolationError, match="reuses a runner turn id"):
        await consumer.commit(
            session_id, _batch(3, TurnOpened(turn_id=runner_turn, cause=WakeCause(), provenance=_frame(5)))
        )
    with pytest.raises(JournalViolationError, match="already ended"):
        await consumer.commit(
            session_id, _batch(3, TurnEnded(turn_id=runner_turn, end=TurnAnswered(), provenance=None))
        )
    assert await _cursor(migrated_sessions, session_id) == 2


async def test_fail_preserves_streamed_text_and_ends_the_journal(
    consumer: JournalConsumer,
    session_store: Store,
    journal_session: tuple[UUID, UUID],
    migrated_sessions: async_sessionmaker[AsyncSession],
) -> None:
    session_id, _conversation_id = journal_session
    runner_turn, runner_item = uuid4(), uuid4()
    await consumer.commit(
        session_id,
        _batch(
            1,
            TurnOpened(turn_id=runner_turn, cause=WakeCause(), provenance=_frame(1)),
            ItemOpened(item_id=runner_item, turn_id=runner_turn, item=MessageOpen(), provenance=_frame(2)),
            ItemSegment(item_id=runner_item, text="half an ans", provenance=_frame(3)),
        ),
    )

    await session_store.fail(session_id, "runner lost")

    (message,) = await session_items(migrated_sessions, session_id)
    assert (message.status, message.item_text) == (ItemStatus.FAILED, "half an ans")
    assert message.closed_seq == message.opened_seq
    with pytest.raises(JournalViolationError, match="ended"):
        await consumer.commit(session_id, _batch(2, ItemSegment(item_id=runner_item, text="wer", provenance=_frame(4))))
    with pytest.raises(JournalViolationError, match="ended"):
        await consumer.resume(session_id, RunnerHello())


async def test_hello_settles_generation_and_version(
    consumer: JournalConsumer, journal_session: tuple[UUID, UUID]
) -> None:
    session_id, _conversation_id = journal_session
    with pytest.raises(JournalViolationError, match="generation"):
        await consumer.resume(session_id, RunnerHello(generation="bridge_v3"))
    with pytest.raises(JournalViolationError, match="version"):
        await consumer.resume(session_id, RunnerHello(supported=(999,)))
    with pytest.raises(KeyError):
        await consumer.resume(uuid4(), RunnerHello())
    with pytest.raises(KeyError):
        await consumer.commit(
            uuid4(), _batch(1, PromptAdmitted(prompt_id=_PROMPT_ONE, after_batch_seq=None, provenance=None))
        )


if __name__ == "__main__":
    pytest_bazel.main()
