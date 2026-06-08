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

from finance.augur.frames import FrameSpec

CASH_BALANCES_SCHEMA = pl.Schema(
    {"rollout_index": pl.Int64(), "agent_id": pl.Utf8(), "account_id": pl.Utf8(), "balance_usd": pl.Float64()}
)

# Running total of ordinary (W-2-style) income for the current tax
# year, per `(rollout_index, agent_id)`. Reset to 0 by year-end
# tax accrual events. Only taxed agents have rows here (the engine
# initializes one row per `TaxProfile.agent_id` per rollout).
ORDINARY_INCOME_YTD_SCHEMA = pl.Schema(
    {"rollout_index": pl.Int64(), "agent_id": pl.Utf8(), "ordinary_income_usd": pl.Float64()}
)

# Outstanding tax liabilities — money owed to a tax authority that
# hasn't been paid yet. The year-end accrual event creates one row
# per `(rollout, agent, jurisdiction, tax_year_end_month)`; later
# layers reduce `amount_owed_usd` as payments fire.
TAX_LIABILITIES_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "agent_id": pl.Utf8(),
        "jurisdiction_id": pl.Utf8(),
        "tax_year_end_month": pl.Int64(),
        "amount_owed_usd": pl.Float64(),
    }
)

# Running total of net capital gains for the current tax year, per
# `(rollout_index, agent_id, classification)` where `classification`
# is one of {"ltcg", "stcg"}. Reset at year-end alongside ordinary
# income. The split is determined at the lot_disposition level by
# `holding_period = sale_month - purchase_month` against the 12-
# month LTCG threshold.
CAPITAL_GAINS_YTD_SCHEMA = pl.Schema(
    {"rollout_index": pl.Int64(), "agent_id": pl.Utf8(), "classification": pl.Utf8(), "gain_usd": pl.Float64()}
)

# Per-rollout terminal status. "active" is the only running state;
# `"failed_insufficient_cash"` is set once and not cleared (L11.2:
# failed rollouts stay failed, and value-bearing state snapshots are
# zeroed after failure). One row per rollout regardless of how many
# agents are in the scenario — the failure is a property of the rollout,
# not of an agent.
ROLLOUT_STATUS_SCHEMA = pl.Schema({"rollout_index": pl.Int64(), "status": pl.Utf8(), "failed_month": pl.Int64()})

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
ASSET_LOT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "lot_id": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "account_id": pl.Utf8(),
        "asset_id": pl.Utf8(),
        "purchase_month_index": pl.Int64(),
        "cost_basis_per_unit_usd": pl.Float64(),
        "remaining_quantity": pl.Float64(),
    }
)

PROPERTY_STATE_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "property_id": pl.Utf8(),
        "location_id": pl.Utf8(),
        "purchase_month_index": pl.Int64(),
        "adjusted_basis_usd": pl.Float64(),
    }
)

PROPERTY_STAKE_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "property_id": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "contribution_used_usd": pl.Float64(),
        "equity_ledger_usd": pl.Float64(),
    }
)

LIABILITY_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "liability_id": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "payment_account_id": pl.Utf8(),
        "counterparty_agent_id": pl.Utf8(),
        "counterparty_account_id": pl.Utf8(),
        "property_id": pl.Utf8(),
        "principal_usd": pl.Float64(),
        "annual_interest_rate": pl.Float64(),
        "term_months": pl.Int64(),
        "origination_month_index": pl.Int64(),
        "monthly_payment_usd": pl.Float64(),
        "interest_paid_ytd_usd": pl.Float64(),
        "principal_paid_ytd_usd": pl.Float64(),
    }
)

CASH_BALANCES_FRAME = FrameSpec("cash_balances", CASH_BALANCES_SCHEMA)
ORDINARY_INCOME_YTD_FRAME = FrameSpec("ordinary_income_ytd", ORDINARY_INCOME_YTD_SCHEMA)
TAX_LIABILITIES_FRAME = FrameSpec("tax_liabilities", TAX_LIABILITIES_SCHEMA)
CAPITAL_GAINS_YTD_FRAME = FrameSpec("capital_gains_ytd", CAPITAL_GAINS_YTD_SCHEMA)
ROLLOUT_STATUS_FRAME = FrameSpec("rollout_status", ROLLOUT_STATUS_SCHEMA)
ASSET_LOT_FRAME = FrameSpec("asset_lots", ASSET_LOT_SCHEMA)
PROPERTY_STATE_FRAME = FrameSpec("property_state", PROPERTY_STATE_SCHEMA)
PROPERTY_STAKE_FRAME = FrameSpec("property_stakes", PROPERTY_STAKE_SCHEMA)
LIABILITY_FRAME = FrameSpec("liabilities", LIABILITY_SCHEMA)


@dataclass(frozen=True)
class StateCrossSection:
    """State at one month boundary, one polars frame per state kind."""

    cash_balances: pl.DataFrame
    asset_lots: pl.DataFrame
    ordinary_income_ytd: pl.DataFrame
    capital_gains_ytd: pl.DataFrame
    tax_liabilities: pl.DataFrame
    property_state: pl.DataFrame
    property_stakes: pl.DataFrame
    liabilities: pl.DataFrame
    rollout_status: pl.DataFrame
