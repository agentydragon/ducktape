"""The proxy's own record of what it decided: a bounded ring per subject, and the JSON log line."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from x.agentplane.egress.policy import DenyReason

logger = logging.getLogger(__name__)


class Outcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class DecisionRecord(BaseModel):
    """One admission as served on the admin port and logged; carries names, never values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    at: datetime
    sandbox: str | None = Field(description="The subject; absent when the token did not prove one.")
    method: str
    host: str
    port: int
    path: str | None = Field(description="Absent for a CONNECT.")
    outcome: Outcome
    reason: DenyReason | None = None
    binding: str | None = None
    policy: str | None = None
    rule: int | None = None
    substituted: bool = Field(default=False, description="Whether a credential replaced a placeholder.")
    address: str | None = Field(default=None, description="The address the host was pinned to, when admitted.")


class DecisionRing:
    """The last `capacity` decisions per subject; unidentified requests share one ring under `None`."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._rings: defaultdict[str | None, deque[DecisionRecord]] = defaultdict(lambda: deque(maxlen=self._capacity))

    def record(self, decision: DecisionRecord) -> None:
        self._rings[decision.sandbox].append(decision)
        logger.info("%s", decision.model_dump_json())

    def recent(self, sandbox: str | None) -> list[DecisionRecord]:
        return list(self._rings[sandbox]) if sandbox in self._rings else []
