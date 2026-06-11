"""Mechanical due-now settlement.

The settlement phase consumes hard demands plus whatever liquidity
sales policy emitted. It either pays every demand for an account in
full or pays none of them and emits failure rows.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from augur.sim.events import EVENT_FRAMES
from augur.sim.liquidity import LIQUIDITY_ATTEMPTS_BY_ACCOUNT
from augur.sim.state import StateCrossSection


@dataclass(frozen=True)
class DueNowSettlementEvents:
    obligation_accruals: pl.DataFrame
    obligation_settlements: pl.DataFrame
    transfers: pl.DataFrame
    lot_dispositions: pl.DataFrame
    mortgage_payments: pl.DataFrame
    tax_settlements: pl.DataFrame
    rollout_failures: pl.DataFrame


def settle_due_now_demands(
    *,
    state: StateCrossSection,
    obligations: pl.DataFrame,
    planned_dispositions: pl.DataFrame,
    attempted_sources_by_account: pl.DataFrame,
    mortgage_payments: pl.DataFrame,
    tax_settlement_candidates: pl.DataFrame,
    month: int,
) -> DueNowSettlementEvents:
    """Settle current hard demands from cash plus planned liquidity.

    Settlement has no asset-sale authority. If the policy did not
    emit enough sale proceeds, the whole account demand group fails:
    no payment transfers are emitted for that account's obligations.
    """

    active_rollouts = state.rollout_status.filter(pl.col("status") == "active").select("rollout_index")
    active_obligations = obligations.filter(pl.col("amount_due_usd") > 0).join(active_rollouts, on="rollout_index")
    if active_obligations.is_empty():
        return DueNowSettlementEvents(
            obligation_accruals=active_obligations,
            obligation_settlements=EVENT_FRAMES.obligation_settlements.empty(),
            transfers=EVENT_FRAMES.transfers.empty(),
            lot_dispositions=planned_dispositions,
            mortgage_payments=EVENT_FRAMES.mortgage_payments.empty(),
            tax_settlements=EVENT_FRAMES.tax_settlements.empty(),
            rollout_failures=EVENT_FRAMES.rollout_failures.empty(),
        )

    due_by_account = _obligation_due_by_account(state, active_obligations, planned_dispositions)
    funded = _with_funding_status(due_by_account, attempted_sources_by_account)
    joined = active_obligations.join(funded, on=["rollout_index", "agent_id", "from_account_id"], how="left")
    settled = joined.with_columns(
        amount_paid_usd=pl.when(pl.col("_fully_paid")).then(pl.col("amount_due_usd")).otherwise(0.0),
        shortfall_usd=pl.when(pl.col("_fully_paid")).then(0.0).otherwise(pl.col("amount_due_usd")),
        attempted_funding_sources=pl.col("_attempted_funding_sources").fill_null(""),
    )
    obligation_settlements = settled.pipe(EVENT_FRAMES.obligation_settlements.normalize)
    return DueNowSettlementEvents(
        obligation_accruals=active_obligations,
        obligation_settlements=obligation_settlements,
        transfers=_obligation_payment_transfers(settled),
        lot_dispositions=planned_dispositions,
        mortgage_payments=_paid_mortgage_payment_events(mortgage_payments, obligation_settlements),
        tax_settlements=_paid_tax_settlement_events(tax_settlement_candidates, obligation_settlements),
        rollout_failures=_obligation_failure_events(settled, month),
    )


def _obligation_due_by_account(
    state: StateCrossSection, obligations: pl.DataFrame, planned_dispositions: pl.DataFrame
) -> pl.DataFrame:
    due = obligations.group_by(["rollout_index", "agent_id", "from_account_id"]).agg(
        pl.col("amount_due_usd").sum().alias("_total_due_usd")
    )
    cash = state.cash_balances.rename({"account_id": "from_account_id", "balance_usd": "_cash_balance_usd"}).select(
        "rollout_index", "agent_id", "from_account_id", "_cash_balance_usd"
    )
    planned_proceeds = _disposition_proceeds_by_account(planned_dispositions)
    return (
        due.join(cash, on=["rollout_index", "agent_id", "from_account_id"], how="left")
        .join(planned_proceeds, on=["rollout_index", "agent_id", "from_account_id"], how="left")
        .with_columns(
            _available_usd=pl.col("_cash_balance_usd").fill_null(0.0) + pl.col("_proceeds_usd").fill_null(0.0)
        )
        .with_columns(
            _account_shortfall_usd=pl.max_horizontal(0.0, pl.col("_total_due_usd") - pl.col("_available_usd"))
        )
        .select(
            "rollout_index", "agent_id", "from_account_id", "_total_due_usd", "_available_usd", "_account_shortfall_usd"
        )
    )


def _disposition_proceeds_by_account(dispositions: pl.DataFrame) -> pl.DataFrame:
    return (
        dispositions.group_by(["rollout_index", "agent_id", "proceeds_account_id"])
        .agg(pl.col("proceeds_usd").sum().alias("_proceeds_usd"))
        .rename({"proceeds_account_id": "from_account_id"})
    )


def _with_funding_status(due_by_account: pl.DataFrame, attempted_sources_by_account: pl.DataFrame) -> pl.DataFrame:
    attempts = (
        LIQUIDITY_ATTEMPTS_BY_ACCOUNT.normalize(attempted_sources_by_account)
        if not attempted_sources_by_account.is_empty()
        else LIQUIDITY_ATTEMPTS_BY_ACCOUNT.empty()
    )
    return (
        due_by_account.join(attempts, on=["rollout_index", "agent_id", "from_account_id"], how="left")
        .with_columns(
            _fully_paid=pl.col("_available_usd") >= pl.col("_total_due_usd") - 1e-9,
            _attempted_funding_sources=pl.col("_attempted_funding_sources").fill_null(""),
        )
        .select(
            "rollout_index",
            "agent_id",
            "from_account_id",
            "_total_due_usd",
            "_account_shortfall_usd",
            "_fully_paid",
            "_attempted_funding_sources",
        )
    )


def _obligation_payment_transfers(settled: pl.DataFrame) -> pl.DataFrame:
    return (
        settled.filter(pl.col("amount_paid_usd") > 0)
        .with_columns(
            pl.col("agent_id").alias("from_agent_id"),
            pl.col("amount_paid_usd").alias("amount_usd"),
            pl.lit(None, dtype=pl.Utf8()).alias("income_category"),
        )
        .pipe(EVENT_FRAMES.transfers.normalize)
    )


def _obligation_failure_events(settled: pl.DataFrame, month: int) -> pl.DataFrame:
    return (
        settled.filter(pl.col("shortfall_usd") > 0)
        .with_columns(
            pl.lit(month, dtype=pl.Int64()).alias("month_index"),
            pl.concat_str([pl.col("obligation_id"), pl.lit("_failure")]).alias("cause_id"),
            pl.col("shortfall_usd").alias("deficit_usd"),
        )
        .pipe(EVENT_FRAMES.rollout_failures.normalize)
    )


def _paid_mortgage_payment_events(
    mortgage_payments: pl.DataFrame, obligation_settlements: pl.DataFrame
) -> pl.DataFrame:
    if mortgage_payments.is_empty() or obligation_settlements.is_empty():
        return EVENT_FRAMES.mortgage_payments.empty()
    paid = obligation_settlements.filter(
        (pl.col("obligation_type") == "mortgage_payment") & (pl.col("shortfall_usd") == 0)
    ).select("rollout_index", pl.col("obligation_id").alias("cause_id"))
    return EVENT_FRAMES.mortgage_payments.normalize(
        mortgage_payments.join(paid, on=["rollout_index", "cause_id"], how="inner")
    )


def _paid_tax_settlement_events(settlements: pl.DataFrame, obligation_settlements: pl.DataFrame) -> pl.DataFrame:
    if settlements.is_empty():
        return EVENT_FRAMES.tax_settlements.empty()
    failed_tax = (
        obligation_settlements.filter(
            pl.col("obligation_type").is_in(["estimated_tax", "tax_true_up"]) & (pl.col("shortfall_usd") > 0)
        )
        .select("rollout_index", "agent_id")
        .unique()
        .with_columns(pl.lit(True).alias("_failed_tax_payment"))
    )
    return (
        settlements.join(failed_tax, on=["rollout_index", "agent_id"], how="left")
        .filter(~pl.col("_failed_tax_payment").fill_null(False))
        .drop("_failed_tax_payment")
        .pipe(EVENT_FRAMES.tax_settlements.normalize)
    )
