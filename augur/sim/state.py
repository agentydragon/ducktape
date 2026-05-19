"""Working-state cross-section at one month boundary.

`StateCrossSection` carries the per-month polars frames the engine
reads and writes. At spike 1 step 4 there are two frames:
`cash_balances` and `asset_lots`. Later layers add `liabilities`,
`property_state`, `property_stakes`, `rollout_status`.

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

# An asset lot is a tax-relevant unit-of-acquisition: a quantity of
# some asset bought at a specific time at a specific per-unit cost
# basis. Sales consume from lots in some order (FIFO for spike 1);
# the holding period from `purchase_month_index` determines LTCG vs
# STCG classification in later layers. `remaining_quantity` shrinks
# as the lot is sold off and reaches 0 when fully disposed.
#
# `(rollout_index, lot_id)` is the unique key: the same configured
# lot fans out into one row per rollout so different rollouts can
# diverge in remaining_quantity if their sale paths differ.
ASSET_LOT_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "lot_id": pl.Utf8(),
    "agent_id": pl.Utf8(),
    "asset_id": pl.Utf8(),
    "purchase_month_index": pl.Int64(),
    "cost_basis_per_unit_usd": pl.Float64(),
    "remaining_quantity": pl.Float64(),
}


@dataclass(frozen=True)
class StateCrossSection:
    """State at one month boundary, one polars frame per state kind."""

    cash_balances: pl.DataFrame
    asset_lots: pl.DataFrame
