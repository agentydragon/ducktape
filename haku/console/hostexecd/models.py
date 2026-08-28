"""Canonical state vocabulary for outbound node daemons."""

from enum import StrEnum


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PresenceStatus(StrEnum):
    CONNECTED = "connected"
    BUSY = "busy"
    STALE = "stale"
    OFFLINE = "offline"
