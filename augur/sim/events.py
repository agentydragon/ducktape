"""Event log for the simulation.

Every state-changing happening is a row on an event-kind frame.
`EventLog` bundles all the kind frames together so the simulate loop
can hand one object to `apply_events`. Each kind frame's schema is
keyed by `(rollout_index, month_index, cause_id)` plus the kind-
specific columns.

At spike 1 only `transfers` is populated; later layers add
asset_purchases, asset_sales, mortgage_payments, obligation
accruals + settlements, tax accruals + payments, occupancy-mode
changes, depreciation accruals, failure events, etc.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

TRANSFER_EVENT_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "month_index": pl.Int64(),
    "cause_id": pl.Utf8(),
    "from_agent_id": pl.Utf8(),
    "from_account_id": pl.Utf8(),
    "to_agent_id": pl.Utf8(),
    "to_account_id": pl.Utf8(),
    "amount_usd": pl.Float64(),
}


@dataclass(frozen=True)
class EventLog:
    """Per-step or per-simulation collection of events, one frame
    per event kind."""

    transfers: pl.DataFrame

    @classmethod
    def empty(cls) -> EventLog:
        return cls(transfers=pl.DataFrame(schema=TRANSFER_EVENT_SCHEMA))
