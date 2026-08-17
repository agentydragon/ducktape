"""Value domains for the session tables.

Stable-side because <database_schema.py> owns the tables these describe, while the chat
surfaces that read and write them live in `x/` — an enum here cannot invert that dependency.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SessionStatus(StrEnum):
    # Where every session starts: one is created only once a sandbox is being provisioned for it,
    # the conversation holding an incoming prompt until then.
    PROVISIONING = "provisioning"
    READY = "ready"
    # Derived, never stored: `session_views.session_view` reports it for a live session with an
    # open `session_turns` row, and no path writes it to `sessions.status`.
    RESPONDING = "responding"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class ChatSurface(StrEnum):
    """Which front end a session was created for.

    Not cosmetic: a past conversation is only findable if the row says what it was, so a Matrix
    session that has been replaced stays distinguishable from an SPA one.
    """

    SPA = "spa"
    MATRIX = "matrix"


class PromptOriginKind(StrEnum):
    """Which arm of `PromptOrigin` a prompt carries.

    Its members coincide with `ChatSurface`'s and it stays its own vocabulary: this discriminates a
    value stored inside a `prompt_enqueued` body, `ChatSurface` names how a session was created, and
    one enum for both would make a change to either meaning rewrite the other's stored strings.
    """

    SPA = "spa"
    MATRIX = "matrix"


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
    (<plans/conversation_layers.md> § 1).

    **`address` is why this is not just a ref.** One bot serves many rooms, so a bare event id
    cannot tell a sibling room's copy from this room's — and telling them apart is the whole job of
    the reader this exists for.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[PromptOriginKind.MATRIX] = PromptOriginKind.MATRIX
    address: str = Field(description="Which room. Never parsed outside the Matrix channel.")
    refs: tuple[str, ...] = Field(description="The events folded into this prompt, oldest first.")


type PromptOrigin = SpaOrigin | MatrixOrigin

# The console's own surface, as one value rather than one per call: `SpaOrigin` carries nothing, so
# every instance is the same statement and a shared frozen one says so.
SPA_ORIGIN = SpaOrigin()


class FrameDirection(StrEnum):
    """Which way a recorded rollout frame crossed the wire.

    Named for the agent rather than for the console and the runner, because which process sits
    at each end is exactly what session re-adoption is expected to change
    (haku/plans/cli_protocol_ownership.md) and a stored record should survive that.
    """

    TO_AGENT = "to_agent"
    FROM_AGENT = "from_agent"


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
    # while the supervisor mints its replacement. The one member that cannot be recorded — a
    # `session_events` row names a session, and here there is none to name.
    NO_SESSION = "no_session"
    SESSION_NOT_READY = "session_not_ready"
    TURN_IN_FLIGHT = "turn_in_flight"
    PROMPT_QUEUED = "prompt_queued"


class ChatMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessageStatus(StrEnum):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETE = "complete"
    FAILED = "failed"


class RecordedToolCall(BaseModel):
    """One tool call, as a transcript row records it: which tool, with what, under which id.

    Spelled in the conversation vocabulary (`x/conversation_events.ToolCallStarted`) rather than in
    a backend's, so nothing here is provider-specific: every tool protocol worth storing has a name,
    some arguments, and an id to answer against.

    **What the call answered is deliberately not here.** `call_id` is the correlation key and the
    only half of the pair this row holds; the answer is joined at read time out of the stored
    events (`x/session_views.tool_calls`).
    """

    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(description="Correlates this call to its result. Unique within a session.")
    tool_name: str
    arguments: dict[str, Any] = Field(description="Whatever the agent passed, as the protocol carried it.")


class ConversationEventKind(StrEnum):
    """What a `session_events` row records that came off the runner↔console wire.

    **Membership is decided by where the row came from, not by what it is about.** Every kind here
    is produced by folding a recorded frame, so every row carries `EventProvenance.FRAME_RANGE` and
    a frame range that says which frames it was read from. A fact the console holds but no frame
    carries belongs in `AuthoredEventKind`, however conversational it reads.

    The vocabulary is `x/conversation_events.ConversationEvent` less its two members that already
    have a durable home: a `TextDelta` is an increment of prose the completed message carries
    whole, and a `TurnCompleted` is the `session_turns` row.
    """

    MESSAGE_COMPLETED = "message_completed"
    REASONING = "reasoning"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"


# The two kinds that name a call rather than a message, and so carry `session_events.call_id`.
TOOL_CALL_EVENT_KINDS = frozenset({ConversationEventKind.TOOL_CALL_STARTED, ConversationEventKind.TOOL_CALL_COMPLETED})


class AuthoredEventKind(StrEnum):
    """What a `session_events` row records that no frame carries — the other category.

    **The console is the only witness**, so these carry `EventProvenance.AUTHORED` and no frame
    range. That is the whole membership test: not whether the fact is about the session rather than
    the conversation, but whether it reached the console over the wire.

    `PROMPT_ENQUEUED` is here for that reason and not because a prompt is a lifecycle fact. A prompt
    is accepted before it is asked: a session holds no sandbox until a prompt buys one, so at
    acceptance there is no runner to send it to, and a session that ends before the prompt is
    claimed never sends it at all. The frame that eventually carries it, if one does, projects to
    nothing — the console already holds the text (`x/claude_code/projection._user`).

    `TURN_ABORTED` is the one member that **names its turn**: the operator stopping an exchange is
    a fact about that exchange. So a reader of this arm cannot assume `turn_id IS NULL`.
    """

    PROMPT_ENQUEUED = "prompt_enqueued"
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
    TURN_ABORTED = "turn_aborted"


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
OPEN_SESSION_STATUSES = frozenset({SessionStatus.PROVISIONING, SessionStatus.READY, SessionStatus.RESPONDING})
# Derived rather than spelled out: the two sets partition the enum, and a status added to one
# without the other is the bug this shape makes unrepresentable.
ENDED_SESSION_STATUSES = frozenset(SessionStatus) - OPEN_SESSION_STATUSES
# Whether something holds this session and is renewing its lease, which is a different question from
# whether it has ended even though every status answers both the same way today. Spelled out rather
# than derived from the set above, so an open status nothing holds is not swept by default: the
# sweep fails a session whose lease lapsed, and a status that renews no lease would be failed on
# sight.
LEASED_SESSION_STATUSES = frozenset({SessionStatus.PROVISIONING, SessionStatus.READY, SessionStatus.RESPONDING})
