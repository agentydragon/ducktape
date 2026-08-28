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
