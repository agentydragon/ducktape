"""Contracts of the session store: the rows, and which of them commit together.

A room is an address on a `chat_attachment` row here and nothing more — no channel is imported, so
this file is what a second channel inherits (<README.md> § The runtime's conftest names no
channel).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from more_itertools import one
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.chat_models import (
    OPEN_SESSION_STATUSES,
    SPA_ORIGIN,
    AuthoredEventKind,
    ChatMessageRole,
    ChatMessageStatus,
    ChatSurface,
    ConversationEventKind,
    EventProvenance,
    FrameDirection,
    LeaseExpiryReason,
    MatrixOrigin,
    PromptRejection,
    SessionStatus,
    SpaOrigin,
    TurnOutcome,
)
from haku.console.database_schema import Conversation, Session, SessionEvent, SessionMessage, SessionPrompt
from haku.console.x.claude_code.frames import PROMPT_FRAME_KIND
from haku.console.x.claude_code.testing.wire import assistant, result, text_block, text_delta
from haku.console.x.conftest import age_lease, attach_channel, lease_of, queued_for_the_room
from haku.console.x.conversation_events import (
    FrameRange,
    MessageCompleted,
    MessageKey,
    Outcome,
    ToolCallCompleted,
    ToolCallStarted,
)
from haku.console.x.conversation_records import FrameCursor, SessionCursor, TranscriptCursor, TurnCursor
from haku.console.x.session_events import PromptBody
from haku.console.x.session_notifications import SessionEventKind
from haku.console.x.session_store import (
    ADOPTION_GRACE,
    REPLICA,
    BridgeAuthentication,
    MatrixSession,
    PositionUnusableError,
    PromptRefusedError,
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
    """Two facts, two columns: liveness gates the candidate set, `claim_cleaned_at` empties it, so
    an interrupted teardown is retryable and a completed one final."""
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
    """The credential survives cleanup, so refusal is the status's doing — which is what tells a
    runner holding the right token to stop (`TERMINAL`) apart from one holding the wrong one
    (`REJECTED`)."""
    view, token = await chat_store.create(operator_id, SpaSession())
    await chat_store.request_close(operator_id, view.session_id)
    await chat_store.complete_claim_cleanup(view.session_id)

    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.TERMINAL
    assert await chat_store.authenticate_bridge(view.session_id, "wrong") == BridgeAuthentication.REJECTED


async def test_a_turn_records_the_message_it_finished_rather_than_the_frames_it_left(
    chat_store, migrated_sessions, operator_id
) -> None:
    """A completed message clears the pointer, records that this turn has spoken, and — for a
    session serving a room — records that the room's outbox holds it, in the transaction that puts
    it there. Reading that off the `assistant` frames could only answer "one was recorded".
    """
    view, token = await chat_store.create(operator_id, MatrixSession())
    session_id = view.session_id
    await attach_channel(migrated_sessions, session_id, ROOM)
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, session_id, "why did it fail?", SPA_ORIGIN)
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
    """An adopted connection re-sends whatever the previous console may not have acknowledged, and
    the agent's own id is what recognises it. The cursor is an optimisation; this is what makes
    replay safe."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    frame = assistant(message_id="msg_01abc")

    assert (await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, "assistant", frame)).fresh
    assert not (await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, "assistant", frame)).fresh

    frames = await chat_store.read_frames(str(session.session_id), cursor=None, limit=25, kinds=None)
    assert [frame.kind for frame in frames] == ["assistant"]


async def test_the_resume_cursor_is_the_highest_number_a_runner_gave_this_session(chat_store, operator_id) -> None:
    """What a reconnecting console hands back, per session rather than per connection: two consoles
    can be adopting one runner's window during a roll, so the cursor has to be a fact about the log
    both can read. It ignores rows no runner numbered, and need not be the newest row — a
    `setup_output` recorded after them carries no number of its own.
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
    """The console's read is the reverse keyset of the MCP reader's: a long session's interesting
    frames are its last ones, so the first page is the tail and the cursor walks towards the start.
    Each page itself stays in wire order.
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
    """The MCP reader is deliberately unscoped; a browser surface must never be."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.record_frame(session.session_id, FrameDirection.FROM_AGENT, "result", {"type": "result"})

    with pytest.raises(KeyError):
        await chat_store.read_operator_frames(uuid4(), session.session_id, before_seq=None, limit=25, kinds=None)


async def test_a_frame_reaches_the_inspector_with_its_payload_whole(chat_store, operator_id) -> None:
    """No clipping on this path: the MCP reader clips for context budget, but here the wire *is* the
    answer."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    payload = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * 20_000}]}}
    await chat_store.record_frame(session.session_id, FrameDirection.TO_AGENT, "user", payload)

    page = await chat_store.read_operator_frames(operator_id, session.session_id, before_seq=None, limit=25, kinds=None)

    assert page.frames[0].payload == payload
    assert page.frames[0].direction == FrameDirection.TO_AGENT


async def test_sessions_come_back_newest_first_with_the_channels_holding_their_thread(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The attachments, not a surface enum: a session says which channels hold a copy of the
    conversation it runs, which is the shape that survives a second one attaching."""
    await chat_store.create(operator_id, SpaSession())
    matrix, _ = await chat_store.create(operator_id, MatrixSession())
    await attach_channel(migrated_sessions, matrix.session_id, "!room:example.org")

    sessions = await chat_store.list_sessions(cursor=None, limit=10)

    assert sessions[0].session_id == matrix.session_id
    assert [attachment.address for attachment in sessions[0].attachments] == ["!room:example.org"]
    assert sessions[1].attachments == []


async def test_a_session_created_between_two_pages_cannot_shift_what_the_second_one_holds(
    chat_store, operator_id
) -> None:
    """This order grows at the top and an offset counts from there, so a session created mid-walk
    would push the first page's last row into the second page again."""
    older, _ = await chat_store.create(operator_id, SpaSession())
    newer, _ = await chat_store.create(operator_id, SpaSession())

    # Two rows for a page of one: the extra row is the one the tool's cursor names.
    first, resume = await chat_store.list_sessions(cursor=None, limit=2)
    await chat_store.create(operator_id, SpaSession())
    second = await chat_store.list_sessions(cursor=SessionCursor.of(resume), limit=1)

    assert first.session_id == newer.session_id
    assert [session.session_id for session in second] == [older.session_id]


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

    page, resume = await chat_store.list_sessions(cursor=None, limit=2)
    rest = await chat_store.list_sessions(cursor=SessionCursor.of(resume), limit=10)

    assert [session.session_id for session in [page, *rest]] == sorted(
        [first.session_id, second.session_id], reverse=True
    )


async def test_a_prompt_records_the_channel_events_it_was_folded_from(
    chat_store, operator_id, migrated_sessions
) -> None:
    """The store carries a surface's origin without reading it: the arm says whose it is, and
    nothing here knows what a Matrix room or event id is.

    The SPA is named rather than left absent, so the reader this exists for cannot confuse "typed
    into a browser" with "we never wrote it down" — which are opposite answers to "does this room
    already have a copy?".
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(
        operator_id, view.session_id, "first\nsecond", MatrixOrigin(address=ROOM, refs=("$a", "$b"))
    )
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)
    await chat_store.enqueue_prompt(operator_id, view.session_id, "do the thing", SPA_ORIGIN)

    asked = [
        PromptBody.model_validate(event.body)
        for event in await authored_events(migrated_sessions, view.session_id)
        if event.kind == AuthoredEventKind.PROMPT_ENQUEUED
    ]

    assert [body.origin for body in asked] == [MatrixOrigin(address=ROOM, refs=("$a", "$b")), SpaOrigin()]


async def test_exchanges_page_by_their_own_keyset(chat_store, operator_id) -> None:
    """`(started_at, turn_id)`, because two exchanges of one session can share a start instant and
    a cursor naming only the timestamp would step over one of a tied pair."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    for index in range(3):
        await chat_store.enqueue_prompt(operator_id, view.session_id, f"prompt {index}", SPA_ORIGIN)
        turn = await chat_store.next_prompt(view.session_id)
        assert turn is not None
        await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)

    # One row past the page, exactly as the tool asks: the cursor names the first row not returned.
    *page, resume = await chat_store.list_turns(view.session_id, cursor=None, limit=3)
    rest = await chat_store.list_turns(view.session_id, cursor=TurnCursor.of(resume), limit=5)

    assert len(page) == 2
    assert [turn.turn_id for turn in rest] == [resume.turn_id]


async def test_a_turn_ends_at_the_frame_it_names_rather_than_at_the_head_of_the_log(chat_store, operator_id) -> None:
    """The CLI emits a `command_lifecycle` frame just after the `result` one and the recorder writes
    it while the turn is still being closed, so a bound taken from the log swallows a frame the turn
    did not produce."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)
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
    await chat_store.enqueue_prompt(operator_id, view.session_id, "first", SPA_ORIGIN)
    silent = await chat_store.next_prompt(view.session_id)
    assert silent is not None
    await chat_store.end_turn(silent.turn_id, TurnOutcome.FAILED)
    await chat_store.enqueue_prompt(operator_id, view.session_id, "second", SPA_ORIGIN)
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
    """What a session meant, with a way back to the frames it was read off."""
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
    chat_store, migrated_sessions, operator_id
) -> None:
    """The Console list is light, while detail carries messages and turn summaries. Both are keyed
    by the conversation and carry its attachments, so a list row says which channels hold this
    thread rather than which surface a session was created for.
    """
    await chat_store.create(operator_id, SpaSession())
    matrix, matrix_token = await chat_store.create(operator_id, MatrixSession())
    await attach_channel(migrated_sessions, matrix.session_id, ROOM)
    assert await chat_store.authenticate_bridge(matrix.session_id, matrix_token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(
        operator_id, matrix.session_id, "What is happening?", MatrixOrigin(address=ROOM, refs=("$asked",))
    )
    conversation_id = await chat_store.conversation_of(matrix.session_id)

    page = await chat_store.list_operator_conversations(operator_id, cursor=None, limit=10)
    detail = await chat_store.get_operator_conversation(operator_id, conversation_id)

    assert page.conversations[0].conversation_id == conversation_id
    assert [attachment.address for attachment in page.conversations[0].attachments] == [ROOM]
    assert page.conversations[0].live_session is not None
    assert page.conversations[0].live_session.session_id == matrix.session_id
    assert page.conversations[0].message_count == 1
    assert [attachment.address for attachment in detail.attachments] == [ROOM]
    assert detail.session.session_id == matrix.session_id
    assert detail.session.messages[0].content == "What is happening?"
    assert detail.session.turns == []
    assert detail.earlier_sessions == []


async def test_a_conversation_a_channel_holds_takes_a_prompt_typed_in_the_browser(
    chat_store, migrated_sessions, operator_id
) -> None:
    """One conversation, two surfaces. Nothing on the browser path asks what channel holds the
    thread, and nothing may start to: a room's session admits a prompt on exactly the terms an SPA
    session does.
    """
    matrix, token = await chat_store.create(operator_id, MatrixSession())
    await attach_channel(migrated_sessions, matrix.session_id, ROOM)
    assert await chat_store.authenticate_bridge(matrix.session_id, token) == BridgeAuthentication.ACCEPTED

    await chat_store.enqueue_prompt(operator_id, matrix.session_id, "typed into the tab", SPA_ORIGIN)
    detail = await chat_store.get_operator_conversation(
        operator_id, await chat_store.conversation_of(matrix.session_id)
    )

    assert [message.content for message in detail.session.messages] == ["typed into the tab"]
    assert [attachment.address for attachment in detail.attachments] == [ROOM]


async def test_a_replacement_session_leaves_the_thread_and_its_attachment_where_they_were(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The successor runs the same thread, so the attachment is untouched and the transcript of the
    session that died stays reachable beside it."""
    first, _ = await chat_store.create(operator_id, MatrixSession())
    await attach_channel(migrated_sessions, first.session_id, ROOM)
    conversation_id = await chat_store.conversation_of(first.session_id)
    await chat_store.fail(first.session_id, "the sandbox went away")
    second, _ = await chat_store.create(operator_id, MatrixSession(), conversation_id=conversation_id)

    page = await chat_store.list_operator_conversations(operator_id, cursor=None, limit=10)
    detail = await chat_store.get_operator_conversation(operator_id, conversation_id)

    assert [conversation.conversation_id for conversation in page.conversations] == [conversation_id]
    assert detail.session.session_id == second.session_id
    assert [session.session_id for session in detail.earlier_sessions] == [first.session_id]
    assert [attachment.address for attachment in detail.attachments] == [ROOM]


async def test_a_conversation_created_between_two_pages_cannot_shift_what_the_second_one_holds(
    chat_store, operator_id
) -> None:
    """A conversation never ends, so this order only grows and only at its top."""
    older, _ = await chat_store.create(operator_id, SpaSession())
    newer, _ = await chat_store.create(operator_id, SpaSession())

    first = await chat_store.list_operator_conversations(operator_id, cursor=None, limit=1)
    await chat_store.create(operator_id, SpaSession())
    second = await chat_store.list_operator_conversations(operator_id, cursor=first.next_cursor, limit=1)

    assert [conversation.conversation_id for conversation in first.conversations] == [
        await chat_store.conversation_of(newer.session_id)
    ]
    assert [conversation.conversation_id for conversation in second.conversations] == [
        await chat_store.conversation_of(older.session_id)
    ]


async def test_the_last_page_of_conversations_offers_no_cursor(chat_store, operator_id) -> None:
    await chat_store.create(operator_id, SpaSession())

    page = await chat_store.list_operator_conversations(operator_id, cursor=None, limit=10)

    assert len(page.conversations) == 1
    assert page.next_cursor is None


async def test_a_second_prompt_is_refused_while_a_turn_is_open(chat_store, operator_id) -> None:
    """Admission asks the turn rather than the session's status, so a mid-turn prompt cannot become
    fold-into-turn with no fold path wired."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "first", SPA_ORIGIN)
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None

    with pytest.raises(PromptRefusedError) as refusal:
        await chat_store.enqueue_prompt(operator_id, view.session_id, "second", SPA_ORIGIN)
    assert refusal.value.reason is PromptRejection.TURN_IN_FLIGHT

    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)
    await chat_store.enqueue_prompt(operator_id, view.session_id, "second", SPA_ORIGIN)


async def test_a_prompt_is_taken_off_the_queue_rather_than_found_by_status(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The queue row is what says a prompt is waiting, and claiming it is what says it no longer is
    — the transcript row's status cannot mean that, since `COMPLETE` on an assistant row means the
    answer finished."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)

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
    """The index and not a scan-plus-rule: two replicas racing on one session would otherwise each
    conclude they may accept."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "first", SPA_ORIGIN)

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
    await chat_store.enqueue_prompt(operator_id, view.session_id, "mine", SPA_ORIGIN)
    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    assert turn.prompt == "mine"


async def test_the_view_says_responding_for_as_long_as_the_turn_is_open(chat_store, operator_id) -> None:
    """`status` is the SPA's contract, and the view derives it from the open turn rather than from
    the column."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "work", SPA_ORIGIN)
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
    await chat_store.enqueue_prompt(operator_id, view.session_id, "work", SPA_ORIGIN)
    assert await chat_store.next_prompt(view.session_id) is not None
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 1

    [record] = await chat_store.list_turns(str(view.session_id), cursor=None, limit=10)
    assert record.ended_at is None, "nothing ran to close it, and the record should say so"
    assert (await chat_store.get(operator_id, view.session_id)).status == SessionStatus.FAILED


async def test_abort_is_refused_until_a_turn_is_actually_running(chat_store, operator_id) -> None:
    """An idle session has nothing to interrupt, and saying so is the point of the 409.

    A *queued* prompt is not a turn either. An abort accepted with nothing to abort leaves its
    event set until the next turn, killing that one on arrival — so the abort names the open turn,
    which does not exist until the prompt is handed to the model.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    # The bridge handshake is what takes a session from provisioning to ready, and only a
    # ready session accepts a prompt.
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    assert await chat_store.request_abort(view.session_id) is False

    await chat_store.enqueue_prompt(operator_id, view.session_id, "work", SPA_ORIGIN)
    assert await chat_store.request_abort(view.session_id) is False, "a queued prompt is not a turn"

    turn = await chat_store.next_prompt(view.session_id)
    assert turn is not None
    assert await chat_store.request_abort(view.session_id) is True

    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)
    assert await chat_store.request_abort(view.session_id) is False


async def test_abort_reaches_the_replica_running_the_turn(
    migrated_db_url, chat_store, notifications, operator_id
) -> None:
    """The two ends of an abort are on different pods, so it has to cross the database: the abort
    event belongs to whichever replica holds the runner's bridge websocket, while `POST .../abort`
    is balanced across all of them. Two stores over two engines is what reproduces that; a single
    store would pass on an in-process path.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "work", SPA_ORIGIN)
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
    """Nothing selects `surface` any more — a session reaches its channel through the
    conversation's attachments — but the previous image does for the length of a roll, so it must
    keep being written until it is unmapped too."""
    spa, _ = await chat_store.create(operator_id, SpaSession())
    matrix, _ = await chat_store.create(operator_id, MatrixSession())

    async with migrated_sessions() as db:
        assert (await db.get(Session, spa.session_id)).surface == ChatSurface.SPA
        assert (await db.get(Session, matrix.session_id)).surface == ChatSurface.MATRIX


async def test_a_session_opens_its_own_conversation_unless_it_is_given_one(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The identity a channel's attachment hangs off. A caller with a thread to continue names it,
    and a caller with none — every session the browser starts — gets one of its own."""
    first, _ = await chat_store.create(operator_id, SpaSession())
    second, _ = await chat_store.create(operator_id, SpaSession())

    async with migrated_sessions() as db:
        opened = (await db.get(Session, first.session_id)).conversation_id
        assert opened != (await db.get(Session, second.session_id)).conversation_id
        assert (await db.get(Conversation, opened)).operator_id == operator_id

    continued, _ = await chat_store.create(operator_id, SpaSession(), conversation_id=opened)

    async with migrated_sessions() as db:
        assert (await db.get(Session, continued.session_id)).conversation_id == opened


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
    """The fact the frame log cannot hold: a lease changing hands crosses no wire, and it happens on
    every roll."""
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
    """The case with nothing else to show: no frames and no turn. The reason is recorded rather than
    parsed back out of the operator-facing prose, because the sweep decides it from two columns the
    failure then overwrites.
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


@pytest.fixture
async def accepted_prompt(chat_store: SessionStore, operator_id: UUID) -> tuple[UUID, UUID]:
    """A ready session with one prompt it has accepted, and no turn yet claiming it.

    Room-backed because the tests using it are about what a channel reads back — but a room is an
    attachment address here, not a homeserver.
    """
    view, token = await chat_store.create(operator_id, MatrixSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    prompt = await chat_store.enqueue_prompt(
        operator_id, view.session_id, "what were we doing", MatrixOrigin(address=ROOM, refs=("$asked",))
    )
    return view.session_id, prompt.message_id


async def test_an_accepted_prompt_is_a_row_in_the_stream_as_well_as_in_the_transcript(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The operator's own question, addressed by `event_seq` like the agent's answer is — without
    it a reader following the stream sees answers to questions that are not in it."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED

    prompt = await chat_store.enqueue_prompt(operator_id, view.session_id, "list the files", SPA_ORIGIN)

    asked = one(await authored_events(migrated_sessions, view.session_id))
    assert asked.kind == AuthoredEventKind.PROMPT_ENQUEUED
    assert asked.body == {"message_id": str(prompt.message_id), "text": "list the files", "origin": {"kind": "spa"}}
    # No frames because nothing has been sent yet, and no turn because admission refuses a prompt
    # while one is open — so a prompt is accepted exactly when there is none to name.
    assert (asked.turn_id, asked.source_first_frame_seq) == (None, None)


async def test_an_aborted_turn_is_a_row_in_the_stream_and_names_its_turn(
    chat_store, migrated_sessions, accepted_prompt
) -> None:
    """The operator's stop, in the ordered stream a channel reads rather than only in a column.

    It is the one authored kind that names a turn: what was stopped is the exchange, so a reader
    folding the stream knows which one, and the room's "aborted" line is this row rendered.
    """
    session_id, _ = accepted_prompt
    turn = await chat_store.next_prompt(session_id)
    assert turn is not None

    await chat_store.end_turn(turn.turn_id, TurnOutcome.ABORTED)

    stopped = one(
        event
        for event in await authored_events(migrated_sessions, session_id)
        if event.kind == AuthoredEventKind.TURN_ABORTED
    )
    assert (stopped.turn_id, stopped.body) == (turn.turn_id, {})


async def test_a_turn_that_ended_any_other_way_leaves_no_abort_row(
    chat_store, migrated_sessions, accepted_prompt
) -> None:
    """A turn that answered was not stopped, and a second close cannot re-decide that — the same
    early return that keeps the first outcome keeps this row from being minted after it."""
    session_id, _ = accepted_prompt
    turn = await chat_store.next_prompt(session_id)
    assert turn is not None

    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)
    await chat_store.end_turn(turn.turn_id, TurnOutcome.ABORTED)

    kinds = [event.kind for event in await authored_events(migrated_sessions, session_id)]
    assert kinds == [AuthoredEventKind.PROMPT_ENQUEUED]


async def test_a_refused_prompt_is_not_in_the_stream(chat_store, migrated_sessions, operator_id) -> None:
    """The row and the event commit together, so what is not accepted is not recorded."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "first", SPA_ORIGIN)

    with pytest.raises(PromptRefusedError) as refusal:
        await chat_store.enqueue_prompt(operator_id, view.session_id, "second", SPA_ORIGIN)
    assert refusal.value.reason is PromptRejection.PROMPT_QUEUED

    asked = one(await authored_events(migrated_sessions, view.session_id))
    assert asked.body["text"] == "first"


async def test_a_live_session_whose_holder_stopped_renewing_is_failed(
    chat_store, migrated_sessions, operator_id
) -> None:
    """A live status nobody is working on. A replica that dies without running its finalizer
    corrects nothing and every other observer reads the status it left as healthy, so the room is
    never answered and never told why; the expired lease is what makes it reclaimable by anyone.
    """
    view, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.renew_lease(view.session_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 1
    assert await chat_store.status(view.session_id) == SessionStatus.FAILED
    assert "went away" in (await chat_store.get(operator_id, view.session_id)).error


async def test_a_session_is_adoptable_before_it_is_dead(chat_store, migrated_sessions, operator_id) -> None:
    """`release_lease` is a finalizer, so SIGKILL and node loss skip it, and failing the row the
    moment the lease lapses beats the runner's redial every time — killing the session while its
    sandbox sits there retrying. An expired lease has to mean unowned for long enough to be
    taken."""
    view, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.renew_lease(view.session_id)
    await age_lease(migrated_sessions, view.session_id, seconds_ago=1)

    assert await chat_store.expire_stale_leases() == 0, "expired is adoptable, not dead"
    assert await chat_store.status(view.session_id) in OPEN_SESSION_STATUSES

    await age_lease(migrated_sessions, view.session_id, seconds_ago=int(ADOPTION_GRACE.total_seconds()) + 1)

    assert await chat_store.expire_stale_leases() == 1, "and dead once nobody took it"


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
    """The reason to record a holder: without it a room says a session died and nothing says which
    process to go read."""
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
    """The projection's own output, stored in the transaction that moves the cursor."""
    view, token = await chat_store.create(operator_id, SpaSession())
    session_id = view.session_id
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, session_id, "list the files", SPA_ORIGIN)
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
    """Either arm is writable and neither can be written half: `frame_range` without a range, and
    `authored` with one, are both refused by the table rather than by whoever remembers. The turn
    goes the same way — required of a projected row, since the fold only runs inside one, and
    optional on the arm the console authors about the session itself.

    Which arm a row may take follows from its kind, so the arms are not interchangeable: a
    `ConversationEventKind` is what folding a frame produced and cannot claim the console authored
    it.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "list the files", SPA_ORIGIN)
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
        event(provenance=EventProvenance.AUTHORED, source_first_frame_seq=None, source_last_frame_seq=None),
    ):
        async with migrated_sessions() as db:
            db.add(unwritable)
            with pytest.raises(IntegrityError):
                await db.commit()

    authored = {
        "kind": AuthoredEventKind.SESSION_ADOPTED,
        "provenance": EventProvenance.AUTHORED,
        "source_first_frame_seq": None,
        "source_last_frame_seq": None,
        "body": {"previous_holder": None, "holder": "haku-console-a"},
    }
    for writable in (event(**authored), event(**authored, turn_id=None)):
        async with migrated_sessions() as db:
            db.add(writable)
            await db.commit()


async def _exchange(chat_store, operator_id, session_id: UUID, prompt: str, answer: str) -> None:
    """One prompt through to one finished answer, with the frames it took, as the loop writes them."""
    await chat_store.enqueue_prompt(operator_id, session_id, prompt, SPA_ORIGIN)
    turn = await chat_store.next_prompt(session_id)
    assert turn is not None
    sent = await chat_store.record_frame(session_id, FrameDirection.TO_AGENT, PROMPT_FRAME_KIND, {"type": "user"})
    await chat_store.set_message_source_frames(session_id, turn.message_id, sent.frame_seq)
    spoke = await chat_store.record_frame(session_id, FrameDirection.FROM_AGENT, "assistant", {"type": "assistant"})
    await chat_store.apply_frame(
        session_id,
        turn.turn_id,
        spoke.frame_seq,
        [
            MessageCompleted(
                message=MessageKey(opened_at_frame_seq=spoke.frame_seq),
                text=answer,
                agent_message_id=None,
                provenance=FrameRange(spoke.frame_seq, spoke.frame_seq),
            )
        ],
    )
    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED, last_frame_seq=spoke.frame_seq)


async def test_an_update_carries_the_rows_the_events_after_a_position_name(chat_store, operator_id) -> None:
    """What the update drops is the history — the part that grows without bound.

    A reader holding the first exchange's position is sent the second exchange and not the first,
    which is the whole of what an update is for.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    session_id = view.session_id
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    conversation_id = await chat_store.conversation_of(session_id)
    await _exchange(chat_store, operator_id, session_id, "first", "one")
    held = await chat_store.conversation_position(conversation_id)

    await _exchange(chat_store, operator_id, session_id, "second", "two")
    changes = await chat_store.read_operator_conversation_changes(operator_id, conversation_id, after=held, limit=50)

    assert [message.content for message in changes.messages] == ["second", "two"]
    assert [turn.outcome for turn in changes.turns] == [TurnOutcome.ANSWERED]
    assert changes.position > held
    # Re-reading the same position is the same answer: the merge is keyed on `message_id`, so a
    # duplicate costs nothing and nothing about delivery has to be exactly-once.
    again = await chat_store.read_operator_conversation_changes(operator_id, conversation_id, after=held, limit=50)
    assert [message.message_id for message in again.messages] == [message.message_id for message in changes.messages]


async def test_an_update_carries_what_a_replaced_session_wrote_after_the_position(chat_store, operator_id) -> None:
    """The position addresses the thread, so it survives the runner under it being replaced: rows
    the old session wrote after it are still owed to the reader, and the new session's follow them.
    """
    first, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(first.session_id, token) == BridgeAuthentication.ACCEPTED
    conversation_id = await chat_store.conversation_of(first.session_id)
    held = await chat_store.conversation_position(conversation_id)
    await _exchange(chat_store, operator_id, first.session_id, "before the sandbox died", "answered")

    second, token = await chat_store.create(operator_id, SpaSession(), conversation_id=conversation_id)
    assert await chat_store.authenticate_bridge(second.session_id, token) == BridgeAuthentication.ACCEPTED
    await _exchange(chat_store, operator_id, second.session_id, "after it was replaced", "answered again")
    changes = await chat_store.read_operator_conversation_changes(operator_id, conversation_id, after=held, limit=50)

    assert [message.content for message in changes.messages] == [
        "before the sandbox died",
        "answered",
        "after it was replaced",
        "answered again",
    ]
    assert changes.session_id == second.session_id


async def test_a_prompt_leaving_pending_reaches_a_reader_that_no_event_told(chat_store, operator_id) -> None:
    """`next_prompt` moves the operator's own question out of `pending` and writes no event.

    The address alone would leave a tab rendering it as still queued forever, which is why the
    newest turn's own rows ride along on every read rather than waiting to be named.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    conversation_id = await chat_store.conversation_of(view.session_id)
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)
    enqueued = await chat_store.read_operator_conversation_changes(operator_id, conversation_id, after=0, limit=50)
    assert [(row.content, row.status) for row in enqueued.messages] == [("why did it fail?", ChatMessageStatus.PENDING)]

    assert await chat_store.next_prompt(view.session_id) is not None
    claimed = await chat_store.read_operator_conversation_changes(
        operator_id, conversation_id, after=enqueued.position, limit=50
    )

    assert [(row.content, row.status) for row in claimed.messages] == [("why did it fail?", ChatMessageStatus.COMPLETE)]
    assert claimed.position == enqueued.position, "a claim writes no event, so the position does not move"
    assert [turn.ended_at for turn in claimed.turns] == [None]


async def test_a_position_the_log_cannot_answer_from_is_refused_rather_than_read_as_empty(
    chat_store, operator_id
) -> None:
    """ "Nothing after N" and "N is not in this log" are different answers, and only one of them is
    safe to serve. `event_seq` is a global `Identity`, so the difference cannot be a comparison: the
    positions a read hands out are 0 and this conversation's own rows, so membership is the check.
    """
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    conversation_id = await chat_store.conversation_of(view.session_id)
    await chat_store.enqueue_prompt(operator_id, view.session_id, "why did it fail?", SPA_ORIGIN)
    held = await chat_store.conversation_position(conversation_id)

    with pytest.raises(PositionUnusableError):
        await chat_store.read_operator_conversation_changes(operator_id, conversation_id, after=held + 1, limit=50)


async def test_an_update_over_its_limit_is_refused_rather_than_shortened(chat_store, operator_id) -> None:
    """Silently short is a message the reader never learns about. `ConversationFollow` turns the
    refusal into a snapshot, which is the honest answer when most of one would be sent anyway."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    conversation_id = await chat_store.conversation_of(view.session_id)
    await _exchange(chat_store, operator_id, view.session_id, "first", "one")
    await _exchange(chat_store, operator_id, view.session_id, "second", "two")

    with pytest.raises(PositionUnusableError):
        await chat_store.read_operator_conversation_changes(operator_id, conversation_id, after=0, limit=2)

    whole = await chat_store.read_operator_conversation_changes(operator_id, conversation_id, after=0, limit=50)
    assert len(whole.messages) == 4


async def test_the_update_refuses_a_conversation_another_operator_owns(chat_store, operator_id) -> None:
    """The MCP reader is deliberately unscoped (R5.3a); a browser surface must never be."""
    view, _ = await chat_store.create(operator_id, SpaSession())

    with pytest.raises(KeyError):
        await chat_store.read_operator_conversation_changes(
            uuid4(), await chat_store.conversation_of(view.session_id), after=0, limit=50
        )


if __name__ == "__main__":
    pytest_bazel.main()
