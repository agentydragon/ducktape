"""Value domains for the session tables — a dissolving grab-bag.

Stable-side because <database_schema.py> reads these for its columns while their target read/event
modules sit above the schema — moving one there today would import-cycle through
`database_schema.py`; each waits for the reshape that gives it a leaf home.
"""

# CLEANUP(added 2026-08-28): transitional grab-bag, dissolved enum-by-enum
#   (docs/naming_and_layout.md §6 C4). Delete the module once the last vocabulary leaves:
#   the conversation-event enums with C5, the item enums with C6, `RuntimeKind` with C4d,
#   `ChannelSurface` with the channels/ packaging.

from enum import StrEnum


class RuntimeKind(StrEnum):
    """Which concrete runner implementation a conversation is pinned to.

    Stored as text plus an ordinary CHECK rather than as a PostgreSQL enum. The application enum
    keeps readers closed, while widening the database constraint for the next implementation is a
    transactional migration instead of a PostgreSQL enum-type lifecycle.
    """

    CLAUDE_CODE = "claude_code"
    CODEX_APP_SERVER = "codex_app_server"


class ChannelSurface(StrEnum):
    """Which channel holds a copy of a conversation.

    A row exists only for a channel that keeps a copy the console owes work against, so a browser
    tab is not a surface here and `ck_channel_attachment_surface` admits only `matrix`. Naming the
    channel keeps a replaced conversation findable by what held it.
    """

    MATRIX = "matrix"


class TurnOutcome(StrEnum):
    """How one exchange ended. Absent while it is still running.

    A turn is one exchange — the harness handing the agent a prompt through to a final answer
    or a failure — containing many assistant messages, many tool uses and many model round
    trips. It is deliberately not the CLI's own `num_turns`, which counts those round trips and
    so lives *inside* one of these.
    """

    ANSWERED = "answered"
    ABORTED = "aborted"
    FAILED = "failed"


class ItemType(StrEnum):
    """What kind of thing an item is.

    A **decision** vocabulary (<README.md> § Vocabularies across a roll): every reader branches on
    it to know which of the per-type columns mean anything, so no reader-side answer is correct for
    a member it does not have and a new one ships a release behind its reader.
    """

    PROMPT = "prompt"
    MESSAGE = "message"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"


class ToolOutcome(StrEnum):
    """How a tool call went, in the harness vocabulary rather than any one tool's.

    `UNKNOWN` is a real outcome and not a missing one: a call whose answer the backend reported
    without saying whether it succeeded, which every harness protocol permits.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ItemStatus(StrEnum):
    """An item's lifecycle, and nothing else.

    What it replaces put a prompt's queue state and an answer's completeness in one enum, told
    apart only by `role`. The queue state is `conversation_prompt`'s now, where a queue belongs.
    """

    OPEN = "open"
    COMPLETE = "complete"
    FAILED = "failed"


class ReasoningDisclosure(StrEnum):
    """How much of a reasoning item's thinking the backend actually handed back.

    No backend we adapt returns raw chain of thought — Anthropic returns summarised thinking,
    OpenAI a generated summary over content it keeps encrypted, Codex a summary too — so the
    distinction worth storing is not summary-versus-reasoning but whether anything was disclosed
    at all. Without it a withheld item is an empty string no surface can explain.
    """

    SUMMARY = "summary"
    WITHHELD = "withheld"


class ConversationEventKind(StrEnum):
    """The item lifecycle, as log rows.

    **These three take either provenance arm**, which is what separates them from
    `AuthoredEventKind`: an assistant message is folded out of frames, a prompt is a console fact
    that crossed no wire, and both are items with the same three-row lifecycle. Which arm a given
    row may take follows from the item's `item_type`, not from the kind.

    **Prose exists only as segments, and a completion carries none.** A backend that streams has
    its adapter cut the stream into `ITEM_SEGMENT` rows; one that produces only a final string
    emits exactly one segment and then completes. So an item's text is the concatenation of its
    segments by construction, and a consumer replaying from a position can never reprint prose it
    already printed.
    """

    ITEM_STARTED = "item_started"
    ITEM_SEGMENT = "item_segment"
    ITEM_COMPLETED = "item_completed"


class AuthoredEventKind(StrEnum):
    """What a `session_events` row records that no frame carries — the other category.

    **The console is the only witness**, so these carry `EventProvenance.AUTHORED` and no frame
    range. That is the whole membership test: not whether the fact is about the session rather than
    the conversation, but whether it reached the console over the wire.

    A prompt is **not** here, and that is a change from what this arm used to hold: a prompt is an
    item like any other, so it takes the item lifecycle in `ConversationEventKind` and carries this
    arm's provenance. The reason it is authored is unchanged — a prompt is accepted before it is
    asked, since a session holds no sandbox until a prompt buys one — but that is a fact about
    which arm its rows take, not about what kind of thing it is.

    An abort is likewise gone: it is a turn's `outcome`, which is where every backend puts it.

    Several members **name their turn**: the exchange's own two ends. So a reader of this arm
    cannot assume `turn_id IS NULL`.

    **A turn's two ends are here rather than in `ConversationEventKind`, and the bracket is why.**
    A turn is the console's construct, not the wire's: it is a range because the CLI folds a prompt
    sent mid-turn into the running one, so one `result` frame can answer two of these. A turn opens
    before anything crosses the wire, and closes on no frame at all when it failed or was aborted
    before its `result` arrived. So neither end can name the frames it was read from, which is what
    the frame-derived arm requires.
    """

    # A prompt admission refused. Recorded rather than only announced because the refusal is
    # terminal — the message is not delivered and is not coming back — so this row is the only
    # copy of what was said, and it is written in the transaction that acknowledges the message to
    # the channel it arrived on.
    PROMPT_REJECTED = "prompt_rejected"
    # Something arrived on a channel that the console has no way to read: an image, a voice memo,
    # a msgtype invented after this release. One row per event.
    UNREADABLE_INPUT = "unreadable_input"
    SESSION_ADOPTED = "session_adopted"
    LEASE_EXPIRED = "lease_expired"
    # A sandbox is being provisioned for this thread. The only account of it today is the Matrix
    # supervisor's stack frame, so a thread whose session failed before a room was bound has none.
    SESSION_PROVISIONING = "session_provisioning"
    # How a session ended, with the reason it ended for. The session row states only its own end,
    # so without this row the conversation's account of why a *replaced* predecessor died lives
    # nowhere a reader of the stream can see.
    SESSION_ENDED = "session_ended"
    # One line the sandbox printed while coming up. A `SetupOutput` envelope does cross the wire, but
    # what is stored is one decoded line of it rather than the frame, so the console is the witness
    # to the row (`session/setup_output.py`).
    SETUP_NARRATION = "setup_narration"
    # The two ends of one exchange, as the stream states them: without these a reader outside the
    # session has to open `session_turns` to know a turn is running.
    TURN_STARTED = "turn_started"
    TURN_ENDED = "turn_ended"


# What `session_events.kind` holds, over both categories of the one ordered stream.
type StoredEventKind = ConversationEventKind | AuthoredEventKind


class EventProvenance(StrEnum):
    """Which arm of `x/conversation_events.Provenance` a stored event carries.

    A discriminator rather than a nullable frame range. An event the console authored crossed no
    wire and never will, so "no frames" and "frames not recorded" are different states, where on
    `session_messages` both are NULL and no constraint can tell them apart (#4143).
    """

    FRAME_RANGE = "frame_range"
    AUTHORED = "authored"
