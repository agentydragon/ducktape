"""The stored conversation log as the MCP surface hands it out.

Rows are minted through `session_events.item_row` and `session_events.authored` — the log's own
encoder, so a body round-trips through the spelling it is stored in. What is hand-written here is
only what a writer allocates: the item ids and the positions. That the writer allocates them this
way is asserted against a real database in <test_session_store.py>.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest_bazel
from more_itertools import one
from pydantic import BaseModel

from haku.console.chat_models import (
    HARNESS_ORIGIN,
    SPA_ORIGIN,
    ConversationEventKind,
    EventProvenance,
    PromptOrigin,
    PromptOriginKind,
    ReasoningDisclosure,
    ToolOutcome,
)
from haku.console.database_schema import ConversationEvent as ConversationEventRow
from haku.console.x import conversation_records, session_events, transcript_entries
from haku.console.x.conversation_events import FrameRange
from util.sqlalchemy_types import UnknownValue

CONVERSATION = UUID("00000000-0000-4000-8000-00000000c0de")
SESSION = UUID("00000000-0000-4000-8000-0000000005e5")
TURN = UUID("00000000-0000-4000-8000-000000007072")
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class _Log:
    """One conversation's rows, positioned as a writer would have allocated them."""

    rows: list[ConversationEventRow] = field(default_factory=list)

    def off_frames(self, kind: ConversationEventKind, body: BaseModel, *, item: UUID, frames: FrameRange) -> None:
        self._item(kind, body, item=item, frames=frames)

    def authored_item(self, kind: ConversationEventKind, body: BaseModel, *, item: UUID) -> None:
        """A row of an item the console accepted before anything crossed a wire — a prompt."""
        self._item(kind, body, item=item, frames=None)

    def authored(self, body: session_events.AuthoredBody) -> None:
        self.rows.append(
            session_events.authored(
                body,
                conversation_id=CONVERSATION,
                event_seq=len(self.rows) + 1,
                session_id=SESSION,
                turn_id=TURN,
                now=NOW,
            )
        )

    def _item(self, kind: ConversationEventKind, body: BaseModel, *, item: UUID, frames: FrameRange | None) -> None:
        self.rows.append(
            session_events.item_row(
                kind,
                body,
                conversation_id=CONVERSATION,
                event_seq=len(self.rows) + 1,
                item_id=item,
                session_id=SESSION,
                turn_id=TURN,
                provenance=frames,
                now=NOW,
            )
        )


def _message(log: _Log, *parts: str, first: int = 1, last: int | None = None) -> None:
    """One whole message: opened, one frame per segment of its prose, then closed.

    *last* is where the completion's span ends, and defaults to the last segment's own frame. A
    caller naming a later one is saying the message was interrupted and spans the interruption.
    """
    item = uuid4()
    last = last if last is not None else first + len(parts) - 1
    log.off_frames(
        ConversationEventKind.ITEM_STARTED,
        session_events.MessageStartedBody(),
        item=item,
        frames=FrameRange(first, first),
    )
    for offset, part in enumerate(parts):
        log.off_frames(
            ConversationEventKind.ITEM_SEGMENT,
            session_events.SegmentBody(text=part),
            item=item,
            frames=FrameRange(first + offset, first + offset),
        )
    log.off_frames(
        ConversationEventKind.ITEM_COMPLETED,
        session_events.MessageCompletedBody(backend_item_id=None),
        item=item,
        frames=FrameRange(first, last),
    )


def _prompt(log: _Log, text: str, origin: PromptOrigin) -> None:
    """A prompt, as the console writes one: opened, spoken and closed in the same breath."""
    item = uuid4()
    log.authored_item(ConversationEventKind.ITEM_STARTED, session_events.PromptStartedBody(origin=origin), item=item)
    log.authored_item(ConversationEventKind.ITEM_SEGMENT, session_events.SegmentBody(text=text), item=item)
    log.authored_item(ConversationEventKind.ITEM_COMPLETED, session_events.PromptCompletedBody(), item=item)


def test_segments_are_folded_into_the_item_they_belong_to() -> None:
    """The log stores prose as increments so a live channel can print them as they arrive; a
    transcript is read after the fact and wants the item, whose text is exactly those increments."""
    log = _Log()
    _message(log, "half ", "an answer")

    entry = one(transcript_entries.fold(log.rows).entries)

    assert isinstance(entry, conversation_records.MessageEntry)
    assert entry.text == "half an answer"


def test_an_entry_is_numbered_by_its_position_among_the_entries() -> None:
    """The index is the cursor's key, so it has to count what a reader will actually receive — one
    entry per finished item, in the order the items finished, not one per row the log holds."""
    log = _Log()
    _message(log, "hello")
    reasoning = uuid4()
    log.off_frames(
        ConversationEventKind.ITEM_STARTED,
        session_events.ReasoningStartedBody(),
        item=reasoning,
        frames=FrameRange(2, 2),
    )
    log.off_frames(
        ConversationEventKind.ITEM_COMPLETED,
        session_events.ReasoningCompletedBody(disclosure=ReasoningDisclosure.SUMMARY),
        item=reasoning,
        frames=FrameRange(2, 2),
    )
    _message(log, "and here", first=3)
    log.authored(session_events.TurnAnsweredBody())

    assert [(entry.index, entry.kind) for entry in transcript_entries.fold(log.rows).entries] == [
        (0, "message"),
        (1, "reasoning"),
        (2, "message"),
        (3, "turn_end"),
    ]


def test_a_multi_frame_message_reports_the_span_it_was_read_off() -> None:
    """The appeal path: an operator disputing a normalization reads the frames behind it, and a
    message that spans several has to name all of them."""
    log = _Log()
    _message(log, "one ", "two", first=4, last=6)

    entry = one(transcript_entries.fold(log.rows).entries)

    assert entry.provenance == conversation_records.FromFrames(first_frame_seq=4, last_frame_seq=6)


def test_a_call_and_its_answer_are_joined_by_the_id_the_protocol_gave_the_ask() -> None:
    """Two entries because the call is real while it is still running. Only the ask carries the
    protocol's id, so the answer's entry can only report it by being folded with the item it closes.
    """
    log = _Log()
    call = uuid4()
    log.off_frames(
        ConversationEventKind.ITEM_STARTED,
        session_events.ToolCallStartedBody(call_id="toolu_1", tool_name="Read", arguments={"path": "/x"}),
        item=call,
        frames=FrameRange(1, 1),
    )
    log.off_frames(
        ConversationEventKind.ITEM_SEGMENT,
        session_events.SegmentBody(text="file contents"),
        item=call,
        frames=FrameRange(2, 2),
    )
    log.off_frames(
        ConversationEventKind.ITEM_COMPLETED,
        session_events.ToolCallCompletedBody(structured={"filePath": "/x"}, outcome=ToolOutcome.SUCCEEDED),
        item=call,
        frames=FrameRange(2, 2),
    )

    asked, answered = transcript_entries.fold(log.rows).entries

    assert isinstance(asked, conversation_records.ToolCallEntry)
    assert isinstance(answered, conversation_records.ToolResultEntry)
    assert (asked.tool_name, asked.call_id) == ("Read", "toolu_1")
    assert answered.call_id == "toolu_1"
    assert answered.content == "file contents"
    assert answered.structured == {"filePath": "/x"}


def test_an_item_the_log_never_closed_is_not_an_entry() -> None:
    """A turn that died mid-message left prose nothing finished saying, and a transcript printing it
    would report a half-sentence as what was said."""
    log = _Log()
    item = uuid4()
    log.off_frames(
        ConversationEventKind.ITEM_STARTED, session_events.MessageStartedBody(), item=item, frames=FrameRange(1, 1)
    )
    log.off_frames(
        ConversationEventKind.ITEM_SEGMENT,
        session_events.SegmentBody(text="half a "),
        item=item,
        frames=FrameRange(1, 1),
    )

    assert transcript_entries.fold(log.rows).entries == []


def test_a_resumed_message_keeps_the_prose_its_predecessor_folded() -> None:
    """A fold resuming mid-message writes a second opening row about the item its predecessor left
    open, so a reader that took it literally would report the tail as the whole answer."""
    log = _Log()
    item = uuid4()
    log.off_frames(
        ConversationEventKind.ITEM_STARTED, session_events.MessageStartedBody(), item=item, frames=FrameRange(1, 1)
    )
    log.off_frames(
        ConversationEventKind.ITEM_SEGMENT,
        session_events.SegmentBody(text="before "),
        item=item,
        frames=FrameRange(1, 1),
    )
    log.off_frames(
        ConversationEventKind.ITEM_STARTED, session_events.MessageStartedBody(), item=item, frames=FrameRange(2, 2)
    )
    log.off_frames(
        ConversationEventKind.ITEM_SEGMENT,
        session_events.SegmentBody(text="and after"),
        item=item,
        frames=FrameRange(2, 2),
    )
    log.off_frames(
        ConversationEventKind.ITEM_COMPLETED,
        session_events.MessageCompletedBody(backend_item_id=None),
        item=item,
        frames=FrameRange(1, 2),
    )

    entry = one(transcript_entries.fold(log.rows).entries)

    assert isinstance(entry, conversation_records.MessageEntry)
    assert entry.text == "before and after"


def test_withheld_reasoning_has_no_summary_rather_than_an_empty_one() -> None:
    """Without the distinction a withheld item is an empty string no surface can explain."""
    log = _Log()
    item = uuid4()
    log.off_frames(
        ConversationEventKind.ITEM_STARTED, session_events.ReasoningStartedBody(), item=item, frames=FrameRange(1, 1)
    )
    log.off_frames(
        ConversationEventKind.ITEM_COMPLETED,
        session_events.ReasoningCompletedBody(disclosure=ReasoningDisclosure.WITHHELD),
        item=item,
        frames=FrameRange(1, 1),
    )

    entry = one(transcript_entries.fold(log.rows).entries)

    assert isinstance(entry, conversation_records.ReasoningEntry)
    assert entry.summary is None


def test_an_absent_is_error_stays_unknown_rather_than_reading_as_fine() -> None:
    """The field is routinely absent, so a two-valued outcome would report every unanswerable
    case as a success."""
    log = _Log()
    item = uuid4()
    log.off_frames(
        ConversationEventKind.ITEM_STARTED,
        session_events.ToolCallStartedBody(call_id="toolu_1", tool_name="Bash", arguments={}),
        item=item,
        frames=FrameRange(1, 1),
    )
    log.off_frames(
        ConversationEventKind.ITEM_COMPLETED,
        session_events.ToolCallCompletedBody(structured=None, outcome=ToolOutcome.UNKNOWN),
        item=item,
        frames=FrameRange(2, 2),
    )

    _, answered = transcript_entries.fold(log.rows).entries

    assert isinstance(answered, conversation_records.ToolResultEntry)
    assert answered.outcome == conversation_records.Outcome.UNKNOWN


def test_an_item_the_log_closed_twice_is_one_entry() -> None:
    """The adapters address a call by the id its protocol supplies and do not deduplicate, so a
    `tool_result` block the wire repeated reaches the log as a second close. The first is the one
    that happened, as it is for a turn — reporting the repeat would print one answer twice."""
    log = _Log()
    item = uuid4()
    log.off_frames(
        ConversationEventKind.ITEM_STARTED,
        session_events.ToolCallStartedBody(call_id="toolu_1", tool_name="Bash", arguments={}),
        item=item,
        frames=FrameRange(1, 1),
    )
    for seq in (2, 3):
        log.off_frames(
            ConversationEventKind.ITEM_SEGMENT,
            session_events.SegmentBody(text="output"),
            item=item,
            frames=FrameRange(seq, seq),
        )
        log.off_frames(
            ConversationEventKind.ITEM_COMPLETED,
            session_events.ToolCallCompletedBody(structured=None, outcome=ToolOutcome.SUCCEEDED),
            item=item,
            frames=FrameRange(seq, seq),
        )

    asked, answered = transcript_entries.fold(log.rows).entries

    assert isinstance(asked, conversation_records.ToolCallEntry)
    assert isinstance(answered, conversation_records.ToolResultEntry)
    assert answered.content == "output"


def test_a_turn_ending_reaches_the_read_surface_with_no_frames_to_appeal_to() -> None:
    """A turn opens before anything crosses the wire and can close on no frame at all, so its ends
    are the console's own statement rather than a reading of one."""
    log = _Log()
    log.authored(session_events.TurnAbortedBody())

    entry = one(transcript_entries.fold(log.rows).entries)

    assert isinstance(entry, conversation_records.TurnEndEntry)
    assert entry.end == conversation_records.TurnAbortedEnd()
    assert entry.provenance == conversation_records.ConsoleAuthored()


def test_a_prompt_is_on_the_transcript_in_the_voice_that_sent_it() -> None:
    """A conversation without its questions is half a record — and the agent resuming its own
    session is not the operator speaking, which no reader may be left to guess."""
    log = _Log()
    _prompt(log, "what is happening?", SPA_ORIGIN)
    _prompt(log, "a background command finished", HARNESS_ORIGIN)

    asked, woke = transcript_entries.fold(log.rows).entries

    assert isinstance(asked, conversation_records.PromptEntry)
    assert isinstance(woke, conversation_records.PromptEntry)
    assert (asked.text, asked.origin) == ("what is happening?", PromptOriginKind.SPA)
    assert (woke.text, woke.origin) == ("a background command finished", PromptOriginKind.HARNESS)
    assert asked.provenance == conversation_records.ConsoleAuthored()


def test_the_sessions_own_narration_is_not_the_conversation() -> None:
    """Which replica took the lease and what the sandbox printed coming up are facts about the
    runner, and a transcript that mixed them in would report them as things that were said."""
    log = _Log()
    log.authored(session_events.SessionProvisioningBody())
    log.authored(session_events.SessionAdoptedBody(previous_holder=None, holder="replica-1"))
    log.authored(session_events.SetupNarrationBody(text="cloning haku-state"))
    _message(log, "hello")

    entry = one(transcript_entries.fold(log.rows).entries)

    assert isinstance(entry, conversation_records.MessageEntry)


def test_a_row_a_newer_release_wrote_is_reported_rather_than_dropped() -> None:
    """The console rolls with both images serving, so the older one reads rows it has no words for.
    Counting them keeps the rest of the transcript readable while saying what is missing from it."""
    log = _Log()
    _message(log, "hello")
    log.rows.append(
        ConversationEventRow(
            conversation_id=CONVERSATION,
            event_seq=len(log.rows) + 1,
            session_id=SESSION,
            turn_id=TURN,
            item_id=None,
            kind=UnknownValue("something_later"),
            provenance=EventProvenance.AUTHORED,
            source_first_frame_seq=None,
            source_last_frame_seq=None,
            body={"whatever": True},
            created_at=NOW,
        )
    )

    folded = transcript_entries.fold(log.rows)

    assert [entry.kind for entry in folded.entries] == ["message"]
    assert folded.unreadable == {"something_later": 1}


def test_nothing_unreadable_is_absent_rather_than_an_empty_map() -> None:
    log = _Log()
    _message(log, "hello")

    assert transcript_entries.fold(log.rows).unreadable is None


if __name__ == "__main__":
    pytest_bazel.main()
