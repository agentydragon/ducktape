"""apply_events — the single state-mutation primitive.

`apply_events(state, events) → state'` is the only function in the
simulator that writes the event-sourced columns of state. The
forward loop calls it once per iteration; tests + an opt-in
`--check-replay` flag use it to validate that incrementally-
maintained state agrees with re-derivation from the cumulative
event log:

    state_at(M).event_sourced ==
        apply_events(initial_state, events_log.filter(month <= M))

for every M. If the invariant ever fails, the bug is here and the
fix is in one place.

Dispatch is by event kind: each kind's frame is consumed by a
kind-specific apply function. `apply_events` composes them in a
fixed order so that downstream kinds (tax accruals) see the
already-updated state from upstream kinds (income transfers).
"""

from __future__ import annotations

import polars as pl

from augur.sim.events import EventLog
from augur.sim.runtime import capital_gain_classification_expr
from augur.sim.state import (
    ASSET_LOT_FRAME,
    LIABILITY_FRAME,
    PROPERTY_STAKE_FRAME,
    PROPERTY_STATE_FRAME,
    TAX_LIABILITIES_FRAME,
    StateCrossSection,
)


def apply_events(state: StateCrossSection, events: EventLog) -> StateCrossSection:
    """Apply all events in `events` to `state`. Returns the new
    cross-section. Pure: does not mutate inputs.

    Order matters: income-carrying transfers increment
    `ordinary_income_ytd` first, asset sales both credit cash and
    bucket capital gains into ltcg/stcg, then year-end tax accruals
    book a liability and zero out the year-to-date totals. Each
    `_apply_*` helper is a polars no-op on an empty event frame, so
    no per-kind guards are needed at this layer."""
    asset_lots = _apply_asset_purchases(state.asset_lots, events.asset_purchases)
    asset_lots = _apply_lot_dispositions_to_lots(asset_lots, events.lot_dispositions)
    property_state = _apply_property_purchases_to_state(state.property_state, events.property_purchases)
    property_stakes = _apply_property_purchases_to_stakes(state.property_stakes, events.property_purchases)
    liabilities = _apply_mortgage_originations(state.liabilities, events.mortgage_originations)
    liabilities = _apply_mortgage_payments(liabilities, events.mortgage_payments)
    cash_balances = _apply_lot_dispositions_to_cash(state.cash_balances, events.lot_dispositions)
    capital_gains_ytd = _apply_dispositions_to_capital_gains_ytd(state.capital_gains_ytd, events.lot_dispositions)
    cash_balances = _apply_transfers(cash_balances, events.transfers)
    ordinary_income_ytd = _apply_income_to_ytd(state.ordinary_income_ytd, events.transfers)
    tax_liabilities = _apply_tax_accruals_to_liabilities(state.tax_liabilities, events.tax_accruals)
    tax_liabilities = _apply_tax_settlements_to_liabilities(tax_liabilities, events.tax_settlements)
    ordinary_income_ytd = _reset_ytd_for_taxed_agents(ordinary_income_ytd, events.tax_accruals)
    capital_gains_ytd = _reset_capital_gains_for_taxed_agents(capital_gains_ytd, events.tax_accruals)
    rollout_status = _apply_rollout_failures(state.rollout_status, events.rollout_failures)

    return _zero_failed_rollout_state(
        StateCrossSection(
            cash_balances=cash_balances,
            asset_lots=asset_lots,
            ordinary_income_ytd=ordinary_income_ytd,
            capital_gains_ytd=capital_gains_ytd,
            tax_liabilities=tax_liabilities,
            property_state=property_state,
            property_stakes=property_stakes,
            liabilities=liabilities,
            rollout_status=rollout_status,
        )
    )


def _zero_failed_rollout_state(state: StateCrossSection) -> StateCrossSection:
    failed_rollouts = state.rollout_status.filter(pl.col("status") != "active").select("rollout_index").unique()
    if failed_rollouts.is_empty():
        return state
    return StateCrossSection(
        cash_balances=_zero_failed_columns(state.cash_balances, failed_rollouts, ("balance_usd",)),
        asset_lots=_zero_failed_columns(state.asset_lots, failed_rollouts, ("remaining_quantity",)),
        ordinary_income_ytd=_zero_failed_columns(state.ordinary_income_ytd, failed_rollouts, ("ordinary_income_usd",)),
        capital_gains_ytd=_zero_failed_columns(state.capital_gains_ytd, failed_rollouts, ("gain_usd",)),
        tax_liabilities=_zero_failed_columns(state.tax_liabilities, failed_rollouts, ("amount_owed_usd",)),
        property_state=_zero_failed_columns(state.property_state, failed_rollouts, ("adjusted_basis_usd",)),
        property_stakes=_zero_failed_columns(
            state.property_stakes, failed_rollouts, ("ownership_pct", "contribution_used_usd", "equity_ledger_usd")
        ),
        liabilities=_zero_failed_columns(
            state.liabilities,
            failed_rollouts,
            ("principal_usd", "monthly_payment_usd", "interest_paid_ytd_usd", "principal_paid_ytd_usd"),
        ),
        rollout_status=state.rollout_status,
    )


def _zero_failed_columns(frame: pl.DataFrame, failed_rollouts: pl.DataFrame, columns: tuple[str, ...]) -> pl.DataFrame:
    if frame.is_empty():
        return frame
    return (
        frame.join(failed_rollouts.with_columns(pl.lit(True).alias("_failed_rollout")), on="rollout_index", how="left")
        .with_columns(
            [
                pl.when(pl.col("_failed_rollout").fill_null(False)).then(0.0).otherwise(pl.col(column)).alias(column)
                for column in columns
            ]
        )
        .drop("_failed_rollout")
    )


def _apply_transfers(cash_balances: pl.DataFrame, transfers: pl.DataFrame) -> pl.DataFrame:
    """Apply transfer events to cash_balances. Each transfer debits
    `from_agent`'s `from_account` and credits `to_agent`'s `to_account`.
    Vectorized: aggregates per-(rollout, agent, account) deltas and
    joins them into the cash_balances frame in one expression."""
    outgoing = (
        transfers.group_by(["rollout_index", "from_agent_id", "from_account_id"])
        .agg(pl.col("amount_usd").sum())
        .rename({"from_agent_id": "agent_id", "from_account_id": "account_id", "amount_usd": "_delta_out"})
    )
    incoming = (
        transfers.group_by(["rollout_index", "to_agent_id", "to_account_id"])
        .agg(pl.col("amount_usd").sum())
        .rename({"to_agent_id": "agent_id", "to_account_id": "account_id", "amount_usd": "_delta_in"})
    )
    return (
        cash_balances.join(outgoing, on=["rollout_index", "agent_id", "account_id"], how="left")
        .join(incoming, on=["rollout_index", "agent_id", "account_id"], how="left")
        .with_columns(
            balance_usd=pl.col("balance_usd") - pl.col("_delta_out").fill_null(0.0) + pl.col("_delta_in").fill_null(0.0)
        )
        .drop(["_delta_out", "_delta_in"])
    )


def _apply_income_to_ytd(ordinary_income_ytd: pl.DataFrame, transfers: pl.DataFrame) -> pl.DataFrame:
    """Increment per-(rollout, recipient) ordinary_income_ytd by
    the sum of transfer amounts whose `income_category == "ordinary"`.
    Transfers without that tag don't touch YTD."""
    deltas = (
        transfers.filter(pl.col("income_category") == "ordinary")
        .group_by(["rollout_index", "to_agent_id"])
        .agg(pl.col("amount_usd").sum().alias("_delta"))
        .rename({"to_agent_id": "agent_id"})
    )
    return (
        ordinary_income_ytd.join(deltas, on=["rollout_index", "agent_id"], how="left")
        .with_columns(ordinary_income_usd=pl.col("ordinary_income_usd") + pl.col("_delta").fill_null(0.0))
        .drop("_delta")
    )


def _apply_asset_purchases(asset_lots: pl.DataFrame, purchases: pl.DataFrame) -> pl.DataFrame:
    """Append purchase events as new lot rows. Each purchase row
    becomes one lot with `remaining_quantity = quantity`. Lots are
    keyed by `(rollout_index, lot_id)`; the scenario is responsible
    for assigning unique `lot_id` strings. Both inputs share the
    ASSET_LOT-shaped schema after projection, so concat-on-empty
    cases naturally no-op."""
    new_lots = purchases.select(
        pl.col("rollout_index"),
        pl.col("lot_id"),
        pl.col("agent_id"),
        pl.col("asset_id"),
        pl.col("month_index").alias("purchase_month_index"),
        pl.col("cost_basis_per_unit_usd"),
        pl.col("quantity").alias("remaining_quantity"),
    ).pipe(ASSET_LOT_FRAME.normalize)
    return pl.concat([asset_lots, new_lots])


def _apply_lot_dispositions_to_lots(asset_lots: pl.DataFrame, dispositions: pl.DataFrame) -> pl.DataFrame:
    """Reduce `remaining_quantity` on the lots consumed by each
    disposition. Aggregates `units_sold` per `(rollout_index,
    lot_id)` so multiple dispositions of the same lot (e.g. two
    sales of the same asset in the same month) compose. Vectorized:
    one left-join + one with_columns."""
    deltas = dispositions.group_by(["rollout_index", "lot_id"]).agg(pl.col("units_sold").sum().alias("_units_consumed"))
    return (
        asset_lots.join(deltas, on=["rollout_index", "lot_id"], how="left")
        .with_columns(remaining_quantity=pl.col("remaining_quantity") - pl.col("_units_consumed").fill_null(0.0))
        .drop("_units_consumed")
    )


def _apply_lot_dispositions_to_cash(cash_balances: pl.DataFrame, dispositions: pl.DataFrame) -> pl.DataFrame:
    """Credit sale proceeds to the configured `proceeds_account_id`
    on each disposition. Aggregates per (rollout, agent, account)
    before joining so multi-lot sales fold into a single delta."""
    credits = (
        dispositions.group_by(["rollout_index", "agent_id", "proceeds_account_id"])
        .agg(pl.col("proceeds_usd").sum().alias("_delta_in"))
        .rename({"proceeds_account_id": "account_id"})
    )
    return (
        cash_balances.join(credits, on=["rollout_index", "agent_id", "account_id"], how="left")
        .with_columns(balance_usd=pl.col("balance_usd") + pl.col("_delta_in").fill_null(0.0))
        .drop("_delta_in")
    )


def _apply_tax_accruals_to_liabilities(tax_liabilities: pl.DataFrame, tax_accruals: pl.DataFrame) -> pl.DataFrame:
    """Append each accrual as a new liability row. Liabilities are
    additive until a later tax-settlement event reduces
    `amount_owed_usd`."""
    new_rows = tax_accruals.select(
        pl.col("rollout_index"),
        pl.col("agent_id"),
        pl.col("jurisdiction_id"),
        pl.col("tax_year_end_month"),
        pl.col("amount_usd").alias("amount_owed_usd"),
    ).pipe(TAX_LIABILITIES_FRAME.normalize)
    return pl.concat([tax_liabilities, new_rows])


def _apply_property_purchases_to_state(property_state: pl.DataFrame, purchases: pl.DataFrame) -> pl.DataFrame:
    """Append acquired properties to the property-state frame."""
    new_rows = purchases.select(
        pl.col("rollout_index"),
        pl.col("property_id"),
        pl.col("location_id"),
        pl.col("month_index").alias("purchase_month_index"),
        pl.col("adjusted_basis_usd"),
    ).pipe(PROPERTY_STATE_FRAME.normalize)
    return pl.concat([property_state, new_rows])


def _apply_property_purchases_to_stakes(property_stakes: pl.DataFrame, purchases: pl.DataFrame) -> pl.DataFrame:
    """Create the buyer's property-stake row for each purchase."""
    new_rows = purchases.select(
        pl.col("rollout_index"),
        pl.col("property_id"),
        pl.col("buyer_agent_id").alias("agent_id"),
        pl.col("ownership_pct"),
        pl.col("stake_contribution_usd").alias("contribution_used_usd"),
        pl.col("equity_ledger_usd"),
    ).pipe(PROPERTY_STAKE_FRAME.normalize)
    return pl.concat([property_stakes, new_rows])


def _apply_mortgage_originations(liabilities: pl.DataFrame, originations: pl.DataFrame) -> pl.DataFrame:
    """Append mortgage-originated liabilities."""
    new_rows = originations.select(
        pl.col("rollout_index"),
        pl.col("liability_id"),
        pl.col("agent_id"),
        pl.col("payment_account_id"),
        pl.col("counterparty_agent_id"),
        pl.col("counterparty_account_id"),
        pl.col("property_id"),
        pl.col("principal_usd"),
        pl.col("annual_interest_rate"),
        pl.col("term_months"),
        pl.col("month_index").alias("origination_month_index"),
        pl.col("monthly_payment_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("interest_paid_ytd_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("principal_paid_ytd_usd"),
    ).pipe(LIABILITY_FRAME.normalize)
    return pl.concat([liabilities, new_rows])


def _apply_mortgage_payments(liabilities: pl.DataFrame, payments: pl.DataFrame) -> pl.DataFrame:
    """Reduce mortgage principal and accumulate YTD payment splits."""
    deltas = payments.group_by(["rollout_index", "liability_id"]).agg(
        pl.col("interest_usd").sum().alias("_interest_paid"), pl.col("principal_usd").sum().alias("_principal_paid")
    )
    return (
        liabilities.join(deltas, on=["rollout_index", "liability_id"], how="left")
        .with_columns(
            principal_usd=pl.max_horizontal(0.0, pl.col("principal_usd") - pl.col("_principal_paid").fill_null(0.0)),
            interest_paid_ytd_usd=pl.col("interest_paid_ytd_usd") + pl.col("_interest_paid").fill_null(0.0),
            principal_paid_ytd_usd=pl.col("principal_paid_ytd_usd") + pl.col("_principal_paid").fill_null(0.0),
        )
        .drop(["_interest_paid", "_principal_paid"])
    )


def _apply_tax_settlements_to_liabilities(tax_liabilities: pl.DataFrame, tax_settlements: pl.DataFrame) -> pl.DataFrame:
    """Reduce outstanding tax liabilities by settlement events.

    Settlements are keyed by (rollout, agent, tax year). Current
    estimated-tax payments are aggregate across jurisdictions, so if
    a partial settlement is ever applied to a multi-jurisdiction
    liability, it is allocated proportionally by each jurisdiction's
    outstanding amount. Full settlements zero every jurisdiction row.
    """
    if tax_liabilities.is_empty() or tax_settlements.is_empty():
        return tax_liabilities
    keys = ["rollout_index", "agent_id", "tax_year_end_month"]
    outstanding = tax_liabilities.group_by(keys).agg(pl.col("amount_owed_usd").sum().alias("_outstanding_usd"))
    settlements = tax_settlements.group_by(keys).agg(pl.col("amount_usd").sum().alias("_settlement_usd"))
    return (
        tax_liabilities.join(outstanding, on=keys, how="left")
        .join(settlements, on=keys, how="left")
        .with_columns(
            _settlement_usd=pl.col("_settlement_usd").fill_null(0.0),
            _settled_usd=pl.when((pl.col("_outstanding_usd") > 0) & (pl.col("_settlement_usd") > 0))
            .then(
                pl.min_horizontal(
                    pl.col("amount_owed_usd"),
                    pl.col("amount_owed_usd") / pl.col("_outstanding_usd") * pl.col("_settlement_usd"),
                )
            )
            .otherwise(0.0),
        )
        .with_columns(
            amount_owed_usd=pl.max_horizontal(pl.lit(0.0), pl.col("amount_owed_usd") - pl.col("_settled_usd"))
        )
        .drop(["_outstanding_usd", "_settlement_usd", "_settled_usd"])
    )


def _apply_dispositions_to_capital_gains_ytd(
    capital_gains_ytd: pl.DataFrame, dispositions: pl.DataFrame
) -> pl.DataFrame:
    """Bucket each lot disposition's `gain_usd = proceeds_usd -
    cost_basis_consumed_usd` into LTCG (holding period ≥ 12 months)
    or STCG, aggregate per (rollout, agent, classification), and
    add to the running YTD. Rollouts × agents that have no
    pre-existing row are appended; existing rows accumulate."""
    classified = dispositions.with_columns(
        gain_usd=pl.col("proceeds_usd") - pl.col("cost_basis_consumed_usd"),
        classification=capital_gain_classification_expr(),
    )
    deltas = classified.group_by(["rollout_index", "agent_id", "classification"]).agg(
        pl.col("gain_usd").sum().alias("_delta")
    )
    merged = capital_gains_ytd.join(
        deltas, on=["rollout_index", "agent_id", "classification"], how="full", coalesce=True
    )
    return merged.with_columns(gain_usd=pl.col("gain_usd").fill_null(0.0) + pl.col("_delta").fill_null(0.0)).drop(
        "_delta"
    )


def _reset_ytd_for_taxed_agents(ordinary_income_ytd: pl.DataFrame, tax_accruals: pl.DataFrame) -> pl.DataFrame:
    """Zero `ordinary_income_usd` for every (rollout, agent) that
    has a tax accrual fired this month — that's "year closed, start
    counting again from zero". Multiple jurisdictions accruing for
    the same agent collapse to one reset via the `_reset` flag from
    the left join."""
    affected = tax_accruals.select("rollout_index", "agent_id").unique().with_columns(pl.lit(True).alias("_reset"))
    return (
        ordinary_income_ytd.join(affected, on=["rollout_index", "agent_id"], how="left")
        .with_columns(
            ordinary_income_usd=pl.when(pl.col("_reset").fill_null(False))
            .then(0.0)
            .otherwise(pl.col("ordinary_income_usd"))
        )
        .drop("_reset")
    )


def _apply_rollout_failures(rollout_status: pl.DataFrame, failures: pl.DataFrame) -> pl.DataFrame:
    """Mark the listed rollouts failed. Once flagged, the status is
    sticky: a second failure event on an already-failed rollout
    leaves the original `failed_month` in place. The `status` field
    transitions monotonically from "active" to a failure state."""
    first_failure = failures.group_by("rollout_index").agg(pl.col("month_index").min().alias("_failed_month"))
    return (
        rollout_status.join(first_failure, on="rollout_index", how="left")
        .with_columns(
            status=pl.when(pl.col("status") != "active")
            .then(pl.col("status"))
            .when(pl.col("_failed_month").is_not_null())
            .then(pl.lit("failed_insufficient_cash"))
            .otherwise(pl.col("status")),
            failed_month=pl.when(pl.col("failed_month").is_not_null())
            .then(pl.col("failed_month"))
            .otherwise(pl.col("_failed_month")),
        )
        .drop("_failed_month")
    )


def _reset_capital_gains_for_taxed_agents(capital_gains_ytd: pl.DataFrame, tax_accruals: pl.DataFrame) -> pl.DataFrame:
    """Year-end reset of LTCG / STCG running totals — same pattern
    as the ordinary-income reset, keyed by (rollout, agent)."""
    affected = tax_accruals.select("rollout_index", "agent_id").unique().with_columns(pl.lit(True).alias("_reset"))
    return (
        capital_gains_ytd.join(affected, on=["rollout_index", "agent_id"], how="left")
        .with_columns(gain_usd=pl.when(pl.col("_reset").fill_null(False)).then(0.0).otherwise(pl.col("gain_usd")))
        .drop("_reset")
    )
