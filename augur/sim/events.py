"""Event log for the simulation.

Every state-changing happening is a row on an event-kind frame.
`EventLog` bundles all the kind frames together so the simulate loop
can hand one object to `apply_events`. Each kind frame's schema is
keyed by `(rollout_index, month_index, cause_id)` plus the kind-
specific columns.

At spike 1 step 4: `transfers`, `asset_purchases`, and
`lot_dispositions` are populated. Later layers add tax accruals +
payments, mortgage payments, obligation accruals + settlements,
occupancy-mode changes, depreciation accruals, failure events, etc.
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
    # Tax classification: when set (e.g. "ordinary" for W-2 wages),
    # apply_events increments the recipient's ordinary_income_ytd.
    # Null for non-income transfers (e.g. expense payments).
    "income_category": pl.Utf8(),
}

# `AssetPurchase` records the creation of a new tax lot — either an
# initial holding seeded at scenario start, or (later) an in-sim buy.
# Initial-holding purchases at spike-1 step 4 do not draw cash; an
# in-sim buy in a later layer will be paired with a transfer that
# debits cash. The lot the purchase creates is keyed by
# `(rollout_index, lot_id)` and shows up as a new row in
# `state.asset_lots` with `remaining_quantity = quantity`.
ASSET_PURCHASE_EVENT_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "month_index": pl.Int64(),
    "cause_id": pl.Utf8(),
    "agent_id": pl.Utf8(),
    "asset_id": pl.Utf8(),
    "lot_id": pl.Utf8(),
    "quantity": pl.Float64(),
    "cost_basis_per_unit_usd": pl.Float64(),
}

# `TaxAccrual` records a year-end tax computation: a single
# year's ordinary income for one agent under one jurisdiction has
# been bracket-walked, and the resulting tax `amount_usd` is now
# owed. apply_events appends a row to `state.tax_liabilities` and
# zeroes the agent's `ordinary_income_ytd` for the next year.
TAX_ACCRUAL_EVENT_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "month_index": pl.Int64(),
    "cause_id": pl.Utf8(),
    "agent_id": pl.Utf8(),
    "jurisdiction_id": pl.Utf8(),
    "tax_year_end_month": pl.Int64(),
    "amount_usd": pl.Float64(),
}

# `LotDisposition` records the consumption of part (or all) of one
# lot by one logical sale. A single AssetSale "sell N units of vti"
# decomposes into one disposition row per lot the sale ate into;
# `cause_id` groups all dispositions of the same sale for downstream
# tax classification. Holding period for LTCG/STCG is
# `month_index - purchase_month_index`.
LOT_DISPOSITION_EVENT_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64(),
    "month_index": pl.Int64(),
    "cause_id": pl.Utf8(),
    "agent_id": pl.Utf8(),
    "asset_id": pl.Utf8(),
    "lot_id": pl.Utf8(),
    "purchase_month_index": pl.Int64(),
    "units_sold": pl.Float64(),
    "cost_basis_consumed_usd": pl.Float64(),
    "proceeds_usd": pl.Float64(),
    "proceeds_account_id": pl.Utf8(),
}


@dataclass(frozen=True)
class EventLog:
    """Per-step or per-simulation collection of events, one frame
    per event kind."""

    transfers: pl.DataFrame
    asset_purchases: pl.DataFrame
    lot_dispositions: pl.DataFrame
    tax_accruals: pl.DataFrame

    @classmethod
    def empty(cls) -> EventLog:
        return cls(
            transfers=pl.DataFrame(schema=TRANSFER_EVENT_SCHEMA),
            asset_purchases=pl.DataFrame(schema=ASSET_PURCHASE_EVENT_SCHEMA),
            lot_dispositions=pl.DataFrame(schema=LOT_DISPOSITION_EVENT_SCHEMA),
            tax_accruals=pl.DataFrame(schema=TAX_ACCRUAL_EVENT_SCHEMA),
        )
