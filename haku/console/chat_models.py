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
