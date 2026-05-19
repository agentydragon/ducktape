"""Working-state cross-section at one month boundary.

`StateCrossSection` carries the per-month polars frames the engine
reads and writes. At spike 1 there's only the `cash_balances` frame;
later layers add `asset_lots`, `liabilities`, `property_state`,
`property_stakes`, `rollout_status`.

Schemas drop the `month_index` column — the cross-section is one
month wide. The simulate loop tags rows with their month when it
appends them to the growing long-form state frames at output time.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

CASH_BALANCES_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "agent_id": pl.Utf8(),
    "account_id": pl.Utf8(),
    "balance_usd": pl.Float64(),
}


@dataclass(frozen=True)
class StateCrossSection:
    """State at one month boundary, one polars frame per state kind."""

    cash_balances: pl.DataFrame
