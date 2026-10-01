"""Agentplane watch-store freshness reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# How many resync periods a kind may miss before a store calls itself stale: one late cycle is a
# slow API server, three in a row is a wedge.
STALE_AFTER_CYCLES = 3


@dataclass
class Freshness:
    """When each watched kind last completed a cycle, and whether that is recent enough to act on.

    Whether the first list finished is a latch: it goes true once and never falls back, so a store
    whose watches have since wedged keeps answering from a frozen copy while every answer stays
    plausible. This is the fact that does fall back. `ListWatch` seeds every kind before it starts
    and records one per completed cycle after, so a process that never finishes a first cycle goes
    stale like any other rather than reading as fresh for having nothing to be late against.
    """

    stale_after_seconds: float
    at: dict[str, datetime] = field(default_factory=dict)

    def record(self, kind: str, at: datetime) -> None:
        self.at[kind] = at

    def ages(self, now: datetime) -> dict[str, float]:
        """Seconds since each kind last completed a cycle, in kind order."""
        return {kind: (now - when).total_seconds() for kind, when in sorted(self.at.items())}

    def fresh(self, now: datetime) -> bool:
        ages = self.ages(now)
        # A negative age is a clock that moved backwards, which is no more trustworthy than a late one.
        return bool(ages) and all(0 <= age <= self.stale_after_seconds for age in ages.values())
