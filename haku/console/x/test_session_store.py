"""Contracts of the session store: the rows, and which of them commit together.

A room is a `room_id` string on a session row here and nothing more — no channel is imported, so
this file is what a second channel inherits (<README.md> § The runtime's conftest names no
channel).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from more_itertools import one
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from haku.console.chat_models import (
    OPEN_SESSION_STATUSES,
    AuthoredEventKind,
    ChatMessageRole,
    ChatMessageStatus,
    ChatSurface,
    ConversationEventKind,
    EventProvenance,
    FrameDirection,
    LeaseExpiryReason,
    PromptFate,
    SessionStatus,
    TurnOutcome,
)
from haku.console.database_schema import Session, SessionEvent, SessionMessage, SessionPrompt
from haku.console.x.claude_code.testing.wire import assistant, result, text_block, text_delta
from haku.console.x.conftest import age_lease, lease_of, queued_for_the_room
from haku.console.x.conversation_events import (
    FrameRange,
    MessageCompleted,
    MessageKey,
    Outcome,
    ToolCallCompleted,
    ToolCallStarted,
)
from haku.console.x.conversation_records import ConversationCursor, FrameCursor, TranscriptCursor, TurnCursor
from haku.console.x.session_notifications import SessionEventKind
from haku.console.x.session_store import (
    ADOPTION_GRACE,
    REPLICA,
    BridgeAuthentication,
    MatrixSession,
    SessionStore,
    SpaSession,
)

ROOM = "!room:example.org"


async def test_bridge_authentication_distinguishes_accept_terminal_and_rejected(
    chat_store, operator_id, migrated_sessions
) -> None:
    view, token = await chat_store.create(operator_id, SpaSession())
    session_id = view.session_id

    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    async with migrated_sessions() as db:
        record = await db.get(Session, session_id)
        assert record is not None
        assert record.status == SessionStatus.READY
        assert record.bridge_connected_at is not None
        # Only the hash is ever kept: it lets a retrying runner prove which session it belongs to
        # without the bearer being retained or recoverable.
        assert record.bridge_token_fingerprint == SessionStore._fingerprint(token)

    await chat_store.fail(session_id, "runner failed")
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.TERMINAL
    assert await chat_store.authenticate_bridge(session_id, "wrong") == BridgeAuthentication.REJECTED


async def test_deliberate_close_is_not_reclassified_as_runner_failure(
    chat_store, operator_id, migrated_sessions
) -> None:
    view, token = await chat_store.create(operator_id, SpaSession())

    await chat_store.request_close(operator_id, view.session_id)
    await chat_store.fail(view.session_id, "sandbox runner disconnected")
    closing = await chat_store.get(operator_id, view.session_id)
    assert closing.status == SessionStatus.CLOSING
    assert closing.error is None

    await chat_store.complete_claim_cleanup(view.session_id)
    closed = await chat_store.get(operator_id, view.session_id)
    assert closed.status == SessionStatus.CLOSED
    async with migrated_sessions() as db:
        record = await db.get(Session, view.session_id)
        assert record is not None
        assert record.claim_cleaned_at is not None
        # The credential column is untouched by cleanup: it verifies, it does not also record
        # that the sandbox is gone.
        assert record.bridge_token_fingerprint == SessionStore._fingerprint(token)


async def test_the_cleanup_sweep_offers_ended_sessions_until_their_claim_is_recorded_gone(
    chat_store, operator_id
) -> None:
    """Two facts, two columns: liveness gates the candidate set, `claim_cleaned_at` empties it.

    A live session is never a candidate however its credential reads, and an ended one stays a
    candidate until cleanup stamps it — which is what makes an interrupted teardown retryable and a
    completed one final.
    """
    live, _ = await chat_store.create(operator_id, SpaSession())
    swept, _ = await chat_store.create(operator_id, SpaSession())
    cleaned, _ = await chat_store.create(operator_id, SpaSession())
    for session in (swept, cleaned):
        await chat_store.fail(session.session_id, "runner failed")

    assert sorted(await chat_store.claim_cleanup_candidates()) == sorted([swept.session_id, cleaned.session_id])

    await chat_store.complete_claim_cleanup(cleaned.session_id)
    assert await chat_store.claim_cleanup_candidates() == [swept.session_id]
    assert live.session_id not in await chat_store.claim_cleanup_candidates()


async def test_a_cleaned_up_session_admits_nobody_and_says_which_of_the_two_reasons(chat_store, operator_id) -> None:
    """The credential survives cleanup, so refusal is the status's doing — and it can now tell a
    runner holding the right token to stop (`TERMINAL`) apart from one holding the wrong one
    (`REJECTED`), which blanking the fingerprint collapsed into the latter."""
    view, token = await chat_store.create(operator_id, SpaSession())
    await chat_store.request_close(operator_id, view.session_id)
    await chat_store.complete_claim_cleanup(view.session_id)

    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.TERMINAL
    assert await chat_store.authenticate_bridge(view.session_id, "wrong") == BridgeAuthentication.REJECTED


async def test_a_turn_records_the_message_it_finished_rather_than_the_frames_it_left(
    chat_store, migrated_sessions, operator_id
) -> None:
    """What `_said_anything` was the only cover for, now asked of the turn instead of the log.

    A completed message clears the pointer, records that this turn has spoken, and — for a session
    serving a room — records that the room's outbox holds it, in the transaction that puts it
    there. Reading that off the `assistant` frames could only ever answer "one was recorded",
    which is a different question from either.
    """
    view, token = await chat_store.create(operator_id, MatrixSession(room_id=ROOM))
    session_id = view.session_id
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, session_id, "why did it fail?")
    started = await chat_store.next_prompt(session_id)
    assert started is not None
    assistant_id = await chat_store.begin_assistant(session_id, started.turn_id, source_first_frame_seq=1)

    assert await chat_store.update_assistant(session_id, assistant_id, "a bad config", complete=True)

    state = await chat_store.turn_state(started.turn_id)
    assert state.assistant_message_id is None, "a completed message leaves no half-written answer behind"
    assert (state.streamed, state.said_anything, state.queued_reply) == ("", True, True)
    assert await queued_for_the_room(migrated_sessions, session_id) == ["a bad config"]


async def test_the_rollout_reads_back_in_wire_order_with_a_keyset_cursor(chat_store, operator_id) -> None:
    """Keyset, not offset: the log is append-only, so new frames landing between pages would
    make an offset skip or repeat a row."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    for kind in ("user", "assistant", "result"):
        await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, kind, {"type": kind})

    first = await chat_store.read_frames(str(session.session_id), cursor=None, limit=2, kinds=None)
    rest = await chat_store.read_frames(
        str(session.session_id), cursor=FrameCursor(frame_seq=first[-1].frame_seq + 1), limit=2, kinds=None
    )

    assert [frame.kind for frame in first] == ["user", "assistant"]
    assert [frame.kind for frame in rest] == ["result"]


async def test_the_kinds_filter_skims_without_paging_through_everything(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id, SpaSession())
    for kind in ("user", "system", "assistant", "system", "result"):
        await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, kind, {"type": kind})

    frames = await chat_store.read_frames(str(session.session_id), cursor=None, limit=25, kinds=["assistant", "result"])

    assert [frame.kind for frame in frames] == ["assistant", "result"]


async def test_a_replayed_frame_is_recorded_once(chat_store, operator_id) -> None:
    """The property the whole of stage 4 rests on. An adopted connection re-sends whatever the
    previous console may not have acknowledged, and the agent's own id is what recognises it —
    the cursor is an optimisation, this is the correctness argument."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    frame = assistant(message_id="msg_01abc")

    assert (await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, "assistant", frame)).fresh
    assert not (await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, "assistant", frame)).fresh

    frames = await chat_store.read_frames(str(session.session_id), cursor=None, limit=25, kinds=None)
    assert [frame.kind for frame in frames] == ["assistant"]


async def test_the_resume_cursor_is_the_highest_number_a_runner_gave_this_session(chat_store, operator_id) -> None:
    """What a reconnecting console hands back, and it is per session rather than per connection.

    Two consoles can be adopting one runner's window during a roll, so the cursor has to be a fact
    about the log both can read. It ignores rows no runner numbered — this console's own writes and
    the frames it authors — and it does not have to be the newest row: a `setup_output` recorded
    after them carries no number of its own.
    """
    session, _ = await chat_store.create(operator_id, SpaSession())
    other, _ = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.highest_runner_seq(session.session_id) is None

    await chat_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, "assistant", {"type": "assistant"}, runner_seq=4
    )
    await chat_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, "result", {"type": "result"}, runner_seq=9
    )
    await chat_store.record_frame(session.session_id, FrameDirection.TO_AGENT, "user", {"type": "user"})
    # A neighbouring session's numbering is its own runner's and says nothing about this one.
    await chat_store.record_frame(
        other.session_id, FrameDirection.FROM_AGENT, "result", {"type": "result"}, runner_seq=99
    )

    assert await chat_store.highest_runner_seq(session.session_id) == 9


async def test_two_sessions_may_hold_the_same_agent_id(chat_store, operator_id) -> None:
    """The index is per session, because a replacement session re-awakened from the room can be
    handed the same message ids by an agent with no idea it is a second session."""
    mine, _ = await chat_store.create(operator_id, SpaSession())
    theirs, _ = await chat_store.create(operator_id, SpaSession())
    frame = assistant(message_id="msg_01abc")

    assert (await chat_store.record_frame(mine.session_id, FrameDirection.FROM_AGENT, "assistant", frame)).fresh
    assert (await chat_store.record_frame(theirs.session_id, FrameDirection.FROM_AGENT, "assistant", frame)).fresh


async def test_frames_with_no_identity_are_never_collapsed(chat_store, operator_id) -> None:
    """ "No identity" is not "the same as the last one". Deltas and console-authored rows have
    none, and two of them are two frames."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    delta = {"type": "stream_event", "event": {"type": "content_block_delta"}}

    assert (await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, "stream_event", delta)).fresh
    assert (await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, "stream_event", delta)).fresh

    frames = await chat_store.read_frames(str(session.session_id), cursor=None, limit=25, kinds=["stream_event"])
    assert len(frames) == 2


async def test_deltas_are_in_the_log_but_not_in_the_default_view(chat_store, operator_id) -> None:
    """A turn streams them in the hundreds and the completed `assistant` frame repeats all of it,
    so "everything" means the frames rather than the typing — and naming the kind is how a reader
    asking how far a cut-off answer got says so."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    session_id = session.session_id
    await chat_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, "stream_event", {"type": "stream_event", "event": {}}
    )
    await chat_store.record_frame(session_id, FrameDirection.FROM_AGENT, "result", {"type": "result", "uuid": "r1"})

    default = await chat_store.read_frames(str(session_id), cursor=None, limit=25, kinds=None)
    asked = await chat_store.read_frames(str(session_id), cursor=None, limit=25, kinds=["stream_event"])

    assert [frame.kind for frame in default] == ["result"]
    assert [frame.kind for frame in asked] == ["stream_event"]


async def test_one_session_never_reads_another_session_frames(chat_store, operator_id) -> None:
    mine, _ = await chat_store.create(operator_id, SpaSession())
    theirs, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.record_frame(mine.session_id, FrameDirection.FROM_AGENT, "assistant", {"type": "assistant"})
    await chat_store.record_frame(theirs.session_id, FrameDirection.FROM_AGENT, "result", {"type": "result"})

    frames = await chat_store.read_frames(str(mine.session_id), cursor=None, limit=25, kinds=None)

    assert [frame.kind for frame in frames] == ["assistant"]


async def test_the_frame_inspector_opens_on_the_end_of_the_log_and_walks_back(chat_store, operator_id) -> None:
    """The console's read is the reverse keyset of the MCP reader's.

    A long session's interesting frames are its last ones, so the first page is the tail and the
    cursor walks towards the start — while each page itself stays in wire order, because reading
    a protocol log backwards within a page is not what anyone means by reading it.
    """
    session, _ = await chat_store.create(operator_id, SpaSession())
    for kind in ("system", "user", "assistant", "result"):
        await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, kind, {"type": kind})

    newest = await chat_store.read_operator_frames(
        operator_id, session.session_id, before_seq=None, limit=2, kinds=None
    )
    earlier = await chat_store.read_operator_frames(
        operator_id, session.session_id, before_seq=newest.next_before_seq, limit=2, kinds=None
    )

    assert [frame.kind for frame in newest.frames] == ["assistant", "result"]
    assert [frame.kind for frame in earlier.frames] == ["system", "user"]
    # A short page is the first one; this page is full, so whether it is the first is unknown.
    assert earlier.next_before_seq == earlier.frames[0].frame_seq


async def test_the_frame_inspector_leaves_deltas_out_until_they_are_asked_for(chat_store, operator_id) -> None:
    """The same default view the MCP reader gets, for the same reason — one policy, one place."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    session_id = session.session_id
    await chat_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, "stream_event", {"type": "stream_event", "event": {}}
    )
    await chat_store.record_frame(session_id, FrameDirection.FROM_AGENT, "result", {"type": "result"})

    default = await chat_store.read_operator_frames(operator_id, session_id, before_seq=None, limit=25, kinds=None)
    asked = await chat_store.read_operator_frames(
        operator_id, session_id, before_seq=None, limit=25, kinds=["stream_event"]
    )

    assert [frame.kind for frame in default.frames] == ["result"]
    assert [frame.kind for frame in asked.frames] == ["stream_event"]
    assert default.next_before_seq is None


async def test_the_frame_inspector_refuses_a_session_another_operator_owns(chat_store, operator_id) -> None:
    """The MCP reader is deliberately unscoped (R5.3a); a browser surface must never be."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, "result", {"type": "result"})

    with pytest.raises(KeyError):
        await chat_store.read_operator_frames(uuid4(), session.session_id, before_seq=None, limit=25, kinds=None)


async def test_a_frame_reaches_the_inspector_with_its_payload_whole(chat_store, operator_id) -> None:
    """No clipping on this path. The MCP reader clips for context budget; here the wire *is* the
    answer, and a clipped frame would be the lossy projection again one level down."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    payload = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * 20_000}]}}
    await chat_store.record_frame(session.session_id, FrameDirection.TO_AGENT, "user", payload)

    page = await chat_store.read_operator_frames(operator_id, session.session_id, before_seq=None, limit=25, kinds=None)

    assert page.frames[0].payload == payload
    assert page.frames[0].direction == FrameDirection.TO_AGENT


async def test_conversations_come_back_newest_first_with_the_room_they_served(chat_store, operator_id) -> None:
    await chat_store.create(operator_id, SpaSession())
    matrix, _ = await chat_store.create(operator_id, MatrixSession(room_id="!room:example.org"))

    conversations = await chat_store.list_conversations(cursor=None, limit=10)

    assert conversations[0].session_id == matrix.session_id
    assert conversations[0].room_id == "!room:example.org"
    assert conversations[1].room_id is None


async def test_a_session_created_between_two_pages_cannot_shift_what_the_second_one_holds(
    chat_store, operator_id
) -> None:
    """The keyset's whole point: this order grows at the top, and an offset counts from there, so
    a session created mid-walk would push the first page's last row into the second page again."""
    older, _ = await chat_store.create(operator_id, SpaSession())
    newer, _ = await chat_store.create(operator_id, SpaSession())

    # Two rows for a page of one: the extra row is the one the tool's cursor names.
    first, resume = await chat_store.list_conversations(cursor=None, limit=2)
    await chat_store.create(operator_id, SpaSession())
    second = await chat_store.list_conversations(cursor=ConversationCursor.of(resume), limit=1)

    assert first.session_id == newer.session_id
    assert [conversation.session_id for conversation in second] == [older.session_id]


async def test_two_sessions_created_in_one_instant_are_paged_exactly_once_each(
    chat_store, operator_id, migrated_sessions
) -> None:
    """`created_at` ties, so it does not order the corpus on its own — a cursor naming only the
    timestamp would either step over one of the pair or hand it out on both pages."""
    first, _ = await chat_store.create(operator_id, SpaSession())
    second, _ = await chat_store.create(operator_id, SpaSession())
    async with migrated_sessions.begin() as db:
        await db.execute(
            update(Session)
            .where(Session.session_id.in_([first.session_id, second.session_id]))
            .values(created_at=datetime(2026, 8, 12, 9, 0, tzinfo=UTC))
        )

    page, resume = await chat_store.list_conversations(cursor=None, limit=2)
    rest = await chat_store.list_conversations(cursor=ConversationCursor.of(resume), limit=10)

    assert [conversation.session_id for conversation in [page, *rest]] == sorted(
        [first.session_id, second.session_id], reverse=True
    )


async def test_exchanges_page_by_their_own_keyset(chat_store, operator_id) -> None:
    """`(started_at, turn_id)`, because two exchanges of one session can share a start instant and
    a cursor naming only the timestamp would step over one of a tied pair."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    for index in range(3):
        await chat_store.enqueue_prompt(operator_id, view.session_id, f"prompt {index}")
        turn = await chat_store.next_prompt(view.session_id)
        assert turn is not None
        await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)

    # One row past the page, exactly as the tool asks: the cursor names the first row not returned.
    *page, resume = await chat_store.list_turns(view.session_id, cursor=None, limit=3)
    rest = await chat_store.list_turns(view.session_id, cursor=TurnCursor.of(resume), limit=5)

    assert len(page) == 2
    assert [turn.turn_id for turn in rest] == [resume.turn_id]


async def test_a_turn_ends_at_the_frame_it_names_rather_than_at_the_head_of_the_log(chat_store, operator_id) -> None:
    """The CLI emits a `command_lifecycle` frame just after the `result` one, and the recorder
    writes it while the turn is still being closed — so a bound taken from the log here swallows a
    frame the turn did not produce."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    ending = await chat_store.record_frame(view.session_id, FrameDirection.FROM_AGENT, "result", result(uuid="r1"))
    await chat_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, "command_lifecycle", {"type": "command_lifecycle"}
    )

    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED, last_frame_seq=ending.frame_seq)

    [record] = await chat_store.list_turns(view.session_id, cursor=None, limit=5)
    assert record.last_frame_seq == ending.frame_seq


async def test_a_turn_that_ended_on_no_frame_is_bounded_by_the_ones_it_recorded(chat_store, operator_id) -> None:
    """A failure has no ending frame to name, and the session's log is not a bound either: what
    came before the turn opened belongs to no turn of its own, and reporting it would hand a reader
    a range that ends before it starts."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.record_frame(view.session_id, FrameDirection.FROM_AGENT, "system", {"type": "system"})
    await chat_store.enqueue_prompt(operator_id, view.session_id, "first")
    silent = await chat_store.next_prompt(view.session_id)
    assert silent is not None
    await chat_store.end_turn(silent.turn_id, TurnOutcome.FAILED)
    await chat_store.enqueue_prompt(operator_id, view.session_id, "second")
    spoke = await chat_store.next_prompt(view.session_id)
    assert spoke is not None
    answer = await chat_store.record_frame(
        view.session_id, FrameDirection.FROM_AGENT, "assistant", assistant(text_block("half an answer"))
    )

    await chat_store.end_turn(spoke.turn_id, TurnOutcome.FAILED)

    brackets = {
        record.turn_id: (record.first_frame_seq, record.last_frame_seq)
        for record in await chat_store.list_turns(view.session_id, cursor=None, limit=5)
    }
    assert brackets[silent.turn_id][1] is None, "it recorded nothing, and the frame before it is not its own"
    assert brackets[spoke.turn_id] == (answer.frame_seq, answer.frame_seq)


async def test_the_transcript_reads_the_conversation_rather_than_the_protocol(chat_store, operator_id) -> None:
    """The neutral half of the read surface: what a session meant, with a way back to the frames
    it was read off."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, "assistant", assistant(text_block("hi"), message_id="msg_1")
    )
    await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, "result", result(uuid="r1"))

    transcript = await chat_store.read_transcript(session.session_id, cursor=None, limit=10)

    assert [entry.kind for entry in transcript.entries] == ["message", "turn_end"]
    assert transcript.entries[0].text == "hi"
    named = await chat_store.read_frames(
        session.session_id,
        cursor=FrameCursor(frame_seq=transcript.entries[0].provenance.first_frame_seq),
        limit=1,
        kinds=None,
    )
    assert named[0].payload["message"]["id"] == "msg_1", "provenance points at the frame it was read off"


async def test_a_transcript_page_holds_what_the_whole_session_holds_at_that_position(chat_store, operator_id) -> None:
    """The fold runs from the session's first frame however far in the cursor is, so a page
    boundary cannot close a message the whole session does not end there."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    for index in range(3):
        await chat_store.record_frame(
            session.session_id,
            FrameDirection.FROM_AGENT,
            "assistant",
            assistant(text_block(f"{index} "), message_id="msg_1"),
        )
        await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, "result", result(uuid=f"r{index}"))

    whole = await chat_store.read_transcript(session.session_id, cursor=None, limit=100)
    first = await chat_store.read_transcript(session.session_id, cursor=None, limit=3)
    rest = await chat_store.read_transcript(session.session_id, cursor=TranscriptCursor(index=3), limit=100)

    assert first.entries + rest.entries == whole.entries
    assert [entry.index for entry in whole.entries] == list(range(len(whole.entries)))


async def test_deltas_are_not_on_the_transcript_at_all(chat_store, operator_id) -> None:
    """They are recorded, and `read_rollout` still serves them by name — but the prose they carry
    arrives again whole in the message that follows, so a reader of a finished conversation would
    get it twice."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, "stream_event", text_delta("h"))
    await chat_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, "assistant", assistant(text_block("hi"), message_id="msg_1")
    )

    transcript = await chat_store.read_transcript(session.session_id, cursor=None, limit=10)

    assert [entry.kind for entry in transcript.entries] == ["message"]
    assert transcript.unreadable is None


async def test_operator_conversation_read_surface_keeps_inventory_and_transcript_separate(
    chat_store, operator_id
) -> None:
    """The Console list is light, while detail carries messages and turn summaries."""
    await chat_store.create(operator_id, SpaSession())
    matrix, matrix_token = await chat_store.create(operator_id, MatrixSession(room_id="!room:example.org"))
    assert await chat_store.authenticate_bridge(matrix.session_id, matrix_token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, matrix.session_id, "What is happening?")

    summaries = await chat_store.list_operator_conversations(operator_id, limit=10)
    detail = await chat_store.get_operator_conversation(operator_id, matrix.session_id)

    assert summaries[0].session_id == matrix.session_id
    assert summaries[0].surface == ChatSurface.MATRIX
    assert summaries[0].room_id == "!room:example.org"
    assert summaries[0].message_count == 1
    assert detail.surface == ChatSurface.MATRIX
    assert detail.messages[0].content == "What is happening?"
    assert detail.turns == []


async def test_a_second_prompt_is_refused_while_a_turn_is_open(chat_store, operator_id) -> None:
    """The gate `enqueue_prompt` used to keep was `status == READY`, which doubled as "not
    mid-turn" only because `enqueue_prompt` itself had written `responding`. Asking the turn
    directly is what keeps R2.2 — hold a batch until the turn ends — from silently becoming
    fold-into-turn with no fold path wired.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "first")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None

    with pytest.raises(RuntimeError, match="turn is already in flight"):
        await chat_store.enqueue_prompt(operator_id, view.session_id, "second")

    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)
    await chat_store.enqueue_prompt(operator_id, view.session_id, "second")


async def test_a_prompt_is_taken_off_the_queue_rather_than_found_by_status(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The transcript row used to be the queue: `COMPLETE` on a user row meant "handed to the
    model" while on an assistant row it means "the answer finished". The queue row is what says a
    prompt is waiting now, and claiming it is what says it no longer is."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?")

    async with migrated_sessions() as db:
        queued = list(await db.scalars(select(SessionPrompt)))
    assert [(row.session_id, row.claimed_at) for row in queued] == [(view.session_id, None)]

    turn = await chat_store.next_prompt(view.session_id)

    assert turn is not None
    assert turn.prompt == "why did it fail?", "the text comes from the transcript row the queue names"
    async with migrated_sessions() as db:
        [claimed] = list(await db.scalars(select(SessionPrompt)))
    assert claimed.claimed_at is not None
    assert claimed.message_id == turn.message_id


async def test_one_prompt_in_flight_is_a_schema_property(chat_store, migrated_sessions, operator_id) -> None:
    """It used to be a scan of the transcript for a `pending` row plus the rule that only one
    exists — so two replicas racing on one session could each conclude they may accept."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "first")

    async with migrated_sessions() as db:
        message = SessionMessage(
            message_id=uuid4(),
            session_id=view.session_id,
            role=ChatMessageRole.USER,
            status=ChatMessageStatus.PENDING,
            content="second",
            error=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(message)
        db.add(
            SessionPrompt(
                prompt_id=uuid4(),
                session_id=view.session_id,
                message_id=message.message_id,
                queued_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()


async def test_a_pending_row_with_no_queue_row_is_not_a_prompt(chat_store, migrated_sessions, operator_id) -> None:
    """The queue is the only admission record. A `pending` transcript row on its own is the
    residue of a session that was already stuck, not a prompt waiting to run."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    async with migrated_sessions.begin() as db:
        db.add(
            SessionMessage(
                message_id=uuid4(),
                session_id=view.session_id,
                role=ChatMessageRole.USER,
                status=ChatMessageStatus.PENDING,
                content="orphaned transcript row",
                error=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    assert await chat_store.next_prompt(view.session_id) is None
    # And it does not block a real one.
    await chat_store.enqueue_prompt(operator_id, view.session_id, "mine")
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    assert turn.prompt == "mine"


@pytest.fixture
async def accepted_prompt(chat_store: SessionStore, operator_id: UUID) -> tuple[UUID, UUID]:
    """A ready session and one prompt it has accepted — where `prompt_fate` starts from.

    Room-backed because `prompt_fate` exists for a source that acknowledges — but a room is a
    `room_id` on the session row here, not a homeserver: the caller that asks the question is
    tested in <channels/matrix/test_session.py>.
    """
    view, token = await chat_store.create(operator_id, MatrixSession(room_id=ROOM))
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    prompt = await chat_store.enqueue_prompt(operator_id, view.session_id, "what were we doing")
    return view.session_id, prompt.message_id


async def test_a_prompt_still_waiting_for_its_turn_is_in_flight(chat_store, accepted_prompt) -> None:
    _, message_id = accepted_prompt

    assert await chat_store.prompt_fate(message_id) == PromptFate.IN_FLIGHT


@pytest.mark.parametrize("outcome", list(TurnOutcome))
async def test_a_prompt_is_complete_once_its_turn_ends_however_it_ended(
    chat_store, accepted_prompt, outcome: TurnOutcome
) -> None:
    """The source of an acknowledgement is owed an answer about the turn, not a good one: a batch
    held until its turn succeeds is a batch held forever the first time one does not."""
    session_id, message_id = accepted_prompt
    turn = await chat_store.next_prompt(session_id)
    assert turn is not None
    await chat_store.end_turn(turn.turn_id, outcome)

    assert await chat_store.prompt_fate(message_id) == PromptFate.COMPLETED


async def test_a_prompt_a_dead_session_never_claimed_is_lost(chat_store, accepted_prompt) -> None:
    """`message_drops.md` I3, at the layer that can see it. The row is still there and still
    unclaimed; what makes it unanswerable is that its session is over and the supervisor's
    replacement has a different `session_id`, so `next_prompt` will never read it."""
    session_id, message_id = accepted_prompt
    await chat_store.fail(session_id, "the sandbox went away")

    assert await chat_store.prompt_fate(message_id) == PromptFate.LOST


async def test_a_turn_left_open_by_a_dead_session_is_lost_too(chat_store, accepted_prompt) -> None:
    """An open turn is only in flight while something holds the session that would close it."""
    session_id, message_id = accepted_prompt
    assert await chat_store.next_prompt(session_id) is not None
    await chat_store.fail(session_id, "the runtime failed")

    assert await chat_store.prompt_fate(message_id) == PromptFate.LOST


async def test_a_prompt_whose_session_was_deleted_is_lost(chat_store, accepted_prompt, migrated_sessions) -> None:
    """The transcript row goes with its session, and a fate that cannot be read is not a hold."""
    session_id, message_id = accepted_prompt
    async with migrated_sessions.begin() as db:
        await db.execute(delete(Session).where(Session.session_id == session_id))

    assert await chat_store.prompt_fate(message_id) == PromptFate.LOST


async def test_the_view_says_responding_for_as_long_as_the_turn_is_open(chat_store, operator_id) -> None:
    """`status` is the SPA's contract, so the column underneath can stop carrying turn state
    without a frontend release — the view derives it from the open turn instead."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "work")
    assert (await chat_store.get(operator_id, view.session_id)).status == SessionStatus.READY, (
        "a queued prompt is not a turn in flight"
    )

    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    assert (await chat_store.get(operator_id, view.session_id)).status == SessionStatus.RESPONDING
    assert await chat_store.status(view.session_id) == SessionStatus.READY, (
        "the column itself no longer carries turn state"
    )

    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)
    assert (await chat_store.get(operator_id, view.session_id)).status == SessionStatus.READY


async def test_a_session_that_ended_does_not_report_a_turn_it_left_open(
    chat_store, migrated_sessions, operator_id
) -> None:
    """A replica losing its pod mid-turn closes nothing, so the open row is exactly the record of
    an abandoned exchange — and must not make a failed session read as still working."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "work")
    assert await chat_store.next_prompt(view.session_id) is not None
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 1

    [record] = await chat_store.list_turns(str(view.session_id), cursor=None, limit=10)
    assert record.ended_at is None, "nothing ran to close it, and the record should say so"
    assert (await chat_store.get(operator_id, view.session_id)).status == SessionStatus.FAILED


async def test_abort_is_refused_until_a_turn_is_actually_running(chat_store, operator_id) -> None:
    """An idle session has nothing to interrupt, and saying so is the point of the 409.

    A *queued* prompt is not a turn either, and this is where that used to go wrong twice over:
    the first check asked "is this session's abort event registered in this process", true for
    the whole life of the runner bridge; the second asked whether the session's status was
    `responding`, which `enqueue_prompt` set before any turn started. Both accepted an abort
    with nothing to abort, and the event then sat set until the next turn, killing it on
    arrival. The abort now names the open turn, which does not exist until the prompt is handed
    to the model.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    # The bridge handshake is what takes a session from provisioning to ready, and only a
    # ready session accepts a prompt.
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    assert await chat_store.request_abort(view.session_id) is False

    await chat_store.enqueue_prompt(operator_id, view.session_id, "work")
    assert await chat_store.request_abort(view.session_id) is False, "a queued prompt is not a turn"

    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    assert await chat_store.request_abort(view.session_id) is True

    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)
    assert await chat_store.request_abort(view.session_id) is False


async def test_abort_reaches_the_replica_running_the_turn(
    migrated_db_url, chat_store, notifications, operator_id
) -> None:
    """The two ends of an abort are on different pods, so it has to cross the database.

    The abort event belongs to whichever replica holds the runner's bridge websocket, while
    `POST .../abort` is balanced across all of them — at `replicas: 2` the operator's abort
    button therefore failed with a spurious 409 about half the time. Two stores over two
    engines is what reproduces that; a single store would pass on the in-process path this
    change removes.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "work")
    assert await chat_store.next_prompt(view.session_id) is not None, "the turn the abort names"

    other_engine = create_async_engine(migrated_db_url, pool_pre_ping=True)
    try:
        requesting = SessionStore(async_sessionmaker(other_engine, expire_on_commit=False))
        async with notifications.subscribe(SessionEventKind.ABORT, view.session_id) as aborted:
            assert await requesting.request_abort(view.session_id) is True
            async with asyncio.timeout(30):
                await aborted.wait()
    finally:
        await other_engine.dispose()


async def test_a_session_records_the_surface_it_was_created_for(chat_store, migrated_sessions, operator_id) -> None:
    """Which surface a conversation belonged to has to outlive the conversation.

    `matrix_conversation` holds one binding, so before this the room link vanished the moment
    the supervisor replaced a session, and a past Matrix session read as an SPA one.
    """
    spa, _ = await chat_store.create(operator_id, SpaSession())
    matrix, _ = await chat_store.create(operator_id, MatrixSession(room_id="!room:allegedly.works"))

    async with migrated_sessions() as db:
        assert (await db.get(Session, spa.session_id)).surface == ChatSurface.SPA
        assert (await db.get(Session, spa.session_id)).room_id is None
        assert (await db.get(Session, matrix.session_id)).surface == ChatSurface.MATRIX
        assert (await db.get(Session, matrix.session_id)).room_id == "!room:allegedly.works"


async def test_a_room_cannot_be_recorded_without_the_matrix_surface(migrated_sessions, operator_id) -> None:
    """The pairing is a schema rule, not only a call-signature one — the columns outlive it."""
    async with migrated_sessions.begin() as db:
        db.add(
            Session(
                session_id=uuid4(),
                operator_id=operator_id,
                surface=ChatSurface.SPA,
                room_id="!room:allegedly.works",
                status=SessionStatus.PROVISIONING,
                bridge_token_fingerprint=b"x" * 32,
                bridge_connected_at=None,
                error=None,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()


async def authored_events(migrated_sessions, session_id: UUID) -> list[SessionEvent]:
    """Every row of *session_id*'s stream the console authored rather than folded, oldest first."""
    async with migrated_sessions() as db:
        return list(
            await db.scalars(
                select(SessionEvent)
                .where(SessionEvent.session_id == session_id, SessionEvent.provenance == EventProvenance.AUTHORED)
                .order_by(SessionEvent.event_seq)
            )
        )


async def test_a_replica_taking_a_session_over_records_who_it_took_it_from(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The fact the frame log cannot hold: a lease changing hands crosses no wire.

    It happens on every roll, and until this row the only evidence a console had rolled was
    somebody's recollection — which three hypotheses in the 2026-08-15 drop investigation turned on.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    with patch("haku.console.x.session_store.REPLICA", "haku-console-b"):
        assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await age_lease(migrated_sessions, view.session_id, seconds_ago=1)

    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    taken = one(await authored_events(migrated_sessions, view.session_id))
    assert taken.kind == AuthoredEventKind.SESSION_ADOPTED
    assert taken.body == {"previous_holder": "haku-console-b", "holder": REPLICA}
    # A fact about the session, not about an exchange — which is what makes it writable at all for
    # a session that never reached a turn, and what keeps re-projection from seeing it.
    assert (taken.turn_id, taken.source_first_frame_seq) == (None, None)


async def test_the_first_runner_to_attach_is_not_a_takeover_and_neither_is_its_redial(
    chat_store, migrated_sessions, operator_id
) -> None:
    """A session being served for the first time changed no hands, and a socket dropping and
    redialling to the same replica changes none either. Recording those would make every session's
    stream open with an ownership event that says nothing happened."""
    view, token = await chat_store.create(operator_id, SpaSession())

    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    assert await authored_events(migrated_sessions, view.session_id) == []


async def test_a_session_that_died_before_a_runner_ever_attached_says_so_in_a_row(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The case with nothing else to show: no frames, no turn, and until now no record of why.

    The reason is recorded rather than parsed back out of the operator-facing error prose, because
    the sweep decides it from two columns the failure then overwrites.
    """
    view, _ = await chat_store.create(operator_id, SpaSession())
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 1

    lapsed = one(await authored_events(migrated_sessions, view.session_id))
    assert lapsed.kind == AuthoredEventKind.LEASE_EXPIRED
    assert lapsed.body == {"reason": LeaseExpiryReason.NEVER_ATTACHED, "last_holder": None}
    assert lapsed.turn_id is None


async def test_a_lease_that_lapsed_names_the_replica_that_held_it(chat_store, migrated_sessions, operator_id) -> None:
    """A different reason and a different answer to "who was serving this", from the same sweep."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.renew_lease(view.session_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 1

    lapsed = one(await authored_events(migrated_sessions, view.session_id))
    assert lapsed.body == {"reason": LeaseExpiryReason.HOLDER_GONE, "last_holder": REPLICA}


async def test_an_accepted_prompt_is_a_row_in_the_stream_as_well_as_in_the_transcript(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The operator's own question, addressed by `event_seq` like the agent's answer is.

    Until this row `session_events` held one side of a conversation, so a reader following the
    stream saw answers to questions that were not in it.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    prompt = await chat_store.enqueue_prompt(operator_id, view.session_id, "list the files")

    asked = one(await authored_events(migrated_sessions, view.session_id))
    assert asked.kind == AuthoredEventKind.PROMPT_ENQUEUED
    assert asked.body == {"message_id": str(prompt.message_id), "text": "list the files"}
    # No frames because nothing has been sent yet, and no turn because admission refuses a prompt
    # while one is open — so a prompt is accepted exactly when there is none to name.
    assert (asked.turn_id, asked.source_first_frame_seq) == (None, None)


async def test_a_refused_prompt_is_not_in_the_stream(chat_store, migrated_sessions, operator_id) -> None:
    """The row and the event commit together, so what is not accepted is not recorded."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "first")

    with pytest.raises(RuntimeError, match="already queued"):
        await chat_store.enqueue_prompt(operator_id, view.session_id, "second")

    asked = one(await authored_events(migrated_sessions, view.session_id))
    assert asked.body["text"] == "first"


async def test_a_live_session_whose_holder_stopped_renewing_is_failed(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The wedge this exists for: a live status nobody is working on.

    A replica that dies without running its finalizer corrects nothing, and every other observer
    reads the live status it left as healthy — so the room is never answered and never told why.
    The expired lease is the evidence that makes it reclaimable by anyone.
    """
    view, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.renew_lease(view.session_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 1
    assert await chat_store.status(view.session_id) == SessionStatus.FAILED
    assert "went away" in (await chat_store.get(operator_id, view.session_id)).error


async def test_a_session_is_adoptable_before_it_is_dead(chat_store, migrated_sessions, operator_id) -> None:
    """The bug a production roll found, in one test. `release_lease` is a finalizer, so SIGKILL and
    node loss skip it — measured, every roll took this path. Failing the row the moment the lease
    lapsed beat the runner's redial every time, so the session died while its sandbox sat there
    retrying. An expired lease has to mean unowned for long enough to be taken."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.renew_lease(view.session_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=1)

    assert await chat_store.expire_stale_leases() == 0, "expired is adoptable, not dead"
    assert await chat_store.status(view.session_id) in OPEN_SESSION_STATUSES

    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 1, "and dead once nobody took it"


async def _set_idle(sessions: async_sessionmaker[AsyncSession], session_id: UUID) -> None:
    """Put the session in the status no production path writes yet (`SessionStatus.IDLE`)."""
    async with sessions.begin() as db:
        chat = await db.get(Session, session_id)
        assert chat is not None
        chat.status = SessionStatus.IDLE


async def test_an_idle_session_holds_no_lease_to_lose(chat_store, migrated_sessions, operator_id) -> None:
    """A session holding no sandbox has no holder, so its lapsed lease is evidence of nothing.

    The sweep and the renewal key on the statuses something is actually holding, not on every
    status worth keeping: one set answering both questions is what would reap this row after
    `ADOPTION_GRACE` for a holder it never had.
    """
    view, _ = await chat_store.create(operator_id, SpaSession())
    await _set_idle(migrated_sessions, view.session_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 0
    assert await chat_store.status(view.session_id) == SessionStatus.IDLE

    await chat_store.renew_lease(view.session_id)

    holder, expires_at = await lease_of(migrated_sessions, view.session_id)
    assert holder is None, "nothing holds it"
    assert expires_at < datetime.now(UTC), "so nothing renews its lease either"


async def test_an_idle_session_is_kept_rather_than_cleaned_up(chat_store, migrated_sessions, operator_id) -> None:
    """Holding no sandbox is not an ending: the session is still the room's, so the claim sweep
    leaves it alone and a runner dialling in is not told it is terminal."""
    view, token = await chat_store.create(operator_id, SpaSession())
    await _set_idle(migrated_sessions, view.session_id)

    assert view.session_id not in await chat_store.claim_cleanup_candidates()
    assert await chat_store.authenticate_bridge(view.session_id, token) is not BridgeAuthentication.TERMINAL


async def test_shutdown_hands_back_every_lease_this_replica_holds(chat_store, migrated_sessions, operator_id) -> None:
    """The graceful-shutdown path: a rolling replica releases all its live sessions in one act, so
    each is adoptable at once instead of waiting out the sweep's grace. Not failed — handed back."""
    held = [await chat_store.create(operator_id, SpaSession()) for _ in range(2)]
    for view, token in held:
        assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    assert await chat_store.release_held_leases() == 2

    for view, _ in held:
        holder, expires_at = await lease_of(migrated_sessions, view.session_id)
        assert holder is None
        assert expires_at <= datetime.now(UTC), "the lease is expired, so any runner may adopt it"
        assert await chat_store.status(view.session_id) in OPEN_SESSION_STATUSES, "adoptable, not failed"
    assert await chat_store.expire_stale_leases() == 0, "within the grace, so no sweep fails it yet"


async def test_shutdown_leaves_another_replicas_lease_alone(chat_store, migrated_sessions, operator_id) -> None:
    """One replica going down must not hand back a session another replica is still serving."""
    view, token = await chat_store.create(operator_id, SpaSession())
    with patch("haku.console.x.session_store.REPLICA", "haku-console-b"):
        assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    assert await chat_store.release_held_leases() == 0
    holder, _ = await lease_of(migrated_sessions, view.session_id)
    assert holder == "haku-console-b"


async def test_shutdown_does_not_touch_an_ended_session(chat_store, migrated_sessions, operator_id) -> None:
    """A session that already ended is not this replica's to hand back, even if it once held it."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.fail(view.session_id, "something went wrong")

    assert await chat_store.release_held_leases() == 0
    assert (await chat_store.get(operator_id, view.session_id)).status == SessionStatus.FAILED


async def test_an_unheld_session_says_no_replica_ever_attached(chat_store, migrated_sessions, operator_id) -> None:
    """The creator's provisioning grant has no holder, so a sandbox that never came up must not
    blame a replica for going away."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 1
    assert "never attached" in (await chat_store.get(operator_id, view.session_id)).error


async def test_a_failed_session_names_the_replica_that_held_it(chat_store, migrated_sessions, operator_id) -> None:
    """The whole reason to record a holder: this message used to be identical for every such
    failure, so a room said a session died and nothing could say which process to go read."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.renew_lease(view.session_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 1
    assert REPLICA in (await chat_store.get(operator_id, view.session_id)).error


async def test_renewing_is_what_claims_the_session(chat_store, migrated_sessions, operator_id) -> None:
    """A session goes from budgeted to held the first time its replica renews, with nothing else
    sequencing the handover."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    async with migrated_sessions() as db:
        assert (await db.get(Session, view.session_id)).lease_holder is None

    await chat_store.renew_lease(view.session_id)

    async with migrated_sessions() as db:
        assert (await db.get(Session, view.session_id)).lease_holder == REPLICA


async def test_a_session_whose_holder_is_still_renewing_is_left_alone(
    chat_store, migrated_sessions, operator_id
) -> None:
    """A busy replica must not have its session reclaimed out from under it."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.renew_lease(view.session_id)

    assert await chat_store.expire_stale_leases() == 0
    assert await chat_store.status(view.session_id) == SessionStatus.PROVISIONING


async def test_an_ended_session_is_not_reclassified_by_the_sweep(chat_store, migrated_sessions, operator_id) -> None:
    """Only a *live* status is a lie worth correcting; a terminal one is already the truth."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.fail(view.session_id, "something else went wrong first")
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 0
    assert (await chat_store.get(operator_id, view.session_id)).error == "something else went wrong first"


async def test_a_frames_events_land_as_rows_with_the_cursor_that_says_they_did(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The projection's own output, stored — and stored in the transaction that moves the cursor.

    A tool call's answer is the row nothing held before these: until them the frames carrying the
    reply were re-parsed on every read.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    session_id = view.session_id
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, session_id, "list the files")
    started = await chat_store.next_prompt(session_id)
    assert started is not None
    message = MessageKey(opened_at_frame_seq=7)

    await chat_store.apply_frame(
        session_id,
        started.turn_id,
        7,
        [
            ToolCallStarted(
                message=message,
                call_id="toolu_1",
                tool_name="Bash",
                arguments={"command": "ls"},
                provenance=FrameRange(7, 7),
            ),
            MessageCompleted(message=message, text="looking", agent_message_id=None, provenance=FrameRange(7, 7)),
        ],
    )
    await chat_store.apply_frame(
        session_id,
        started.turn_id,
        8,
        # A result comes back on its own frame, and finds its call by the id rather than by
        # anything the message said.
        [
            ToolCallCompleted(
                call_id="toolu_1",
                content="a.py",
                structured={"exit_code": 0},
                outcome=Outcome.SUCCEEDED,
                provenance=FrameRange(8, 8),
            )
        ],
    )

    async with migrated_sessions() as db:
        rows = list(
            (
                await db.scalars(
                    select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(SessionEvent.event_seq)
                )
            ).all()
        )
        assert (await db.get(Session, session_id)).projected_frame_seq == 8
    assert [row.kind for row in rows] == [
        AuthoredEventKind.PROMPT_ENQUEUED,
        ConversationEventKind.TOOL_CALL_STARTED,
        ConversationEventKind.MESSAGE_COMPLETED,
        ConversationEventKind.TOOL_CALL_COMPLETED,
    ]
    assert {row.turn_id for row in rows if row.provenance is EventProvenance.FRAME_RANGE} == {started.turn_id}
    answered = one(row for row in rows if row.kind == ConversationEventKind.TOOL_CALL_COMPLETED)
    assert answered.call_id == "toolu_1"
    assert answered.body["content"] == {"shape": "text", "text": "a.py"}


async def test_an_event_row_cannot_be_written_without_a_provenance_union(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The requirement #4143 could not put on `session_messages`, where NULL means two things.

    Either arm is writable and neither can be written half: `frame_range` without a range, and
    `authored` with one, are both refused by the table rather than by whoever remembers. The turn
    goes the same way — required of a projected row, since the fold only runs inside one, and
    optional on the arm the console authors about the session itself.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "list the files")
    started = await chat_store.next_prompt(view.session_id)
    assert started is not None

    def event(**overrides) -> SessionEvent:
        values = {
            "session_id": view.session_id,
            "turn_id": started.turn_id,
            "kind": ConversationEventKind.REASONING,
            "provenance": EventProvenance.FRAME_RANGE,
            "source_first_frame_seq": 3,
            "source_last_frame_seq": 4,
            "call_id": None,
            "body": {"summary": None},
            "created_at": datetime.now(UTC),
        }
        return SessionEvent(**(values | overrides))

    for unwritable in (
        event(source_first_frame_seq=None, source_last_frame_seq=None),
        event(source_last_frame_seq=None),
        event(provenance=EventProvenance.AUTHORED),
        event(source_first_frame_seq=9),
        event(call_id="toolu_1"),
        event(turn_id=None),
    ):
        async with migrated_sessions() as db:
            db.add(unwritable)
            with pytest.raises(IntegrityError):
                await db.commit()

    for writable in (
        event(provenance=EventProvenance.AUTHORED, source_first_frame_seq=None, source_last_frame_seq=None),
        event(
            provenance=EventProvenance.AUTHORED, source_first_frame_seq=None, source_last_frame_seq=None, turn_id=None
        ),
    ):
        async with migrated_sessions() as db:
            db.add(writable)
            await db.commit()


if __name__ == "__main__":
    pytest_bazel.main()
