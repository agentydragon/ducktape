"""Column-major builders + materializers for per-rollout-per-month event streams.

The simulation engine accumulates per-event records (effects, decisions,
obligations, failure events, …) during `run_scenario_vectorized`. Historically
these were `list[PydanticModel]` accumulated row-by-row inside the per-month
loop, then sorted, `model_copy(update=trajectory_id)`'d, frozen into
`tuple[Effect, ...]` on `ScenarioRunArrays`. py-spy showed ~75% of simulate
time going to that Pydantic construction + copy + sort dance.

This module holds the column-major replacement: long-format polars frames
keyed by `(rollout_index, month_index, …)`, identity-joined once at the end
of the run instead of per-record `model_copy`. The Pydantic `tuple[...]`
surface stays — `ScenarioRunArrays` exposes `@property` shims that
materialize records lazily from the underlying frame(s) so test access and
wire-response paths read unchanged.

See `augur/plans/event_stream_polars_refactor.md` for the migration plan and
target shape. Roots-to-leaves: each root data table maps to one or more
Pydantic streams via projection/filter/join; the streams are not their own
roots.

Migrated so far:

* **Obligation lifecycle** (root) → `Obligation`, `SettlementResult`,
  `FailureEvent` (the latter two are filter+projection over the same root
  frame; one accumulator, three Pydantic surfaces).
* **Funding decisions** (root) → `FundingDecision` (separate cardinality
  from obligations — multiple funding decisions per obligation when the
  policy tries cash, then sells SP500, then crypto, etc.).
* **Lot dispositions** (root) → `LotDisposition` (one row per tax-lot
  consumption during a sale event).
* **Market observations** (two roots) → `MarketObservation` (union of
  `MarketPathObservation` — dense one-per-`(rollout, month)` from the
  `MarketBundle` multiplier matrices — and `PrivateEquitySaleOpportunityObservation`
  — sparse one-per-`(rollout, month, issuer)` from the opportunity recorders).
* **Accounting details** (two roots) → `AccountingDetail` (union of
  `PropertySaleBasisGainDetail` — one-per-rollout at the property-sale
  month — and `TaxPaymentAllocationDetail` — one-per-rollout per tax-year
  end).
* **Effects** (four roots) → `Effect` (one frame per variant —
  `SellSp500Effect`, `SellCryptoEffect`, `SellPrivateEquityEffect`,
  `SettlePropertySaleEffect`).
* **Policy decisions** (five roots) → `PolicyDecision` (one frame per
  variant — `MonthlySpendDecision`, `SellPublicStockDecision`,
  `SellCryptoDecision`, `PrivateEquitySaleDecision`,
  `PartnerContributionDecision`).
* **Tax lots** (root) → `TaxLot` (one row per opening-balance asset lot).
* **Liabilities** (root) → `LiabilityState` (one row per opening-balance
  liability, e.g. mortgage).
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator, Mapping
from typing import Any

import numpy as np
import polars as pl

from augur.core.accounting import LiabilityState, LiabilityType, LotAssetClass, LotDisposition, TaxLot
from augur.core.scenario_set import (
    AccountingDetail,
    AccountingDetailType,
    AccountType,
    AssetType,
    Effect,
    EffectType,
    EventType,
    FailureEvent,
    FailureEventType,
    FundingDecision,
    FundingDecisionType,
    FundingSourceType,
    MarketObservation,
    MarketObservationType,
    MarketPathObservation,
    MonthlySpendDecision,
    Obligation,
    ObligationStatus,
    ObligationType,
    PartnerContributionDecision,
    PolicyDecision,
    PolicyDecisionType,
    PrivateEquitySaleDecision,
    PrivateEquitySaleDecisionReason,
    PrivateEquitySaleOpportunityObservation,
    PrivateEquitySaleRuleType,
    PropertySaleBasisGainDetail,
    SellCryptoDecision,
    SellCryptoEffect,
    SellPrivateEquityEffect,
    SellPublicStockDecision,
    SellSp500Effect,
    SettlementResult,
    SettlementStatus,
    SettlePropertySaleEffect,
    TaxPaymentAllocationDetail,
    TaxPaymentTiming,
)


class StreamFrameBuilder:
    """Accumulates per-recorder-call row-blocks (`dict[str, np.ndarray | list]`)
    and concatenates them into one `pl.DataFrame` on `build()`.

    Schema is declared up-front so empty builders still produce a frame with
    the right columns + dtypes, and individual `extend` calls don't have to
    care about column order.

    Builders are mutable; callers own the lifecycle (typically one builder
    per stream per scenario run, fed by recorders during the per-month loop,
    drained once at end-of-run).

    `block_count()` + `build_slice(start)` exist for mid-run consumers
    (e.g. `_estimated_payments_credit_per_year_usd`) that need to read back
    the rows just emitted by a sub-loop — the equivalent of
    `recorded_list[initial_count:]` for the legacy Python-list path."""

    def __init__(self, schema: dict[str, pl.DataType]) -> None:
        self._schema = schema
        self._blocks: list[dict[str, Any]] = []

    def extend(self, columns: dict[str, Any]) -> None:
        if missing := set(self._schema) - set(columns):
            raise KeyError(f"missing columns for stream block: {sorted(missing)}")
        if extra := set(columns) - set(self._schema):
            raise KeyError(f"unexpected columns for stream block: {sorted(extra)}")
        self._blocks.append(columns)

    def build(self) -> pl.DataFrame:
        return self._concat(self._blocks)

    def block_count(self) -> int:
        return len(self._blocks)

    def build_slice(self, start_block_index: int) -> pl.DataFrame:
        return self._concat(self._blocks[start_block_index:])

    def _concat(self, blocks: list[dict[str, Any]]) -> pl.DataFrame:
        if not blocks:
            return pl.DataFrame(schema=self._schema)
        return pl.concat([pl.DataFrame(block, schema=self._schema) for block in blocks])


# Identity columns added to every event stream at end-of-run by
# `join_trajectory_identity`. Mirrors the four trace-identity fields in
# `scenario_set._TraceBase` (`path_set_id`, `exogenous_path_id`,
# `scenario_input_id`, `projection_trajectory_id`).
_IDENTITY_COLUMN_NAMES: tuple[str, ...] = (
    "path_set_id",
    "exogenous_path_id",
    "scenario_input_id",
    "projection_trajectory_id",
)


def build_identity_frame(identity_by_rollout: Mapping[int, Mapping[str, str]]) -> pl.DataFrame:
    """Materialize the `rollout_index -> {trajectory identity fields}` mapping
    built by `_trace_identity_by_rollout` into a small polars frame for
    one-shot left-joining onto event-stream frames."""

    rows = sorted(identity_by_rollout.items())
    columns: dict[str, list[Any]] = {"rollout_index": [int(rollout) for rollout, _ in rows]}
    for column_name in _IDENTITY_COLUMN_NAMES:
        columns[column_name] = [identity.get(column_name) for _, identity in rows]
    schema = {"rollout_index": pl.Int64} | dict.fromkeys(_IDENTITY_COLUMN_NAMES, pl.String)
    return pl.DataFrame(columns, schema=schema)


def join_trajectory_identity(df: pl.DataFrame, identity_df: pl.DataFrame) -> pl.DataFrame:
    """Stamp the per-rollout trajectory identity columns onto an event-stream
    frame in one shot, replacing the per-record `model_copy(update=...)` pass
    in `_with_trajectory_identity`."""

    return df.join(identity_df, on="rollout_index", how="left")


# -- obligation lifecycle ------------------------------------------------------
#
# Single source of truth for `Obligation`, `SettlementResult`, `FailureEvent`.
# All three Pydantic surfaces are projection/filter views over the same frame:
#
# * `obligations`         — the frame itself.
# * `settlement_results`  — strict column subset (drop `creditor_id`,
#                           `due_month_index`, `source_policy_id`, `required`).
#                           Pydantic uses `SettlementStatus` for the `status`
#                           field but the values are identical to
#                           `ObligationStatus` so the column is shared.
# * `failure_events`      — filter `unpaid_amount_usd > 0 & required`, then
#                           derive `failure_event_id = obligation_id + ":failure"`.
#                           Sort by `(month, rollout, failure_event_type,
#                           failure_event_id)` matches the legacy
#                           `_sorted_failure_events` key because failure_event_type
#                           is always `UNSETTLED_OBLIGATION` and `failure_event_id`
#                           sorts lex-equivalent to `obligation_id`.

OBLIGATION_LIFECYCLE_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "obligation_id": pl.String,
    "obligation_type": pl.String,
    "actor_id": pl.String,
    "creditor_id": pl.String,
    "due_month_index": pl.Int64,
    "amount_due_usd": pl.Float64,
    "amount_paid_usd": pl.Float64,
    "unpaid_amount_usd": pl.Float64,
    "status": pl.String,
    "source_policy_id": pl.String,
    "required": pl.Boolean,
}

_OBLIGATION_LIFECYCLE_SORT_KEY: tuple[str, ...] = ("month_index", "rollout_index", "obligation_type", "obligation_id")


def sort_obligation_lifecycle(df: pl.DataFrame) -> pl.DataFrame:
    """Polars equivalent of `_sorted_obligations` / `_sorted_settlement_results`
    over the legacy Pydantic lists. `_sorted_failure_events` uses
    `(month, rollout, failure_event_type, failure_event_id)` but
    `failure_event_type` is single-valued and `failure_event_id` sorts
    lex-equivalent to `obligation_id`, so this same key reproduces it on
    the filtered subset."""

    return df.sort(list(_OBLIGATION_LIFECYCLE_SORT_KEY))


def materialize_obligations(df: pl.DataFrame) -> Iterator[Obligation]:
    for row in df.iter_rows(named=True):
        yield Obligation(
            rollout_index=int(row["rollout_index"]),
            month_index=int(row["month_index"]),
            obligation_id=row["obligation_id"],
            obligation_type=ObligationType(row["obligation_type"]),
            actor_id=row["actor_id"],
            creditor_id=row["creditor_id"],
            due_month_index=int(row["due_month_index"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            unpaid_amount_usd=float(row["unpaid_amount_usd"]),
            status=ObligationStatus(row["status"]),
            source_policy_id=row["source_policy_id"],
            path_set_id=row.get("path_set_id"),
            exogenous_path_id=row.get("exogenous_path_id"),
            scenario_input_id=row.get("scenario_input_id"),
            projection_trajectory_id=row.get("projection_trajectory_id"),
        )


def materialize_settlement_results(df: pl.DataFrame) -> Iterator[SettlementResult]:
    for row in df.iter_rows(named=True):
        yield SettlementResult(
            rollout_index=int(row["rollout_index"]),
            month_index=int(row["month_index"]),
            obligation_id=row["obligation_id"],
            obligation_type=ObligationType(row["obligation_type"]),
            actor_id=row["actor_id"],
            status=SettlementStatus(row["status"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            unpaid_amount_usd=float(row["unpaid_amount_usd"]),
            path_set_id=row.get("path_set_id"),
            exogenous_path_id=row.get("exogenous_path_id"),
            scenario_input_id=row.get("scenario_input_id"),
            projection_trajectory_id=row.get("projection_trajectory_id"),
        )


# -- funding decisions ---------------------------------------------------------
#
# Separate cardinality from obligations (multiple funding decisions per
# obligation when the policy tries cash, then sells SP500, then crypto, etc.),
# so this is its own root frame. Sort key matches the legacy
# `_sorted_funding_decisions` Python-list sort:
# `(month, rollout, fillna(policy_sequence_index, -1), decision_type,
#   fillna(policy_id, ""), obligation_id)`.

FUNDING_DECISION_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "obligation_id": pl.String,
    "decision_type": pl.String,
    "actor_id": pl.String,
    "policy_id": pl.String,
    "policy_sequence_index": pl.Int64,
    "source_type": pl.String,
    "source_account_id": pl.String,
    "source_account_type": pl.String,
    "source_asset_id": pl.String,
    "source_asset_type": pl.String,
    "available_cash_usd": pl.Float64,
    "requested_cash_usd": pl.Float64,
    "requested_sale_usd": pl.Float64,
    "funded_cash_usd": pl.Float64,
    "shortfall_usd": pl.Float64,
}


def sort_funding_decisions(df: pl.DataFrame) -> pl.DataFrame:
    """Polars equivalent of `_sorted_funding_decisions` over the Pydantic list.
    `policy_sequence_index = None` sorted as `-1` and `policy_id = None`
    sorted as empty string in the legacy code, so we fill-null those columns
    into transient sort keys."""

    return (
        df.with_columns(
            pl.col("policy_sequence_index").fill_null(-1).alias("_sort_seq"),
            pl.col("policy_id").fill_null("").alias("_sort_pid"),
        )
        .sort(["month_index", "rollout_index", "_sort_seq", "decision_type", "_sort_pid", "obligation_id"])
        .drop(["_sort_seq", "_sort_pid"])
    )


def _row_optional_enum(row: dict[str, Any], column: str, enum_cls: type) -> Any:
    value = row.get(column)
    if value is None:
        return None
    return enum_cls(value)


def materialize_funding_decisions(df: pl.DataFrame) -> Iterator[FundingDecision]:
    for row in df.iter_rows(named=True):
        yield FundingDecision(
            rollout_index=int(row["rollout_index"]),
            month_index=int(row["month_index"]),
            obligation_id=row["obligation_id"],
            decision_type=FundingDecisionType(row["decision_type"]),
            actor_id=row["actor_id"],
            policy_id=row["policy_id"],
            policy_sequence_index=row["policy_sequence_index"],
            source_type=_row_optional_enum(row, "source_type", FundingSourceType),
            source_account_id=row["source_account_id"],
            source_account_type=_row_optional_enum(row, "source_account_type", AccountType),
            source_asset_id=row["source_asset_id"],
            source_asset_type=_row_optional_enum(row, "source_asset_type", AssetType),
            available_cash_usd=float(row["available_cash_usd"]),
            requested_cash_usd=float(row["requested_cash_usd"]),
            requested_sale_usd=float(row["requested_sale_usd"]),
            funded_cash_usd=float(row["funded_cash_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
            path_set_id=row.get("path_set_id"),
            exogenous_path_id=row.get("exogenous_path_id"),
            scenario_input_id=row.get("scenario_input_id"),
            projection_trajectory_id=row.get("projection_trajectory_id"),
        )


# -- lot dispositions ----------------------------------------------------------
#
# One row per tax-lot consumption during a sale (property, SP500 stock, crypto,
# private equity). Sort key from the legacy `_sorted_lot_dispositions` is
# `(month, rollout, asset_class, lot_disposition_id)`.

LOT_DISPOSITION_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "lot_disposition_id": pl.String,
    "journal_entry_id": pl.String,
    "lot_id": pl.String,
    "asset_class": pl.String,
    "proceeds_usd": pl.Float64,
    "cost_basis_usd": pl.Float64,
    "realized_gain_usd": pl.Float64,
    "taxable_gain_usd": pl.Float64,
    "quantity_sold": pl.Float64,
    "tax_expense_usd": pl.Float64,
}


def sort_lot_dispositions(df: pl.DataFrame) -> pl.DataFrame:
    return df.sort(["month_index", "rollout_index", "asset_class", "lot_disposition_id"])


def materialize_lot_dispositions(df: pl.DataFrame) -> Iterator[LotDisposition]:
    for row in df.iter_rows(named=True):
        yield LotDisposition(
            lot_disposition_id=row["lot_disposition_id"],
            journal_entry_id=row["journal_entry_id"],
            rollout_index=int(row["rollout_index"]),
            month_index=int(row["month_index"]),
            lot_id=row["lot_id"],
            asset_class=LotAssetClass(row["asset_class"]),
            proceeds_usd=float(row["proceeds_usd"]),
            cost_basis_usd=float(row["cost_basis_usd"]),
            realized_gain_usd=float(row["realized_gain_usd"]),
            taxable_gain_usd=float(row["taxable_gain_usd"]),
            quantity_sold=row["quantity_sold"],  # nullable
            tax_expense_usd=float(row["tax_expense_usd"]),
            path_set_id=row.get("path_set_id"),
            exogenous_path_id=row.get("exogenous_path_id"),
            scenario_input_id=row.get("scenario_input_id"),
            projection_trajectory_id=row.get("projection_trajectory_id"),
        )


# -- market observations -------------------------------------------------------
#
# Two roots: a dense `(rollouts × months)` path frame from the `MarketBundle`
# multiplier matrices, and a sparse opportunity frame for per-issuer tender
# events. The unified `materialize_market_observations` merges them in the
# canonical `(month, rollout, observation_type)` order — `market_path` sorts
# before `private_equity_sale_opportunity` lexicographically.

MARKET_PATH_OBSERVATION_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "location_id": pl.String,
    "inflation_multiplier": pl.Float64,
    "sp500_multiplier": pl.Float64,
    "private_equity_value_multiplier": pl.Float64,
    "home_value_multiplier": pl.Float64,
    "rent_multiplier": pl.Float64,
    "mortgage_30y_rate_pct": pl.Float64,
    "private_equity_sale_opportunity_event": pl.Boolean,
}

PE_SALE_OPPORTUNITY_OBSERVATION_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "source_asset_id": pl.String,
    "opportunity_id": pl.String,
    "opportunity_cause_id": pl.String,
    "sale_opportunity_value_usd": pl.Float64,
    "private_equity_value_before_sale_usd": pl.Float64,
}


def build_market_path_observations_frame(
    *,
    rollout_count: int,
    horizon_months: int,
    month_index: np.ndarray,
    location_id: str | None,
    inflation_multipliers: np.ndarray,
    sp500_multipliers: np.ndarray,
    pe_value_multipliers: np.ndarray,
    home_value_multipliers: np.ndarray,
    rent_multipliers: np.ndarray,
    mortgage_30y_rate_pct: np.ndarray,
    pe_sale_opportunity_mask: np.ndarray,
) -> pl.DataFrame:
    """Build the dense `(rollouts × (months+1))` market-path frame in one
    shot from the bundle's multiplier matrices, replacing the legacy
    per-cell Pydantic loop in `_market_path_observations` that constructed
    `rollouts × (months+1)` `MarketPathObservation` instances at scenario
    start."""

    months = horizon_months + 1
    rollout_axis, month_axis = np.indices((rollout_count, months), sparse=False)
    rollout_col = rollout_axis.ravel().astype(np.int64)
    month_col = month_index[month_axis.ravel()].astype(np.int64)
    return pl.DataFrame(
        {
            "rollout_index": rollout_col,
            "month_index": month_col,
            "location_id": [location_id] * rollout_col.size,
            "inflation_multiplier": inflation_multipliers.ravel().astype(np.float64),
            "sp500_multiplier": sp500_multipliers.ravel().astype(np.float64),
            "private_equity_value_multiplier": pe_value_multipliers.ravel().astype(np.float64),
            "home_value_multiplier": home_value_multipliers.ravel().astype(np.float64),
            "rent_multiplier": rent_multipliers.ravel().astype(np.float64),
            "mortgage_30y_rate_pct": mortgage_30y_rate_pct.ravel().astype(np.float64),
            "private_equity_sale_opportunity_event": pe_sale_opportunity_mask.ravel().astype(np.bool_),
        },
        schema=MARKET_PATH_OBSERVATION_SCHEMA,
    )


def sort_market_path_observations(df: pl.DataFrame) -> pl.DataFrame:
    return df.sort(["month_index", "rollout_index"])


def sort_pe_sale_opportunity_observations(df: pl.DataFrame) -> pl.DataFrame:
    # Within the same (month, rollout, observation_type) the legacy code's
    # tuple key has no further tiebreaker. Stable sort preserves the
    # recording order from the per-issuer recorder, which is what callers
    # see today.
    return df.sort(["month_index", "rollout_index"])


def materialize_market_observations(
    market_path_frame: pl.DataFrame, opportunity_frame: pl.DataFrame
) -> Iterator[MarketObservation]:
    """Merge the two market-observation frames in the canonical
    `(month, rollout, observation_type)` lex order — at the same
    `(month, rollout)` the `market_path` observation comes first because
    `"market_path" < "private_equity_sale_opportunity"` lexicographically."""

    path_iter = iter(market_path_frame.iter_rows(named=True))
    opp_iter = iter(opportunity_frame.iter_rows(named=True))
    path_row = next(path_iter, None)
    opp_row = next(opp_iter, None)
    while path_row is not None or opp_row is not None:
        if path_row is None:
            yield _build_pe_opportunity_observation(opp_row)
            opp_row = next(opp_iter, None)
        elif opp_row is None:
            yield _build_market_path_observation(path_row)
            path_row = next(path_iter, None)
        else:
            path_key = (path_row["month_index"], path_row["rollout_index"], MarketObservationType.MARKET_PATH.value)
            opp_key = (
                opp_row["month_index"],
                opp_row["rollout_index"],
                MarketObservationType.PRIVATE_EQUITY_SALE_OPPORTUNITY.value,
            )
            if path_key <= opp_key:
                yield _build_market_path_observation(path_row)
                path_row = next(path_iter, None)
            else:
                yield _build_pe_opportunity_observation(opp_row)
                opp_row = next(opp_iter, None)


def _build_market_path_observation(row: dict[str, Any]) -> MarketPathObservation:
    return MarketPathObservation(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        location_id=row["location_id"],
        inflation_multiplier=float(row["inflation_multiplier"]),
        sp500_multiplier=float(row["sp500_multiplier"]),
        private_equity_value_multiplier=float(row["private_equity_value_multiplier"]),
        home_value_multiplier=float(row["home_value_multiplier"]),
        rent_multiplier=float(row["rent_multiplier"]),
        mortgage_30y_rate_pct=float(row["mortgage_30y_rate_pct"]),
        private_equity_sale_opportunity_event=bool(row["private_equity_sale_opportunity_event"]),
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


def _build_pe_opportunity_observation(row: dict[str, Any]) -> PrivateEquitySaleOpportunityObservation:
    return PrivateEquitySaleOpportunityObservation(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        source_asset_id=row["source_asset_id"],
        opportunity_id=row["opportunity_id"],
        opportunity_cause_id=row["opportunity_cause_id"],
        sale_opportunity_value_usd=float(row["sale_opportunity_value_usd"]),
        private_equity_value_before_sale_usd=float(row["private_equity_value_before_sale_usd"]),
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


# -- accounting details --------------------------------------------------------
#
# Two roots; no field overlap. Legacy `_sorted_accounting_details` sorts by
# `(month, rollout, detail_type, actor_id, policy_id or '', event_id or '',
# property_id or '')`. With two frames merge-sorted at materialization the
# `detail_type` portion is implicit (`property_sale_basis_gain` sorts
# lexicographically before `tax_payment_allocation`); within each frame we
# sort by the remaining sub-key.

PROPERTY_SALE_BASIS_GAIN_DETAIL_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "actor_id": pl.String,
    "policy_id": pl.String,
    "event_id": pl.String,
    "property_id": pl.String,
    "gross_sale_usd": pl.Float64,
    "selling_cost_usd": pl.Float64,
    "debt_payoff_usd": pl.Float64,
    "adjusted_basis_usd": pl.Float64,
    "realized_gain_usd": pl.Float64,
    "depreciation_recapture_usd": pl.Float64,
    "capital_gain_usd": pl.Float64,
    "capital_gain_exclusion_usd": pl.Float64,
    "taxable_capital_gain_usd": pl.Float64,
    "taxable_gain_usd": pl.Float64,
}

TAX_PAYMENT_ALLOCATION_DETAIL_SCHEMA: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "actor_id": pl.String,
    "policy_id": pl.String,
    "event_id": pl.String,
    "property_id": pl.String,
    "tax_year_index": pl.Int64,
    "payment_timing": pl.String,
    "federal_income_tax_usd": pl.Float64,
    "california_income_tax_usd": pl.Float64,
    "total_income_tax_usd": pl.Float64,
    "property_sale_tax_usd": pl.Float64,
    "generic_sp500_sale_tax_usd": pl.Float64,
    "private_equity_sale_tax_usd": pl.Float64,
    "rental_income_tax_usd": pl.Float64,
    "property_depreciation_recapture_usd": pl.Float64,
    "taxable_property_capital_gain_usd": pl.Float64,
    "generic_sp500_taxable_gain_usd": pl.Float64,
    "private_equity_taxable_gain_usd": pl.Float64,
    "net_rental_taxable_income_usd": pl.Float64,
    "total_taxable_income_usd": pl.Float64,
}


def _sort_accounting_detail_subkey(df: pl.DataFrame) -> pl.DataFrame:
    """Sort an accounting-detail frame on the post-detail_type sub-key:
    `(month, rollout, actor_id, policy_id or '', event_id or '', property_id or '')`.

    Polars treats `None < ''` by default but the legacy sort fills nulls with
    `""` before comparing — we do the same via transient sort-key columns."""

    return (
        df.with_columns(
            pl.col("policy_id").fill_null("").alias("_sort_pid"),
            pl.col("event_id").fill_null("").alias("_sort_eid"),
            pl.col("property_id").fill_null("").alias("_sort_propid"),
        )
        .sort(["month_index", "rollout_index", "actor_id", "_sort_pid", "_sort_eid", "_sort_propid"])
        .drop(["_sort_pid", "_sort_eid", "_sort_propid"])
    )


def sort_property_sale_basis_gain_details(df: pl.DataFrame) -> pl.DataFrame:
    return _sort_accounting_detail_subkey(df)


def sort_tax_payment_allocation_details(df: pl.DataFrame) -> pl.DataFrame:
    return _sort_accounting_detail_subkey(df)


def materialize_accounting_details(
    property_sale_frame: pl.DataFrame, tax_payment_frame: pl.DataFrame
) -> Iterator[AccountingDetail]:
    """Merge-sort the two variant frames in the canonical
    `(month, rollout, detail_type, …)` order. At the same `(month, rollout)`
    `property_sale_basis_gain` sorts before `tax_payment_allocation`
    lexicographically."""

    property_iter = iter(property_sale_frame.iter_rows(named=True))
    tax_iter = iter(tax_payment_frame.iter_rows(named=True))
    p_row = next(property_iter, None)
    t_row = next(tax_iter, None)
    while p_row is not None or t_row is not None:
        if p_row is None:
            yield _build_tax_payment_allocation_detail(t_row)
            t_row = next(tax_iter, None)
        elif t_row is None:
            yield _build_property_sale_basis_gain_detail(p_row)
            p_row = next(property_iter, None)
        else:
            p_key = (p_row["month_index"], p_row["rollout_index"], AccountingDetailType.PROPERTY_SALE_BASIS_GAIN.value)
            t_key = (t_row["month_index"], t_row["rollout_index"], AccountingDetailType.TAX_PAYMENT_ALLOCATION.value)
            if p_key <= t_key:
                yield _build_property_sale_basis_gain_detail(p_row)
                p_row = next(property_iter, None)
            else:
                yield _build_tax_payment_allocation_detail(t_row)
                t_row = next(tax_iter, None)


def _build_property_sale_basis_gain_detail(row: dict[str, Any]) -> PropertySaleBasisGainDetail:
    return PropertySaleBasisGainDetail(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        actor_id=row["actor_id"],
        policy_id=row["policy_id"],
        event_id=row["event_id"],
        property_id=row["property_id"],
        gross_sale_usd=float(row["gross_sale_usd"]),
        selling_cost_usd=float(row["selling_cost_usd"]),
        debt_payoff_usd=float(row["debt_payoff_usd"]),
        adjusted_basis_usd=float(row["adjusted_basis_usd"]),
        realized_gain_usd=float(row["realized_gain_usd"]),
        depreciation_recapture_usd=float(row["depreciation_recapture_usd"]),
        capital_gain_usd=float(row["capital_gain_usd"]),
        capital_gain_exclusion_usd=float(row["capital_gain_exclusion_usd"]),
        taxable_capital_gain_usd=float(row["taxable_capital_gain_usd"]),
        taxable_gain_usd=float(row["taxable_gain_usd"]),
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


def _build_tax_payment_allocation_detail(row: dict[str, Any]) -> TaxPaymentAllocationDetail:
    return TaxPaymentAllocationDetail(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        actor_id=row["actor_id"],
        policy_id=row["policy_id"],
        event_id=row["event_id"],
        property_id=row["property_id"],
        tax_year_index=int(row["tax_year_index"]),
        payment_timing=TaxPaymentTiming(row["payment_timing"]),
        federal_income_tax_usd=float(row["federal_income_tax_usd"]),
        california_income_tax_usd=float(row["california_income_tax_usd"]),
        total_income_tax_usd=float(row["total_income_tax_usd"]),
        property_sale_tax_usd=float(row["property_sale_tax_usd"]),
        generic_sp500_sale_tax_usd=float(row["generic_sp500_sale_tax_usd"]),
        private_equity_sale_tax_usd=float(row["private_equity_sale_tax_usd"]),
        rental_income_tax_usd=float(row["rental_income_tax_usd"]),
        property_depreciation_recapture_usd=float(row["property_depreciation_recapture_usd"]),
        taxable_property_capital_gain_usd=float(row["taxable_property_capital_gain_usd"]),
        generic_sp500_taxable_gain_usd=float(row["generic_sp500_taxable_gain_usd"]),
        private_equity_taxable_gain_usd=float(row["private_equity_taxable_gain_usd"]),
        net_rental_taxable_income_usd=float(row["net_rental_taxable_income_usd"]),
        total_taxable_income_usd=float(row["total_taxable_income_usd"]),
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


# -- effects -------------------------------------------------------------------
#
# Four roots, one per `EffectType` variant. The legacy
# `_sorted_effects` sorts on `(month, rollout, effect_type)` — since
# `effect_type` is constant inside each variant frame, we sort each
# frame by `(month, rollout)` and merge-sort by the full key at
# materialization time using `heapq.merge`.

_EFFECT_BASE_COLUMNS: dict[str, pl.DataType] = {
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "actor_id": pl.String,
    "policy_id": pl.String,
}

SELL_SP500_EFFECT_SCHEMA: dict[str, pl.DataType] = {
    **_EFFECT_BASE_COLUMNS,
    "amount_usd": pl.Float64,
    "after_tax_proceeds_usd": pl.Float64,
    "basis_usd": pl.Float64,
    "gain_usd": pl.Float64,
    "tax_usd": pl.Float64,
    "shortfall_usd": pl.Float64,
}

SELL_CRYPTO_EFFECT_SCHEMA: dict[str, pl.DataType] = {
    **_EFFECT_BASE_COLUMNS,
    "source_asset_id": pl.String,
    "asset_symbol": pl.String,
    "amount_usd": pl.Float64,
    "quantity_sold": pl.Float64,
    "basis_usd": pl.Float64,
    "gain_usd": pl.Float64,
    "shortfall_usd": pl.Float64,
}

SELL_PRIVATE_EQUITY_EFFECT_SCHEMA: dict[str, pl.DataType] = {
    **_EFFECT_BASE_COLUMNS,
    "event_id": pl.String,
    "event_type": pl.String,
    "opportunity_id": pl.String,
    "opportunity_cause_id": pl.String,
    "amount_usd": pl.Float64,
    "after_tax_proceeds_usd": pl.Float64,
    "basis_usd": pl.Float64,
    "taxable_gain_usd": pl.Float64,
    "estimated_tax_usd": pl.Float64,
    "units_sold": pl.Float64,
    "sold_fraction": pl.Float64,
    "proceeds_destination": pl.String,
}

SETTLE_PROPERTY_SALE_EFFECT_SCHEMA: dict[str, pl.DataType] = {
    **_EFFECT_BASE_COLUMNS,
    "event_id": pl.String,
    "property_id": pl.String,
    "gross_sale_usd": pl.Float64,
    "selling_cost_usd": pl.Float64,
    "debt_payoff_usd": pl.Float64,
    "adjusted_basis_usd": pl.Float64,
    "realized_gain_usd": pl.Float64,
    "depreciation_recapture_usd": pl.Float64,
    "capital_gain_usd": pl.Float64,
    "capital_gain_exclusion_usd": pl.Float64,
    "taxable_capital_gain_usd": pl.Float64,
    "taxable_gain_usd": pl.Float64,
    "tax_usd": pl.Float64,
    "net_proceeds_usd": pl.Float64,
    "proceeds_destination": pl.String,
}


def sort_effects_variant_frame(df: pl.DataFrame) -> pl.DataFrame:
    return df.sort(["month_index", "rollout_index"])


def materialize_effects(frames: Mapping[EffectType, pl.DataFrame]) -> Iterator[Effect]:
    """Merge-sort the four variant frames in canonical
    `(month, rollout, effect_type)` lex order. Each variant frame is
    pre-sorted by `(month, rollout)` so `heapq.merge` only needs the
    composite key."""

    builders: dict[EffectType, Any] = {
        EffectType.SELL_SP500: _build_sell_sp500_effect,
        EffectType.SELL_CRYPTO: _build_sell_crypto_effect,
        EffectType.SELL_PRIVATE_EQUITY: _build_sell_private_equity_effect,
        EffectType.SETTLE_PROPERTY_SALE: _build_settle_property_sale_effect,
    }

    def _stream(
        effect_type: EffectType, frame: pl.DataFrame
    ) -> Iterator[tuple[tuple[int, int, str], Any, dict[str, Any]]]:
        builder = builders[effect_type]
        type_value = effect_type.value
        for row in frame.iter_rows(named=True):
            yield ((row["month_index"], row["rollout_index"], type_value), builder, row)

    iterators = [_stream(effect_type, frame) for effect_type, frame in frames.items()]
    for _, builder, row in heapq.merge(*iterators, key=lambda x: x[0]):
        yield builder(row)


def _build_sell_sp500_effect(row: dict[str, Any]) -> SellSp500Effect:
    return SellSp500Effect(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        actor_id=row["actor_id"],
        policy_id=row["policy_id"],
        amount_usd=float(row["amount_usd"]),
        after_tax_proceeds_usd=float(row["after_tax_proceeds_usd"]),
        basis_usd=float(row["basis_usd"]),
        gain_usd=float(row["gain_usd"]),
        tax_usd=float(row["tax_usd"]),
        shortfall_usd=float(row["shortfall_usd"]),
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


def _build_sell_crypto_effect(row: dict[str, Any]) -> SellCryptoEffect:
    return SellCryptoEffect(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        actor_id=row["actor_id"],
        policy_id=row["policy_id"],
        source_asset_id=row["source_asset_id"],
        asset_symbol=row["asset_symbol"],
        amount_usd=float(row["amount_usd"]),
        quantity_sold=float(row["quantity_sold"]),
        basis_usd=float(row["basis_usd"]),
        gain_usd=float(row["gain_usd"]),
        shortfall_usd=float(row["shortfall_usd"]),
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


def _build_sell_private_equity_effect(row: dict[str, Any]) -> SellPrivateEquityEffect:
    event_type_value = row["event_type"]
    return SellPrivateEquityEffect(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        actor_id=row["actor_id"],
        policy_id=row["policy_id"],
        event_id=row["event_id"],
        event_type=EventType(event_type_value) if event_type_value is not None else None,
        opportunity_id=row["opportunity_id"],
        opportunity_cause_id=row["opportunity_cause_id"],
        amount_usd=float(row["amount_usd"]),
        after_tax_proceeds_usd=float(row["after_tax_proceeds_usd"]),
        basis_usd=float(row["basis_usd"]),
        taxable_gain_usd=float(row["taxable_gain_usd"]),
        estimated_tax_usd=float(row["estimated_tax_usd"]),
        units_sold=float(row["units_sold"]),
        sold_fraction=float(row["sold_fraction"]),
        proceeds_destination=_coerce_proceeds_destination(row["proceeds_destination"]),
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


def _build_settle_property_sale_effect(row: dict[str, Any]) -> SettlePropertySaleEffect:
    return SettlePropertySaleEffect(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        actor_id=row["actor_id"],
        policy_id=row["policy_id"],
        event_id=row["event_id"],
        property_id=row["property_id"],
        gross_sale_usd=float(row["gross_sale_usd"]),
        selling_cost_usd=float(row["selling_cost_usd"]),
        debt_payoff_usd=float(row["debt_payoff_usd"]),
        adjusted_basis_usd=float(row["adjusted_basis_usd"]),
        realized_gain_usd=float(row["realized_gain_usd"]),
        depreciation_recapture_usd=float(row["depreciation_recapture_usd"]),
        capital_gain_usd=float(row["capital_gain_usd"]),
        capital_gain_exclusion_usd=float(row["capital_gain_exclusion_usd"]),
        taxable_capital_gain_usd=float(row["taxable_capital_gain_usd"]),
        taxable_gain_usd=float(row["taxable_gain_usd"]),
        tax_usd=float(row["tax_usd"]),
        net_proceeds_usd=float(row["net_proceeds_usd"]),
        proceeds_destination=AccountType(row["proceeds_destination"]),
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


def _coerce_proceeds_destination(value: str) -> AccountType | AssetType:
    """`SellPrivateEquityEffect.proceeds_destination: AccountType | AssetType`.
    We don't know at column-read time which enum it is, so try AccountType
    first then fall through to AssetType."""

    try:
        return AccountType(value)
    except ValueError:
        return AssetType(value)


# -- policy decisions ----------------------------------------------------------
#
# Single wide frame discriminated by `decision_type`. Variant-specific columns
# are nullable. One sort call by `(month, rollout, actor_id,
# policy_sequence_index, decision_type, policy_id)` reproduces the legacy
# `_sorted_policy_decisions` tuple key directly — no merge step needed.

POLICY_DECISION_SCHEMA: dict[str, pl.DataType] = {
    # Common base.
    "rollout_index": pl.Int64,
    "month_index": pl.Int64,
    "decision_type": pl.String,
    "actor_id": pl.String,
    "policy_id": pl.String,
    "policy_sequence_index": pl.Int64,
    # MonthlySpendDecision-specific.
    "amount_usd": pl.Float64,
    "inflation_multiplier": pl.Float64,
    # SellPublicStockDecision / SellCryptoDecision (shared) / PE / partner.
    "requested_amount_usd": pl.Float64,
    "current_cash_usd": pl.Float64,
    "target_cash_floor_usd": pl.Float64,
    # SellCryptoDecision / PrivateEquitySaleDecision (shared).
    "source_asset_id": pl.String,
    # PrivateEquitySaleDecision-specific.
    "decision_reason": pl.String,
    "sale_rule_type": pl.String,
    "configured_sale_amount_usd": pl.Float64,
    "opportunity_id": pl.String,
    "opportunity_cause_id": pl.String,
    "sale_opportunity_value_usd": pl.Float64,
    "private_equity_value_before_sale_usd": pl.Float64,
    "liquid_net_worth_usd": pl.Float64,
    "target_liquid_net_worth_floor_usd": pl.Float64,
    "proceeds_destination": pl.String,
    # PartnerContributionDecision-specific.
    "recipient_actor_id": pl.String,
    "property_id": pl.String,
}


def sort_policy_decisions(df: pl.DataFrame) -> pl.DataFrame:
    return df.sort(["month_index", "rollout_index", "actor_id", "policy_sequence_index", "decision_type", "policy_id"])


def materialize_policy_decisions(df: pl.DataFrame) -> Iterator[PolicyDecision]:
    """Iterate the single wide frame, dispatching to the right Pydantic
    variant per row by `decision_type`."""

    dispatch: dict[str, Any] = {
        PolicyDecisionType.MONTHLY_SPEND.value: _build_monthly_spend_decision,
        PolicyDecisionType.SELL_PUBLIC_STOCK.value: _build_sell_public_stock_decision,
        PolicyDecisionType.SELL_CRYPTO.value: _build_sell_crypto_decision,
        PolicyDecisionType.PRIVATE_EQUITY_SALE.value: _build_private_equity_sale_decision,
        PolicyDecisionType.PARTNER_CONTRIBUTION.value: _build_partner_contribution_decision,
    }
    for row in df.iter_rows(named=True):
        yield dispatch[row["decision_type"]](row)


def _build_monthly_spend_decision(row: dict[str, Any]) -> MonthlySpendDecision:
    return MonthlySpendDecision(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        actor_id=row["actor_id"],
        policy_id=row["policy_id"],
        policy_sequence_index=int(row["policy_sequence_index"]),
        amount_usd=float(row["amount_usd"]),
        inflation_multiplier=float(row["inflation_multiplier"]),
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


def _build_sell_public_stock_decision(row: dict[str, Any]) -> SellPublicStockDecision:
    return SellPublicStockDecision(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        actor_id=row["actor_id"],
        policy_id=row["policy_id"],
        policy_sequence_index=int(row["policy_sequence_index"]),
        requested_amount_usd=float(row["requested_amount_usd"]),
        current_cash_usd=float(row["current_cash_usd"]),
        target_cash_floor_usd=row["target_cash_floor_usd"],
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


def _build_sell_crypto_decision(row: dict[str, Any]) -> SellCryptoDecision:
    return SellCryptoDecision(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        actor_id=row["actor_id"],
        policy_id=row["policy_id"],
        policy_sequence_index=int(row["policy_sequence_index"]),
        source_asset_id=row["source_asset_id"],
        requested_amount_usd=float(row["requested_amount_usd"]),
        current_cash_usd=float(row["current_cash_usd"]),
        target_cash_floor_usd=row["target_cash_floor_usd"],
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


def _build_private_equity_sale_decision(row: dict[str, Any]) -> PrivateEquitySaleDecision:
    return PrivateEquitySaleDecision(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        actor_id=row["actor_id"],
        policy_id=row["policy_id"],
        policy_sequence_index=int(row["policy_sequence_index"]),
        decision_reason=PrivateEquitySaleDecisionReason(row["decision_reason"]),
        source_asset_id=row["source_asset_id"],
        sale_rule_type=PrivateEquitySaleRuleType(row["sale_rule_type"]),
        configured_sale_amount_usd=float(row["configured_sale_amount_usd"]),
        opportunity_id=row["opportunity_id"],
        opportunity_cause_id=row["opportunity_cause_id"],
        requested_amount_usd=float(row["requested_amount_usd"]),
        sale_opportunity_value_usd=float(row["sale_opportunity_value_usd"]),
        private_equity_value_before_sale_usd=float(row["private_equity_value_before_sale_usd"]),
        liquid_net_worth_usd=float(row["liquid_net_worth_usd"]),
        target_liquid_net_worth_floor_usd=row["target_liquid_net_worth_floor_usd"],
        proceeds_destination=_coerce_proceeds_destination(row["proceeds_destination"]),
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


def _build_partner_contribution_decision(row: dict[str, Any]) -> PartnerContributionDecision:
    return PartnerContributionDecision(
        rollout_index=int(row["rollout_index"]),
        month_index=int(row["month_index"]),
        actor_id=row["actor_id"],
        policy_id=row["policy_id"],
        policy_sequence_index=int(row["policy_sequence_index"]),
        recipient_actor_id=row["recipient_actor_id"],
        requested_amount_usd=float(row["requested_amount_usd"]),
        property_id=row["property_id"],
        path_set_id=row.get("path_set_id"),
        exogenous_path_id=row.get("exogenous_path_id"),
        scenario_input_id=row.get("scenario_input_id"),
        projection_trajectory_id=row.get("projection_trajectory_id"),
    )


def materialize_failure_events(df: pl.DataFrame) -> Iterator[FailureEvent]:
    """`df` is the full obligation lifecycle frame; we filter to the failed
    rows (`unpaid_amount_usd > 0 & required`) inside this function so callers
    don't have to keep two frames around."""

    failed = df.filter((pl.col("unpaid_amount_usd") > 0) & pl.col("required"))
    for row in failed.iter_rows(named=True):
        obligation_id = row["obligation_id"]
        yield FailureEvent(
            rollout_index=int(row["rollout_index"]),
            month_index=int(row["month_index"]),
            failure_event_id=f"{obligation_id}:failure",
            failure_event_type=FailureEventType.UNSETTLED_OBLIGATION,
            obligation_id=obligation_id,
            actor_id=row["actor_id"],
            unpaid_amount_usd=float(row["unpaid_amount_usd"]),
            path_set_id=row.get("path_set_id"),
            exogenous_path_id=row.get("exogenous_path_id"),
            scenario_input_id=row.get("scenario_input_id"),
            projection_trajectory_id=row.get("projection_trajectory_id"),
        )


# -- tax lots ------------------------------------------------------------------
#
# End-of-run snapshot, one row per opening-balance asset lot. Sort key from
# the legacy `_sorted_tax_lots` is just `lot_id`. No trajectory identity
# fields on `TaxLot` (snapshot, not per-trajectory).

TAX_LOT_SCHEMA: dict[str, pl.DataType] = {
    "lot_id": pl.String,
    "asset_class": pl.String,
    "owner_actor_id": pl.String,
    "source_account_id": pl.String,
    "source_asset_id": pl.String,
    "property_id": pl.String,
    "quantity": pl.Float64,
    "cost_basis_usd": pl.Float64,
    "acquisition_month_index": pl.Int64,
}


def sort_tax_lots(df: pl.DataFrame) -> pl.DataFrame:
    return df.sort(["lot_id"])


def materialize_tax_lots(df: pl.DataFrame) -> Iterator[TaxLot]:
    for row in df.iter_rows(named=True):
        yield TaxLot(
            lot_id=row["lot_id"],
            asset_class=LotAssetClass(row["asset_class"]),
            owner_actor_id=row["owner_actor_id"],
            source_account_id=row["source_account_id"],
            source_asset_id=row["source_asset_id"],
            property_id=row["property_id"],
            quantity=row["quantity"],
            cost_basis_usd=float(row["cost_basis_usd"]),
            acquisition_month_index=int(row["acquisition_month_index"]),
        )


# -- liabilities ---------------------------------------------------------------
#
# End-of-run snapshot, one row per opening-balance liability (e.g. mortgage).
# Sort key from the legacy `_sorted_liabilities` is just `liability_id`. No
# trajectory identity fields (snapshot, not per-trajectory).

LIABILITY_STATE_SCHEMA: dict[str, pl.DataType] = {
    "liability_id": pl.String,
    "liability_type": pl.String,
    "actor_id": pl.String,
    "creditor_id": pl.String,
    "counterparty_actor_id": pl.String,
    "property_id": pl.String,
    "balance_usd": pl.Float64,
}


def sort_liabilities(df: pl.DataFrame) -> pl.DataFrame:
    return df.sort(["liability_id"])


def materialize_liabilities(df: pl.DataFrame) -> Iterator[LiabilityState]:
    for row in df.iter_rows(named=True):
        yield LiabilityState(
            liability_id=row["liability_id"],
            liability_type=LiabilityType(row["liability_type"]),
            actor_id=row["actor_id"],
            creditor_id=row["creditor_id"],
            counterparty_actor_id=row["counterparty_actor_id"],
            property_id=row["property_id"],
            balance_usd=float(row["balance_usd"]),
        )
