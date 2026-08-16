"""Value domains for the session tables.

Stable-side because <database_schema.py> owns the tables these describe, while the chat
surfaces that read and write them live in `x/` — an enum here cannot invert that dependency.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionStatus(StrEnum):
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


class PromptFate(StrEnum):
    """What became of an accepted prompt, for a caller still owing an acknowledgement for it.

    `enqueue_prompt` accepting a prompt is not the same as anything working on it: the session it
    was queued against can end before it is ever claimed, and the replacement session's
    `next_prompt` never sees a row keyed to its predecessor. A surface that acknowledged the
    prompt's source the moment it was accepted therefore has no way back to it — which is what
    Matrix ingress asks this to avoid (R2.5).
    """

    IN_FLIGHT = "in_flight"
    # Its turn ended, whatever the turn's `TurnOutcome` was. **A failed or aborted turn is a
    # completed one here**: waiting for `ANSWERED` would hold a source's acknowledgement against
    # a turn that will never produce one, and re-offering work that already crashed the runtime
    # once converges no better than re-offering an unreadable event does (message_drops.md I1).
    COMPLETED = "completed"
    # Its session ended without it ever being claimed into a turn that ran, so nothing will
    # answer it. The prompt is not re-queued anywhere: the source still holds it, and offering it
    # again to the replacement session is the only thing that can answer it.
    LOST = "lost"


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
    a backend's. `tool_use_id`/`name`/`input` — the shape this replaces — are Anthropic's wire
    words, so a second backend had to pretend to be Claude in order to record a call its agent
    made. Nothing here is provider-specific: every tool protocol worth storing has a name, some
    arguments, and an id to answer against.

    **What the call answered is deliberately not here.** `call_id` is the correlation key and the
    only half of the pair this row holds; the answer is joined at read time out of the frame log
    (`x/session_views.SessionToolCallView`).
    """

    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(description="Correlates this call to its result. Unique within a session.")
    tool_name: str
    arguments: dict[str, Any] = Field(description="Whatever the agent passed, as the protocol carried it.")


LIVE_SESSION_STATUSES = frozenset({SessionStatus.PROVISIONING, SessionStatus.READY, SessionStatus.RESPONDING})
# Derived rather than spelled out: the two sets partition the enum, and a status added to one
# without the other is the bug this shape makes unrepresentable.
ENDED_SESSION_STATUSES = frozenset(SessionStatus) - LIVE_SESSION_STATUSES
