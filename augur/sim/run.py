"""`SimulationRun` — outputs of one `simulate()` call.

The state-over-time frames (long-form, keyed by
`(rollout_index, month_index, ...)`) plus the append-only event
log. At spike 1 only `cash_balances` is populated; later layers
add `asset_lots`, `liabilities`, `property_state`, etc.
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
    events_log: EventLog
