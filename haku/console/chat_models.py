"""Value domains for the session tables.

Stable-side because <database_schema.py> owns the tables these describe, while the chat
surfaces that read and write them live in `x/` — an enum here cannot invert that dependency.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SessionStatus(StrEnum):
    """A session's lifecycle, derived from the row's facts rather than stored.

    `database_schema.Session.status` computes every member but one from the fact columns; the wire
    and event vocabulary is this enum, so consumers are untouched by where a member comes from.
    """

    IDLE = "idle"
    PROVISIONING = "provisioning"
    READY = "ready"
    # The one member the row cannot spell: whether a turn is open is `conversation_turn`'s fact,
    # and `conversation_views.live_status` derives it on top of the row's member.
    RESPONDING = "responding"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


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


class PromptOriginKind(StrEnum):
    """Which arm of `PromptOrigin` a prompt carries.

    Its own vocabulary, overlapping `ChannelSurface` only at `MATRIX`: this discriminates a value
    stored inside a `prompt_enqueued` body, `ChannelSurface` names which channel holds a
    conversation's copy, and one enum for both would make a change to either meaning rewrite the
    other's stored strings. `SPA` (the operator typing into the console) and `HARNESS` (the harness
    resuming its own session) have no surface at all — neither is a channel anything can attach to.
    """

    SPA = "spa"
    MATRIX = "matrix"
    HARNESS = "harness"


class SpaOrigin(BaseModel):
    """The operator typed this into the console.

    **No address, and that is not an inconsistency with the room's arm.** An address exists so a
    channel can tell its own copy of a prompt from a sibling attachment's; a browser tab holds no
    copy to confuse, because it renders the record rather than keeping one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[PromptOriginKind.SPA] = PromptOriginKind.SPA


class MatrixOrigin(BaseModel):
    """The operator said this in a Matrix room, as one or more events folded into one prompt.

    **Both strings are opaque to everything but the Matrix channel.** Only the channel that minted
    an origin may look inside one; **everything else compares, it never interprets**. That is what
    lets the conversation layer hold a channel's address without learning its vocabulary
    (<docs/chat_layers.md>).

    **`address` is why this is not just a ref.** One bot serves many rooms, so a bare event id
    cannot tell a sibling room's copy from this room's — and telling them apart is the whole job of
    the reader this exists for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[PromptOriginKind.MATRIX] = PromptOriginKind.MATRIX
    address: str = Field(description="Which room. Never parsed outside the Matrix channel.")
    refs: tuple[str, ...] = Field(description="The events folded into this prompt, oldest first.")


class HarnessOrigin(BaseModel):
    """The harness resumed the session itself — nobody typed this.

    Claude Code wakes its own session to observe work it left running: a background command's
    completion notification, a `ScheduleWakeup` firing. The exchange that follows has no operator
    behind it, and a transcript that rendered its opening as the operator speaking would put words
    in their mouth. What woke the harness is the prompt item's own text; the origin only has to say
    whose voice it is.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[PromptOriginKind.HARNESS] = PromptOriginKind.HARNESS


type PromptOrigin = SpaOrigin | MatrixOrigin | HarnessOrigin

# The console's own surface, as one value rather than one per call: `SpaOrigin` carries nothing, so
# every instance is the same statement and a shared frozen one says so.
SPA_ORIGIN = SpaOrigin()
HARNESS_ORIGIN = HarnessOrigin()


class FrameDirection(StrEnum):
    """Which way a recorded rollout frame crossed the wire.

    Named for the agent rather than for the console and the runner, because which process sits
    at each end is exactly what session re-adoption changes
    (haku/runtime/x/bridge/docs/design.md) and a stored record should survive that.
    """

    TO_AGENT = "to_agent"
    FROM_AGENT = "from_agent"


class BridgeFrameKind(StrEnum):
    """Which Haku bridge envelope was durably recorded.

    The selected harness is immutable on the session, and its native discriminator remains inside
    the opaque payload.  This enum therefore names only Haku's framing vocabulary, never Claude's
    ``type`` or Codex's JSON-RPC method.
    """

    HARNESS_FRAME = "harness_frame"
    SETUP_OUTPUT = "setup_output"


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


class PromptRejection(StrEnum):
    """Which state refused a prompt, in the vocabulary a surface answers its sender in.

    Admission is `enqueue_prompt`'s decision under `SELECT … FOR UPDATE`, and a refusal is
    terminal: nothing queues behind it and the sender is told to send again. The member is what
    the answer says, so it is coarse on purpose — an operator acts on "Haku is busy" and on "there
    is nothing behind this room yet" differently, and on nothing finer.
    """

    # No session behind the surface at all: none has ever been provisioned, or the row has gone
    # while the supervisor mints its replacement. Recorded like every other refusal — what a
    # rejection is about is the conversation, which exists whether or not a session does.
    NO_SESSION = "no_session"
    SESSION_NOT_READY = "session_not_ready"
    TURN_IN_FLIGHT = "turn_in_flight"
    PROMPT_QUEUED = "prompt_queued"


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
    # to the row (`x/setup_output.py`).
    SETUP_NARRATION = "setup_narration"
    # The two ends of one exchange, as the stream states them: without these a reader outside the
    # session has to open `session_turns` to know a turn is running.
    TURN_STARTED = "turn_started"
    TURN_ENDED = "turn_ended"


# What `session_events.kind` holds, over both categories of the one ordered stream.
type StoredEventKind = ConversationEventKind | AuthoredEventKind


class LeaseExpiryReason(StrEnum):
    """Which of the three ways a session's lease lapsed past `ADOPTION_GRACE` and failed it.

    Recorded rather than derived from the prose the operator is shown: the sweep decides between
    these by looking at columns that are gone by the time anyone reads the error string.
    """

    # A replica held it and went away without handing it back — SIGKILL, OOM, node loss.
    HOLDER_GONE = "holder_gone"
    # A runner was here and released or dropped, and no replica took it back over: a roll, or the
    # sandbox reaching its TTL. The common case.
    UNADOPTED = "unadopted"
    # No runner ever attached, so the session died having produced nothing at all.
    NEVER_ATTACHED = "never_attached"


class EventProvenance(StrEnum):
    """Which arm of `x/conversation_events.Provenance` a stored event carries.

    A discriminator rather than a nullable frame range. An event the console authored crossed no
    wire and never will, so "no frames" and "frames not recorded" are different states, where on
    `session_messages` both are NULL and no constraint can tell them apart (#4143).
    """

    FRAME_RANGE = "frame_range"
    AUTHORED = "authored"


# Whether the session is worth keeping: nothing has ended it, so a supervisor must not replace it
# and the claim sweep must not clean up after it.
OPEN_SESSION_STATUSES = frozenset(
    {SessionStatus.IDLE, SessionStatus.PROVISIONING, SessionStatus.READY, SessionStatus.RESPONDING}
)
# Derived rather than spelled out: the two sets partition the enum, and a status added to one
# without the other is the bug this shape makes unrepresentable.
ENDED_SESSION_STATUSES = frozenset(SessionStatus) - OPEN_SESSION_STATUSES
# Whether something holds this session and is renewing its lease. An idle session is open but has
# no sandbox or lease holder, so deriving this from `OPEN_SESSION_STATUSES` would make the stale
# lease sweep fail healthy empty sessions.
LEASED_SESSION_STATUSES = frozenset({SessionStatus.PROVISIONING, SessionStatus.READY, SessionStatus.RESPONDING})
