"""`SimulationRun` — outputs of one `simulate()` call.

The state-over-time frames (long-form, keyed by
`(rollout_index, month_index, ...)`) plus the append-only event
log. At spike 1 step 4: `cash_balances` and `asset_lots`. Later
layers add `liabilities`, `property_state`, etc.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from augur.sim.events import EventLog


@dataclass(frozen=True)
class SimulationRun:
    """Outputs of a simulation. Long-form polars frames keyed by
    `(rollout_index, month_index, ...)` plus the event log."""

    cash_balances: pl.DataFrame
    asset_lots: pl.DataFrame
    market_prices: pl.DataFrame
    events_log: EventLog
