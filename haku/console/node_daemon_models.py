"""Canonical state vocabulary for outbound node daemons."""

from enum import StrEnum


class NodeDaemonExecutionStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class NodeDaemonPresenceStatus(StrEnum):
    CONNECTED = "connected"
    BUSY = "busy"
    STALE = "stale"
    OFFLINE = "offline"
