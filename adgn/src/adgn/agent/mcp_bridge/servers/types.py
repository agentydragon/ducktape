"""Types for agents bridge MCP servers."""

from enum import StrEnum

from pydantic import BaseModel


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
