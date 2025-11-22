"""Shared types for MCP bridge."""

from enum import StrEnum

from adgn.agent.types import AgentID

__all__ = ["AgentID", "AgentMode", "RunPhase", "ApprovalStatus"]


class AgentMode(StrEnum):
    """Agent mode enumeration."""

    LOCAL = "local"
    BRIDGE = "bridge"


class RunPhase(StrEnum):
    """Agent run phase status."""

    IDLE = "idle"
    WAITING_APPROVAL = "waiting_approval"
    SAMPLING = "sampling"


class ApprovalStatus(StrEnum):
    """Status of an approval (pending or decided)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DENIED = "denied"
    ABORTED = "aborted"
