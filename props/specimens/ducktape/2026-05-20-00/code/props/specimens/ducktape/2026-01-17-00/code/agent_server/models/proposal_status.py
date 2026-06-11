from __future__ import annotations

from enum import StrEnum


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ERROR = "error"
