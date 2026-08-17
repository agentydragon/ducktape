"""Value domains for the session tables.

Stable-side because <database_schema.py> owns the tables these describe, while the chat
surfaces that read and write them live in `x/` — an enum here cannot invert that dependency.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionStatus(StrEnum):
    # CLEANUP(added 2026-08-16): `idle` deliberately has no writer yet — a session that exists and
    #   holds no sandbox (<../plans/chat_runtime_cleanup.md> § Stage 6). This column is parsed
    #   (`database_schema.TextBackedStrEnumColumn`), so a replica on the previous image reading an
    #   `idle` row raises rather than degrading, and the console rolls with `maxUnavailable: 0`
    #   (<README.md> § Perimeter / deploy). Write the first `idle` row only once every pod runs an
    #   image at or after the release carrying migration 0054 — one tag out of
    #   `kubectl get pods -n haku-console -o jsonpath='{.items[*].spec.containers[0].image}'`,
    #   whose commit suffix is at or after this commit. Delete this comment with that writer; the
    #   sets below already classify `idle` as open and unleased, so nothing else waits on it.
    IDLE = "idle"
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
    ACTIVITY_STARTED = "activity_started"
    ACTIVITY_COMPLETED = "activity_completed"


# The two kinds that name a call rather than a message, and so carry `session_events.call_id`.
TOOL_CALL_EVENT_KINDS = frozenset({ConversationEventKind.TOOL_CALL_STARTED, ConversationEventKind.TOOL_CALL_COMPLETED})


class AuthoredEventKind(StrEnum):
    """What a `session_events` row records that no frame carries — the other category.

    **The console is the only witness**, so these carry `EventProvenance.AUTHORED` and no frame
    range (<../plans/chat_runtime_projection.md> § stage 4). That is the whole membership test:
    not whether the fact is about the session rather than the conversation, but whether it reached
    the console over the wire. A session that never had a runner at all is exactly the case this
    category exists to record.

    `PROMPT_ENQUEUED` is here for that reason and not because a prompt is a lifecycle fact. It is
    conversation — half of what a transcript renders — but a prompt is accepted before it is asked:
    a session holds no sandbox until a prompt buys one, so at acceptance there is no runner to send
    it to, and a session that ends before the prompt is claimed never sends it at all
    (`PromptFate.LOST`). The frame that eventually carries it, if one does, projects to nothing —
    the console already holds the text (`x/claude_code/projection._user`), so it is recorded once.
    """

    PROMPT_ENQUEUED = "prompt_enqueued"
    SESSION_ADOPTED = "session_adopted"
    LEASE_EXPIRED = "lease_expired"


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
# Whether something holds this session and is renewing its lease. A lapsed lease is evidence its
# holder died only for these: an `idle` session is open with nothing holding it, so a sweep keyed
# on `OPEN_SESSION_STATUSES` would fail a session nothing is wrong with. Spelled out rather than
# derived from it, so a status added there with no holder is not swept by default.
LEASED_SESSION_STATUSES = frozenset({SessionStatus.PROVISIONING, SessionStatus.READY, SessionStatus.RESPONDING})
