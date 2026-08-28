"""The session lifecycle vocabulary: status, the status sets, and how a lease can fail one."""

from enum import StrEnum


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
