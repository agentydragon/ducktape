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
    (haku/plans/session_readoption.md) and a stored record should survive that.
    """

    TO_AGENT = "to_agent"
    FROM_AGENT = "from_agent"


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
