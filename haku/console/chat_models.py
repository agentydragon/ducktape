"""Value domains for the Claude chat tables.

Stable-side because <database_schema.py> owns the tables these describe, while the chat
surfaces that read and write them live in `x/` — an enum here cannot invert that dependency.
"""

from enum import StrEnum


class ChatSessionStatus(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    RESPONDING = "responding"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class ChatSurface(StrEnum):
    """Which front end a session was created for.

    Not cosmetic: a past conversation is only findable if the row says what it was, and until
    this existed the room binding lived in `matrix_conversation`, which holds exactly one
    `session_id` — so a replaced Matrix session became indistinguishable from an SPA one the
    moment the supervisor moved on (matrix_chat_runtime.md R11.3a).
    """

    SPA = "spa"
    MATRIX = "matrix"


class FrameDirection(StrEnum):
    """Which way a recorded rollout frame crossed the wire.

    Named for the agent rather than for the console and the runner, because which process sits
    at each end is exactly what session re-adoption is expected to change
    (haku/plans/cli_protocol_ownership.md) and a stored record should survive that.
    """

    TO_AGENT = "to_agent"
    FROM_AGENT = "from_agent"


class FrameKind(StrEnum):
    """The kinds of rollout frame the console names.

    **Deliberately not `ChatMessageRole`**, which shares two of these spellings by coincidence: a
    `user` frame from the agent carries a tool result, not a user message, and either enum gaining
    a value would silently change what the other selects.

    **Deliberately not the type of `claude_chat_frames.kind` either.** That column is the CLI's own
    top-level `type` verbatim, and the CLI may send a type this console has never heard of — a
    record that refuses to store one is worse than a name it cannot match. So the column stays
    text and this names the subset the console dispatches on, plus the two kinds the console
    authors itself and which say so.
    """

    # The CLI's own.
    ASSISTANT = "assistant"
    USER = "user"
    RESULT = "result"
    SYSTEM = "system"
    STREAM_EVENT = "stream_event"
    CONTROL_REQUEST = "control_request"
    CONTROL_RESPONSE = "control_response"
    COMMAND_LIFECYCLE = "command_lifecycle"
    # The console's own: a line the sandbox printed, and its reconstruction of an answer still
    # streaming. Both are renderings rather than wire frames, which is why they are named here.
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


class ChatMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessageStatus(StrEnum):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETE = "complete"
    FAILED = "failed"


LIVE_SESSION_STATUSES = frozenset(
    {ChatSessionStatus.PROVISIONING, ChatSessionStatus.READY, ChatSessionStatus.RESPONDING}
)
# Derived rather than spelled out: the two sets partition the enum, and a status added to one
# without the other is the bug this shape makes unrepresentable.
ENDED_SESSION_STATUSES = frozenset(ChatSessionStatus) - LIVE_SESSION_STATUSES
