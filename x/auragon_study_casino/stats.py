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


class BlackjackOutcomeFreq(BaseModel):
    """Per-outcome frequency row. Drops the payout / EV / win-rate columns
    that are tautological once the outcome is fixed (within "Win", every hand
    pays 2× wager; the percent columns add no information beyond the label)."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    count: int
    freq: float
    avg_wager: float


class BlackjackSlice(BaseModel):
    """W/L/P + RTP/EV breakdown over a non-trivial slice of hands (e.g. by
    dealer upcard, by doubled-or-not). Unlike per-outcome rows, all six
    outcomes can occur within a slice, so the percent columns are informative."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    count: int
    wins: int
    losses: int
    pushes: int
    wagered: int
    returned: int
    net: int
    rtp: float | None
    ev_per_credit: float | None


class BlackjackSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    wins: int
    losses: int
    pushes: int
    blackjacks: int
    busts: int
    # W / (W + L) — excludes pushes from the denominator, unlike the legacy
    # `payout_rate` which counted any returned-stake outcome as a win.
    win_rate_excl_push: float | None
    blackjack_rate: float | None


class BlackjackStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: BlackjackSummary
    outcome_freq: list[BlackjackOutcomeFreq]
    by_dealer_upcard: list[BlackjackSlice]
    by_doubled: list[BlackjackSlice]


class GameStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    game: str
    total: WagerBucketStats
    buckets: list[WagerBucketStats]
    timeline: list[TimeBucketStats]
    # Populated only for `game == "blackjack"`; replaces the per-outcome
    # WagerBucketStats rendering on the frontend with strategy-relevant slices.
    blackjack: BlackjackStats | None = None


class CasinoStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    since_date: str
    event_count: int
    games: list[GameStats]
