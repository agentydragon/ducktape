"""Aggregated casino stats — empirical payout rates / EV per wager type.

Only `server_resolved` `game_events` rows are aggregated. Pre-2026-05-07
`client_reported` rows remain in the table for ledger reconstruction but
are excluded here because their `outcome` payload shape is not guaranteed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# Cutover date from `client_reported` to `server_resolved`. Empirical
# stats only cover events from this date forward.
SERVER_RESOLVED_SINCE_DATE = "2026-05-07"


class WagerBucketStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    count: int
    wins: int
    wagered: int
    returned: int
    net: int
    payout_rate: float | None
    rtp: float | None
    ev_per_credit: float | None
    theoretical_payout_rate: float | None = None
    theoretical_rtp: float | None = None
    theoretical_ev_per_credit: float | None = None


class TimeBucketStats(BaseModel):
    """Aggregate over a single time slice (UTC day) for one game."""

    model_config = ConfigDict(extra="forbid")

    date: str  # ISO date "YYYY-MM-DD" (UTC)
    count: int
    wins: int
    wagered: int
    returned: int
    net: int
    rtp: float | None


class GameStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    game: str
    total: WagerBucketStats
    buckets: list[WagerBucketStats]
    timeline: list[TimeBucketStats]


class CasinoStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    since_date: str
    event_count: int
    games: list[GameStats]
