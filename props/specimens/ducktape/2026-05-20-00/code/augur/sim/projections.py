"""Read-model projections over `SimulationRun`.

The simulator's raw state and event frames remain the source of truth.
This module builds frontend/API-shaped projection frames from that truth
without adding new mutation paths.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from augur.frames import FrameSpec
from augur.sim.run import SimulationRun

NET_WORTH_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "agent_id": pl.Utf8(),
        "cash_usd": pl.Float64(),
        "liquid_asset_value_usd": pl.Float64(),
        "asset_book_value_usd": pl.Float64(),
        "property_book_value_usd": pl.Float64(),
        "liability_principal_usd": pl.Float64(),
        "liquid_net_worth_usd": pl.Float64(),
        "book_net_worth_usd": pl.Float64(),
    }
)

ACCOUNT_BALANCE_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "agent_id": pl.Utf8(),
        "account_id": pl.Utf8(),
        "account_type": pl.Utf8(),
        "balance_usd": pl.Float64(),
    }
)

TRANSACTION_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "transaction_id": pl.Utf8(),
        "transaction_type": pl.Utf8(),
        "cause_id": pl.Utf8(),
        "from_agent_id": pl.Utf8(),
        "from_account_id": pl.Utf8(),
        "to_agent_id": pl.Utf8(),
        "to_account_id": pl.Utf8(),
        "asset_id": pl.Utf8(),
        "lot_id": pl.Utf8(),
        "amount_usd": pl.Float64(),
        "quantity": pl.Float64(),
    }
)

TAX_BREAKDOWN_PROJECTION_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "jurisdiction_id": pl.Utf8(),
        "tax_year": pl.Int64(),
        "tax_year_end_month": pl.Int64(),
        "ordinary_income_usd": pl.Float64(),
        "ltcg_usd": pl.Float64(),
        "stcg_usd": pl.Float64(),
        "standard_deduction_usd": pl.Float64(),
        "ordinary_taxable_usd": pl.Float64(),
        "capital_gain_taxable_usd": pl.Float64(),
        "ordinary_tax_usd": pl.Float64(),
        "capital_gain_tax_usd": pl.Float64(),
        "total_tax_usd": pl.Float64(),
    }
)

OBLIGATION_LIFECYCLE_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "obligation_id": pl.Utf8(),
        "obligation_type": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "from_account_id": pl.Utf8(),
        "to_agent_id": pl.Utf8(),
        "to_account_id": pl.Utf8(),
        "amount_due_usd": pl.Float64(),
        "amount_paid_usd": pl.Float64(),
        "shortfall_usd": pl.Float64(),
        "attempted_funding_sources": pl.Utf8(),
        "status": pl.Utf8(),
    }
)

FAILURE_PROJECTION_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "failure_id": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "deficit_usd": pl.Float64(),
        "obligation_id": pl.Utf8(),
        "obligation_type": pl.Utf8(),
        "shortfall_usd": pl.Float64(),
        "attempted_funding_sources": pl.Utf8(),
    }
)

ROLLOUT_SUMMARY_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "status": pl.Utf8(),
        "failed_month": pl.Int64(),
        "failure_count": pl.Int64(),
        "first_failure_month": pl.Int64(),
        "final_month_index": pl.Int64(),
        "final_liquid_net_worth_usd": pl.Float64(),
        "final_book_net_worth_usd": pl.Float64(),
    }
)

_NET_WORTH_COMPONENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "agent_id": pl.Utf8(),
        "cash_usd": pl.Float64(),
        "liquid_asset_value_usd": pl.Float64(),
        "asset_book_value_usd": pl.Float64(),
        "property_book_value_usd": pl.Float64(),
        "liability_principal_usd": pl.Float64(),
    }
)

NET_WORTH_FRAME = FrameSpec("net_worth", NET_WORTH_SCHEMA)
ACCOUNT_BALANCE_FRAME = FrameSpec("account_balances", ACCOUNT_BALANCE_SCHEMA)
TRANSACTION_FRAME = FrameSpec("transactions", TRANSACTION_SCHEMA)
TAX_BREAKDOWN_PROJECTION_FRAME = FrameSpec("tax_breakdowns", TAX_BREAKDOWN_PROJECTION_SCHEMA)
OBLIGATION_LIFECYCLE_FRAME = FrameSpec("obligation_lifecycle", OBLIGATION_LIFECYCLE_SCHEMA)
FAILURE_PROJECTION_FRAME = FrameSpec("failures", FAILURE_PROJECTION_SCHEMA)
ROLLOUT_SUMMARY_FRAME = FrameSpec("rollout_summary", ROLLOUT_SUMMARY_SCHEMA)
_NET_WORTH_COMPONENT_FRAME = FrameSpec("_net_worth_components", _NET_WORTH_COMPONENT_SCHEMA)
_FINAL_NET_WORTH_FRAME = FrameSpec(
    "_final_net_worth",
    pl.Schema(
        {
            "rollout_index": pl.Int64(),
            "final_month_index": pl.Int64(),
            "final_liquid_net_worth_usd": pl.Float64(),
            "final_book_net_worth_usd": pl.Float64(),
        }
    ),
)
_FAILURE_COUNTS_FRAME = FrameSpec(
    "_failure_counts",
    pl.Schema({"rollout_index": pl.Int64(), "failure_count": pl.Int64(), "first_failure_month": pl.Int64()}),
)


@dataclass(frozen=True)
class ProjectionRun:
    """Frontend/API read models for a simulated scenario."""

    net_worth: pl.DataFrame
    account_balances: pl.DataFrame
    transactions: pl.DataFrame
    tax_breakdowns: pl.DataFrame
    obligation_lifecycle: pl.DataFrame
    failures: pl.DataFrame
    rollout_summary: pl.DataFrame

    def trajectory(self, rollout_index: int) -> ProjectionTrajectory:
        """Return every projection frame filtered to one rollout."""

        return ProjectionTrajectory(
            rollout_index=rollout_index,
            net_worth=_filter_rollout(self.net_worth, rollout_index),
            account_balances=_filter_rollout(self.account_balances, rollout_index),
            transactions=_filter_rollout(self.transactions, rollout_index),
            tax_breakdowns=_filter_rollout(self.tax_breakdowns, rollout_index),
            obligation_lifecycle=_filter_rollout(self.obligation_lifecycle, rollout_index),
            failures=_filter_rollout(self.failures, rollout_index),
            rollout_summary=_filter_rollout(self.rollout_summary, rollout_index),
        )


@dataclass(frozen=True)
class ProjectionTrajectory:
    """One-rollout inspection view over `ProjectionRun`."""

    rollout_index: int
    net_worth: pl.DataFrame
    account_balances: pl.DataFrame
    transactions: pl.DataFrame
    tax_breakdowns: pl.DataFrame
    obligation_lifecycle: pl.DataFrame
    failures: pl.DataFrame
    rollout_summary: pl.DataFrame


def project_simulation_run(run: SimulationRun) -> ProjectionRun:
    """Build every stable projection frame for a simulation run."""

    net_worth = project_net_worth(run)
    failures = project_failures(run)
    return ProjectionRun(
        net_worth=net_worth,
        account_balances=project_account_balances(run),
        transactions=project_transactions(run),
        tax_breakdowns=project_tax_breakdowns(run),
        obligation_lifecycle=project_obligation_lifecycle(run),
        failures=failures,
        rollout_summary=project_rollout_summary(run, net_worth=net_worth, failures=failures),
    )


def project_net_worth(run: SimulationRun) -> pl.DataFrame:
    """Per-rollout/per-month/per-agent balance and net-worth metrics.

    `property_book_value_usd` is adjusted basis, not market value. Real
    property market valuation belongs in exogenous paths before this
    projection can expose market-value real-estate net worth.
    """

    components = _NET_WORTH_COMPONENT_FRAME.concat(
        [
            _cash_net_worth_components(run),
            _asset_net_worth_components(run),
            _property_net_worth_components(run),
            _liability_net_worth_components(run),
        ]
    )
    if components.is_empty():
        return NET_WORTH_FRAME.empty()
    metric_names = [
        "cash_usd",
        "liquid_asset_value_usd",
        "asset_book_value_usd",
        "property_book_value_usd",
        "liability_principal_usd",
    ]
    return (
        components.group_by(["rollout_index", "month_index", "agent_id"])
        .agg(pl.col(name).sum().alias(name) for name in metric_names)
        .with_columns(
            liquid_net_worth_usd=pl.col("cash_usd") + pl.col("liquid_asset_value_usd"),
            book_net_worth_usd=pl.col("cash_usd")
            + pl.col("asset_book_value_usd")
            + pl.col("property_book_value_usd")
            - pl.col("liability_principal_usd"),
        )
        .sort(["rollout_index", "month_index", "agent_id"])
        .pipe(NET_WORTH_FRAME.normalize)
    )


def project_account_balances(run: SimulationRun) -> pl.DataFrame:
    """Cash and liability balances in one account-like read model."""

    cash = run.cash_balances.select(
        "rollout_index",
        "month_index",
        "agent_id",
        "account_id",
        pl.lit("cash", dtype=pl.Utf8()).alias("account_type"),
        "balance_usd",
    )
    liabilities = run.liabilities.select(
        "rollout_index",
        "month_index",
        "agent_id",
        pl.col("liability_id").alias("account_id"),
        pl.lit("liability", dtype=pl.Utf8()).alias("account_type"),
        (-pl.col("principal_usd")).alias("balance_usd"),
    )
    return ACCOUNT_BALANCE_FRAME.concat([cash, liabilities]).sort(
        ["rollout_index", "month_index", "agent_id", "account_type", "account_id"]
    )


def project_transactions(run: SimulationRun) -> pl.DataFrame:
    """Transaction/audit rows projected from event frames."""

    return TRANSACTION_FRAME.concat(
        [
            _transfer_transactions(run),
            _lot_disposition_transactions(run),
            _obligation_settlement_transactions(run),
            _tax_settlement_transactions(run),
        ]
    ).sort(["rollout_index", "month_index", "transaction_type", "transaction_id"])


def project_tax_breakdowns(run: SimulationRun) -> pl.DataFrame:
    if run.events_log.tax_breakdowns.is_empty():
        return TAX_BREAKDOWN_PROJECTION_FRAME.empty()
    return (
        run.events_log.tax_breakdowns.with_columns(tax_year=pl.col("tax_year_end_month") // 12)
        .pipe(TAX_BREAKDOWN_PROJECTION_FRAME.normalize)
        .sort(["rollout_index", "tax_year", "agent_id", "jurisdiction_id"])
    )


def project_obligation_lifecycle(run: SimulationRun) -> pl.DataFrame:
    accruals = run.events_log.obligation_accruals
    if accruals.is_empty():
        return OBLIGATION_LIFECYCLE_FRAME.empty()
    settlements = run.events_log.obligation_settlements.select(
        "rollout_index", "obligation_id", "amount_paid_usd", "shortfall_usd", "attempted_funding_sources"
    )
    return (
        accruals.join(settlements, on=["rollout_index", "obligation_id"], how="left")
        .with_columns(
            _has_settlement=pl.col("amount_paid_usd").is_not_null() | pl.col("shortfall_usd").is_not_null(),
            amount_paid_usd=pl.col("amount_paid_usd").fill_null(0.0),
            shortfall_usd=pl.col("shortfall_usd").fill_null(pl.col("amount_due_usd")),
            attempted_funding_sources=pl.col("attempted_funding_sources").fill_null(""),
        )
        .with_columns(
            status=pl.when(~pl.col("_has_settlement"))
            .then(pl.lit("due"))
            .when(pl.col("shortfall_usd") <= 1e-9)
            .then(pl.lit("paid"))
            .when(pl.col("amount_paid_usd") > 0)
            .then(pl.lit("partial"))
            .otherwise(pl.lit("failed"))
        )
        .pipe(OBLIGATION_LIFECYCLE_FRAME.normalize)
        .sort(["rollout_index", "month_index", "obligation_id"])
    )


def project_failures(run: SimulationRun) -> pl.DataFrame:
    failures = run.events_log.rollout_failures
    if failures.is_empty():
        return FAILURE_PROJECTION_FRAME.empty()
    return (
        failures.with_columns(pl.col("cause_id").alias("failure_id"))
        .pipe(FAILURE_PROJECTION_FRAME.normalize)
        .sort(["rollout_index", "month_index", "failure_id"])
    )


def project_rollout_summary(run: SimulationRun, *, net_worth: pl.DataFrame, failures: pl.DataFrame) -> pl.DataFrame:
    status = run.rollout_status.select("rollout_index", "status", "failed_month")
    final_net_worth = _final_net_worth_by_rollout(net_worth)
    failure_counts = _failure_counts_by_rollout(failures)
    return (
        status.join(failure_counts, on="rollout_index", how="left")
        .join(final_net_worth, on="rollout_index", how="left")
        .with_columns(
            failure_count=pl.col("failure_count").fill_null(0),
            final_liquid_net_worth_usd=pl.col("final_liquid_net_worth_usd").fill_null(0.0),
            final_book_net_worth_usd=pl.col("final_book_net_worth_usd").fill_null(0.0),
        )
        .pipe(ROLLOUT_SUMMARY_FRAME.normalize)
        .sort("rollout_index")
    )


def _cash_net_worth_components(run: SimulationRun) -> pl.DataFrame:
    return run.cash_balances.select(
        "rollout_index",
        "month_index",
        "agent_id",
        pl.col("balance_usd").alias("cash_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("liquid_asset_value_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("asset_book_value_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("property_book_value_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("liability_principal_usd"),
    )


def _asset_net_worth_components(run: SimulationRun) -> pl.DataFrame:
    if run.asset_lots.is_empty():
        return _NET_WORTH_COMPONENT_FRAME.empty()
    priced = run.asset_lots.join(run.market_prices, on=["rollout_index", "month_index", "asset_id"], how="left")
    return priced.select(
        "rollout_index",
        "month_index",
        "agent_id",
        pl.lit(0.0, dtype=pl.Float64()).alias("cash_usd"),
        (pl.col("remaining_quantity") * pl.col("price_per_unit_usd").fill_null(0.0)).alias("liquid_asset_value_usd"),
        (pl.col("remaining_quantity") * pl.col("cost_basis_per_unit_usd")).alias("asset_book_value_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("property_book_value_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("liability_principal_usd"),
    )


def _property_net_worth_components(run: SimulationRun) -> pl.DataFrame:
    if run.property_state.is_empty() or run.property_stakes.is_empty():
        return _NET_WORTH_COMPONENT_FRAME.empty()
    owned_property = run.property_stakes.join(
        run.property_state, on=["rollout_index", "month_index", "property_id"], how="inner"
    )
    return owned_property.select(
        "rollout_index",
        "month_index",
        "agent_id",
        pl.lit(0.0, dtype=pl.Float64()).alias("cash_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("liquid_asset_value_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("asset_book_value_usd"),
        (pl.col("adjusted_basis_usd") * pl.col("ownership_pct")).alias("property_book_value_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("liability_principal_usd"),
    )


def _liability_net_worth_components(run: SimulationRun) -> pl.DataFrame:
    if run.liabilities.is_empty():
        return _NET_WORTH_COMPONENT_FRAME.empty()
    return run.liabilities.select(
        "rollout_index",
        "month_index",
        "agent_id",
        pl.lit(0.0, dtype=pl.Float64()).alias("cash_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("liquid_asset_value_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("asset_book_value_usd"),
        pl.lit(0.0, dtype=pl.Float64()).alias("property_book_value_usd"),
        pl.col("principal_usd").alias("liability_principal_usd"),
    )


def _transfer_transactions(run: SimulationRun) -> pl.DataFrame:
    transfers = run.events_log.transfers
    if transfers.is_empty():
        return TRANSACTION_FRAME.empty()
    return transfers.select(
        "rollout_index",
        "month_index",
        pl.concat_str([pl.col("cause_id"), pl.lit(":cash")]).alias("transaction_id"),
        pl.lit("cash_transfer", dtype=pl.Utf8()).alias("transaction_type"),
        "cause_id",
        "from_agent_id",
        "from_account_id",
        "to_agent_id",
        "to_account_id",
        pl.lit(None, dtype=pl.Utf8()).alias("asset_id"),
        pl.lit(None, dtype=pl.Utf8()).alias("lot_id"),
        "amount_usd",
        pl.lit(None, dtype=pl.Float64()).alias("quantity"),
    )


def _lot_disposition_transactions(run: SimulationRun) -> pl.DataFrame:
    dispositions = run.events_log.lot_dispositions
    if dispositions.is_empty():
        return TRANSACTION_FRAME.empty()
    return dispositions.select(
        "rollout_index",
        "month_index",
        pl.concat_str([pl.col("cause_id"), pl.lit(":"), pl.col("lot_id")]).alias("transaction_id"),
        pl.lit("asset_sale", dtype=pl.Utf8()).alias("transaction_type"),
        "cause_id",
        pl.col("agent_id").alias("from_agent_id"),
        pl.col("asset_id").alias("from_account_id"),
        pl.col("agent_id").alias("to_agent_id"),
        pl.col("proceeds_account_id").alias("to_account_id"),
        "asset_id",
        "lot_id",
        pl.col("proceeds_usd").alias("amount_usd"),
        pl.col("units_sold").alias("quantity"),
    )


def _obligation_settlement_transactions(run: SimulationRun) -> pl.DataFrame:
    settlements = run.events_log.obligation_settlements
    if settlements.is_empty():
        return TRANSACTION_FRAME.empty()
    destinations = run.events_log.obligation_accruals.select(
        "rollout_index", "obligation_id", "to_agent_id", "to_account_id"
    )
    return settlements.join(destinations, on=["rollout_index", "obligation_id"], how="left").select(
        "rollout_index",
        "month_index",
        pl.concat_str([pl.col("obligation_id"), pl.lit(":settlement")]).alias("transaction_id"),
        pl.lit("obligation_settlement", dtype=pl.Utf8()).alias("transaction_type"),
        "cause_id",
        pl.col("agent_id").alias("from_agent_id"),
        "from_account_id",
        "to_agent_id",
        "to_account_id",
        pl.lit(None, dtype=pl.Utf8()).alias("asset_id"),
        pl.lit(None, dtype=pl.Utf8()).alias("lot_id"),
        pl.col("amount_paid_usd").alias("amount_usd"),
        pl.lit(None, dtype=pl.Float64()).alias("quantity"),
    )


def _tax_settlement_transactions(run: SimulationRun) -> pl.DataFrame:
    settlements = run.events_log.tax_settlements
    if settlements.is_empty():
        return TRANSACTION_FRAME.empty()
    return settlements.select(
        "rollout_index",
        "month_index",
        pl.concat_str([pl.col("cause_id"), pl.lit(":tax-liability")]).alias("transaction_id"),
        pl.lit("tax_liability_settlement", dtype=pl.Utf8()).alias("transaction_type"),
        "cause_id",
        pl.col("agent_id").alias("from_agent_id"),
        pl.lit(None, dtype=pl.Utf8()).alias("from_account_id"),
        pl.lit(None, dtype=pl.Utf8()).alias("to_agent_id"),
        pl.lit(None, dtype=pl.Utf8()).alias("to_account_id"),
        pl.lit(None, dtype=pl.Utf8()).alias("asset_id"),
        pl.lit(None, dtype=pl.Utf8()).alias("lot_id"),
        "amount_usd",
        pl.lit(None, dtype=pl.Float64()).alias("quantity"),
    )


def _final_net_worth_by_rollout(net_worth: pl.DataFrame) -> pl.DataFrame:
    if net_worth.is_empty():
        return _FINAL_NET_WORTH_FRAME.empty()
    by_month = net_worth.group_by(["rollout_index", "month_index"]).agg(
        pl.col("liquid_net_worth_usd").sum().alias("final_liquid_net_worth_usd"),
        pl.col("book_net_worth_usd").sum().alias("final_book_net_worth_usd"),
    )
    final_months = by_month.group_by("rollout_index").agg(pl.col("month_index").max().alias("month_index"))
    return (
        final_months.join(by_month, on=["rollout_index", "month_index"], how="left")
        .rename({"month_index": "final_month_index"})
        .pipe(_FINAL_NET_WORTH_FRAME.normalize)
    )


def _failure_counts_by_rollout(failures: pl.DataFrame) -> pl.DataFrame:
    if failures.is_empty():
        return _FAILURE_COUNTS_FRAME.empty()
    return failures.group_by("rollout_index").agg(
        pl.len().alias("failure_count"), pl.col("month_index").min().alias("first_failure_month")
    )


def _filter_rollout(frame: pl.DataFrame, rollout_index: int) -> pl.DataFrame:
    return frame.filter(pl.col("rollout_index") == rollout_index)
