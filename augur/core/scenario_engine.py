from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

import numpy as np
import polars as pl

from augur.core import event_streams, posting_schemas
from augur.core.accounting import (
    ChartAccountRole,
    JournalEntryType,
    LiabilityState,
    LiabilityType,
    LotAssetClass,
    LotDisposition,
    PostingSide,
    TaxLot,
)
from augur.core.accounting_tables import AccountingTrace, AccountingTraceBuilder, validate_trace
from augur.core.action_log import (
    ASSET_CHANGE_LOG_SCHEMA,
    AssetKindForLog,
    TaxTreatment,
    derive_per_month_taxable_gain_matrix,
)
from augur.core.annual_tax import AnnualSaleTaxAllocation, annual_sale_tax_allocation
from augur.core.local_regulation import LocalRegulation
from augur.core.market_bundle import MarketBundle
from augur.core.policy_runtime import (
    ActorPolicyStep,
    BalanceSnapshotBatch,
    JournalEntryBatch,
    PrivateEquitySaleApplication,
    PrivateEquitySaleInstructionBatch,
    PrivateEquitySaleOpportunityBatch,
    SellAssetInstructionBatch,
    actor_policy_programs,
    actor_policy_steps,
    apply_crypto_sale_instruction,
    apply_debit_account_instruction,
    apply_generic_sp500_sale_instruction,
    apply_partner_house_cost_contribution,
    apply_partner_ownership_accrual,
    apply_partner_ownership_aggregate,
    apply_private_equity_sale_instruction,
    apply_property_operating_cash_flows,
    checking_floor_sell_public_stock_instruction,
    monthly_spend_debit_instruction,
    partner_contribution_instruction,
    private_equity_sale_instruction,
    private_equity_sale_opportunity,
)
from augur.core.property_depreciation import rental_active_mask
from augur.core.property_sale import (
    PropertyDispositionArrays,
    empty_property_disposition_arrays,
    property_disposition_arrays,
)
from augur.core.property_tax import monthly_property_tax_usd
from augur.core.provenance import policy_program_set_id, projection_trajectory_id, scenario_input_id
from augur.core.scenario_set import (
    AccountBalance,
    AccountingDetail,
    AccountType,
    Acquisition,
    ActorRole,
    AssetType,
    CheckingFloorSellPublicStockPolicy,
    CryptoAssetPosition,
    Effect,
    EffectType,
    FailureEvent,
    FinancingMode,
    FixedAmountPrivateEquitySaleRule,
    FundingDecision,
    FundingDecisionType,
    FundingSourceType,
    GenericSp500StockPosition,
    LiquidityEventOnly,
    LiquidNetWorthFloorPrivateEquitySaleRule,
    MarketObservation,
    MonthlySpendPolicy,
    Obligation,
    ObligationType,
    OccupancyMode,
    PartnerEquityAccrualPolicy,
    Policy,
    PolicyDecision,
    PolicyDecisionType,
    PrivateEquityPosition,
    PrivateEquitySaleDecisionReason,
    PrivateEquitySalePolicy,
    PrivateEquitySaleRule,
    PropertyPurchaseEvent,
    PublicMarket,
    RentalMode,
    ReportMetric,
    RolloutStatus,
    RolloutStatusType,
    Scenario,
    SettlementResult,
    SpecialAssessmentEvent,
    TaxPaymentTiming,
)
from augur.core.scheduled_cashflows import ScheduledCashflowKind, build_scheduled_cashflows
from augur.core.schemas import ColumnarTable
from augur.core.simulation_state import (
    AgentState,
    AssetHolding,
    AssetKind,
    LiabilityBalance,
    LiabilityKind,
    PropertyStake,
    PropertyState,
    SimulationState,
)
from augur.core.tax_actor import TaxActor

MONTHS_PER_YEAR = 12
MORTGAGE_SERVICING_POLICY_ID = "mortgage_servicing"
PROPERTY_OPERATING_CASH_FLOW_POLICY_ID = "property_operating_cash_flow"
PROPERTY_SALE_SETTLEMENT_POLICY_ID = "property_sale_settlement"
ANNUAL_TAX_ACCOUNTING_POLICY_ID = "annual_tax_accounting"
ESTIMATED_TAX_ACCOUNTING_POLICY_ID = "estimated_tax_accounting"
SPECIAL_ASSESSMENT_POLICY_ID = "special_assessment"
PROPERTY_TAX_POLICY_ID = "property_tax_obligation"
HOA_DUES_POLICY_ID = "hoa_dues_obligation"
INSURANCE_POLICY_ID = "insurance_premium_obligation"
MAINTENANCE_POLICY_ID = "maintenance_obligation"
OUTSIDE_RENT_POLICY_ID = "outside_rent_obligation"
PARTNER_CONTRIBUTION_POLICY_ID = "partner_contribution_obligation"

# IRS quarterly estimated tax due dates expressed as month offsets within a
# tax year. The simulator anchors month index 0 to January (no explicit
# calendar anchor exists today): Q1 = Apr 15 (offset 3), Q2 = Jun 15 (offset
# 5), Q3 = Sep 15 (offset 8), Q4 = Jan 15 of the following year (offset 12
# of the tax year, i.e. month 0 of year N+1).
_ESTIMATED_TAX_QUARTER_MONTH_OFFSETS: tuple[int, ...] = (3, 5, 8, 12)


def _build_numerics_frame(month_index: np.ndarray, metric_arrays: dict[str, np.ndarray]) -> pl.DataFrame:
    """Build the `ScenarioRunArrays.numerics` wide-format polars frame from
    the per-metric `(rollouts, months+1)` numpy arrays.

    Each metric value is flattened in row-major (rollout-major) order; the
    `rollout_index` / `month_index` columns are broadcast to match. 1D
    arrays (a `(months+1,)` per-month vector) are broadcast across rollouts.

    TODO(refactor-d): the polars frame is built at the very end from a dict
    of ~70 numpy ndarrays produced by separate engine stages. The natural
    Refactor D direction is to have each engine stage emit an indexed polars
    frame (keyed by `(rollout_index, month_index)`) and assemble the final
    `numerics` via `join` instead of `dict` + `_build_numerics_frame`. This
    would let intermediate engine arithmetic (e.g. the property-cash-flow
    aggregation, partner-equity ledger derivations, sale-action records)
    stay in polars throughout. See `augur/TODO.md`.
    """
    sample = next(iter(metric_arrays.values()))
    n_rollouts, n_months_plus_one = sample.shape
    columns: dict[str, np.ndarray] = {
        "rollout_index": np.repeat(np.arange(n_rollouts, dtype=np.int32), n_months_plus_one),
        "month_index": np.tile(month_index.astype(np.int32), n_rollouts),
    }
    for name, value in metric_arrays.items():
        flat = (
            value.reshape(-1)
            if value.ndim == 2
            else np.broadcast_to(value, (n_rollouts, n_months_plus_one)).reshape(-1)
        )
        columns[name] = flat
    return pl.DataFrame(columns)


@dataclass(frozen=True)
class ScenarioRunArrays:
    scenario_id: str
    scenario_label: str
    month_index: np.ndarray
    # Wide-format polars frame keyed by `(rollout_index, month_index)`
    # carrying every `ReportMetric` column (one column per metric, named
    # for the matching `ReportMetric` enum value). Reach individual metrics
    # via `metric_array(ReportMetric.X)` to get a `(rollouts, months+1)`
    # numpy view.
    numerics: pl.DataFrame
    sp500_effects_frame: pl.DataFrame
    crypto_effects_frame: pl.DataFrame
    private_equity_effects_frame: pl.DataFrame
    settle_property_sale_effects_frame: pl.DataFrame
    policy_decisions_frame: pl.DataFrame
    market_path_observations_frame: pl.DataFrame
    pe_sale_opportunity_observations_frame: pl.DataFrame
    accounting_trace: AccountingTrace
    tax_lots_frame: pl.DataFrame
    lot_dispositions_frame: pl.DataFrame
    liabilities_frame: pl.DataFrame
    property_sale_basis_gain_details_frame: pl.DataFrame
    tax_payment_allocation_details_frame: pl.DataFrame
    funding_decisions_frame: pl.DataFrame
    # Single root for the obligation lifecycle. Three Pydantic surfaces
    # (`obligations`, `settlement_results`, `failure_events`) materialize
    # from this one frame via projection/filter at end-of-run — `obligations`
    # is the full set, `settlement_results` is a column subset (different
    # status-enum class, identical string values), `failure_events` is the
    # `unpaid > 0 & required` filter with a derived `failure_event_id`.
    # See `augur/plans/event_stream_polars_refactor.md`.
    obligations_frame: pl.DataFrame

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return tuple(event_streams.materialize_obligations(self.obligations_frame))

    @property
    def funding_decisions(self) -> tuple[FundingDecision, ...]:
        return tuple(event_streams.materialize_funding_decisions(self.funding_decisions_frame))

    @property
    def lot_dispositions(self) -> tuple[LotDisposition, ...]:
        return tuple(event_streams.materialize_lot_dispositions(self.lot_dispositions_frame))

    @property
    def tax_lots(self) -> tuple[TaxLot, ...]:
        return tuple(event_streams.materialize_tax_lots(self.tax_lots_frame))

    @property
    def liabilities(self) -> tuple[LiabilityState, ...]:
        return tuple(event_streams.materialize_liabilities(self.liabilities_frame))

    @property
    def policy_decisions(self) -> tuple[PolicyDecision, ...]:
        return tuple(event_streams.materialize_policy_decisions(self.policy_decisions_frame))

    @property
    def effects(self) -> tuple[Effect, ...]:
        return tuple(
            event_streams.materialize_effects(
                {
                    EffectType.SELL_SP500: self.sp500_effects_frame,
                    EffectType.SELL_CRYPTO: self.crypto_effects_frame,
                    EffectType.SELL_PRIVATE_EQUITY: self.private_equity_effects_frame,
                    EffectType.SETTLE_PROPERTY_SALE: self.settle_property_sale_effects_frame,
                }
            )
        )

    @property
    def accounting_details(self) -> tuple[AccountingDetail, ...]:
        return tuple(
            event_streams.materialize_accounting_details(
                self.property_sale_basis_gain_details_frame, self.tax_payment_allocation_details_frame
            )
        )

    @property
    def market_observations(self) -> tuple[MarketObservation, ...]:
        return tuple(
            event_streams.materialize_market_observations(
                self.market_path_observations_frame, self.pe_sale_opportunity_observations_frame
            )
        )

    @property
    def settlement_results(self) -> tuple[SettlementResult, ...]:
        return tuple(event_streams.materialize_settlement_results(self.obligations_frame))

    @property
    def failure_events(self) -> tuple[FailureEvent, ...]:
        return tuple(event_streams.materialize_failure_events(self.obligations_frame))

    @property
    def rollout_count(self) -> int:
        # numerics is row-major (rollout, month_index): rows = rollouts × (months+1).
        return int(self.numerics.height // self.month_index.size)

    @property
    def horizon_months(self) -> int:
        return int(self.month_index.size - 1)

    def metric_array(self, metric: ReportMetric) -> np.ndarray:
        """Look up a `ReportMetric` column as a `(rollouts, months+1)` 2D
        numpy array. Polars handles the string→column dispatch."""
        if metric is ReportMetric.MONTH_INDEX:
            return self.month_index
        flat: np.ndarray = self.numerics[metric.value].to_numpy()
        return flat.reshape(self.rollout_count, self.horizon_months + 1)

    def rollout_statuses(self) -> tuple[RolloutStatus, ...]:
        cash_summary = (
            self.numerics.lazy()
            .group_by("rollout_index", maintain_order=True)
            .agg(
                min_cash_usd=pl.col("cash_usd").min(),
                first_negative_month=pl.when(pl.col("cash_usd") < 0).then(pl.col("month_index")).otherwise(None).min(),
            )
            .sort("rollout_index")
            .collect()
        )
        # Pre-aggregate failures per rollout from the long-format frame so
        # the status loop below doesn't have to iterate the (possibly huge)
        # event tuple. One row per rollout that ever produced a failure.
        failure_summary = (
            self.obligations_frame.lazy()
            .filter((pl.col("unpaid_amount_usd") > 0) & pl.col("required"))
            .group_by("rollout_index")
            .agg(
                first_failed_obligation_month_index=pl.col("month_index").min(),
                failed_obligation_count=pl.col("obligation_type").count(),
                unpaid_obligation_usd=pl.col("unpaid_amount_usd").sum(),
            )
            .collect()
        )
        failure_by_rollout: dict[int, dict[str, Any]] = {
            int(row["rollout_index"]): row for row in failure_summary.iter_rows(named=True)
        }
        statuses: list[RolloutStatus] = []
        for row in cash_summary.iter_rows(named=True):
            rollout_index = int(row["rollout_index"])
            min_cash_usd = float(row["min_cash_usd"])
            first_negative_month = row["first_negative_month"]
            failure_row = failure_by_rollout.get(rollout_index)
            if failure_row is not None:
                statuses.append(
                    RolloutStatus(
                        rollout_index=rollout_index,
                        status=RolloutStatusType.FAILED,
                        min_cash_usd=min_cash_usd,
                        first_negative_cash_month_index=(
                            int(first_negative_month) if first_negative_month is not None else None
                        ),
                        first_failed_obligation_month_index=int(failure_row["first_failed_obligation_month_index"]),
                        failed_obligation_count=int(failure_row["failed_obligation_count"]),
                        unpaid_obligation_usd=float(failure_row["unpaid_obligation_usd"]),
                    )
                )
            elif first_negative_month is None:
                statuses.append(
                    RolloutStatus(
                        rollout_index=rollout_index, status=RolloutStatusType.ACTIVE, min_cash_usd=min_cash_usd
                    )
                )
            else:
                statuses.append(
                    RolloutStatus(
                        rollout_index=rollout_index,
                        status=RolloutStatusType.CASH_NEGATIVE,
                        min_cash_usd=min_cash_usd,
                        first_negative_cash_month_index=int(first_negative_month),
                    )
                )
        return tuple(statuses)

    def monthly_columns(self) -> ColumnarTable:
        metric_names = [spec.metric.value for spec in _MONTHLY_COLUMN_SPECS]
        frame = self.numerics.select(
            pl.lit(self.scenario_id).alias("scenario_id"),
            pl.lit(self.scenario_label).alias("scenario_label"),
            pl.col("rollout_index").cast(pl.Int64),
            pl.col("month_index").cast(pl.Int64),
            *[pl.col(name) for name in metric_names],
        )
        return ColumnarTable(row_count=frame.height, columns=frame.to_dict(as_series=False))

    def terminal_columns(self) -> ColumnarTable:
        aggregates = (
            self.numerics.lazy()
            .group_by("rollout_index", maintain_order=True)
            .agg(*[_terminal_agg_expr(spec) for spec in _TERMINAL_COLUMN_SPECS])
            .sort("rollout_index")
            .with_columns(
                pl.col("rollout_index").cast(pl.Int64),
                pl.lit(self.scenario_id).alias("scenario_id"),
                pl.lit(self.scenario_label).alias("scenario_label"),
                pl.lit(int(self.month_index[-1]), dtype=pl.Int64).alias("month_index"),
            )
            .select(
                "scenario_id",
                "scenario_label",
                "rollout_index",
                "month_index",
                *(spec.output_name for spec in _TERMINAL_COLUMN_SPECS),
            )
            .collect()
        )
        return ColumnarTable(row_count=aggregates.height, columns=aggregates.to_dict(as_series=False))

    def metric_fan_columns(self) -> dict[str, ColumnarTable]:
        return {name: _fan_columns(self.metric_array(ReportMetric(name))) for name in _FAN_METRIC_NAMES}


class MonthlyColumnSource(StrEnum):
    TRAJECTORY_STATE = "trajectory_state"
    MARKET_OBSERVATION = "market_observation"
    LEDGER_ENTRY = "ledger_entry"
    BALANCE_SNAPSHOT = "balance_snapshot"
    ACCOUNTING_DETAIL = "accounting_detail"
    REPORT_PROJECTION = "report_projection"


@dataclass(frozen=True)
class MonthlyColumnSpec:
    metric: ReportMetric
    source: MonthlyColumnSource
    note: str


_MONTHLY_COLUMN_SPECS = (
    MonthlyColumnSpec(ReportMetric.CASH_USD, MonthlyColumnSource.TRAJECTORY_STATE, "projected cash state"),
    MonthlyColumnSpec(
        ReportMetric.GENERIC_SP500_VALUE_USD, MonthlyColumnSource.TRAJECTORY_STATE, "projected public-stock state"
    ),
    MonthlyColumnSpec(
        ReportMetric.GENERIC_SP500_SALE_USD, MonthlyColumnSource.LEDGER_ENTRY, "asset/generic_sp500_sale"
    ),
    MonthlyColumnSpec(
        ReportMetric.GENERIC_SP500_SALE_BASIS_USD, MonthlyColumnSource.LEDGER_ENTRY, "basis/generic_sp500_sale_basis"
    ),
    MonthlyColumnSpec(ReportMetric.GENERIC_SP500_SALE_GAIN_USD, MonthlyColumnSource.REPORT_PROJECTION, "sale - basis"),
    MonthlyColumnSpec(
        ReportMetric.GENERIC_SP500_SALE_TAX_USD, MonthlyColumnSource.LEDGER_ENTRY, "tax/generic_sp500_sale_tax"
    ),
    MonthlyColumnSpec(ReportMetric.CRYPTO_VALUE_USD, MonthlyColumnSource.TRAJECTORY_STATE, "projected crypto state"),
    MonthlyColumnSpec(ReportMetric.CRYPTO_SALE_USD, MonthlyColumnSource.LEDGER_ENTRY, "asset/crypto_sale"),
    MonthlyColumnSpec(ReportMetric.CRYPTO_SALE_BASIS_USD, MonthlyColumnSource.LEDGER_ENTRY, "basis/crypto_sale_basis"),
    MonthlyColumnSpec(ReportMetric.CRYPTO_SALE_GAIN_USD, MonthlyColumnSource.REPORT_PROJECTION, "sale - basis"),
    MonthlyColumnSpec(
        ReportMetric.CRYPTO_SALE_TAX_USD, MonthlyColumnSource.LEDGER_ENTRY, "tax/generic_crypto_sale_tax"
    ),
    MonthlyColumnSpec(
        ReportMetric.CHECKING_FLOOR_ACTION_USD, MonthlyColumnSource.LEDGER_ENTRY, "asset/generic_sp500_sale"
    ),
    MonthlyColumnSpec(
        ReportMetric.CHECKING_FLOOR_SHORTFALL_USD, MonthlyColumnSource.REPORT_PROJECTION, "policy shortfall output"
    ),
    MonthlyColumnSpec(
        ReportMetric.PRIVATE_EQUITY_VALUE_USD, MonthlyColumnSource.TRAJECTORY_STATE, "projected private-equity state"
    ),
    MonthlyColumnSpec(
        ReportMetric.PRIVATE_EQUITY_SALE_OPPORTUNITY_VALUE_USD,
        MonthlyColumnSource.REPORT_PROJECTION,
        "private-equity sale opportunity stream minus applied sales",
    ),
    MonthlyColumnSpec(
        ReportMetric.PRIVATE_EQUITY_SALE_USD, MonthlyColumnSource.LEDGER_ENTRY, "asset/private_equity_sale"
    ),
    MonthlyColumnSpec(
        ReportMetric.PRIVATE_EQUITY_SALE_BASIS_USD, MonthlyColumnSource.LEDGER_ENTRY, "basis/private_equity_sale_basis"
    ),
    MonthlyColumnSpec(
        ReportMetric.PRIVATE_EQUITY_SALE_TAX_USD, MonthlyColumnSource.LEDGER_ENTRY, "tax/private_equity_sale_tax"
    ),
    MonthlyColumnSpec(
        ReportMetric.RENTAL_INCOME_TAX_USD,
        MonthlyColumnSource.ACCOUNTING_DETAIL,
        "tax_payment_allocation.rental_income_tax_usd",
    ),
    MonthlyColumnSpec(
        ReportMetric.FEDERAL_INCOME_TAX_USD,
        MonthlyColumnSource.ACCOUNTING_DETAIL,
        "tax_payment_allocation.federal_income_tax_usd",
    ),
    MonthlyColumnSpec(
        ReportMetric.CALIFORNIA_INCOME_TAX_USD,
        MonthlyColumnSource.ACCOUNTING_DETAIL,
        "tax_payment_allocation.california_income_tax_usd",
    ),
    MonthlyColumnSpec(
        ReportMetric.TOTAL_INCOME_TAX_USD,
        MonthlyColumnSource.ACCOUNTING_DETAIL,
        "tax_payment_allocation.total_income_tax_usd",
    ),
    MonthlyColumnSpec(
        ReportMetric.PRIVATE_EQUITY_SALE_OPPORTUNITY_EVENT,
        MonthlyColumnSource.MARKET_OBSERVATION,
        "private-equity sale opportunity event",
    ),
    MonthlyColumnSpec(
        ReportMetric.PROPERTY_VALUE_USD, MonthlyColumnSource.TRAJECTORY_STATE, "projected property state"
    ),
    MonthlyColumnSpec(
        ReportMetric.MORTGAGE_BALANCE_USD, MonthlyColumnSource.TRAJECTORY_STATE, "projected mortgage state"
    ),
    MonthlyColumnSpec(ReportMetric.MORTGAGE_INTEREST_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/mortgage_interest"),
    MonthlyColumnSpec(ReportMetric.MORTGAGE_PRINCIPAL_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/mortgage_principal"),
    MonthlyColumnSpec(ReportMetric.MORTGAGE_PAYMENT_USD, MonthlyColumnSource.REPORT_PROJECTION, "interest + principal"),
    MonthlyColumnSpec(ReportMetric.PROPERTY_TAX_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/property_tax"),
    MonthlyColumnSpec(ReportMetric.HOA_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/hoa"),
    MonthlyColumnSpec(ReportMetric.INSURANCE_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/insurance"),
    MonthlyColumnSpec(ReportMetric.MAINTENANCE_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/maintenance"),
    MonthlyColumnSpec(ReportMetric.RENTAL_INCOME_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/rental_income"),
    MonthlyColumnSpec(
        ReportMetric.RENTAL_MANAGEMENT_FEE_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/rental_management_fee"
    ),
    MonthlyColumnSpec(ReportMetric.RENTAL_LEASING_FEE_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/rental_leasing_fee"),
    MonthlyColumnSpec(
        ReportMetric.PROPERTY_CARRYING_COST_USD,
        MonthlyColumnSource.REPORT_PROJECTION,
        "tax + hoa + insurance + maintenance + rental fees",
    ),
    MonthlyColumnSpec(
        ReportMetric.NET_PROPERTY_CASH_FLOW_USD,
        MonthlyColumnSource.REPORT_PROJECTION,
        "rental income - carrying cost - mortgage payment",
    ),
    MonthlyColumnSpec(
        ReportMetric.PURCHASE_CLOSING_COST_USD, MonthlyColumnSource.TRAJECTORY_STATE, "property purchase state"
    ),
    MonthlyColumnSpec(
        ReportMetric.SALE_CLOSING_COST_USD, MonthlyColumnSource.LEDGER_ENTRY, "property_sale/sale_closing_cost"
    ),
    MonthlyColumnSpec(
        ReportMetric.PROPERTY_DEPRECIATION_USD, MonthlyColumnSource.TRAJECTORY_STATE, "depreciation schedule state"
    ),
    MonthlyColumnSpec(
        ReportMetric.CUMULATIVE_PROPERTY_DEPRECIATION_USD,
        MonthlyColumnSource.TRAJECTORY_STATE,
        "depreciation schedule state",
    ),
    MonthlyColumnSpec(
        ReportMetric.PROPERTY_SALE_GROSS_USD, MonthlyColumnSource.LEDGER_ENTRY, "property_sale/property_sale_gross"
    ),
    MonthlyColumnSpec(
        ReportMetric.PROPERTY_SALE_NET_PROCEEDS_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/property_sale_net_proceeds"
    ),
    MonthlyColumnSpec(
        ReportMetric.PROPERTY_SALE_TAX_USD,
        MonthlyColumnSource.LEDGER_ENTRY,
        "lot_disposition.property.tax_expense_usd (accrual provenance; settled at year-end)",
    ),
    MonthlyColumnSpec(
        ReportMetric.PROPERTY_SALE_DEBT_PAYOFF_USD,
        MonthlyColumnSource.LEDGER_ENTRY,
        "property_sale/property_sale_debt_payoff",
    ),
    MonthlyColumnSpec(
        ReportMetric.PROPERTY_SALE_ADJUSTED_BASIS_USD,
        MonthlyColumnSource.ACCOUNTING_DETAIL,
        "property_sale_basis_gain.adjusted_basis_usd",
    ),
    MonthlyColumnSpec(
        ReportMetric.REALIZED_PROPERTY_GAIN_USD,
        MonthlyColumnSource.ACCOUNTING_DETAIL,
        "property_sale_basis_gain.realized_gain_usd",
    ),
    MonthlyColumnSpec(
        ReportMetric.PROPERTY_SALE_CAPITAL_GAIN_USD,
        MonthlyColumnSource.ACCOUNTING_DETAIL,
        "property_sale_basis_gain.capital_gain_usd",
    ),
    MonthlyColumnSpec(
        ReportMetric.PROPERTY_SALE_CAPITAL_GAIN_EXCLUSION_USD,
        MonthlyColumnSource.ACCOUNTING_DETAIL,
        "property_sale_basis_gain.capital_gain_exclusion_usd",
    ),
    MonthlyColumnSpec(
        ReportMetric.TAXABLE_PROPERTY_CAPITAL_GAIN_USD,
        MonthlyColumnSource.ACCOUNTING_DETAIL,
        "property_sale_basis_gain.taxable_capital_gain_usd",
    ),
    MonthlyColumnSpec(
        ReportMetric.TAXABLE_PROPERTY_GAIN_USD,
        MonthlyColumnSource.ACCOUNTING_DETAIL,
        "property_sale_basis_gain.taxable_gain_usd",
    ),
    MonthlyColumnSpec(
        ReportMetric.DEPRECIATION_RECAPTURE_USD,
        MonthlyColumnSource.ACCOUNTING_DETAIL,
        "property_sale_basis_gain.depreciation_recapture_usd",
    ),
    MonthlyColumnSpec(
        ReportMetric.NET_PROPERTY_SALE_CASH_FLOW_USD,
        MonthlyColumnSource.LEDGER_ENTRY,
        "cash/property_sale_net_proceeds",
    ),
    MonthlyColumnSpec(ReportMetric.HOME_EQUITY_USD, MonthlyColumnSource.TRAJECTORY_STATE, "property - mortgage"),
    MonthlyColumnSpec(
        ReportMetric.OWNER_HOME_EQUITY_CLAIM_USD,
        MonthlyColumnSource.BALANCE_SNAPSHOT,
        "ownership/owner_home_equity_claim",
    ),
    MonthlyColumnSpec(
        ReportMetric.PARTNER_HOME_EQUITY_CLAIM_USD,
        MonthlyColumnSource.BALANCE_SNAPSHOT,
        "ownership/partner_home_equity_claim",
    ),
    MonthlyColumnSpec(
        ReportMetric.PARTNER_CONTRIBUTION_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/partner_contribution_transfer"
    ),
    MonthlyColumnSpec(
        ReportMetric.PARTNER_CONTRIBUTION_USED_USD,
        MonthlyColumnSource.LEDGER_ENTRY,
        "cash/partner_contribution_used_for_house_costs",
    ),
    MonthlyColumnSpec(
        ReportMetric.PARTNER_UNALLOCATED_EXCESS_USD,
        MonthlyColumnSource.LEDGER_ENTRY,
        "escrow/partner_contribution_unallocated",
    ),
    MonthlyColumnSpec(
        ReportMetric.PARTNER_HOUSE_COSTS_USD,
        MonthlyColumnSource.REPORT_PROJECTION,
        "partner-eligible house costs before contribution allocation",
    ),
    MonthlyColumnSpec(
        ReportMetric.PARTNER_PRINCIPAL_CREDIT_USD,
        MonthlyColumnSource.LEDGER_ENTRY,
        "ownership/partner_principal_credit",
    ),
    MonthlyColumnSpec(
        ReportMetric.OWNER_PRINCIPAL_CREDIT_USD, MonthlyColumnSource.LEDGER_ENTRY, "ownership/owner_principal_credit"
    ),
    MonthlyColumnSpec(
        ReportMetric.PARTNER_HOUSE_COST_SHARE,
        MonthlyColumnSource.REPORT_PROJECTION,
        "partner contribution used / partner-eligible house costs",
    ),
    MonthlyColumnSpec(
        ReportMetric.PARTNER_EQUITY_LEDGER_USD, MonthlyColumnSource.BALANCE_SNAPSHOT, "ownership/partner_equity_ledger"
    ),
    MonthlyColumnSpec(
        ReportMetric.OWNER_EQUITY_LEDGER_USD, MonthlyColumnSource.BALANCE_SNAPSHOT, "ownership/owner_equity_ledger"
    ),
    MonthlyColumnSpec(
        ReportMetric.PARTNER_OWNERSHIP_PCT,
        MonthlyColumnSource.REPORT_PROJECTION,
        "partner claim / positive home equity",
    ),
    MonthlyColumnSpec(
        ReportMetric.LIQUID_NET_WORTH_USD, MonthlyColumnSource.REPORT_PROJECTION, "cash + public stock + crypto"
    ),
    MonthlyColumnSpec(
        ReportMetric.NET_WORTH_USD,
        MonthlyColumnSource.REPORT_PROJECTION,
        "cash + public stock + crypto + private equity + home",
    ),
    MonthlyColumnSpec(ReportMetric.PARTNER_PRESENT, MonthlyColumnSource.TRAJECTORY_STATE, "scenario actor state"),
    MonthlyColumnSpec(ReportMetric.MONTHLY_SPEND_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/monthly_spend"),
)


def monthly_column_specs() -> tuple[MonthlyColumnSpec, ...]:
    return _MONTHLY_COLUMN_SPECS


def available_report_metrics() -> tuple[ReportMetric, ...]:
    return tuple(ReportMetric)


class _TerminalAggregation(StrEnum):
    FINAL = "final"  # pl.col(metric).last()
    TOTAL = "total"  # pl.col(metric).sum()


@dataclass(frozen=True)
class _TerminalSpec:
    output_name: str
    source_metric: str
    aggregation: _TerminalAggregation


# Output specs for `ScenarioRunArrays.terminal_columns`. Order is preserved in
# the emitted `ColumnarTable`; the `output_name`/`source_metric` split is
# explicit because some source metrics already begin with `total_`
# (e.g. `total_income_tax_usd`), so a `f"total_{metric}"` naming convention
# would produce wrong keys.
_TERMINAL_COLUMN_SPECS: tuple[_TerminalSpec, ...] = (
    _TerminalSpec("final_cash_usd", "cash_usd", _TerminalAggregation.FINAL),
    _TerminalSpec("final_generic_sp500_value_usd", "generic_sp500_value_usd", _TerminalAggregation.FINAL),
    _TerminalSpec("total_generic_sp500_sale_usd", "generic_sp500_sale_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_generic_sp500_sale_basis_usd", "generic_sp500_sale_basis_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_generic_sp500_sale_gain_usd", "generic_sp500_sale_gain_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_generic_sp500_sale_tax_usd", "generic_sp500_sale_tax_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_crypto_sale_tax_usd", "crypto_sale_tax_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("final_checking_floor_shortfall_usd", "checking_floor_shortfall_usd", _TerminalAggregation.FINAL),
    _TerminalSpec("final_private_equity_value_usd", "private_equity_value_usd", _TerminalAggregation.FINAL),
    _TerminalSpec(
        "final_private_equity_sale_opportunity_value_usd",
        "private_equity_sale_opportunity_value_usd",
        _TerminalAggregation.FINAL,
    ),
    _TerminalSpec("total_private_equity_sale_usd", "private_equity_sale_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_private_equity_sale_basis_usd", "private_equity_sale_basis_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_private_equity_sale_tax_usd", "private_equity_sale_tax_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_federal_income_tax_usd", "federal_income_tax_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_california_income_tax_usd", "california_income_tax_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_income_tax_usd", "total_income_tax_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("final_property_value_usd", "property_value_usd", _TerminalAggregation.FINAL),
    _TerminalSpec("final_mortgage_balance_usd", "mortgage_balance_usd", _TerminalAggregation.FINAL),
    _TerminalSpec("final_home_equity_usd", "home_equity_usd", _TerminalAggregation.FINAL),
    _TerminalSpec("final_owner_home_equity_claim_usd", "owner_home_equity_claim_usd", _TerminalAggregation.FINAL),
    _TerminalSpec("final_partner_home_equity_claim_usd", "partner_home_equity_claim_usd", _TerminalAggregation.FINAL),
    _TerminalSpec("final_partner_ownership_pct", "partner_ownership_pct", _TerminalAggregation.FINAL),
    _TerminalSpec("total_partner_contribution_used_usd", "partner_contribution_used_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_partner_principal_credit_usd", "partner_principal_credit_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_owner_principal_credit_usd", "owner_principal_credit_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("final_partner_equity_ledger_usd", "partner_equity_ledger_usd", _TerminalAggregation.FINAL),
    _TerminalSpec("final_owner_equity_ledger_usd", "owner_equity_ledger_usd", _TerminalAggregation.FINAL),
    _TerminalSpec("total_rental_income_usd", "rental_income_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_property_carrying_cost_usd", "property_carrying_cost_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_net_property_cash_flow_usd", "net_property_cash_flow_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_purchase_closing_cost_usd", "purchase_closing_cost_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_sale_closing_cost_usd", "sale_closing_cost_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_property_depreciation_usd", "property_depreciation_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec(
        "final_cumulative_property_depreciation_usd", "cumulative_property_depreciation_usd", _TerminalAggregation.FINAL
    ),
    _TerminalSpec("total_property_sale_gross_usd", "property_sale_gross_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_property_sale_net_proceeds_usd", "property_sale_net_proceeds_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_property_sale_tax_usd", "property_sale_tax_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_property_sale_debt_payoff_usd", "property_sale_debt_payoff_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec(
        "total_property_sale_adjusted_basis_usd", "property_sale_adjusted_basis_usd", _TerminalAggregation.TOTAL
    ),
    _TerminalSpec("total_realized_property_gain_usd", "realized_property_gain_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_property_sale_capital_gain_usd", "property_sale_capital_gain_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec(
        "total_property_sale_capital_gain_exclusion_usd",
        "property_sale_capital_gain_exclusion_usd",
        _TerminalAggregation.TOTAL,
    ),
    _TerminalSpec(
        "total_taxable_property_capital_gain_usd", "taxable_property_capital_gain_usd", _TerminalAggregation.TOTAL
    ),
    _TerminalSpec("total_taxable_property_gain_usd", "taxable_property_gain_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec("total_depreciation_recapture_usd", "depreciation_recapture_usd", _TerminalAggregation.TOTAL),
    _TerminalSpec(
        "total_net_property_sale_cash_flow_usd", "net_property_sale_cash_flow_usd", _TerminalAggregation.TOTAL
    ),
    _TerminalSpec("final_liquid_net_worth_usd", "liquid_net_worth_usd", _TerminalAggregation.FINAL),
    _TerminalSpec("final_net_worth_usd", "net_worth_usd", _TerminalAggregation.FINAL),
)


def _terminal_agg_expr(spec: _TerminalSpec) -> pl.Expr:
    col = pl.col(spec.source_metric)
    reducer = col.last() if spec.aggregation is _TerminalAggregation.FINAL else col.sum()
    return reducer.alias(spec.output_name)


# Metric names that participate in `ScenarioRunArrays.metric_fan_columns`,
# emitted as `(rollouts, months+1)` matrices reduced to per-month percentile
# tables for the UI's fan-chart views.
_FAN_METRIC_NAMES: tuple[str, ...] = (
    "cash_usd",
    "net_worth_usd",
    "liquid_net_worth_usd",
    "generic_sp500_value_usd",
    "checking_floor_shortfall_usd",
    "property_value_usd",
    "home_equity_usd",
    "owner_home_equity_claim_usd",
    "partner_home_equity_claim_usd",
    "partner_principal_credit_usd",
    "partner_equity_ledger_usd",
    "owner_equity_ledger_usd",
    "partner_ownership_pct",
    "mortgage_balance_usd",
    "rental_income_usd",
    "net_property_cash_flow_usd",
    "property_sale_net_proceeds_usd",
    "net_property_sale_cash_flow_usd",
    "private_equity_value_usd",
    "private_equity_sale_opportunity_value_usd",
)


# Column names carried inside `PropertyCashFlowArrays.numerics`, in display
# order. Kept as a module constant so the producer + `column()` accessor can
# enforce one shared schema without re-declaring it inline.
_PROPERTY_CASH_FLOW_COLUMNS: tuple[str, ...] = (
    "mortgage_payment_usd",
    "property_tax_usd",
    "hoa_usd",
    "insurance_usd",
    "maintenance_usd",
    "rental_income_usd",
    "rental_management_fee_usd",
    "rental_leasing_fee_usd",
    "property_carrying_cost_usd",
    "net_property_cash_flow_usd",
)


@dataclass(frozen=True)
class PropertyCashFlowArrays:
    """Per-rollout-per-month property cash flow inputs as a wide polars frame.

    Each entry in `_PROPERTY_CASH_FLOW_COLUMNS` is a polars column of the
    `numerics` frame, flattened from a `(rollouts, months+1)` ndarray in
    rollout-major order. `column(name)` returns the named column reshaped
    back to its 2D form; this is the Refactor-D-style polars-canonical
    storage applied to one engine intermediate (`PropertyCashFlowArrays`
    was the demo target). Other engine intermediates carrying per-rollout
    per-month ndarrays — `PartnerEquityArrays`, `PartnerEquityAgreementArrays`,
    `Sp500SaleActionRecord`, `CryptoSaleActionRecord`,
    `PropertyDispositionArrays` (in `property_sale`), and the
    `metric_arrays` assembly inside `run_scenario_vectorized` itself —
    follow the same shape and should migrate to this pattern; see
    `augur/TODO.md` and `augur/plans/plan-it-out-stateless-snowglobe.md`
    for the rollup.
    """

    rollout_count: int
    horizon_months: int
    numerics: pl.DataFrame
    journal_entries: tuple[JournalEntryBatch, ...]

    def column(self, name: str) -> np.ndarray:
        flat: np.ndarray = self.numerics[name].to_numpy()
        return flat.reshape(self.rollout_count, self.horizon_months + 1)


def _build_property_cash_flow_frame(arrays: dict[str, np.ndarray]) -> pl.DataFrame:
    """Build a `PropertyCashFlowArrays.numerics` frame from a dict of
    `(rollouts, months+1)` ndarrays, one per column in
    `_PROPERTY_CASH_FLOW_COLUMNS`. Flattens row-major (rollout-major) so
    `column()` can reshape back without copy."""
    sample = next(iter(arrays.values()))
    n_rollouts, n_months_plus_one = sample.shape
    return pl.DataFrame(
        {
            "rollout_index": np.repeat(np.arange(n_rollouts, dtype=np.int32), n_months_plus_one),
            "month_index": np.tile(np.arange(n_months_plus_one, dtype=np.int32), n_rollouts),
            **{name: arrays[name].reshape(-1) for name in _PROPERTY_CASH_FLOW_COLUMNS},
        }
    )


# Column names carried in `PartnerEquity*Arrays.numerics`, identical across
# the agreement-scoped and aggregate-scoped variants.
_PARTNER_EQUITY_COLUMNS: tuple[str, ...] = (
    "contribution_usd",
    "contribution_used_usd",
    "unallocated_excess_usd",
    "house_costs_usd",
    "mortgage_payment_usd",
    "mortgage_interest_usd",
    "mortgage_principal_usd",
    "principal_credit_usd",
    "owner_principal_usd",
    "house_cost_share",
    "partner_equity_ledger_usd",
    "owner_equity_ledger_usd",
    "ownership_pct",
    "home_equity_claim_usd",
    "owner_home_equity_claim_usd",
)


def _build_partner_equity_frame(arrays: dict[str, np.ndarray]) -> pl.DataFrame:
    """Build a `PartnerEquity*Arrays.numerics` frame from a dict of
    `(rollouts, months+1)` ndarrays, one per column in
    `_PARTNER_EQUITY_COLUMNS`. Flattens rollout-major so `column()` can
    reshape back without copy."""
    sample = next(iter(arrays.values()))
    n_rollouts, n_months_plus_one = sample.shape
    return pl.DataFrame(
        {
            "rollout_index": np.repeat(np.arange(n_rollouts, dtype=np.int32), n_months_plus_one),
            "month_index": np.tile(np.arange(n_months_plus_one, dtype=np.int32), n_rollouts),
            **{name: arrays[name].reshape(-1) for name in _PARTNER_EQUITY_COLUMNS},
        }
    )


@dataclass(frozen=True)
class PartnerEquityAgreementArrays:
    """Per-agreement partner-equity arrays, with the 15 per-rollout-per-month
    numeric columns held in `numerics` (see `_PARTNER_EQUITY_COLUMNS`)."""

    policy_sequence_index: int
    policy: PartnerEquityAccrualPolicy
    property_id: str
    recipient_actor_id: str
    rollout_count: int
    horizon_months: int
    numerics: pl.DataFrame
    journal_entries: tuple[JournalEntryBatch, ...]
    balance_snapshots: tuple[BalanceSnapshotBatch, ...]

    def column(self, name: str) -> np.ndarray:
        flat: np.ndarray = self.numerics[name].to_numpy()
        return flat.reshape(self.rollout_count, self.horizon_months + 1)

    def with_numerics(self, **updates: np.ndarray) -> PartnerEquityAgreementArrays:
        """Return a new instance with the named columns replaced and the
        rest carried forward unchanged from the existing frame."""
        cols = {name: self.column(name) for name in _PARTNER_EQUITY_COLUMNS}
        cols.update(updates)
        return replace(self, numerics=_build_partner_equity_frame(cols))


@dataclass(frozen=True)
class PartnerEquityArrays:
    """Aggregate (across all agreements) partner-equity arrays, with the 15
    per-rollout-per-month numeric columns held in `numerics` (see
    `_PARTNER_EQUITY_COLUMNS`)."""

    rollout_count: int
    horizon_months: int
    numerics: pl.DataFrame
    agreements: tuple[PartnerEquityAgreementArrays, ...]
    journal_entries: tuple[JournalEntryBatch, ...]
    balance_snapshots: tuple[BalanceSnapshotBatch, ...]

    def column(self, name: str) -> np.ndarray:
        flat: np.ndarray = self.numerics[name].to_numpy()
        return flat.reshape(self.rollout_count, self.horizon_months + 1)

    def with_numerics(self, **updates: np.ndarray) -> PartnerEquityArrays:
        cols = {name: self.column(name) for name in _PARTNER_EQUITY_COLUMNS}
        cols.update(updates)
        return replace(self, numerics=_build_partner_equity_frame(cols))


_SP500_SALE_ACTION_COLUMNS: tuple[str, ...] = ("amount_usd", "basis_usd", "shortfall_usd")
_CRYPTO_SALE_ACTION_COLUMNS: tuple[str, ...] = ("amount_usd", "basis_usd", "quantity_sold", "shortfall_usd")


def _build_sale_action_frame(arrays: dict[str, np.ndarray], columns: tuple[str, ...]) -> pl.DataFrame:
    """Build a per-rollout sale-action frame. Each entry in `arrays` is a
    `(rollouts,)` ndarray; the resulting frame is keyed by `rollout_index`
    with one column per name in `columns`."""
    sample = arrays[columns[0]]
    n_rollouts = sample.shape[0]
    return pl.DataFrame(
        {"rollout_index": np.arange(n_rollouts, dtype=np.int32), **{name: arrays[name] for name in columns}}
    )


@dataclass(frozen=True)
class Sp500SaleActionRecord:
    """One SP500 sale action emitted in month `month_index`. The per-rollout
    numeric columns live in `numerics` (a polars frame keyed by
    `rollout_index`); reach them via `column(name)`."""

    month_position: int
    month_index: int
    policy: Policy
    cause_id_prefix: str
    numerics: pl.DataFrame

    def column(self, name: str) -> np.ndarray:
        return self.numerics[name].to_numpy()


@dataclass(frozen=True)
class CryptoSaleActionRecord:
    """One crypto sale action emitted in month `month_index`. Per-rollout
    columns live in `numerics`; the asset-identifying scalars stay as
    separate fields."""

    month_position: int
    month_index: int
    policy: Policy
    cause_id_prefix: str
    source_asset_id: str
    asset_symbol: str
    numerics: pl.DataFrame

    def column(self, name: str) -> np.ndarray:
        return self.numerics[name].to_numpy()


@dataclass(frozen=True)
class PrivateEquitySaleActionRecord:
    month_position: int
    month_index: int
    instruction: PrivateEquitySaleInstructionBatch
    sale_application: PrivateEquitySaleApplication


def _asset_change_log_row_block(
    *,
    rollout_indices: np.ndarray,
    month_indices: np.ndarray,
    actor_ids: list[str],
    asset_id: str,
    asset_kind_value: str,
    delta_units: np.ndarray,
    delta_basis_usd: np.ndarray,
    cash_proceeds_usd: np.ndarray,
    taxable_gain_usd: np.ndarray,
    tax_treatment_value: str | None,
    cause_kind: str,
    cause_ids: list[str],
) -> pl.DataFrame:
    """Construct one column-block frame matching `ASSET_CHANGE_LOG_SCHEMA`.

    All arrays must be 1D with matching length; per-row scalar fields
    (asset_id, asset_kind_value, tax_treatment_value, cause_kind) are
    broadcast to that length."""
    size = int(rollout_indices.size)
    return pl.DataFrame(
        {
            "rollout_index": rollout_indices.astype(np.int64),
            "month_index": month_indices.astype(np.int64),
            "actor_id": actor_ids,
            "asset_id": [asset_id] * size,
            "asset_kind": [asset_kind_value] * size,
            "delta_units": delta_units.astype(np.float64),
            "delta_basis_usd": delta_basis_usd.astype(np.float64),
            "cash_proceeds_usd": cash_proceeds_usd.astype(np.float64),
            "taxable_gain_usd": taxable_gain_usd.astype(np.float64),
            "tax_treatment": [tax_treatment_value] * size,
            "cause_kind": [cause_kind] * size,
            "cause_id": cause_ids,
        },
        schema=ASSET_CHANGE_LOG_SCHEMA,
    )


def _sp500_records_to_asset_change_block(
    records: Sequence[Sp500SaleActionRecord], *, asset_id: str, primary_owner_actor_id: str
) -> pl.DataFrame | None:
    """Convert SP500 sale records (which carry amount_usd, basis_usd
    per-rollout but no unit count) into an asset_change_log column
    block. One row per (rollout, record) where the sale was active.
    `delta_units` is zero because the records don't carry unit deltas."""
    if not records:
        return None
    rollout_blocks: list[np.ndarray] = []
    month_blocks: list[np.ndarray] = []
    amount_blocks: list[np.ndarray] = []
    basis_blocks: list[np.ndarray] = []
    cause_ids: list[str] = []
    for record in records:
        amount = record.column("amount_usd")
        basis = record.column("basis_usd")
        active = (amount != 0.0) | (basis != 0.0)
        active_idx = np.nonzero(active)[0]
        if active_idx.size == 0:
            continue
        rollout_blocks.append(active_idx)
        month_blocks.append(np.full(active_idx.size, record.month_index, dtype=np.int64))
        amount_blocks.append(amount[active_idx])
        basis_blocks.append(basis[active_idx])
        cause_ids.extend([record.cause_id_prefix] * int(active_idx.size))
    if not rollout_blocks:
        return None
    rollout_indices = np.concatenate(rollout_blocks)
    month_indices = np.concatenate(month_blocks)
    amounts = np.concatenate(amount_blocks)
    basis = np.concatenate(basis_blocks)
    size = int(rollout_indices.size)
    return _asset_change_log_row_block(
        rollout_indices=rollout_indices,
        month_indices=month_indices,
        actor_ids=[primary_owner_actor_id] * size,
        asset_id=asset_id,
        asset_kind_value=AssetKindForLog.GENERIC_SP500.value,
        delta_units=np.zeros(size, dtype=np.float64),
        delta_basis_usd=-basis,
        cash_proceeds_usd=amounts,
        taxable_gain_usd=amounts - basis,
        tax_treatment_value=TaxTreatment.LONG_TERM_CAPITAL.value,
        cause_kind="OBLIGATION_OR_POLICY_SALE",
        cause_ids=cause_ids,
    )


def _crypto_records_to_asset_change_block(
    records: Sequence[CryptoSaleActionRecord], *, primary_owner_actor_id: str
) -> pl.DataFrame | None:
    """Convert crypto sale records into an asset_change_log column
    block. Crypto records carry quantity_sold per-rollout (unlike SP500),
    so `delta_units` is populated."""
    if not records:
        return None
    rollout_blocks: list[np.ndarray] = []
    month_blocks: list[np.ndarray] = []
    amount_blocks: list[np.ndarray] = []
    basis_blocks: list[np.ndarray] = []
    qty_blocks: list[np.ndarray] = []
    cause_ids: list[str] = []
    asset_ids: list[str] = []
    for record in records:
        amount = record.column("amount_usd")
        basis = record.column("basis_usd")
        quantity = record.column("quantity_sold")
        active = (amount != 0.0) | (basis != 0.0) | (quantity != 0.0)
        active_idx = np.nonzero(active)[0]
        if active_idx.size == 0:
            continue
        rollout_blocks.append(active_idx)
        month_blocks.append(np.full(active_idx.size, record.month_index, dtype=np.int64))
        amount_blocks.append(amount[active_idx])
        basis_blocks.append(basis[active_idx])
        qty_blocks.append(quantity[active_idx])
        cause_ids.extend([record.cause_id_prefix] * int(active_idx.size))
        asset_ids.extend([record.source_asset_id] * int(active_idx.size))
    if not rollout_blocks:
        return None
    rollout_indices = np.concatenate(rollout_blocks)
    month_indices = np.concatenate(month_blocks)
    amounts = np.concatenate(amount_blocks)
    basis = np.concatenate(basis_blocks)
    quantities = np.concatenate(qty_blocks)
    size = int(rollout_indices.size)
    # Crypto records carry varying asset_ids (per-symbol), so we can't use
    # the simple broadcast helper — emit the asset_id column directly.
    return pl.DataFrame(
        {
            "rollout_index": rollout_indices.astype(np.int64),
            "month_index": month_indices.astype(np.int64),
            "actor_id": [primary_owner_actor_id] * size,
            "asset_id": asset_ids,
            "asset_kind": [AssetKindForLog.CRYPTO.value] * size,
            "delta_units": -quantities,
            "delta_basis_usd": -basis,
            "cash_proceeds_usd": amounts,
            "taxable_gain_usd": amounts - basis,
            "tax_treatment": [TaxTreatment.LONG_TERM_CAPITAL.value] * size,
            "cause_kind": ["OBLIGATION_OR_POLICY_SALE"] * size,
            "cause_id": cause_ids,
        },
        schema=ASSET_CHANGE_LOG_SCHEMA,
    )


def _pe_records_to_asset_change_block(
    records: Sequence[PrivateEquitySaleActionRecord], *, primary_owner_actor_id: str
) -> pl.DataFrame | None:
    """Convert PE sale records into an asset_change_log column block.
    PE records carry sold_units + basis_usd + sale_usd + taxable_gain_usd
    on the sale_application."""
    if not records:
        return None
    rollout_blocks: list[np.ndarray] = []
    month_blocks: list[np.ndarray] = []
    sale_blocks: list[np.ndarray] = []
    basis_blocks: list[np.ndarray] = []
    units_blocks: list[np.ndarray] = []
    gain_blocks: list[np.ndarray] = []
    cause_ids: list[str] = []
    for record in records:
        sale = record.sale_application.sale_usd
        basis = record.sale_application.basis_usd
        units = record.sale_application.sold_units
        gain = record.sale_application.taxable_gain_usd
        active = (sale != 0.0) | (basis != 0.0) | (units != 0.0)
        active_idx = np.nonzero(active)[0]
        if active_idx.size == 0:
            continue
        rollout_blocks.append(active_idx)
        month_blocks.append(np.full(active_idx.size, record.month_index, dtype=np.int64))
        sale_blocks.append(sale[active_idx])
        basis_blocks.append(basis[active_idx])
        units_blocks.append(units[active_idx])
        gain_blocks.append(gain[active_idx])
        # Use the instruction's policy_id as cause_id_prefix surrogate.
        cause_ids.extend([record.instruction.policy_id] * int(active_idx.size))
    if not rollout_blocks:
        return None
    rollout_indices = np.concatenate(rollout_blocks)
    month_indices = np.concatenate(month_blocks)
    sales = np.concatenate(sale_blocks)
    basis = np.concatenate(basis_blocks)
    units = np.concatenate(units_blocks)
    gains = np.concatenate(gain_blocks)
    size = int(rollout_indices.size)
    return _asset_change_log_row_block(
        rollout_indices=rollout_indices,
        month_indices=month_indices,
        actor_ids=[primary_owner_actor_id] * size,
        asset_id="private_equity",
        asset_kind_value=AssetKindForLog.PRIVATE_EQUITY.value,
        delta_units=-units,
        delta_basis_usd=-basis,
        cash_proceeds_usd=sales,
        taxable_gain_usd=gains,
        tax_treatment_value=TaxTreatment.LONG_TERM_CAPITAL.value,
        cause_kind="OBLIGATION_OR_POLICY_SALE",
        cause_ids=cause_ids,
    )


def _property_disposition_to_asset_change_blocks(
    *, disposition: PropertyDispositionArrays, month_index: np.ndarray, primary_owner_actor_id: str, property_id: str
) -> list[pl.DataFrame]:
    """Build property-sale rows for the asset_change_log. A property
    sale generates TWO rows per (rollout, sale_month) — one for the
    LONG_TERM_CAPITAL portion (taxable_property_capital_gain_usd) and
    one for the DEPRECIATION_RECAPTURE_1250 portion. Different tax-rate
    buckets even though they share the disposition event; downstream
    `derive_per_month_taxable_gain_matrix(tax_treatment=...)` filters
    to one bucket and sums without double-counting because the
    other bucket's row has 0 in the unrelated column."""
    if disposition.sale_month is None:
        return []
    sale_month_positions = np.nonzero(month_index == int(disposition.sale_month))[0]
    if sale_month_positions.size == 0:
        return []
    sale_position = int(sale_month_positions[0])
    cap_gain = disposition.column("taxable_property_capital_gain_usd")[:, sale_position]
    recapture = disposition.column("depreciation_recapture_usd")[:, sale_position]
    gross = disposition.column("property_sale_gross_usd")[:, sale_position]
    debt_payoff = disposition.column("property_sale_debt_payoff_usd")[:, sale_position]
    closing_cost = disposition.column("sale_closing_cost_usd")[:, sale_position]
    net_proceeds = gross - closing_cost - debt_payoff
    rollout_count = cap_gain.shape[0]
    active = (cap_gain != 0.0) | (recapture != 0.0)
    if not active.any():
        return []
    active_idx = np.nonzero(active)[0]
    n = int(active_idx.size)
    months = np.full(n, int(disposition.sale_month), dtype=np.int64)
    # LT_CAPITAL row carries the units (the property itself, -1) and
    # cash proceeds + basis change; recapture row only carries the
    # taxable_gain_usd for its bucket. This prevents double-counting
    # in any downstream sum-without-filter.
    lt_row = _asset_change_log_row_block(
        rollout_indices=active_idx,
        month_indices=months,
        actor_ids=[primary_owner_actor_id] * n,
        asset_id=property_id,
        asset_kind_value=AssetKindForLog.PROPERTY.value,
        delta_units=np.full(n, -1.0, dtype=np.float64),
        delta_basis_usd=np.zeros(n, dtype=np.float64),
        cash_proceeds_usd=net_proceeds[active_idx],
        taxable_gain_usd=cap_gain[active_idx],
        tax_treatment_value=TaxTreatment.LONG_TERM_CAPITAL.value,
        cause_kind="PROPERTY_SALE",
        cause_ids=[f"property_sale:{property_id}"] * n,
    )
    recapture_row = _asset_change_log_row_block(
        rollout_indices=active_idx,
        month_indices=months,
        actor_ids=[primary_owner_actor_id] * n,
        asset_id=property_id,
        asset_kind_value=AssetKindForLog.PROPERTY.value,
        delta_units=np.zeros(n, dtype=np.float64),
        delta_basis_usd=np.zeros(n, dtype=np.float64),
        cash_proceeds_usd=np.zeros(n, dtype=np.float64),
        taxable_gain_usd=recapture[active_idx],
        tax_treatment_value=TaxTreatment.DEPRECIATION_RECAPTURE_1250.value,
        cause_kind="PROPERTY_SALE",
        cause_ids=[f"property_sale_recapture:{property_id}"] * n,
    )
    blocks: list[pl.DataFrame] = []
    if rollout_count > 0:
        blocks.append(lt_row)
        blocks.append(recapture_row)
    return blocks


def _build_asset_change_log(
    *,
    sp500_records: Sequence[Sp500SaleActionRecord],
    crypto_records: Sequence[CryptoSaleActionRecord],
    pe_records: Sequence[PrivateEquitySaleActionRecord],
    disposition: PropertyDispositionArrays,
    primary_owner_actor_id: str,
    sp500_asset_id: str,
    property_id: str | None,
    month_index: np.ndarray,
) -> pl.DataFrame:
    """Build the unified asset_change_log frame from today's per-asset-
    class action-record lists plus the property sale's disposition columns.
    See `ASSET_CHANGE_LOG_SCHEMA` for the row shape."""
    blocks: list[pl.DataFrame] = []
    sp500_block = _sp500_records_to_asset_change_block(
        sp500_records, asset_id=sp500_asset_id, primary_owner_actor_id=primary_owner_actor_id
    )
    if sp500_block is not None:
        blocks.append(sp500_block)
    crypto_block = _crypto_records_to_asset_change_block(crypto_records, primary_owner_actor_id=primary_owner_actor_id)
    if crypto_block is not None:
        blocks.append(crypto_block)
    pe_block = _pe_records_to_asset_change_block(pe_records, primary_owner_actor_id=primary_owner_actor_id)
    if pe_block is not None:
        blocks.append(pe_block)
    if property_id is not None:
        blocks.extend(
            _property_disposition_to_asset_change_blocks(
                disposition=disposition,
                month_index=month_index,
                primary_owner_actor_id=primary_owner_actor_id,
                property_id=property_id,
            )
        )
    if not blocks:
        return pl.DataFrame(schema=ASSET_CHANGE_LOG_SCHEMA)
    return pl.concat(blocks)


@dataclass(frozen=True)
class ObligationFundingPolicyApplication:
    policy_step: ActorPolicyStep[Policy]
    instruction: SellAssetInstructionBatch
    sale_usd: np.ndarray
    basis_usd: np.ndarray
    funded_cash_usd: np.ndarray
    shortfall_usd: np.ndarray
    remaining_due_usd: np.ndarray
    remaining_units: np.ndarray
    remaining_basis_usd: np.ndarray


@dataclass(frozen=True)
class CryptoObligationFundingPolicyApplication:
    policy_step: ActorPolicyStep[Policy]
    instruction: SellAssetInstructionBatch
    sale_usd: np.ndarray
    basis_usd: np.ndarray
    funded_cash_usd: np.ndarray
    shortfall_usd: np.ndarray
    remaining_due_usd: np.ndarray
    remaining_quantity: np.ndarray
    remaining_basis_usd: np.ndarray


@dataclass(frozen=True)
class PrivateEquityObligationFundingPolicyApplication:
    """Result of funding an obligation by selling a `PublicMarket`-regime PE position.

    Realized gain feeds the existing annual PE sale-tax allocation (long-term
    capital gain treatment), and the lot disposition / journal entries are
    recorded through the standard PE sale recorder.
    """

    policy_step: ActorPolicyStep[Policy]
    instruction: SellAssetInstructionBatch
    sale_usd: np.ndarray
    basis_usd: np.ndarray
    taxable_gain_usd: np.ndarray
    funded_cash_usd: np.ndarray
    shortfall_usd: np.ndarray
    remaining_due_usd: np.ndarray
    remaining_units: np.ndarray
    remaining_basis_usd: np.ndarray
    sold_units: np.ndarray
    sold_fraction: np.ndarray


def _trace_row_id(prefix: str, *, rollout_index: int, month_index: int) -> str:
    return f"{prefix}:rollout:{rollout_index}:month:{month_index}"


def _record_opening_accounting_state(
    accounting: AccountingTraceBuilder,
    tax_lots: event_streams.StreamFrameBuilder,
    liabilities: event_streams.StreamFrameBuilder,
    *,
    scenario: Scenario,
    rollout_count: int,
    initial_cash_usd: float,
    initial_sp500_value_usd: float,
    initial_sp500_basis_usd: float,
    initial_crypto_value_usd: float,
    initial_crypto_basis_usd: float,
    initial_private_equity_value_usd: float,
    initial_private_equity_basis_usd: float,
    purchase_price_usd: float,
    down_payment_usd: float,
    purchase_closing_cost_usd: np.ndarray,
    mortgage_balance_usd: np.ndarray,
) -> None:
    actor_id = _primary_owner_actor_id(scenario)
    cash_source = _single_checking_account_source(scenario, actor_id=actor_id)
    sp500_source = _single_sp500_asset_source(scenario, actor_id=actor_id)
    private_equity_source_id = _private_equity_source_holding_id(scenario)
    month_zero = 0

    if initial_cash_usd > 0:
        amount = np.full(rollout_count, initial_cash_usd, dtype="float64")
        accounting.record_entry_firings(
            schema=posting_schemas.OPENING_CHECKING_CASH,
            month_index=month_zero,
            cause_id_prefix="opening:checking_cash",
            actor_id=actor_id,
            description="opening checking cash",
            amount_bindings={"amount": amount},
            leg_chart_account_keys=(
                {
                    "actor_id": actor_id,
                    "source_account_id": cash_source.account_id if cash_source is not None else None,
                },
                {"actor_id": actor_id},
            ),
        )

    if initial_sp500_value_usd > 0:
        amount = np.full(rollout_count, initial_sp500_value_usd, dtype="float64")
        lot_id = _tax_lot_id(LotAssetClass.PUBLIC_SECURITY, sp500_source.asset_id if sp500_source else "portfolio")
        accounting.record_entry_firings(
            schema=posting_schemas.OPENING_PUBLIC_SECURITY,
            month_index=month_zero,
            cause_id_prefix="opening:public_security",
            actor_id=actor_id,
            description="opening public security holdings",
            amount_bindings={"amount": amount},
            leg_chart_account_keys=(
                {"actor_id": actor_id, "source_asset_id": sp500_source.asset_id if sp500_source is not None else None},
                {"actor_id": actor_id},
            ),
        )
        tax_lots.extend(
            {
                "lot_id": [lot_id],
                "asset_class": [LotAssetClass.PUBLIC_SECURITY.value],
                "owner_actor_id": [actor_id],
                "source_account_id": [None],
                "source_asset_id": [sp500_source.asset_id if sp500_source is not None else None],
                "property_id": [None],
                "quantity": [None],
                "cost_basis_usd": [max(0.0, float(initial_sp500_basis_usd))],
                "acquisition_month_index": [0],
            }
        )

    if initial_crypto_value_usd > 0:
        crypto_amount = np.full(rollout_count, initial_crypto_value_usd, dtype="float64")
        crypto_source_id = _crypto_source_holding_id(scenario, actor_id=actor_id)
        crypto_lot_id = _tax_lot_id(LotAssetClass.CRYPTO, crypto_source_id)
        accounting.record_entry_firings(
            schema=posting_schemas.OPENING_CRYPTO_ASSET,
            month_index=month_zero,
            cause_id_prefix="opening:crypto_asset",
            actor_id=actor_id,
            description="opening crypto holdings",
            amount_bindings={"amount": crypto_amount},
            leg_chart_account_keys=(
                {"actor_id": actor_id, "source_asset_id": crypto_source_id},
                {"actor_id": actor_id},
            ),
        )
        tax_lots.extend(
            {
                "lot_id": [crypto_lot_id],
                "asset_class": [LotAssetClass.CRYPTO.value],
                "owner_actor_id": [actor_id],
                "source_account_id": [None],
                "source_asset_id": [crypto_source_id],
                "property_id": [None],
                "quantity": [None],
                "cost_basis_usd": [max(0.0, float(initial_crypto_basis_usd))],
                "acquisition_month_index": [0],
            }
        )

    if initial_private_equity_value_usd > 0:
        amount = np.full(rollout_count, initial_private_equity_value_usd, dtype="float64")
        lot_id = _tax_lot_id(LotAssetClass.PRIVATE_EQUITY, private_equity_source_id)
        accounting.record_entry_firings(
            schema=posting_schemas.OPENING_PRIVATE_EQUITY,
            month_index=month_zero,
            cause_id_prefix="opening:private_equity",
            actor_id=actor_id,
            description="opening private equity holdings",
            amount_bindings={"amount": amount},
            leg_chart_account_keys=(
                {"actor_id": actor_id, "source_asset_id": private_equity_source_id},
                {"actor_id": actor_id},
            ),
        )
        tax_lots.extend(
            {
                "lot_id": [lot_id],
                "asset_class": [LotAssetClass.PRIVATE_EQUITY.value],
                "owner_actor_id": [actor_id],
                "source_account_id": [None],
                "source_asset_id": [private_equity_source_id],
                "property_id": [None],
                "quantity": [_initial_private_equity_units(scenario) or None],
                "cost_basis_usd": [max(0.0, float(initial_private_equity_basis_usd))],
                "acquisition_month_index": [0],
            }
        )

    property_id = scenario.property_selection.property_id
    if property_id is None or purchase_price_usd <= 0:
        return

    purchase = np.full(rollout_count, purchase_price_usd, dtype="float64")
    closing = np.asarray(purchase_closing_cost_usd, dtype="float64")
    mortgage = np.asarray(mortgage_balance_usd, dtype="float64")
    cash_outlay = np.full(rollout_count, down_payment_usd, dtype="float64") + closing
    liability_id = _mortgage_liability_id(property_id)
    accounting.record_entry_firings(
        schema=posting_schemas.OPENING_PROPERTY,
        month_index=month_zero,
        cause_id_prefix=f"opening:property:{property_id}",
        actor_id=actor_id,
        description="opening property purchase",
        amount_bindings={"purchase": purchase, "closing": closing, "cash_outlay": cash_outlay, "mortgage": mortgage},
        leg_chart_account_keys=(
            {"actor_id": actor_id, "property_id": property_id},
            {"actor_id": actor_id, "property_id": property_id},
            {"actor_id": actor_id, "source_account_id": cash_source.account_id if cash_source is not None else None},
            {"actor_id": actor_id, "liability_id": liability_id, "property_id": property_id},
        ),
    )
    tax_lots.extend(
        {
            "lot_id": [_tax_lot_id(LotAssetClass.PROPERTY, property_id)],
            "asset_class": [LotAssetClass.PROPERTY.value],
            "owner_actor_id": [actor_id],
            "source_account_id": [None],
            "source_asset_id": [None],
            "property_id": [property_id],
            "quantity": [None],
            "cost_basis_usd": [max(0.0, purchase_price_usd + float(np.mean(closing)))],
            "acquisition_month_index": [0],
        }
    )
    initial_mortgage = float(np.max(mortgage))
    if initial_mortgage > 0:
        liabilities.extend(
            {
                "liability_id": [liability_id],
                "liability_type": [LiabilityType.MORTGAGE.value],
                "actor_id": [actor_id],
                "creditor_id": ["mortgage_lender"],
                "counterparty_actor_id": [None],
                "property_id": [property_id],
                "balance_usd": [initial_mortgage],
            }
        )


def _record_state_balance_snapshots(
    accounting: AccountingTraceBuilder,
    *,
    scenario: Scenario,
    month_index: np.ndarray,
    cash_usd: np.ndarray,
    generic_sp500_value_usd: np.ndarray,
    crypto_value_usd: np.ndarray,
    private_equity_value_usd: np.ndarray,
    property_value_usd: np.ndarray,
    mortgage_balance_usd: np.ndarray,
    property_balance_mask: np.ndarray,
) -> None:
    actor_id = _primary_owner_actor_id(scenario)
    cash_source = _single_checking_account_source(scenario, actor_id=actor_id)
    sp500_source = _single_sp500_asset_source(scenario, actor_id=actor_id)
    crypto_source_id = _crypto_source_holding_id(scenario, actor_id=actor_id)
    private_equity_source_id = _private_equity_source_holding_id(scenario)
    accounting.record_snapshot(
        month_index=month_index,
        snapshot=BalanceSnapshotBatch(
            role=ChartAccountRole.CHECKING_CASH,
            amount_usd=cash_usd,
            actor_id=actor_id,
            source_account_id=cash_source.account_id if cash_source is not None else None,
        ),
    )
    accounting.record_snapshot(
        month_index=month_index,
        snapshot=BalanceSnapshotBatch(
            role=ChartAccountRole.PUBLIC_SECURITY,
            amount_usd=generic_sp500_value_usd,
            actor_id=actor_id,
            source_asset_id=sp500_source.asset_id if sp500_source is not None else None,
        ),
    )
    accounting.record_snapshot(
        month_index=month_index,
        snapshot=BalanceSnapshotBatch(
            role=ChartAccountRole.CRYPTO_ASSET,
            amount_usd=crypto_value_usd,
            actor_id=actor_id,
            source_asset_id=crypto_source_id,
        ),
    )
    accounting.record_snapshot(
        month_index=month_index,
        snapshot=BalanceSnapshotBatch(
            role=ChartAccountRole.PRIVATE_EQUITY,
            amount_usd=private_equity_value_usd,
            actor_id=actor_id,
            source_asset_id=private_equity_source_id,
        ),
    )
    property_id = scenario.property_selection.property_id
    if property_id is None:
        return
    accounting.record_snapshot(
        month_index=month_index,
        snapshot=BalanceSnapshotBatch(
            role=ChartAccountRole.PROPERTY,
            amount_usd=property_value_usd * property_balance_mask,
            actor_id=actor_id,
            property_id=property_id,
        ),
    )
    accounting.record_snapshot(
        month_index=month_index,
        snapshot=BalanceSnapshotBatch(
            role=ChartAccountRole.MORTGAGE_PAYABLE,
            amount_usd=mortgage_balance_usd * property_balance_mask,
            actor_id=actor_id,
            liability_id=_mortgage_liability_id(property_id),
            property_id=property_id,
        ),
    )


def run_scenario_vectorized(scenario: Scenario, market_bundle: MarketBundle) -> ScenarioRunArrays:
    month_index = market_bundle.month_index
    rollout_count = market_bundle.rollout_count
    month_count = market_bundle.horizon_months + 1
    location_id = scenario.location_id
    initial_cash = _initial_cash_usd(scenario)
    initial_sp500 = _initial_sp500_value_usd(scenario)
    initial_sp500_basis = _initial_sp500_cost_basis_usd(scenario)
    initial_crypto = _initial_crypto_value_usd(scenario)
    initial_crypto_basis = _initial_crypto_cost_basis_usd(scenario)
    pe_unit_price_usd = float(market_bundle.metadata.current_private_equity_price_usd)
    initial_private_equity = _initial_private_equity_value_usd(scenario, current_unit_price_usd=pe_unit_price_usd)
    initial_private_equity_basis = _initial_private_equity_cost_basis_usd(scenario)
    initial_private_equity_units = _initial_private_equity_units(scenario)
    private_equity_source_holding_id = _private_equity_source_holding_id(scenario)
    pe_liquidity_regime = _effective_pe_liquidity_regime(scenario)
    # The engine aggregates PE state into a single `private_equity_*` path. With
    # explicit per-issuer keying the engine routes through the first issuer's path
    # (one-issuer scenarios pick that issuer's path; multi-issuer scenarios pick
    # one and per-issuer observations are emitted separately via
    # `_record_per_issuer_sale_opportunity_observations`). Scenarios with no PE
    # positions get the all-ones / no-tender stub — the multiplier is never read
    # because initial PE value is zero.
    pe_issuer_keys = _private_equity_issuer_routing_keys(scenario)
    engine_pe_issuer_key = pe_issuer_keys[0] if pe_issuer_keys else None
    if engine_pe_issuer_key is None:
        shape = (rollout_count, month_count)
        pe_value_multipliers = np.ones(shape, dtype="float64")
        pe_sale_opportunity_mask = np.zeros(shape, dtype=np.bool_)
    else:
        pe_value_multipliers = market_bundle.private_equity_value_multiplier(engine_pe_issuer_key)
        pe_sale_opportunity_mask = market_bundle.private_equity_sale_opportunity_mask_for(engine_pe_issuer_key)
    # PublicMarket regime makes the holding freely sellable from `lockup_end_month`
    # onward, so we widen the tender-window mask the engine uses when computing
    # PE sale opportunities. The reported `private_equity_sale_opportunity_event`
    # series keeps the original market-sampled mask: the widening is an
    # engine-internal extension of "when can this position be sold", not a
    # claim that the market emitted a tender opportunity. Tender-based scenarios
    # (LiquidityEventOnly) keep their original mask byte-for-byte.
    effective_pe_sale_opportunity_mask = pe_sale_opportunity_mask
    if isinstance(pe_liquidity_regime, PublicMarket):
        lockup_end_month = pe_liquidity_regime.lockup_end_month or 0
        sellable_months = (month_index >= lockup_end_month).astype(np.bool_)
        effective_pe_sale_opportunity_mask = pe_sale_opportunity_mask | np.broadcast_to(
            sellable_months[None, :], pe_sale_opportunity_mask.shape
        )
    purchase_price = _purchase_price_usd(scenario)
    policy_programs = actor_policy_programs(scenario)
    policy_steps = actor_policy_steps(policy_programs)

    property_value, mortgage_balance, mortgage_interest, mortgage_principal = _property_and_mortgage_arrays(
        scenario, market_bundle, location_id=location_id
    )
    down_payment = _initial_property_cash_outlay_usd(scenario)
    property_cash_flow = _property_cash_flow_arrays(
        scenario,
        market_bundle,
        location_id=location_id,
        property_value_usd=property_value,
        mortgage_interest_usd=mortgage_interest,
        mortgage_principal_usd=mortgage_principal,
    )
    if scenario.property_selection.property_id is None:
        disposition = empty_property_disposition_arrays(market_bundle)
    else:
        disposition = property_disposition_arrays(
            scenario,
            market_bundle,
            property_value_usd=property_value,
            mortgage_balance_usd=mortgage_balance,
            purchase_price_usd=purchase_price,
            local_regulation=_required_local_regulation(scenario),
        )
    if disposition.sale_month is None:
        property_live_mask = np.ones((rollout_count, month_count), dtype="float64")
    else:
        property_live_mask = (month_index <= disposition.sale_month).astype("float64")
        property_live_mask = np.broadcast_to(property_live_mask[None, :], (rollout_count, month_count)).copy()
    mortgage_interest = mortgage_interest * property_live_mask
    mortgage_principal = mortgage_principal * property_live_mask
    net_property_cash_flow = property_cash_flow.column("net_property_cash_flow_usd") * property_live_mask
    # Per-line cost arrays settle through the obligation pipeline (in-loop, before
    # within-month policies) so each carrying-cost line records its own
    # obligation/settlement/funding-decision rows on the trace. Masking out
    # post-sale months ensures the obligation amount is zero once the property is
    # sold.
    property_tax_obligation_due = property_cash_flow.column("property_tax_usd") * property_live_mask
    hoa_obligation_due = property_cash_flow.column("hoa_usd") * property_live_mask
    insurance_obligation_due = property_cash_flow.column("insurance_usd") * property_live_mask
    maintenance_obligation_due = property_cash_flow.column("maintenance_usd") * property_live_mask
    home_equity = property_value - mortgage_balance
    partner_equity = _partner_equity_arrays(
        scenario,
        market_bundle,
        policy_steps=policy_steps,
        owner_initial_equity_usd=down_payment,
        home_equity_usd=home_equity,
        mortgage_interest_usd=mortgage_interest,
        mortgage_principal_usd=mortgage_principal,
        property_tax_usd=property_cash_flow.column("property_tax_usd") * property_live_mask,
        hoa_usd=property_cash_flow.column("hoa_usd") * property_live_mask,
        insurance_usd=property_cash_flow.column("insurance_usd") * property_live_mask,
        maintenance_usd=property_cash_flow.column("maintenance_usd") * property_live_mask,
    )
    # Pre-computed per-month inputs that don't depend on policy decisions —
    # the engine's main month loop reads these via `scheduled.amount_at(...)`.
    # Phase 0 of the state-vector simulation refactor; see
    # `augur/plans/state_vector_simulation_refactor.md`.
    scheduled = build_scheduled_cashflows(
        rollout_count=rollout_count,
        month_index=month_index,
        property_net_cash_flow_usd=net_property_cash_flow,
        property_sale_cash_flow_usd=disposition.column("net_property_sale_cash_flow_usd"),
        partner_contribution_used_usd=partner_equity.column("contribution_used_usd"),
        property_tax_accrual_usd=property_tax_obligation_due,
        hoa_accrual_usd=hoa_obligation_due,
        insurance_accrual_usd=insurance_obligation_due,
        maintenance_accrual_usd=maintenance_obligation_due,
    )
    generic_sp500_value = np.zeros((rollout_count, month_count), dtype="float64")
    generic_sp500_sale_gain = np.zeros((rollout_count, month_count), dtype="float64")
    generic_sp500_sale_tax = np.zeros((rollout_count, month_count), dtype="float64")
    checking_floor_shortfall = np.zeros((rollout_count, month_count), dtype="float64")
    crypto_value = np.zeros((rollout_count, month_count), dtype="float64")
    crypto_sale_usd = np.zeros((rollout_count, month_count), dtype="float64")
    crypto_sale_basis_usd = np.zeros((rollout_count, month_count), dtype="float64")
    remaining_crypto_quantity_by_month = np.zeros((rollout_count, month_count), dtype="float64")
    remaining_crypto_basis_by_month = np.zeros((rollout_count, month_count), dtype="float64")
    private_equity_value = np.zeros((rollout_count, month_count), dtype="float64")
    private_equity_sale_opportunity_value = np.zeros((rollout_count, month_count), dtype="float64")
    private_equity_sale_taxable_gain = np.zeros((rollout_count, month_count), dtype="float64")
    private_equity_sale_tax = np.zeros((rollout_count, month_count), dtype="float64")
    cash = np.zeros((rollout_count, month_count), dtype="float64")
    remaining_sp500_units_by_month = np.zeros((rollout_count, month_count), dtype="float64")
    remaining_sp500_basis_by_month = np.zeros((rollout_count, month_count), dtype="float64")
    remaining_private_equity_units_by_month = np.zeros((rollout_count, month_count), dtype="float64")
    remaining_private_equity_basis_by_month = np.zeros((rollout_count, month_count), dtype="float64")
    private_equity_sale_usd_by_month = np.zeros((rollout_count, month_count), dtype="float64")
    private_equity_sale_opportunity_event = pe_sale_opportunity_mask.copy()
    remaining_private_equity_fraction = np.ones(rollout_count, dtype="float64")
    remaining_sp500_units = np.divide(
        initial_sp500,
        market_bundle.generic_sp500_multipliers[:, 0],
        out=np.zeros(rollout_count, dtype="float64"),
        where=market_bundle.generic_sp500_multipliers[:, 0] > 0,
    )
    remaining_sp500_basis = np.full(rollout_count, initial_sp500_basis, dtype="float64")
    # Crypto state: quantity = value_usd / unit_price; month 0 unit price is 1.0 by
    # contract, so initial quantity equals initial value. A fitted crypto model will
    # change this, but the engine code treats month 0 multipliers as 1.0 by
    # MarketBundle validation, mirroring the SP500 path. Scenarios without crypto
    # positions get an all-ones stub — the multiplier is never read because the
    # initial crypto value is zero.
    crypto_engine_routing_key = _crypto_engine_routing_key(scenario)
    if crypto_engine_routing_key is None:
        crypto_value_multipliers = np.ones((rollout_count, month_count), dtype="float64")
    else:
        crypto_value_multipliers = market_bundle.crypto_value_multiplier(crypto_engine_routing_key)
    crypto_unit_price_month_zero = crypto_value_multipliers[:, 0]
    remaining_crypto_quantity = np.divide(
        initial_crypto,
        crypto_unit_price_month_zero,
        out=np.zeros(rollout_count, dtype="float64"),
        where=crypto_unit_price_month_zero > 0,
    )
    remaining_crypto_basis = np.full(rollout_count, initial_crypto_basis, dtype="float64")
    remaining_private_equity_basis = np.full(rollout_count, initial_private_equity_basis, dtype="float64")
    remaining_private_equity_units = np.full(rollout_count, initial_private_equity_units, dtype="float64")
    current_cash = (
        np.full(rollout_count, initial_cash - down_payment, dtype="float64")
        - disposition.column("purchase_closing_cost_usd")[:, 0]
    )
    effects: dict[EffectType, event_streams.StreamFrameBuilder] = {
        EffectType.SELL_SP500: event_streams.StreamFrameBuilder(event_streams.SELL_SP500_EFFECT_SCHEMA),
        EffectType.SELL_CRYPTO: event_streams.StreamFrameBuilder(event_streams.SELL_CRYPTO_EFFECT_SCHEMA),
        EffectType.SELL_PRIVATE_EQUITY: event_streams.StreamFrameBuilder(
            event_streams.SELL_PRIVATE_EQUITY_EFFECT_SCHEMA
        ),
        EffectType.SETTLE_PROPERTY_SALE: event_streams.StreamFrameBuilder(
            event_streams.SETTLE_PROPERTY_SALE_EFFECT_SCHEMA
        ),
    }
    policy_decisions = event_streams.StreamFrameBuilder(event_streams.POLICY_DECISION_SCHEMA)
    market_path_observations_frame = _market_path_observations_frame(scenario, market_bundle)
    pe_sale_opportunity_observations = event_streams.StreamFrameBuilder(
        event_streams.PE_SALE_OPPORTUNITY_OBSERVATION_SCHEMA
    )
    accounting = AccountingTraceBuilder()
    tax_lots = event_streams.StreamFrameBuilder(event_streams.TAX_LOT_SCHEMA)
    lot_dispositions = event_streams.StreamFrameBuilder(event_streams.LOT_DISPOSITION_SCHEMA)
    liabilities = event_streams.StreamFrameBuilder(event_streams.LIABILITY_STATE_SCHEMA)
    property_sale_basis_gain_details = event_streams.StreamFrameBuilder(
        event_streams.PROPERTY_SALE_BASIS_GAIN_DETAIL_SCHEMA
    )
    tax_payment_allocation_details = event_streams.StreamFrameBuilder(
        event_streams.TAX_PAYMENT_ALLOCATION_DETAIL_SCHEMA
    )
    # One root accumulator for the entire obligation lifecycle. The
    # `obligations`, `settlement_results`, and `failure_events` Pydantic
    # surfaces are projection/filter views over this single frame at
    # end-of-run (see `event_streams.materialize_*`).
    obligations = event_streams.StreamFrameBuilder(event_streams.OBLIGATION_LIFECYCLE_SCHEMA)
    funding_decisions = event_streams.StreamFrameBuilder(event_streams.FUNDING_DECISION_SCHEMA)
    sp500_sale_action_records: list[Sp500SaleActionRecord] = []
    crypto_sale_action_records: list[CryptoSaleActionRecord] = []
    private_equity_sale_action_records: list[PrivateEquitySaleActionRecord] = []
    _record_opening_accounting_state(
        accounting,
        tax_lots,
        liabilities,
        scenario=scenario,
        rollout_count=rollout_count,
        initial_cash_usd=initial_cash,
        initial_sp500_value_usd=initial_sp500,
        initial_sp500_basis_usd=initial_sp500_basis,
        initial_crypto_value_usd=initial_crypto,
        initial_crypto_basis_usd=initial_crypto_basis,
        initial_private_equity_value_usd=initial_private_equity,
        initial_private_equity_basis_usd=initial_private_equity_basis,
        purchase_price_usd=purchase_price,
        down_payment_usd=down_payment,
        purchase_closing_cost_usd=disposition.column("purchase_closing_cost_usd")[:, 0],
        mortgage_balance_usd=mortgage_balance[:, 0],
    )

    primary_owner_actor_id = _primary_owner_actor_id(scenario)
    primary_owner_funding_sources = _ObligationFundingSources.for_actor(scenario, actor_id=primary_owner_actor_id)
    property_cost_obligation_specs: tuple[tuple[np.ndarray, _CashDebitObligationKind, str, str], ...] = (
        (
            property_tax_obligation_due,
            _CashDebitObligationKind(
                obligation_type=ObligationType.PROPERTY_TAX, expense_role=ChartAccountRole.PROPERTY_TAX_EXPENSE
            ),
            "property_tax_authority",
            PROPERTY_TAX_POLICY_ID,
        ),
        (
            hoa_obligation_due,
            _CashDebitObligationKind(
                obligation_type=ObligationType.HOA_DUES, expense_role=ChartAccountRole.HOA_EXPENSE
            ),
            "hoa",
            HOA_DUES_POLICY_ID,
        ),
        (
            insurance_obligation_due,
            _CashDebitObligationKind(
                obligation_type=ObligationType.INSURANCE_PREMIUM, expense_role=ChartAccountRole.INSURANCE_EXPENSE
            ),
            "insurance_carrier",
            INSURANCE_POLICY_ID,
        ),
        (
            maintenance_obligation_due,
            _CashDebitObligationKind(
                obligation_type=ObligationType.MAINTENANCE, expense_role=ChartAccountRole.MAINTENANCE_EXPENSE
            ),
            "maintenance_vendor",
            MAINTENANCE_POLICY_ID,
        ),
    )

    # One accumulator per property-cost obligation kind, lives across the
    # whole month loop. Per-month settlement writes `(rollouts,)` slices
    # into the accumulator's `(rollouts, months)` matrices; emit melts to
    # row-blocks once at end-of-loop.
    property_cost_obligation_accumulators: tuple[_ObligationFundingAccumulator, ...] = tuple(
        _ObligationFundingAccumulator.new(
            obligation_kind=kind,
            creditor_id=creditor_id,
            source_policy_id=policy_id,
            actor_id=primary_owner_actor_id,
            cash_source_account_id=(
                primary_owner_funding_sources.cash_source_account.account_id
                if primary_owner_funding_sources.cash_source_account is not None
                else None
            ),
            rollout_count=rollout_count,
            month_index=month_index,
            amount_due_usd=due,
        )
        for due, kind, creditor_id, policy_id in property_cost_obligation_specs
    )

    # Agent-centric `SimulationState` maintained in parallel with the
    # existing 1D locals + matrix snapshots (Phases 1-3b of the
    # state-vector refactor). State.agents[primary_owner_actor_id]
    # carries the owner's cash + holdings + liabilities + property
    # stake; partner-equity scenarios add a second AgentState entry
    # for the partner. State.properties carries shared per-property
    # facts. The end-of-month snapshot block at :~1833 reads the
    # cash / SP500 / crypto / PE matrix columns through `state.agent(
    # owner).cash(...)` / `.holding(...)`; the rest of the snapshot
    # block still reads from locals until later phases bring more
    # state through the object.
    cash_account_id = (
        primary_owner_funding_sources.cash_source_account.account_id
        if primary_owner_funding_sources.cash_source_account is not None
        else "checking"
    )
    selected_property_id = scenario.property_selection.property_id

    def _build_state(*, month_position: int, month_col: int) -> SimulationState:
        """Snapshot SimulationState from the 1D locals + the
        precomputed matrix columns at `month_col` (the position in the
        property/mortgage/partner_equity matrices we're snapshotting).
        `month_position` is the same value, called separately to make
        the initial-state (-1) case obvious — the property matrices are
        column-0 entries that represent the start-of-loop state."""
        owner_liabilities: dict[str, LiabilityBalance] = {}
        owner_stakes: dict[str, PropertyStake] = {}
        properties: dict[str, PropertyState] = {}
        agents: dict[str, AgentState] = {}
        if selected_property_id is not None:
            mortgage_id = _mortgage_liability_id(selected_property_id)
            owner_liabilities[mortgage_id] = LiabilityBalance(
                liability_id=mortgage_id,
                liability_kind=LiabilityKind.MORTGAGE,
                property_id=selected_property_id,
                principal_usd=mortgage_balance[:, month_col],
                interest_accrued_this_month_usd=mortgage_interest[:, month_col],
                principal_paid_this_month_usd=mortgage_principal[:, month_col],
            )
            properties[selected_property_id] = PropertyState(
                property_id=selected_property_id,
                live=property_live_mask[:, month_col],
                value_usd=property_value[:, month_col],
                cumulative_depreciation_usd=disposition.column("cumulative_property_depreciation_usd")[:, month_col],
            )
            partner_ownership_pct = partner_equity.column("ownership_pct")[:, month_col]
            owner_stakes[selected_property_id] = PropertyStake(
                property_id=selected_property_id,
                ownership_pct=1.0 - partner_ownership_pct,
                contribution_used_usd=np.zeros(rollout_count, dtype="float64"),
                equity_ledger_usd=partner_equity.column("owner_equity_ledger_usd")[:, month_col],
            )
            for agreement in partner_equity.agreements:
                agents[agreement.recipient_actor_id] = AgentState(
                    actor_id=agreement.recipient_actor_id,
                    cash_by_account={},
                    holdings={},
                    liabilities={},
                    property_stakes={
                        selected_property_id: PropertyStake(
                            property_id=selected_property_id,
                            ownership_pct=partner_ownership_pct,
                            contribution_used_usd=partner_equity.column("contribution_used_usd")[:, month_col],
                            equity_ledger_usd=partner_equity.column("partner_equity_ledger_usd")[:, month_col],
                        )
                    },
                )
        agents[primary_owner_actor_id] = AgentState(
            actor_id=primary_owner_actor_id,
            cash_by_account={cash_account_id: current_cash},
            holdings={
                "sp500": AssetHolding(
                    asset_id="sp500",
                    asset_kind=AssetKind.GENERIC_SP500,
                    units=remaining_sp500_units,
                    basis_usd=remaining_sp500_basis,
                ),
                "crypto": AssetHolding(
                    asset_id="crypto",
                    asset_kind=AssetKind.CRYPTO,
                    units=remaining_crypto_quantity,
                    basis_usd=remaining_crypto_basis,
                ),
                "private_equity": AssetHolding(
                    asset_id="private_equity",
                    asset_kind=AssetKind.PRIVATE_EQUITY,
                    units=remaining_private_equity_units,
                    basis_usd=remaining_private_equity_basis,
                ),
            },
            liabilities=owner_liabilities,
            property_stakes=owner_stakes,
        )
        return SimulationState(month_position=month_position, agents=agents, properties=properties)

    state = _build_state(month_position=-1, month_col=0)

    # Phase 4b: TaxActor + obligation accumulators constructed before
    # the main loop. Estimated and annual tax obligations emit at their
    # natural months inside the loop (quarterly markers + year-end
    # true-up), routed through the same
    # `_settle_required_cash_obligation_at_month_position` path used
    # for property-cost obligations. Replaces the post-loop
    # `_settle_required_cash_obligations` sweep.
    #
    # Year-0 behavior: when `tax_profile.prior_year_tax_usd` is None,
    # no quarterly obligations emit for year 0; the year-end true-up
    # settles the full year tax. Users wanting year-0 quarterlies
    # supply `prior_year_tax_usd` as a user-settable knob (changed
    # from today's forward-looking-90%-of-actual behavior, which
    # required two-pass simulation to honor honestly).
    pe_funding_state = _PrivateEquityFundingState(
        private_equity_value_usd=private_equity_value,
        remaining_units_by_month=remaining_private_equity_units_by_month,
        remaining_basis_by_month=remaining_private_equity_basis_by_month,
        private_equity_sale_usd=private_equity_sale_usd_by_month,
        private_equity_sale_taxable_gain_usd=private_equity_sale_taxable_gain,
        pe_value_multipliers=pe_value_multipliers,
        initial_private_equity=initial_private_equity,
        source_holding_id=private_equity_source_holding_id,
        sale_action_records=private_equity_sale_action_records,
    )
    tax_actor = TaxActor(tax_profile=scenario.tax_profile, rollout_count=rollout_count)
    _tax_obligation_cash_source_account_id = (
        primary_owner_funding_sources.cash_source_account.account_id
        if primary_owner_funding_sources.cash_source_account is not None
        else None
    )
    estimated_tax_accumulator = _ObligationFundingAccumulator.new(
        obligation_kind=_EstimatedTaxObligationKind(),
        creditor_id="tax_authority",
        source_policy_id=ESTIMATED_TAX_ACCOUNTING_POLICY_ID,
        actor_id=primary_owner_actor_id,
        cash_source_account_id=_tax_obligation_cash_source_account_id,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_due_usd=np.zeros((rollout_count, month_count), dtype="float64"),
    )
    annual_tax_accumulator = _ObligationFundingAccumulator.new(
        obligation_kind=_AnnualTaxObligationKind(),
        creditor_id="tax_authority",
        source_policy_id=ANNUAL_TAX_ACCOUNTING_POLICY_ID,
        actor_id=primary_owner_actor_id,
        cash_source_account_id=_tax_obligation_cash_source_account_id,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_due_usd=np.zeros((rollout_count, month_count), dtype="float64"),
    )
    # Mortgage / special-assessment / outside-rent obligations: today's
    # post-loop `_settle_required_cash_obligations(...)` calls are
    # equivalent to per-iteration inline settlement (each iterates
    # months internally; settlement order within a month was always
    # main-loop-property-cost → mortgage → special_assessment →
    # outside_rent). Move inline so the engine is a single forward DAG.
    mortgage_payment_due = property_cash_flow.column("mortgage_payment_usd") * property_live_mask
    mortgage_accumulator: _ObligationFundingAccumulator | None = None
    if scenario.property_selection.property_id is not None:
        mortgage_accumulator = _ObligationFundingAccumulator.new(
            obligation_kind=_MortgageObligationKind(
                interest_usd=mortgage_interest,
                principal_usd=mortgage_principal,
                property_id=scenario.property_selection.property_id,
            ),
            creditor_id="mortgage_lender",
            source_policy_id=MORTGAGE_SERVICING_POLICY_ID,
            actor_id=primary_owner_actor_id,
            cash_source_account_id=_tax_obligation_cash_source_account_id,
            rollout_count=rollout_count,
            month_index=month_index,
            amount_due_usd=mortgage_payment_due,
        )
    special_assessment_due = _special_assessment_obligation_due_usd(
        scenario, rollout_count=rollout_count, month_index=month_index
    )
    special_assessment_accumulator = _ObligationFundingAccumulator.new(
        obligation_kind=_CashDebitObligationKind(
            obligation_type=ObligationType.SPECIAL_ASSESSMENT, expense_role=ChartAccountRole.HOA_EXPENSE
        ),
        creditor_id="hoa",
        source_policy_id=SPECIAL_ASSESSMENT_POLICY_ID,
        actor_id=primary_owner_actor_id,
        cash_source_account_id=_tax_obligation_cash_source_account_id,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_due_usd=special_assessment_due,
    )
    outside_rent_due = _outside_rent_obligation_due_usd(scenario, rollout_count=rollout_count, month_index=month_index)
    outside_rent_accumulator = _ObligationFundingAccumulator.new(
        obligation_kind=_CashDebitObligationKind(
            obligation_type=ObligationType.OUTSIDE_RENT, expense_role=ChartAccountRole.OUTSIDE_RENT_EXPENSE
        ),
        creditor_id="landlord",
        source_policy_id=OUTSIDE_RENT_POLICY_ID,
        actor_id=primary_owner_actor_id,
        cash_source_account_id=_tax_obligation_cash_source_account_id,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_due_usd=outside_rent_due,
    )
    # Partner-contribution obligations: per-agreement state lives on the
    # contributing actor (its own checking cash, sp500, crypto). Build
    # one settlement context per agreement so the main month loop can
    # settle inline like the primary owner's obligations do.
    partner_settlement_contexts = [
        _PartnerSettlementContext.new(
            scenario=scenario,
            agreement=agreement,
            owner_actor_id=primary_owner_actor_id,
            rollout_count=rollout_count,
            month_count=month_count,
            month_index=month_index,
        )
        for agreement in partner_equity.agreements
    ]
    # Pre-inline-tax record lists feed `annual_sale_tax_allocation`'s
    # gain inputs so the per-month tax reporting matrices reflect the
    # same gains TaxActor used to size the inline obligation amounts
    # — without filtering, tax-driven sales would recursively appear
    # in TOTAL_INCOME_TAX_USD even though we don't tax them.
    sp500_records_idx_at_observe = 0
    pe_records_idx_at_observe = 0
    crypto_records_idx_at_observe = 0
    pre_inline_tax_sp500_records: list[Sp500SaleActionRecord] = []
    pre_inline_tax_pe_records: list[PrivateEquitySaleActionRecord] = []
    pre_inline_tax_crypto_records: list[CryptoSaleActionRecord] = []

    for month in range(month_count):
        current_cash = current_cash + scheduled.amount_at(
            kind=ScheduledCashflowKind.PROPERTY_SALE_CASH_FLOW, month_position=month
        )
        if month > 0:
            current_cash = (
                current_cash
                + scheduled.amount_at(kind=ScheduledCashflowKind.PROPERTY_NET_CASH_FLOW, month_position=month)
                + scheduled.amount_at(kind=ScheduledCashflowKind.PARTNER_CONTRIBUTION_USED, month_position=month)
            )
        # Settle property-cost obligations for this month BEFORE within-month
        # policies run. This keeps within-month policy decisions (which depend on
        # current_cash) observing the post-carrying-cost cash balance, matching
        # the pre-refactor behavior where carrying costs were deducted via
        # net_property_cash_flow at month start. Settlement takes 1D state in
        # and returns 1D state out; the end-of-month snapshot at :1818-1826
        # writes the post-settlement 1D state into the matrices.
        if any(np.any(due[:, month] > 0) for due, _, _, _ in property_cost_obligation_specs):
            sp500_multiplier_at_month = market_bundle.generic_sp500_multipliers[:, month]
            crypto_multiplier_at_month = crypto_value_multipliers[:, month]
            current_sp500_value = remaining_sp500_units * sp500_multiplier_at_month
            current_crypto_value = remaining_crypto_quantity * crypto_multiplier_at_month
            for spec, accumulator in zip(
                property_cost_obligation_specs, property_cost_obligation_accumulators, strict=True
            ):
                due, kind, creditor_id, policy_id = spec
                if not np.any(due[:, month] > 0):
                    continue
                settled = _settle_required_cash_obligation_at_month_position(
                    market_bundle=market_bundle,
                    month_position=month,
                    due_month_index=int(month_index[month]),
                    policy_steps=policy_steps,
                    obligation_amount_usd=due[:, month],
                    obligation_kind=kind,
                    creditor_id=creditor_id,
                    source_policy_id=policy_id,
                    actor_id=primary_owner_actor_id,
                    sources=primary_owner_funding_sources,
                    cash_usd=current_cash,
                    generic_sp500_value_usd=current_sp500_value,
                    remaining_sp500_units=remaining_sp500_units,
                    remaining_sp500_basis=remaining_sp500_basis,
                    crypto_value_usd=current_crypto_value,
                    remaining_crypto_quantity=remaining_crypto_quantity,
                    remaining_crypto_basis=remaining_crypto_basis,
                    crypto_sale_usd=crypto_sale_usd,
                    crypto_sale_basis_usd=crypto_sale_basis_usd,
                    checking_floor_shortfall_usd=checking_floor_shortfall,
                    accumulator=accumulator,
                    accounting=accounting,
                    sp500_sale_action_records=sp500_sale_action_records,
                    crypto_sale_action_records=crypto_sale_action_records,
                )
                current_cash = settled.cash_usd
                remaining_sp500_units = settled.remaining_sp500_units
                remaining_sp500_basis = settled.remaining_sp500_basis
                current_sp500_value = settled.generic_sp500_value_usd
                remaining_crypto_quantity = settled.remaining_crypto_quantity
                remaining_crypto_basis = settled.remaining_crypto_basis
                current_crypto_value = settled.crypto_value_usd

        # Compute pre-acquisition value first so the downstream "value - sale"
        # bookkeeping nets to zero rather than going negative when the entire
        # remaining position converts.
        private_equity_value_before_sale = (
            initial_private_equity * remaining_private_equity_fraction * pe_value_multipliers[:, month]
        )
        # Acquisition regime: forced conversion of the entire remaining PE
        # position into cash on `event_month`. Realized gain feeds the existing
        # annual sale-tax allocation (long-term capital gain treatment). The
        # PE position drops to zero units after this month. We use the
        # current spot mark (units × multiplier-driven unit price) as the
        # accounting value reduction, but cash proceeds use
        # `units × cash_per_unit_usd` as the contract specifies.
        acquisition_sale_month = np.zeros(rollout_count, dtype="float64")
        acquisition_taxable_gain_month = np.zeros(rollout_count, dtype="float64")
        if isinstance(pe_liquidity_regime, Acquisition) and month == int(pe_liquidity_regime.event_month):
            acquisition_proceeds = remaining_private_equity_units * float(pe_liquidity_regime.cash_per_unit_usd)
            acquisition_basis = remaining_private_equity_basis.copy()
            acquisition_taxable_gain = np.maximum(0.0, acquisition_proceeds - acquisition_basis)
            acquisition_units_sold = remaining_private_equity_units.copy()
            acquisition_sold_fraction = remaining_private_equity_fraction.copy()
            current_cash = current_cash + acquisition_proceeds
            acquisition_instruction = PrivateEquitySaleInstructionBatch(
                actor_id=_primary_owner_actor_id(scenario),
                policy_id=f"private_equity_acquisition:{private_equity_source_holding_id}",
                requested_amount_usd=acquisition_proceeds,
                proceeds_destination=AccountType.CHECKING,
                opportunity_id=np.array([None] * rollout_count, dtype=object),
                opportunity_cause_id=np.array(
                    [
                        f"private_equity_acquisition:{private_equity_source_holding_id}:rollout:{i}:month:"
                        f"{int(month_index[month])}"
                        for i in range(rollout_count)
                    ],
                    dtype=object,
                ),
            )
            acquisition_application = PrivateEquitySaleApplication(
                sale_usd=acquisition_proceeds,
                basis_usd=acquisition_basis,
                taxable_gain_usd=acquisition_taxable_gain,
                sold_units=acquisition_units_sold,
                sold_fraction=acquisition_sold_fraction,
                remaining_units=np.zeros(rollout_count, dtype="float64"),
                remaining_basis_usd=np.zeros(rollout_count, dtype="float64"),
                remaining_fraction=np.zeros(rollout_count, dtype="float64"),
                journal_entries=(),
            )
            private_equity_sale_action_records.append(
                PrivateEquitySaleActionRecord(
                    month_position=month,
                    month_index=int(month_index[month]),
                    instruction=acquisition_instruction,
                    sale_application=acquisition_application,
                )
            )
            remaining_private_equity_fraction = acquisition_application.remaining_fraction
            remaining_private_equity_basis = acquisition_application.remaining_basis_usd
            remaining_private_equity_units = acquisition_application.remaining_units
            acquisition_sale_month = acquisition_proceeds
            acquisition_taxable_gain_month = acquisition_taxable_gain
            # Override pre-sale value to match cash proceeds for accounting
            # parity: the PE value debit and cash credit settle to zero.
            private_equity_value_before_sale = acquisition_proceeds
        market_opportunity = private_equity_sale_opportunity(
            sale_opportunity_mask=effective_pe_sale_opportunity_mask[:, month],
            private_equity_value_before_sale_usd=private_equity_value_before_sale,
            path_set_id=market_bundle.metadata.path_set_id,
            month_index=int(month_index[month]),
            source_holding_id=private_equity_source_holding_id,
        )
        _record_per_issuer_sale_opportunity_observations(
            pe_sale_opportunity_observations,
            scenario=scenario,
            market_bundle=market_bundle,
            month=month,
            month_index=int(month_index[month]),
            private_equity_value_before_sale_usd=private_equity_value_before_sale,
            pe_liquidity_regime=pe_liquidity_regime,
            engine_pe_issuer_key=engine_pe_issuer_key,
            aggregate_source_asset_id=private_equity_source_holding_id,
            aggregate_opportunity=market_opportunity,
        )
        market_sale_opportunity_value = market_opportunity.sale_opportunity_value_usd
        private_equity_sale_month = acquisition_sale_month.copy()
        private_equity_sale_taxable_gain_month = acquisition_taxable_gain_month.copy()
        private_equity_sale_tax_month = np.zeros(rollout_count, dtype="float64")
        sp500_multiplier = market_bundle.generic_sp500_multipliers[:, month]
        sp500_sale = np.zeros(rollout_count, dtype="float64")
        sp500_basis = np.zeros(rollout_count, dtype="float64")
        sp500_shortfall = np.zeros(rollout_count, dtype="float64")
        for policy_step in policy_steps:
            policy = policy_step.policy
            if isinstance(policy, PartnerEquityAccrualPolicy):
                continue
            if isinstance(policy, MonthlySpendPolicy):
                if month == 0:
                    continue
                spend_decision = monthly_spend_debit_instruction(
                    policy, inflation_multiplier=market_bundle.inflation_multipliers[:, month]
                )
                spend_application = apply_debit_account_instruction(spend_decision.debit, current_cash_usd=current_cash)
                current_cash = spend_application.current_cash_usd
                accounting.record_entry(month_index=int(month_index[month]), entry=spend_application.journal_entries[0])
                _record_monthly_spend_decisions(
                    policy_decisions,
                    month_index=int(month_index[month]),
                    policy_step=policy_step,
                    amount_usd=spend_decision.debit.amount_usd,
                    inflation_multiplier=spend_decision.inflation_multiplier,
                )
                continue
            if isinstance(policy, PrivateEquitySalePolicy):
                current_private_equity_value = (
                    initial_private_equity * remaining_private_equity_fraction * pe_value_multipliers[:, month]
                )
                current_opportunity = private_equity_sale_opportunity(
                    sale_opportunity_mask=effective_pe_sale_opportunity_mask[:, month],
                    private_equity_value_before_sale_usd=current_private_equity_value,
                    path_set_id=market_bundle.metadata.path_set_id,
                    month_index=int(month_index[month]),
                    source_holding_id=private_equity_source_holding_id,
                )
                liquid_net_worth = current_cash + remaining_sp500_units * sp500_multiplier
                sale_instruction = private_equity_sale_instruction(
                    policy, opportunity=current_opportunity, liquid_net_worth_usd=liquid_net_worth
                )
                _record_private_equity_sale_decisions(
                    policy_decisions,
                    month_index=int(month_index[month]),
                    policy_step=policy_step,
                    source_asset_id=private_equity_source_holding_id,
                    instruction=sale_instruction,
                    opportunity=current_opportunity,
                    liquid_net_worth_usd=liquid_net_worth,
                )
                sale_application = apply_private_equity_sale_instruction(
                    sale_instruction,
                    opportunity=current_opportunity,
                    remaining_basis_usd=remaining_private_equity_basis,
                    remaining_units=remaining_private_equity_units,
                    remaining_fraction=remaining_private_equity_fraction,
                )
                if sale_instruction.proceeds_destination is AssetType.GENERIC_SP500_STOCK:
                    remaining_sp500_units = remaining_sp500_units + np.divide(
                        sale_application.sale_usd,
                        sp500_multiplier,
                        out=np.zeros_like(sale_application.sale_usd),
                        where=sp500_multiplier > 0,
                    )
                    remaining_sp500_basis = remaining_sp500_basis + sale_application.sale_usd
                else:
                    current_cash = current_cash + sale_application.sale_usd
                private_equity_sale_action_records.append(
                    PrivateEquitySaleActionRecord(
                        month_position=month,
                        month_index=int(month_index[month]),
                        instruction=sale_instruction,
                        sale_application=sale_application,
                    )
                )
                remaining_private_equity_fraction = sale_application.remaining_fraction
                remaining_private_equity_basis = sale_application.remaining_basis_usd
                remaining_private_equity_units = sale_application.remaining_units
                private_equity_sale_month = private_equity_sale_month + sale_application.sale_usd
                private_equity_sale_taxable_gain_month = (
                    private_equity_sale_taxable_gain_month + sale_application.taxable_gain_usd
                )
                continue
            if isinstance(policy, CheckingFloorSellPublicStockPolicy):
                sp500_sale_instruction = checking_floor_sell_public_stock_instruction(
                    policy, current_cash_usd=current_cash
                )
                _record_sell_public_stock_decisions(
                    policy_decisions,
                    month_index=int(month_index[month]),
                    policy_step=policy_step,
                    current_cash_usd=current_cash,
                    requested_amount_usd=sp500_sale_instruction.requested_amount_usd,
                )
                sp500_sale_application = apply_generic_sp500_sale_instruction(
                    sp500_sale_instruction,
                    current_cash_usd=current_cash,
                    remaining_units=remaining_sp500_units,
                    remaining_basis_usd=remaining_sp500_basis,
                    sp500_unit_price_usd=sp500_multiplier,
                )
                current_cash = sp500_sale_application.current_cash_usd
                remaining_sp500_units = sp500_sale_application.remaining_units
                remaining_sp500_basis = sp500_sale_application.remaining_basis_usd
                sp500_sale = sp500_sale + sp500_sale_application.sale_usd
                sp500_basis = sp500_basis + sp500_sale_application.basis_usd
                sp500_shortfall = np.maximum(sp500_shortfall, sp500_sale_application.shortfall_usd)
                sp500_sale_action_records.append(
                    Sp500SaleActionRecord(
                        month_position=month,
                        month_index=int(month_index[month]),
                        policy=policy,
                        cause_id_prefix=f"policy:{policy.policy_id}:generic_sp500_sale",
                        numerics=_build_sale_action_frame(
                            {
                                "amount_usd": sp500_sale_application.sale_usd,
                                "basis_usd": sp500_sale_application.basis_usd,
                                "shortfall_usd": sp500_sale_application.shortfall_usd,
                            },
                            _SP500_SALE_ACTION_COLUMNS,
                        ),
                    )
                )
        sp500_value_after_sale = remaining_sp500_units * sp500_multiplier
        crypto_multiplier = crypto_value_multipliers[:, month]
        crypto_value_after_sale = remaining_crypto_quantity * crypto_multiplier

        # Phase 4b — TaxActor observes this month's taxable events,
        # then checks quarterly/year-end markers and settles tax
        # obligations inline via the same settlement function that
        # handles property-cost obligations. Observation EXCLUDES
        # tax-driven sales (which append to records during the inline
        # settlement below); those don't recurse-tax themselves.
        _sp500_gain_this_month = np.zeros(rollout_count, dtype="float64")
        for _sp500_record in sp500_sale_action_records[sp500_records_idx_at_observe:]:
            if _sp500_record.month_position == month:
                _sp500_gain_this_month = _sp500_gain_this_month + (
                    _sp500_record.column("amount_usd") - _sp500_record.column("basis_usd")
                )
        _pe_gain_this_month = np.zeros(rollout_count, dtype="float64")
        for _pe_record in private_equity_sale_action_records[pe_records_idx_at_observe:]:
            if _pe_record.month_position == month:
                _pe_gain_this_month = _pe_gain_this_month + _pe_record.sale_application.taxable_gain_usd
        _crypto_gain_this_month = np.zeros(rollout_count, dtype="float64")
        for _crypto_record in crypto_sale_action_records[crypto_records_idx_at_observe:]:
            if _crypto_record.month_position == month:
                _crypto_gain_this_month = _crypto_gain_this_month + (
                    _crypto_record.column("amount_usd") - _crypto_record.column("basis_usd")
                )
        # Snapshot pre-inline-tax records for this month so post-loop
        # annual_sale_tax_allocation sees the same gains TaxActor used.
        pre_inline_tax_sp500_records.extend(
            r for r in sp500_sale_action_records[sp500_records_idx_at_observe:] if r.month_position == month
        )
        pre_inline_tax_pe_records.extend(
            r for r in private_equity_sale_action_records[pe_records_idx_at_observe:] if r.month_position == month
        )
        pre_inline_tax_crypto_records.extend(
            r for r in crypto_sale_action_records[crypto_records_idx_at_observe:] if r.month_position == month
        )
        sp500_records_idx_at_observe = len(sp500_sale_action_records)
        pe_records_idx_at_observe = len(private_equity_sale_action_records)
        crypto_records_idx_at_observe = len(crypto_sale_action_records)

        _this_month_property_tax = (
            property_cash_flow.column("property_tax_usd")[:, month] * property_live_mask[:, month]
        )
        _this_month_property_depreciation = disposition.column("property_depreciation_usd")[:, month]
        _this_month_rental_taxable_income = (
            property_cash_flow.column("rental_income_usd")[:, month]
            - property_cash_flow.column("rental_management_fee_usd")[:, month]
            - property_cash_flow.column("rental_leasing_fee_usd")[:, month]
            - property_cash_flow.column("property_tax_usd")[:, month]
            - property_cash_flow.column("hoa_usd")[:, month]
            - property_cash_flow.column("insurance_usd")[:, month]
            - property_cash_flow.column("maintenance_usd")[:, month]
            - mortgage_interest[:, month]
            - _this_month_property_depreciation
        ) * property_live_mask[:, month]
        tax_actor.observe_month(
            month_index=int(month_index[month]),
            property_depreciation_recapture_usd=disposition.column("depreciation_recapture_usd")[:, month],
            taxable_property_capital_gain_usd=disposition.column("taxable_property_capital_gain_usd")[:, month],
            generic_sp500_sale_gain_usd=_sp500_gain_this_month,
            generic_crypto_sale_gain_usd=_crypto_gain_this_month,
            private_equity_sale_taxable_gain_usd=_pe_gain_this_month,
            net_rental_taxable_income_usd=_this_month_rental_taxable_income,
            property_tax_paid_usd=_this_month_property_tax,
            mortgage_interest_paid_usd=mortgage_interest[:, month],
            mortgage_principal_balance_usd=mortgage_balance[:, month] * property_live_mask[:, month],
        )
        # Year-end annual-tax: fires at offset 11 of the tax year OR
        # the last in-horizon month of the tax year — matches the
        # horizon-clipping behavior of the now-deleted
        # `_year_end_tax_obligation_due_usd`.
        _this_tax_year = int(month_index[month]) // MONTHS_PER_YEAR
        _is_last_month_of_year_in_horizon = (
            month == month_count - 1 or int(month_index[month + 1]) // MONTHS_PER_YEAR != _this_tax_year
        )
        for _tax_due, _accumulator, _tax_kind, _policy_id in (
            (
                tax_actor.quarterly_obligation_due(month_index=int(month_index[month])),
                estimated_tax_accumulator,
                _EstimatedTaxObligationKind(),
                ESTIMATED_TAX_ACCOUNTING_POLICY_ID,
            ),
            (
                tax_actor.annual_obligation_due(
                    month_index=int(month_index[month]), force_year_end=_is_last_month_of_year_in_horizon
                ),
                annual_tax_accumulator,
                _AnnualTaxObligationKind(),
                ANNUAL_TAX_ACCOUNTING_POLICY_ID,
            ),
        ):
            if _tax_due is None or not np.any(_tax_due > 0):
                continue
            _accumulator.amount_due_usd[:, month] = _tax_due
            _tax_settled = _settle_required_cash_obligation_at_month_position(
                market_bundle=market_bundle,
                month_position=month,
                due_month_index=int(month_index[month]),
                policy_steps=policy_steps,
                obligation_amount_usd=_tax_due,
                obligation_kind=_tax_kind,
                creditor_id="tax_authority",
                source_policy_id=_policy_id,
                actor_id=primary_owner_actor_id,
                sources=primary_owner_funding_sources,
                cash_usd=current_cash,
                generic_sp500_value_usd=remaining_sp500_units * sp500_multiplier,
                remaining_sp500_units=remaining_sp500_units,
                remaining_sp500_basis=remaining_sp500_basis,
                crypto_value_usd=remaining_crypto_quantity * crypto_multiplier,
                remaining_crypto_quantity=remaining_crypto_quantity,
                remaining_crypto_basis=remaining_crypto_basis,
                crypto_sale_usd=crypto_sale_usd,
                crypto_sale_basis_usd=crypto_sale_basis_usd,
                checking_floor_shortfall_usd=checking_floor_shortfall,
                accumulator=_accumulator,
                accounting=accounting,
                sp500_sale_action_records=sp500_sale_action_records,
                crypto_sale_action_records=crypto_sale_action_records,
                pe_state=pe_funding_state,
            )
            current_cash = _tax_settled.cash_usd
            remaining_sp500_units = _tax_settled.remaining_sp500_units
            remaining_sp500_basis = _tax_settled.remaining_sp500_basis
            remaining_crypto_quantity = _tax_settled.remaining_crypto_quantity
            remaining_crypto_basis = _tax_settled.remaining_crypto_basis
            if isinstance(_tax_kind, _EstimatedTaxObligationKind):
                tax_actor.record_estimated_paid(
                    month_index=int(month_index[month]), paid_usd=_accumulator.amount_paid_usd[:, month]
                )
        # Mortgage + special-assessment + outside-rent obligations: same
        # inline-settlement pattern as the tax block above. Order
        # within the iteration: property-cost → tax → mortgage →
        # special_assessment → outside_rent, matching today's
        # post-loop order to preserve cash-priority behavior for
        # cash-strapped rollouts.
        for _ob_due, _ob_acc, _ob_creditor, _ob_policy_id in (
            (mortgage_payment_due, mortgage_accumulator, "mortgage_lender", MORTGAGE_SERVICING_POLICY_ID),
            (special_assessment_due, special_assessment_accumulator, "hoa", SPECIAL_ASSESSMENT_POLICY_ID),
            (outside_rent_due, outside_rent_accumulator, "landlord", OUTSIDE_RENT_POLICY_ID),
        ):
            if _ob_acc is None:
                continue
            _ob_month_due = _ob_due[:, month]
            if not np.any(_ob_month_due > 0):
                continue
            _ob_settled = _settle_required_cash_obligation_at_month_position(
                market_bundle=market_bundle,
                month_position=month,
                due_month_index=int(month_index[month]),
                policy_steps=policy_steps,
                obligation_amount_usd=_ob_month_due,
                obligation_kind=_ob_acc.obligation_kind,
                creditor_id=_ob_creditor,
                source_policy_id=_ob_policy_id,
                actor_id=primary_owner_actor_id,
                sources=primary_owner_funding_sources,
                cash_usd=current_cash,
                generic_sp500_value_usd=remaining_sp500_units * sp500_multiplier,
                remaining_sp500_units=remaining_sp500_units,
                remaining_sp500_basis=remaining_sp500_basis,
                crypto_value_usd=remaining_crypto_quantity * crypto_multiplier,
                remaining_crypto_quantity=remaining_crypto_quantity,
                remaining_crypto_basis=remaining_crypto_basis,
                crypto_sale_usd=crypto_sale_usd,
                crypto_sale_basis_usd=crypto_sale_basis_usd,
                checking_floor_shortfall_usd=checking_floor_shortfall,
                accumulator=_ob_acc,
                accounting=accounting,
                sp500_sale_action_records=sp500_sale_action_records,
                crypto_sale_action_records=crypto_sale_action_records,
                pe_state=pe_funding_state,
            )
            current_cash = _ob_settled.cash_usd
            remaining_sp500_units = _ob_settled.remaining_sp500_units
            remaining_sp500_basis = _ob_settled.remaining_sp500_basis
            remaining_crypto_quantity = _ob_settled.remaining_crypto_quantity
            remaining_crypto_basis = _ob_settled.remaining_crypto_basis
        # Partner-contribution obligations: per-agreement settlement on the
        # contributing actor's checking cash. Each agreement has its own
        # 1D state vectors (independent of the primary owner's cash path);
        # the settlement journal entry is a cross-actor transfer that
        # debits the owner and credits the contributor, so settling here
        # doesn't compete with the primary owner's cash for the month's
        # taxes / mortgage / special-assessment / outside-rent.
        for _partner_ctx in partner_settlement_contexts:
            _partner_due = _partner_ctx.accumulator.amount_due_usd[:, month]
            if not np.any(_partner_due > 0):
                continue
            _partner_settled = _settle_required_cash_obligation_at_month_position(
                market_bundle=market_bundle,
                month_position=month,
                due_month_index=int(month_index[month]),
                policy_steps=policy_steps,
                obligation_amount_usd=_partner_due,
                obligation_kind=_partner_ctx.accumulator.obligation_kind,
                creditor_id=primary_owner_actor_id,
                source_policy_id=_partner_ctx.agreement.policy.policy_id,
                actor_id=_partner_ctx.agreement.policy.actor_id,
                sources=_partner_ctx.sources,
                cash_usd=_partner_ctx.current_cash,
                generic_sp500_value_usd=_partner_ctx.remaining_sp500_units * sp500_multiplier,
                remaining_sp500_units=_partner_ctx.remaining_sp500_units,
                remaining_sp500_basis=_partner_ctx.remaining_sp500_basis,
                crypto_value_usd=_partner_ctx.remaining_crypto_quantity * crypto_multiplier,
                remaining_crypto_quantity=_partner_ctx.remaining_crypto_quantity,
                remaining_crypto_basis=_partner_ctx.remaining_crypto_basis,
                crypto_sale_usd=_partner_ctx.crypto_sale_usd,
                crypto_sale_basis_usd=_partner_ctx.crypto_sale_basis_usd,
                checking_floor_shortfall_usd=_partner_ctx.checking_floor_shortfall_usd,
                accumulator=_partner_ctx.accumulator,
                accounting=accounting,
                sp500_sale_action_records=sp500_sale_action_records,
                crypto_sale_action_records=crypto_sale_action_records,
            )
            _partner_ctx.current_cash = _partner_settled.cash_usd
            _partner_ctx.remaining_sp500_units = _partner_settled.remaining_sp500_units
            _partner_ctx.remaining_sp500_basis = _partner_settled.remaining_sp500_basis
            _partner_ctx.remaining_crypto_quantity = _partner_settled.remaining_crypto_quantity
            _partner_ctx.remaining_crypto_basis = _partner_settled.remaining_crypto_basis
        # Recompute the per-asset values after any inline tax sales for
        # the snapshot block below.
        sp500_value_after_sale = remaining_sp500_units * sp500_multiplier
        crypto_value_after_sale = remaining_crypto_quantity * crypto_multiplier

        # End-of-month snapshot. Rebuild `SimulationState` from the 1D
        # locals and use it as the source for the cash + SP500 + crypto
        # + PE per-asset matrix columns the state object covers; the
        # other matrix lines (sale gain, value, PE sale tax) still read
        # from locals.
        state = _build_state(month_position=month, month_col=month)
        owner_agent = state.agent(primary_owner_actor_id)
        cash[:, month] = owner_agent.cash(cash_account_id)
        sp500_holding = owner_agent.holding("sp500")
        remaining_sp500_units_by_month[:, month] = sp500_holding.units
        remaining_sp500_basis_by_month[:, month] = sp500_holding.basis_usd
        generic_sp500_value[:, month] = sp500_value_after_sale
        # `generic_sp500_sale_gain` is derived from the unified
        # `asset_change_log` after the main loop (see _build_asset_change_log
        # call just before annual_sale_tax_allocation). The old imperative
        # overwrite `generic_sp500_sale_gain[:, month] = sp500_sale -
        # sp500_basis` missed property-cost-driven sales' gains (those
        # flow through `sp500_sale_action_records` rather than the
        # within-month-policy locals).
        checking_floor_shortfall[:, month] = sp500_shortfall
        crypto_holding = owner_agent.holding("crypto")
        remaining_crypto_quantity_by_month[:, month] = crypto_holding.units
        remaining_crypto_basis_by_month[:, month] = crypto_holding.basis_usd
        crypto_value[:, month] = crypto_value_after_sale
        # `private_equity_sale_taxable_gain` is derived from the unified
        # `asset_change_log` after the main loop (same pattern as SP500).
        # The previous imperative snapshot from `private_equity_sale_taxable_gain_month`
        # local accumulator was only populated by acquisition + within-month
        # PE policy sales; today's behavior was patched up by the settlement
        # function's `pe_state.private_equity_sale_taxable_gain_usd[:, M] += ...`
        # add at the post-loop tax-settlement path (now also removed). All
        # PE sale paths emit into `private_equity_sale_action_records`, so
        # the log-derived matrix sees every sale uniformly.
        private_equity_sale_tax[:, month] = private_equity_sale_tax_month
        private_equity_sale_opportunity_value[:, month] = np.maximum(
            0.0, market_sale_opportunity_value - private_equity_sale_month
        )
        private_equity_value[:, month] = private_equity_value_before_sale - private_equity_sale_month
        private_equity_sale_usd_by_month[:, month] = private_equity_sale_month
        pe_holding = owner_agent.holding("private_equity")
        remaining_private_equity_units_by_month[:, month] = pe_holding.units
        remaining_private_equity_basis_by_month[:, month] = pe_holding.basis_usd

    # Property-cost obligation accumulators emit their row-blocks once the
    # month loop has finished writing per-month decisions into their matrices.
    for accumulator in property_cost_obligation_accumulators:
        accumulator.emit_obligations(obligations)
        accumulator.emit_funding_decisions(funding_decisions)

    property_tax_for_tax_allocation = property_cash_flow.column("property_tax_usd") * property_live_mask
    net_rental_taxable_income = (
        property_cash_flow.column("rental_income_usd")
        - property_cash_flow.column("rental_management_fee_usd")
        - property_cash_flow.column("rental_leasing_fee_usd")
        - property_cash_flow.column("property_tax_usd")
        - property_cash_flow.column("hoa_usd")
        - property_cash_flow.column("insurance_usd")
        - property_cash_flow.column("maintenance_usd")
        - mortgage_interest
        - disposition.column("property_depreciation_usd")
    ) * property_live_mask
    # Build the unified capital-gain event log from the per-asset-class
    # action-record lists + property disposition. All SP500/crypto/PE/
    # property sales emit into this single frame; downstream
    # `derive_per_month_taxable_gain_matrix(asset_kind=..., tax_treatment=...)`
    # provides the per-month gain matrices that tax-allocation /
    # tax-share consumers need. This replaces the previous imperative
    # `generic_sp500_sale_gain[:, month] = sp500_sale - sp500_basis`
    # overwrite at end-of-month, which missed property-cost-driven
    # main-loop SP500 sales' gains (those only flowed through
    # `sp500_sale_action_records`, never updating the gain matrix).
    # Build the asset_change_log from the pre-inline-tax record lists.
    # These exclude tax-driven sales (which were appended during each
    # iteration's inline tax settlement, AFTER TaxActor.observe_month),
    # so the gain matrices fed to `annual_sale_tax_allocation` match
    # the gains TaxActor used to size the inline tax obligations.
    # Without this filter, downstream consumers see a
    # TOTAL_INCOME_TAX_USD that recursively taxes tax-driven sales —
    # diverging from what was actually settled and breaking
    # cash-balance assertions.
    asset_change_log = _build_asset_change_log(
        sp500_records=pre_inline_tax_sp500_records,
        crypto_records=pre_inline_tax_crypto_records,
        pe_records=pre_inline_tax_pe_records,
        disposition=disposition,
        primary_owner_actor_id=primary_owner_actor_id,
        sp500_asset_id=(
            primary_owner_funding_sources.sp500_source_asset.asset_id
            if primary_owner_funding_sources.sp500_source_asset is not None
            else "sp500"
        ),
        property_id=scenario.property_selection.property_id,
        month_index=month_index,
    )
    generic_sp500_sale_gain = derive_per_month_taxable_gain_matrix(
        asset_change_log,
        rollout_count=rollout_count,
        month_index=month_index,
        asset_kind=AssetKindForLog.GENERIC_SP500,
        tax_treatment=TaxTreatment.LONG_TERM_CAPITAL,
    )
    generic_crypto_sale_gain = derive_per_month_taxable_gain_matrix(
        asset_change_log,
        rollout_count=rollout_count,
        month_index=month_index,
        asset_kind=AssetKindForLog.CRYPTO,
        tax_treatment=TaxTreatment.LONG_TERM_CAPITAL,
    )
    private_equity_sale_taxable_gain = derive_per_month_taxable_gain_matrix(
        asset_change_log,
        rollout_count=rollout_count,
        month_index=month_index,
        asset_kind=AssetKindForLog.PRIVATE_EQUITY,
        tax_treatment=TaxTreatment.LONG_TERM_CAPITAL,
    )
    # Reuse the federal+CA year totals TaxActor already computed inline
    # (one set of `federal_income_tax_due_usd` / `california_income_tax_due_usd`
    # calls per year) — annual_sale_tax_allocation reads them back via
    # `precomputed_year_tax_usd` and skips its own per-year computation,
    # so the year-tax math runs exactly once per scenario per year.
    annual_tax = annual_sale_tax_allocation(
        scenario.tax_profile,
        month_index=month_index,
        property_depreciation_recapture_usd=disposition.column("depreciation_recapture_usd"),
        taxable_property_capital_gain_usd=disposition.column("taxable_property_capital_gain_usd"),
        generic_sp500_sale_gain_usd=generic_sp500_sale_gain,
        generic_crypto_sale_gain_usd=generic_crypto_sale_gain,
        private_equity_sale_taxable_gain_usd=private_equity_sale_taxable_gain,
        property_tax_usd=property_tax_for_tax_allocation,
        mortgage_interest_usd=mortgage_interest,
        mortgage_principal_balance_usd=mortgage_balance * property_live_mask,
        net_rental_taxable_income_usd=net_rental_taxable_income,
        precomputed_year_tax_usd={
            year: (federal, tax_actor.year_california_tax_usd[year])
            for year, federal in tax_actor.year_federal_tax_usd.items()
        },
    )
    generic_sp500_sale_tax = annual_tax.generic_sp500_sale_tax_usd
    generic_crypto_sale_tax = annual_tax.generic_crypto_sale_tax_usd
    private_equity_sale_tax = annual_tax.private_equity_sale_tax_usd
    property_sale_tax = annual_tax.property_sale_tax_usd
    # Property sale net proceeds reflect the cash actually received at sale. Tax is
    # accrued in the source month but settled at year-end via the annual-tax
    # obligation path, so it does not reduce the sale-event cash inflow.
    property_sale_net_proceeds = (
        disposition.column("property_sale_gross_usd")
        - disposition.column("sale_closing_cost_usd")
        - disposition.column("property_sale_debt_payoff_usd")
    )
    partner_equity = _settle_partner_equity_on_property_sale(
        partner_equity, sale_month=disposition.sale_month, property_sale_net_proceeds_usd=property_sale_net_proceeds
    )
    # Phase 4b: estimated_tax and annual_tax obligation emission +
    # settlement now happen inline in the main month loop via TaxActor.
    # The accumulators were constructed pre-loop; emit their row-blocks
    # now that the loop has finished writing per-month decisions.
    estimated_tax_accumulator.emit_obligations(obligations)
    estimated_tax_accumulator.emit_funding_decisions(funding_decisions)
    annual_tax_accumulator.emit_obligations(obligations)
    annual_tax_accumulator.emit_funding_decisions(funding_decisions)

    # Re-derive the gain matrices from the (now-final) action records.
    # The post-loop tax settlement above may have appended more PE
    # (and possibly SP500) sale records via the obligation-funding
    # chain; the downstream `_tax_share_for_sale_action` loops at
    # :~2519 / :~2570 read these matrices as denominators per (rollout,
    # month). Today's behavior includes post-loop sales in the PE
    # denominator (via the now-removed `pe_state.private_equity_sale
    # _taxable_gain_usd[:, M] +=` in settlement); the SP500 denominator
    # only ever had main-loop sales (today's `[:, month] = sp500_sale -
    # sp500_basis` overwrite limited it to within-month-policy locals,
    # and post-loop settlement never updated this matrix). Match both
    # by re-building the asset_change_log over all records and
    # re-deriving PE; SP500 stays at its main-loop value so existing
    # consumers don't see a behavior change from including post-loop
    # SP500 sales in the denominator.
    asset_change_log = _build_asset_change_log(
        sp500_records=sp500_sale_action_records,
        crypto_records=crypto_sale_action_records,
        pe_records=private_equity_sale_action_records,
        disposition=disposition,
        primary_owner_actor_id=primary_owner_actor_id,
        sp500_asset_id=(
            primary_owner_funding_sources.sp500_source_asset.asset_id
            if primary_owner_funding_sources.sp500_source_asset is not None
            else "sp500"
        ),
        property_id=scenario.property_selection.property_id,
        month_index=month_index,
    )
    private_equity_sale_taxable_gain = derive_per_month_taxable_gain_matrix(
        asset_change_log,
        rollout_count=rollout_count,
        month_index=month_index,
        asset_kind=AssetKindForLog.PRIVATE_EQUITY,
        tax_treatment=TaxTreatment.LONG_TERM_CAPITAL,
    )

    partner_present = np.full((rollout_count, month_count), _has_partner(scenario), dtype=np.bool_)
    owner_home_equity_claim = partner_equity.column("owner_home_equity_claim_usd")
    if disposition.sale_month is None:
        owner_home_equity_claim_for_net_worth = owner_home_equity_claim
    else:
        unsold_mask = (month_index < disposition.sale_month).astype("float64")
        unsold_mask = np.broadcast_to(unsold_mask[None, :], (rollout_count, month_count))
        owner_home_equity_claim_for_net_worth = owner_home_equity_claim * unsold_mask
    liquid_net_worth = cash + generic_sp500_value + crypto_value
    net_worth = cash + generic_sp500_value + crypto_value + private_equity_value + owner_home_equity_claim_for_net_worth
    _record_property_sale_effects(
        effects,
        scenario=scenario,
        disposition=disposition,
        tax_usd=property_sale_tax,
        net_proceeds_usd=property_sale_net_proceeds,
    )
    _record_property_sale_journal_entries(
        accounting,
        lot_dispositions,
        scenario=scenario,
        disposition=disposition,
        tax_usd=property_sale_tax,
        net_proceeds_usd=property_sale_net_proceeds,
    )
    _record_property_sale_accounting_details(
        property_sale_basis_gain_details, scenario=scenario, disposition=disposition
    )
    _record_tax_payment_allocation_details(
        tax_payment_allocation_details,
        scenario=scenario,
        month_index=month_index,
        annual_tax=annual_tax,
        property_depreciation_recapture_usd=disposition.column("depreciation_recapture_usd"),
        taxable_property_capital_gain_usd=disposition.column("taxable_property_capital_gain_usd"),
        generic_sp500_sale_gain_usd=generic_sp500_sale_gain,
        generic_crypto_sale_gain_usd=generic_crypto_sale_gain,
        private_equity_sale_taxable_gain_usd=private_equity_sale_taxable_gain,
        net_rental_taxable_income_usd=net_rental_taxable_income,
    )
    for sp500_sale_action_record in sp500_sale_action_records:
        source_tax = _tax_share_for_sale_action(
            source_tax_usd=generic_sp500_sale_tax[:, sp500_sale_action_record.month_position],
            action_taxable_income_usd=np.maximum(
                0.0, sp500_sale_action_record.column("amount_usd") - sp500_sale_action_record.column("basis_usd")
            ),
            source_taxable_income_usd=np.maximum(
                0.0, generic_sp500_sale_gain[:, sp500_sale_action_record.month_position]
            ),
        )
        _record_sp500_sale_journal_entries(
            accounting,
            lot_dispositions,
            month_index=sp500_sale_action_record.month_index,
            policy=sp500_sale_action_record.policy,
            cause_id_prefix=sp500_sale_action_record.cause_id_prefix,
            amount_usd=sp500_sale_action_record.column("amount_usd"),
            basis_usd=sp500_sale_action_record.column("basis_usd"),
            tax_usd=source_tax,
        )
        _record_sp500_sale_effects(
            effects,
            month_index=sp500_sale_action_record.month_index,
            policy=sp500_sale_action_record.policy,
            amount_usd=sp500_sale_action_record.column("amount_usd"),
            basis_usd=sp500_sale_action_record.column("basis_usd"),
            tax_usd=source_tax,
            shortfall_usd=sp500_sale_action_record.column("shortfall_usd"),
        )
    for crypto_sale_action_record in crypto_sale_action_records:
        source_tax = _tax_share_for_sale_action(
            source_tax_usd=generic_crypto_sale_tax[:, crypto_sale_action_record.month_position],
            action_taxable_income_usd=np.maximum(
                0.0, crypto_sale_action_record.column("amount_usd") - crypto_sale_action_record.column("basis_usd")
            ),
            source_taxable_income_usd=np.maximum(
                0.0, generic_crypto_sale_gain[:, crypto_sale_action_record.month_position]
            ),
        )
        _record_crypto_sale_journal_entries(
            accounting,
            lot_dispositions,
            month_index=crypto_sale_action_record.month_index,
            policy=crypto_sale_action_record.policy,
            cause_id_prefix=crypto_sale_action_record.cause_id_prefix,
            source_asset_id=crypto_sale_action_record.source_asset_id,
            amount_usd=crypto_sale_action_record.column("amount_usd"),
            basis_usd=crypto_sale_action_record.column("basis_usd"),
            tax_usd=source_tax,
        )
        _record_crypto_sale_effects(
            effects,
            month_index=crypto_sale_action_record.month_index,
            policy=crypto_sale_action_record.policy,
            source_asset_id=crypto_sale_action_record.source_asset_id,
            asset_symbol=crypto_sale_action_record.asset_symbol,
            amount_usd=crypto_sale_action_record.column("amount_usd"),
            basis_usd=crypto_sale_action_record.column("basis_usd"),
            quantity_sold=crypto_sale_action_record.column("quantity_sold"),
            shortfall_usd=crypto_sale_action_record.column("shortfall_usd"),
        )
    for private_equity_sale_action_record in private_equity_sale_action_records:
        source_tax = _tax_share_for_sale_action(
            source_tax_usd=private_equity_sale_tax[:, private_equity_sale_action_record.month_position],
            action_taxable_income_usd=private_equity_sale_action_record.sale_application.taxable_gain_usd,
            source_taxable_income_usd=private_equity_sale_taxable_gain[
                :, private_equity_sale_action_record.month_position
            ],
        )
        _record_private_equity_sale_journal_entries(
            accounting,
            lot_dispositions,
            month_index=private_equity_sale_action_record.month_index,
            instruction=private_equity_sale_action_record.instruction,
            sale_application=private_equity_sale_action_record.sale_application,
            tax_usd=source_tax,
            source_holding_id=private_equity_source_holding_id,
        )
        _record_private_equity_sale_effects(
            effects,
            month_index=private_equity_sale_action_record.month_index,
            instruction=private_equity_sale_action_record.instruction,
            sale_application=private_equity_sale_action_record.sale_application,
            estimated_tax_usd=source_tax,
        )
    _record_partner_contribution_decisions(policy_decisions, month_index=month_index, partner_equity=partner_equity)
    _record_partner_agreement_accounting_detail(accounting, month_index=month_index, partner_equity=partner_equity)
    # Mortgage / special_assessment / outside_rent settlements happen
    # inline in the main month loop above (at the per-iteration block
    # at :~2484). Here we just flush the per-accumulator obligation +
    # funding-decision row blocks to the event streams.
    if mortgage_accumulator is not None:
        mortgage_accumulator.emit_obligations(obligations)
        mortgage_accumulator.emit_funding_decisions(funding_decisions)
    special_assessment_accumulator.emit_obligations(obligations)
    special_assessment_accumulator.emit_funding_decisions(funding_decisions)
    outside_rent_accumulator.emit_obligations(obligations)
    outside_rent_accumulator.emit_funding_decisions(funding_decisions)
    # Partner-contribution obligations: settlement happens inline in the
    # main month loop above (per-agreement, after the primary owner's
    # tax/mortgage/special/outside_rent block). Flush each agreement's
    # accumulator row blocks to the event streams here.
    for _partner_ctx in partner_settlement_contexts:
        _partner_ctx.accumulator.emit_obligations(obligations)
        _partner_ctx.accumulator.emit_funding_decisions(funding_decisions)
    _record_journal_entry_batches(
        accounting,
        month_index=month_index,
        entries=property_cash_flow.journal_entries,
        amount_multiplier=property_live_mask,
    )
    if disposition.sale_month is None:
        property_balance_mask = property_live_mask
    else:
        property_balance_mask = (month_index < disposition.sale_month).astype("float64")
        property_balance_mask = np.broadcast_to(property_balance_mask[None, :], (rollout_count, month_count)).copy()
    _record_state_balance_snapshots(
        accounting,
        scenario=scenario,
        month_index=month_index,
        cash_usd=cash,
        generic_sp500_value_usd=generic_sp500_value,
        crypto_value_usd=crypto_value,
        private_equity_value_usd=private_equity_value,
        property_value_usd=property_value,
        mortgage_balance_usd=mortgage_balance,
        property_balance_mask=property_balance_mask,
    )
    accounting_trace = accounting.finalize()
    validate_trace(accounting_trace)
    monthly_spend_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.MONTHLY_LIVING_EXPENSE,
        side=PostingSide.DEBIT,
    )
    mortgage_interest_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.MORTGAGE_INTEREST_EXPENSE,
        side=PostingSide.DEBIT,
    )
    mortgage_principal_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.MORTGAGE_PAYABLE,
        side=PostingSide.DEBIT,
        journal_entry_type=JournalEntryType.MORTGAGE_PAYMENT,
    )
    mortgage_payment_from_accounting = mortgage_interest_from_accounting + mortgage_principal_from_accounting
    property_tax_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PROPERTY_TAX_EXPENSE,
        side=PostingSide.DEBIT,
    )
    hoa_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.HOA_EXPENSE,
        side=PostingSide.DEBIT,
    )
    insurance_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.INSURANCE_EXPENSE,
        side=PostingSide.DEBIT,
    )
    maintenance_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.MAINTENANCE_EXPENSE,
        side=PostingSide.DEBIT,
    )
    rental_income_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.RENTAL_INCOME,
        side=PostingSide.CREDIT,
    )
    rental_management_fee_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.RENTAL_MANAGEMENT_FEE_EXPENSE,
        side=PostingSide.DEBIT,
    )
    rental_leasing_fee_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.RENTAL_LEASING_FEE_EXPENSE,
        side=PostingSide.DEBIT,
    )
    property_carrying_cost_from_accounting = (
        property_tax_from_accounting
        + hoa_from_accounting
        + insurance_from_accounting
        + maintenance_from_accounting
        + rental_management_fee_from_accounting
        + rental_leasing_fee_from_accounting
    )
    net_property_cash_flow_from_accounting = (
        rental_income_from_accounting - property_carrying_cost_from_accounting - mortgage_payment_from_accounting
    )
    generic_sp500_sale_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PUBLIC_SECURITY,
        side=PostingSide.CREDIT,
        journal_entry_type=JournalEntryType.ASSET_SALE,
    )
    generic_sp500_sale_basis_from_accounting = _lot_disposition_amount_matrix(
        lot_dispositions,
        rollout_count=rollout_count,
        month_index=month_index,
        asset_class=LotAssetClass.PUBLIC_SECURITY,
        amount_field="cost_basis_usd",
    )
    generic_sp500_sale_tax_from_accounting = _lot_disposition_amount_matrix(
        lot_dispositions,
        rollout_count=rollout_count,
        month_index=month_index,
        asset_class=LotAssetClass.PUBLIC_SECURITY,
        amount_field="tax_expense_usd",
    )
    crypto_sale_tax_from_accounting = _lot_disposition_amount_matrix(
        lot_dispositions,
        rollout_count=rollout_count,
        month_index=month_index,
        asset_class=LotAssetClass.CRYPTO,
        amount_field="tax_expense_usd",
    )
    private_equity_sale_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PRIVATE_EQUITY,
        side=PostingSide.CREDIT,
        journal_entry_type=JournalEntryType.ASSET_SALE,
    )
    private_equity_sale_basis_from_accounting = _lot_disposition_amount_matrix(
        lot_dispositions,
        rollout_count=rollout_count,
        month_index=month_index,
        asset_class=LotAssetClass.PRIVATE_EQUITY,
        amount_field="cost_basis_usd",
    )
    private_equity_sale_tax_from_accounting = _lot_disposition_amount_matrix(
        lot_dispositions,
        rollout_count=rollout_count,
        month_index=month_index,
        asset_class=LotAssetClass.PRIVATE_EQUITY,
        amount_field="tax_expense_usd",
    )
    property_sale_gross_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PROPERTY,
        side=PostingSide.CREDIT,
        journal_entry_type=JournalEntryType.PROPERTY_SALE,
    )
    sale_closing_cost_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PROPERTY_SALE_CLOSING_EXPENSE,
        side=PostingSide.DEBIT,
        journal_entry_type=JournalEntryType.PROPERTY_SALE,
    )
    property_sale_debt_payoff_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.MORTGAGE_PAYABLE,
        side=PostingSide.DEBIT,
        journal_entry_type=JournalEntryType.PROPERTY_SALE,
    )
    # Sale tax no longer posts to the property sale journal entry (it accrues
    # per source month and settles at year-end via the annual-tax obligation
    # pipeline). Per-sale tax attribution lives on the lot disposition row.
    property_sale_tax_from_accounting = _lot_disposition_amount_matrix(
        lot_dispositions,
        rollout_count=rollout_count,
        month_index=month_index,
        asset_class=LotAssetClass.PROPERTY,
        amount_field="tax_expense_usd",
    )
    property_sale_cash_in_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.CHECKING_CASH,
        side=PostingSide.DEBIT,
        journal_entry_type=JournalEntryType.PROPERTY_SALE,
    )
    property_sale_cash_out_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.CHECKING_CASH,
        side=PostingSide.CREDIT,
        journal_entry_type=JournalEntryType.PROPERTY_SALE,
    )
    property_sale_net_proceeds_from_accounting = (
        property_sale_cash_in_from_accounting - property_sale_cash_out_from_accounting
    )
    partner_contribution_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PARTNER_CONTRIBUTION_TRANSFER,
        side=PostingSide.CREDIT,
    )
    partner_contribution_used_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PARTNER_CONTRIBUTION_USED,
        side=PostingSide.DEBIT,
    )
    partner_unallocated_excess_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PARTNER_UNALLOCATED_CLAIM,
        side=PostingSide.DEBIT,
    )
    partner_principal_credit_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PARTNER_PRINCIPAL_CREDIT,
        side=PostingSide.DEBIT,
    )
    owner_principal_credit_from_accounting = _posting_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.OWNER_PRINCIPAL_CREDIT,
        side=PostingSide.DEBIT,
    )
    partner_equity_ledger_from_snapshot = _balance_snapshot_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PARTNER_EQUITY_LEDGER,
    )
    owner_equity_ledger_from_snapshot = _balance_snapshot_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.OWNER_EQUITY_LEDGER,
    )
    partner_home_equity_claim_from_snapshot = _balance_snapshot_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PARTNER_HOME_EQUITY_CLAIM,
    )
    owner_home_equity_claim_from_snapshot = _balance_snapshot_amount_matrix(
        accounting_trace,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.OWNER_HOME_EQUITY_CLAIM,
    )
    if not partner_equity.agreements:
        owner_principal_credit_from_accounting = partner_equity.column("owner_principal_usd")
        partner_equity_ledger_from_snapshot = partner_equity.column("partner_equity_ledger_usd")
        owner_equity_ledger_from_snapshot = partner_equity.column("owner_equity_ledger_usd")
        partner_home_equity_claim_from_snapshot = partner_equity.column("home_equity_claim_usd")
        owner_home_equity_claim_from_snapshot = partner_equity.column("owner_home_equity_claim_usd")
    federal_income_tax_from_accounting = _accounting_detail_amount_matrix(
        tax_payment_allocation_details,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_field="federal_income_tax_usd",
    )
    california_income_tax_from_accounting = _accounting_detail_amount_matrix(
        tax_payment_allocation_details,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_field="california_income_tax_usd",
    )
    total_income_tax_from_accounting = _accounting_detail_amount_matrix(
        tax_payment_allocation_details,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_field="total_income_tax_usd",
    )
    rental_income_tax_from_accounting = _accounting_detail_amount_matrix(
        tax_payment_allocation_details,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_field="rental_income_tax_usd",
    )
    property_sale_adjusted_basis_from_accounting = _accounting_detail_amount_matrix(
        property_sale_basis_gain_details,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_field="adjusted_basis_usd",
    )
    realized_property_gain_from_accounting = _accounting_detail_amount_matrix(
        property_sale_basis_gain_details,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_field="realized_gain_usd",
    )
    property_sale_capital_gain_from_accounting = _accounting_detail_amount_matrix(
        property_sale_basis_gain_details,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_field="capital_gain_usd",
    )
    property_sale_capital_gain_exclusion_from_accounting = _accounting_detail_amount_matrix(
        property_sale_basis_gain_details,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_field="capital_gain_exclusion_usd",
    )
    taxable_property_capital_gain_from_accounting = _accounting_detail_amount_matrix(
        property_sale_basis_gain_details,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_field="taxable_capital_gain_usd",
    )
    taxable_property_gain_from_accounting = _accounting_detail_amount_matrix(
        property_sale_basis_gain_details,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_field="taxable_gain_usd",
    )
    depreciation_recapture_from_accounting = _accounting_detail_amount_matrix(
        property_sale_basis_gain_details,
        rollout_count=rollout_count,
        month_index=month_index,
        amount_field="depreciation_recapture_usd",
    )
    trace_identity_by_rollout = _trace_identity_by_rollout(scenario, market_bundle)
    trace_identity_frame = event_streams.build_identity_frame(trace_identity_by_rollout)
    metric_arrays: dict[str, np.ndarray] = {
        "cash_usd": cash,
        "generic_sp500_value_usd": generic_sp500_value,
        "generic_sp500_sale_usd": generic_sp500_sale_from_accounting,
        "generic_sp500_sale_basis_usd": generic_sp500_sale_basis_from_accounting,
        "generic_sp500_sale_gain_usd": generic_sp500_sale_from_accounting - generic_sp500_sale_basis_from_accounting,
        "generic_sp500_sale_tax_usd": generic_sp500_sale_tax_from_accounting,
        "crypto_value_usd": crypto_value,
        "crypto_sale_usd": crypto_sale_usd,
        "crypto_sale_basis_usd": crypto_sale_basis_usd,
        "crypto_sale_gain_usd": crypto_sale_usd - crypto_sale_basis_usd,
        "crypto_sale_tax_usd": crypto_sale_tax_from_accounting,
        "checking_floor_action_usd": generic_sp500_sale_from_accounting,
        "checking_floor_shortfall_usd": checking_floor_shortfall,
        "private_equity_value_usd": private_equity_value,
        "private_equity_sale_opportunity_value_usd": private_equity_sale_opportunity_value,
        "private_equity_sale_usd": private_equity_sale_from_accounting,
        "private_equity_sale_basis_usd": private_equity_sale_basis_from_accounting,
        "private_equity_sale_tax_usd": private_equity_sale_tax_from_accounting,
        "rental_income_tax_usd": rental_income_tax_from_accounting,
        "federal_income_tax_usd": federal_income_tax_from_accounting,
        "california_income_tax_usd": california_income_tax_from_accounting,
        "total_income_tax_usd": total_income_tax_from_accounting,
        "private_equity_sale_opportunity_event": private_equity_sale_opportunity_event,
        "property_value_usd": property_value,
        "mortgage_balance_usd": mortgage_balance,
        "mortgage_interest_usd": mortgage_interest_from_accounting,
        "mortgage_principal_usd": mortgage_principal_from_accounting,
        "mortgage_payment_usd": mortgage_payment_from_accounting,
        "property_tax_usd": property_tax_from_accounting,
        "hoa_usd": hoa_from_accounting,
        "insurance_usd": insurance_from_accounting,
        "maintenance_usd": maintenance_from_accounting,
        "rental_income_usd": rental_income_from_accounting,
        "rental_management_fee_usd": rental_management_fee_from_accounting,
        "rental_leasing_fee_usd": rental_leasing_fee_from_accounting,
        "property_carrying_cost_usd": property_carrying_cost_from_accounting,
        "net_property_cash_flow_usd": net_property_cash_flow_from_accounting,
        "purchase_closing_cost_usd": disposition.column("purchase_closing_cost_usd"),
        "sale_closing_cost_usd": sale_closing_cost_from_accounting,
        "property_depreciation_usd": disposition.column("property_depreciation_usd"),
        "cumulative_property_depreciation_usd": disposition.column("cumulative_property_depreciation_usd"),
        "property_sale_gross_usd": property_sale_gross_from_accounting,
        "property_sale_net_proceeds_usd": property_sale_net_proceeds_from_accounting,
        "property_sale_tax_usd": property_sale_tax_from_accounting,
        "property_sale_debt_payoff_usd": property_sale_debt_payoff_from_accounting,
        "property_sale_adjusted_basis_usd": property_sale_adjusted_basis_from_accounting,
        "realized_property_gain_usd": realized_property_gain_from_accounting,
        "property_sale_capital_gain_usd": property_sale_capital_gain_from_accounting,
        "property_sale_capital_gain_exclusion_usd": property_sale_capital_gain_exclusion_from_accounting,
        "taxable_property_capital_gain_usd": taxable_property_capital_gain_from_accounting,
        "taxable_property_gain_usd": taxable_property_gain_from_accounting,
        "depreciation_recapture_usd": depreciation_recapture_from_accounting,
        "net_property_sale_cash_flow_usd": property_sale_net_proceeds_from_accounting,
        "home_equity_usd": home_equity,
        "owner_home_equity_claim_usd": owner_home_equity_claim_from_snapshot,
        "partner_home_equity_claim_usd": partner_home_equity_claim_from_snapshot,
        "partner_contribution_usd": partner_contribution_from_accounting,
        "partner_contribution_used_usd": partner_contribution_used_from_accounting,
        "partner_unallocated_excess_usd": partner_unallocated_excess_from_accounting,
        "partner_house_costs_usd": partner_equity.column("house_costs_usd"),
        "partner_principal_credit_usd": partner_principal_credit_from_accounting,
        "owner_principal_credit_usd": owner_principal_credit_from_accounting,
        "partner_house_cost_share": partner_equity.column("house_cost_share"),
        "partner_equity_ledger_usd": partner_equity_ledger_from_snapshot,
        "owner_equity_ledger_usd": owner_equity_ledger_from_snapshot,
        "partner_ownership_pct": partner_equity.column("ownership_pct"),
        "liquid_net_worth_usd": liquid_net_worth,
        "net_worth_usd": net_worth,
        "partner_present": partner_present,
        "monthly_spend_usd": monthly_spend_from_accounting,
    }
    return ScenarioRunArrays(
        scenario_id=scenario.scenario_id,
        scenario_label=scenario.label,
        month_index=month_index,
        numerics=_build_numerics_frame(month_index, metric_arrays),
        sp500_effects_frame=event_streams.sort_effects_variant_frame(
            event_streams.join_trajectory_identity(effects[EffectType.SELL_SP500].build(), trace_identity_frame)
        ),
        crypto_effects_frame=event_streams.sort_effects_variant_frame(
            event_streams.join_trajectory_identity(effects[EffectType.SELL_CRYPTO].build(), trace_identity_frame)
        ),
        private_equity_effects_frame=event_streams.sort_effects_variant_frame(
            event_streams.join_trajectory_identity(
                effects[EffectType.SELL_PRIVATE_EQUITY].build(), trace_identity_frame
            )
        ),
        settle_property_sale_effects_frame=event_streams.sort_effects_variant_frame(
            event_streams.join_trajectory_identity(
                effects[EffectType.SETTLE_PROPERTY_SALE].build(), trace_identity_frame
            )
        ),
        policy_decisions_frame=event_streams.sort_policy_decisions(
            event_streams.join_trajectory_identity(policy_decisions.build(), trace_identity_frame)
        ),
        market_path_observations_frame=event_streams.sort_market_path_observations(
            event_streams.join_trajectory_identity(market_path_observations_frame, trace_identity_frame)
        ),
        pe_sale_opportunity_observations_frame=event_streams.sort_pe_sale_opportunity_observations(
            event_streams.join_trajectory_identity(pe_sale_opportunity_observations.build(), trace_identity_frame)
        ),
        accounting_trace=accounting_trace.with_trajectory_identity(trace_identity_by_rollout).sorted_canonical(),
        tax_lots_frame=event_streams.sort_tax_lots(tax_lots.build()),
        lot_dispositions_frame=event_streams.sort_lot_dispositions(
            event_streams.join_trajectory_identity(lot_dispositions.build(), trace_identity_frame)
        ),
        liabilities_frame=event_streams.sort_liabilities(liabilities.build()),
        property_sale_basis_gain_details_frame=event_streams.sort_property_sale_basis_gain_details(
            event_streams.join_trajectory_identity(property_sale_basis_gain_details.build(), trace_identity_frame)
        ),
        tax_payment_allocation_details_frame=event_streams.sort_tax_payment_allocation_details(
            event_streams.join_trajectory_identity(tax_payment_allocation_details.build(), trace_identity_frame)
        ),
        funding_decisions_frame=event_streams.sort_funding_decisions(
            event_streams.join_trajectory_identity(funding_decisions.build(), trace_identity_frame)
        ),
        obligations_frame=event_streams.sort_obligation_lifecycle(
            event_streams.join_trajectory_identity(obligations.build(), trace_identity_frame)
        ),
    )


def _trace_identity_by_rollout(scenario: Scenario, market_bundle: MarketBundle) -> dict[int, dict[str, str]]:
    scenario_policy_program_set_id = policy_program_set_id(scenario_id=scenario.scenario_id, policies=scenario.policies)
    scenario_identity = scenario_input_id(scenario)
    return {
        rollout_index: {
            "path_set_id": market_bundle.metadata.path_set_id,
            "exogenous_path_id": exogenous_path_id,
            "scenario_input_id": scenario_identity,
            "projection_trajectory_id": projection_trajectory_id(
                scenario_id=scenario.scenario_id,
                scenario_input_id=scenario_identity,
                exogenous_path_id=exogenous_path_id,
                policy_program_set_id=scenario_policy_program_set_id,
            ),
        }
        for rollout_index, exogenous_path_id in enumerate(market_bundle.metadata.exogenous_path_ids)
    }


def _market_path_observations_frame(scenario: Scenario, market_bundle: MarketBundle) -> pl.DataFrame:
    """Pick the per-scenario market-path keys (location, first PE issuer
    if any) and hand them to the bundle to assemble the dense
    `(rollouts × (months+1))` frame. Scenario→key translation lives in
    the engine; per-key lookup + fallback to all-ones for absent keys
    lives in `MarketBundle.market_path_observations_frame`."""

    pe_issuer_keys = _private_equity_issuer_routing_keys(scenario)
    return market_bundle.market_path_observations_frame(
        location_id=scenario.location_id, pe_issuer_key=pe_issuer_keys[0] if pe_issuer_keys else None
    )


def _record_private_equity_sale_opportunity_observations(
    pe_sale_opportunity_observations: event_streams.StreamFrameBuilder,
    *,
    month_index: int,
    source_asset_id: str,
    opportunity: PrivateEquitySaleOpportunityBatch,
) -> None:
    mask = opportunity.sale_opportunity_mask
    if not mask.any():
        return
    rollouts = np.nonzero(mask)[0].astype(np.int64)
    size = int(rollouts.size)
    pe_sale_opportunity_observations.extend(
        {
            "rollout_index": rollouts,
            "month_index": np.full(size, month_index, dtype=np.int64),
            "source_asset_id": [source_asset_id] * size,
            "opportunity_id": [str(opportunity.opportunity_id[r]) for r in rollouts],
            "opportunity_cause_id": [str(opportunity.opportunity_cause_id[r]) for r in rollouts],
            "sale_opportunity_value_usd": opportunity.sale_opportunity_value_usd[rollouts].astype(np.float64),
            "private_equity_value_before_sale_usd": opportunity.private_equity_value_before_sale_usd[rollouts].astype(
                np.float64
            ),
        }
    )


def _record_per_issuer_sale_opportunity_observations(
    pe_sale_opportunity_observations: event_streams.StreamFrameBuilder,
    *,
    scenario: Scenario,
    market_bundle: MarketBundle,
    month: int,
    month_index: int,
    private_equity_value_before_sale_usd: np.ndarray,
    pe_liquidity_regime: LiquidityEventOnly | PublicMarket | Acquisition,
    engine_pe_issuer_key: str | None,
    aggregate_source_asset_id: str,
    aggregate_opportunity: PrivateEquitySaleOpportunityBatch,
) -> None:
    """Emit one PrivateEquitySaleOpportunityObservation per (rollout, month, issuer).

    Single-issuer (or zero-issuer) scenarios fall through to the legacy aggregated
    emission so existing tests remain byte-identical. Multi-issuer scenarios emit
    one row per issuer, each computed from that issuer's per-issuer multiplier and
    tender-mask paths so downstream consumers can split by issuer.
    """
    issuer_keys = _private_equity_issuer_routing_keys(scenario)
    if len(issuer_keys) <= 1:
        _record_private_equity_sale_opportunity_observations(
            pe_sale_opportunity_observations,
            month_index=month_index,
            source_asset_id=aggregate_source_asset_id,
            opportunity=aggregate_opportunity,
        )
        return

    # Per-issuer slice of the aggregated PE pre-sale value. We allocate the engine's
    # current `private_equity_value_before_sale_usd` (already reflecting remaining
    # fraction and the engine's routing-key multiplier) across issuers in proportion
    # to each issuer's initial mark × its own multiplier ratio relative to the
    # engine's routing key. With a single global "default" path (the legacy macro
    # provider) the ratios collapse to 1.0 and per-issuer values equal each issuer's
    # initial-share × engine value — which is what a reviewer would expect.
    pe_unit_price_usd = float(market_bundle.metadata.current_private_equity_price_usd)
    initial_by_issuer: dict[str, float] = dict.fromkeys(issuer_keys, 0.0)
    for asset in scenario.initial_balance_sheet.assets:
        if not isinstance(asset, PrivateEquityPosition):
            continue
        initial_by_issuer[asset.market_routing_key] += _private_equity_position_value_usd(
            asset, current_unit_price_usd=pe_unit_price_usd
        )
    initial_total = sum(initial_by_issuer.values())
    if initial_total <= 0:
        return

    # `engine_pe_issuer_key` is non-None whenever the scenario has PE positions,
    # which is the only path that reaches here (`len(issuer_keys) > 1` above).
    assert engine_pe_issuer_key is not None
    engine_multiplier_at_month = market_bundle.private_equity_value_multiplier(engine_pe_issuer_key)[:, month]
    # Engine value before sale = initial_total * remaining_fraction * engine_multiplier.
    # Derive remaining_fraction-equivalent from the per-rollout array; numerically
    # stable for engine_multiplier > 0 (guaranteed by MarketBundle validation).
    remaining_fraction_eq = np.where(
        engine_multiplier_at_month > 0,
        private_equity_value_before_sale_usd / np.maximum(engine_multiplier_at_month * initial_total, 1e-12),
        0.0,
    )
    for issuer_key in issuer_keys:
        issuer_multiplier = market_bundle.private_equity_value_multiplier(issuer_key)[:, month]
        per_issuer_value_before_sale = initial_by_issuer[issuer_key] * remaining_fraction_eq * issuer_multiplier
        issuer_mask = market_bundle.private_equity_sale_opportunity_mask_for(issuer_key)[:, month]
        if isinstance(pe_liquidity_regime, PublicMarket):
            lockup_end_month = pe_liquidity_regime.lockup_end_month or 0
            if month >= lockup_end_month:
                issuer_mask = issuer_mask | True
        issuer_opportunity = private_equity_sale_opportunity(
            sale_opportunity_mask=issuer_mask,
            private_equity_value_before_sale_usd=per_issuer_value_before_sale,
            path_set_id=market_bundle.metadata.path_set_id,
            month_index=month_index,
            source_holding_id=issuer_key,
        )
        _record_private_equity_sale_opportunity_observations(
            pe_sale_opportunity_observations,
            month_index=month_index,
            source_asset_id=issuer_key,
            opportunity=issuer_opportunity,
        )


_POLICY_DECISION_VARIANT_ONLY_COLUMNS: frozenset[str] = frozenset(event_streams.POLICY_DECISION_SCHEMA) - {
    "rollout_index",
    "month_index",
    "decision_type",
    "actor_id",
    "policy_id",
    "policy_sequence_index",
}


def _policy_decision_block(
    *,
    decision_type: PolicyDecisionType,
    rollouts: np.ndarray,
    month_index_value: int | np.ndarray,
    actor_id_per_row: list[str],
    policy_id_per_row: list[str],
    policy_sequence_index_per_row: np.ndarray | int,
    variant_columns: dict[str, Any],
) -> dict[str, Any]:
    """Build the dict-of-columns one `PolicyDecision` row-block needs, padded
    with `None` for variant-specific columns the caller didn't supply.
    Single source of truth for the wide POLICY_DECISION_SCHEMA so each
    recorder only spells out the columns its own variant cares about."""

    size = int(rollouts.size)
    block: dict[str, Any] = {
        "rollout_index": rollouts.astype(np.int64),
        "month_index": (
            np.full(size, int(month_index_value), dtype=np.int64)
            if not isinstance(month_index_value, np.ndarray)
            else month_index_value.astype(np.int64)
        ),
        "decision_type": [decision_type.value] * size,
        "actor_id": actor_id_per_row,
        "policy_id": policy_id_per_row,
        "policy_sequence_index": (
            np.full(size, int(policy_sequence_index_per_row), dtype=np.int64)
            if not isinstance(policy_sequence_index_per_row, np.ndarray)
            else policy_sequence_index_per_row.astype(np.int64)
        ),
    }
    unexpected = set(variant_columns) - _POLICY_DECISION_VARIANT_ONLY_COLUMNS
    if unexpected:
        raise KeyError(f"unknown variant-specific columns: {sorted(unexpected)}")
    block.update(variant_columns)
    for missing in _POLICY_DECISION_VARIANT_ONLY_COLUMNS - set(variant_columns):
        block[missing] = [None] * size
    return block


def _record_monthly_spend_decisions(
    policy_decisions: event_streams.StreamFrameBuilder,
    *,
    month_index: int,
    policy_step: ActorPolicyStep[Policy],
    amount_usd: np.ndarray,
    inflation_multiplier: np.ndarray,
) -> None:
    policy = policy_step.policy
    if not isinstance(policy, MonthlySpendPolicy):
        raise TypeError(f"monthly spend decision recorder received {type(policy).__name__}")
    mask = amount_usd > 0
    if not mask.any():
        return
    rollouts = np.nonzero(mask)[0].astype(np.int64)
    size = int(rollouts.size)
    policy_decisions.extend(
        _policy_decision_block(
            decision_type=PolicyDecisionType.MONTHLY_SPEND,
            rollouts=rollouts,
            month_index_value=month_index,
            actor_id_per_row=[policy.actor_id] * size,
            policy_id_per_row=[policy.policy_id] * size,
            policy_sequence_index_per_row=policy_step.sequence_index,
            variant_columns={
                "amount_usd": amount_usd[rollouts].astype(np.float64),
                "inflation_multiplier": inflation_multiplier[rollouts].astype(np.float64),
            },
        )
    )


def _record_sell_public_stock_decisions(
    policy_decisions: event_streams.StreamFrameBuilder,
    *,
    month_index: int,
    policy_step: ActorPolicyStep[Policy],
    current_cash_usd: np.ndarray,
    requested_amount_usd: np.ndarray,
) -> None:
    policy = policy_step.policy
    if not isinstance(policy, CheckingFloorSellPublicStockPolicy):
        raise TypeError(f"public stock decision recorder received {type(policy).__name__}")
    mask = requested_amount_usd > 0
    if not mask.any():
        return
    rollouts = np.nonzero(mask)[0].astype(np.int64)
    size = int(rollouts.size)
    policy_decisions.extend(
        _policy_decision_block(
            decision_type=PolicyDecisionType.SELL_PUBLIC_STOCK,
            rollouts=rollouts,
            month_index_value=month_index,
            actor_id_per_row=[policy.actor_id] * size,
            policy_id_per_row=[policy.policy_id] * size,
            policy_sequence_index_per_row=policy_step.sequence_index,
            variant_columns={
                "requested_amount_usd": requested_amount_usd[rollouts].astype(np.float64),
                "current_cash_usd": current_cash_usd[rollouts].astype(np.float64),
                "target_cash_floor_usd": np.full(size, float(policy.floor_usd), dtype=np.float64),
            },
        )
    )


def _record_private_equity_sale_decisions(
    policy_decisions: event_streams.StreamFrameBuilder,
    *,
    month_index: int,
    policy_step: ActorPolicyStep[Policy],
    source_asset_id: str,
    instruction: PrivateEquitySaleInstructionBatch,
    opportunity: PrivateEquitySaleOpportunityBatch,
    liquid_net_worth_usd: np.ndarray,
) -> None:
    policy = policy_step.policy
    if not isinstance(policy, PrivateEquitySalePolicy):
        raise TypeError(f"private equity decision recorder received {type(policy).__name__}")
    target_liquid_net_worth_floor_usd = (
        float(policy.sale_rule.min_liquid_net_worth_usd)
        if isinstance(policy.sale_rule, LiquidNetWorthFloorPrivateEquitySaleRule)
        else None
    )
    sale_rule_type_value = policy.sale_rule.sale_rule_type.value
    configured_sale_amount_usd = _private_equity_configured_sale_amount_usd(policy.sale_rule)
    rollouts = np.arange(instruction.requested_amount_usd.shape[0], dtype=np.int64)
    size = int(rollouts.size)
    if size == 0:
        return
    proceeds_destination_value = (
        instruction.proceeds_destination.value
        if hasattr(instruction.proceeds_destination, "value")
        else str(instruction.proceeds_destination)
    )
    requested = instruction.requested_amount_usd[rollouts].astype(np.float64)
    opportunity_value = opportunity.sale_opportunity_value_usd[rollouts].astype(np.float64)
    decision_reasons = [
        _private_equity_sale_decision_reason(
            requested_amount_usd=float(req), sale_opportunity_value_usd=float(opv)
        ).value
        for req, opv in zip(requested.tolist(), opportunity_value.tolist(), strict=True)
    ]
    target_floor_col: np.ndarray | list[Any] = (
        np.full(size, target_liquid_net_worth_floor_usd, dtype=np.float64)
        if target_liquid_net_worth_floor_usd is not None
        else [None] * size
    )
    policy_decisions.extend(
        _policy_decision_block(
            decision_type=PolicyDecisionType.PRIVATE_EQUITY_SALE,
            rollouts=rollouts,
            month_index_value=month_index,
            actor_id_per_row=[instruction.actor_id] * size,
            policy_id_per_row=[instruction.policy_id] * size,
            policy_sequence_index_per_row=policy_step.sequence_index,
            variant_columns={
                "decision_reason": decision_reasons,
                "source_asset_id": [source_asset_id] * size,
                "sale_rule_type": [sale_rule_type_value] * size,
                "configured_sale_amount_usd": np.full(size, configured_sale_amount_usd, dtype=np.float64),
                "opportunity_id": [instruction.opportunity_id[r] for r in rollouts],
                "opportunity_cause_id": [str(instruction.opportunity_cause_id[r]) for r in rollouts],
                "requested_amount_usd": requested,
                "sale_opportunity_value_usd": opportunity_value,
                "private_equity_value_before_sale_usd": opportunity.private_equity_value_before_sale_usd[
                    rollouts
                ].astype(np.float64),
                "liquid_net_worth_usd": liquid_net_worth_usd[rollouts].astype(np.float64),
                "target_liquid_net_worth_floor_usd": target_floor_col,
                "proceeds_destination": [proceeds_destination_value] * size,
            },
        )
    )


def _private_equity_configured_sale_amount_usd(sale_rule: PrivateEquitySaleRule) -> float:
    if isinstance(sale_rule, FixedAmountPrivateEquitySaleRule):
        return float(sale_rule.amount_usd)
    if isinstance(sale_rule, LiquidNetWorthFloorPrivateEquitySaleRule):
        return float(sale_rule.sale_amount_usd)
    raise TypeError(f"unsupported private equity sale rule: {sale_rule!r}")


def _private_equity_sale_decision_reason(
    *, requested_amount_usd: float, sale_opportunity_value_usd: float
) -> PrivateEquitySaleDecisionReason:
    if requested_amount_usd > 0:
        return PrivateEquitySaleDecisionReason.SALE_REQUESTED
    if sale_opportunity_value_usd <= 0:
        return PrivateEquitySaleDecisionReason.NO_SALE_OPPORTUNITY
    return PrivateEquitySaleDecisionReason.POLICY_NOT_TRIGGERED


def _record_partner_contribution_decisions(
    policy_decisions: event_streams.StreamFrameBuilder, *, month_index: np.ndarray, partner_equity: PartnerEquityArrays
) -> None:
    for agreement in partner_equity.agreements:
        policy = agreement.policy
        contribution_matrix = agreement.column("contribution_usd")
        rollout_axis, month_axis = np.nonzero(contribution_matrix > 0)
        if rollout_axis.size == 0:
            continue
        size = int(rollout_axis.size)
        policy_decisions.extend(
            _policy_decision_block(
                decision_type=PolicyDecisionType.PARTNER_CONTRIBUTION,
                rollouts=rollout_axis,
                month_index_value=month_index[month_axis],
                actor_id_per_row=[policy.actor_id] * size,
                policy_id_per_row=[policy.policy_id] * size,
                policy_sequence_index_per_row=int(agreement.policy_sequence_index),
                variant_columns={
                    "recipient_actor_id": [agreement.recipient_actor_id] * size,
                    "requested_amount_usd": contribution_matrix[rollout_axis, month_axis].astype(np.float64),
                    "property_id": [agreement.property_id] * size,
                },
            )
        )


def _record_journal_entry_batches(
    accounting: AccountingTraceBuilder,
    *,
    month_index: np.ndarray,
    entries: tuple[JournalEntryBatch, ...],
    amount_multiplier: np.ndarray | None = None,
) -> None:
    for entry in entries:
        accounting.record_entry(month_index=month_index, entry=entry, amount_multiplier=amount_multiplier)


def _record_balance_snapshot_batches(
    accounting: AccountingTraceBuilder, *, month_index: np.ndarray, entries: tuple[BalanceSnapshotBatch, ...]
) -> None:
    for entry in entries:
        accounting.record_snapshot(month_index=month_index, snapshot=entry)


def _posting_amount_matrix(
    accounting_trace: AccountingTrace,
    *,
    rollout_count: int,
    month_index: np.ndarray,
    role: ChartAccountRole,
    side: PostingSide | None = None,
    journal_entry_type: JournalEntryType | None = None,
) -> np.ndarray:
    return accounting_trace.posting_amount_matrix(
        rollout_count=rollout_count,
        month_index=month_index,
        role=role,
        side=side,
        journal_entry_type=journal_entry_type,
    )


def _balance_snapshot_amount_matrix(
    accounting_trace: AccountingTrace, *, rollout_count: int, month_index: np.ndarray, role: ChartAccountRole
) -> np.ndarray:
    return accounting_trace.balance_snapshot_amount_matrix(
        rollout_count=rollout_count, month_index=month_index, role=role
    )


def _lot_disposition_amount_matrix(
    lot_dispositions: event_streams.StreamFrameBuilder,
    *,
    rollout_count: int,
    month_index: np.ndarray,
    asset_class: LotAssetClass,
    amount_field: str,
) -> np.ndarray:
    """Reshape the in-progress `lot_dispositions` builder into a
    `(rollouts, months)` matrix of `amount_field` summed per
    `(rollout_index, month_index)` for the given asset class."""

    matrix = np.zeros((rollout_count, len(month_index)), dtype="float64")
    frame = lot_dispositions.build()
    if frame.height == 0:
        return matrix
    month_position_by_index = {int(month): position for position, month in enumerate(month_index.tolist())}
    aggregated = (
        frame.lazy()
        .filter(pl.col("asset_class") == asset_class.value)
        .group_by(["rollout_index", "month_index"])
        .agg(amount=pl.col(amount_field).sum())
        .collect()
    )
    for row in aggregated.iter_rows(named=True):
        month = int(row["month_index"])
        try:
            month_position = month_position_by_index[month]
        except KeyError as exc:
            raise ValueError(f"lot disposition has month outside result horizon: {month}") from exc
        matrix[int(row["rollout_index"]), month_position] += float(row["amount"])
    return matrix


def _tax_lot_id(asset_class: LotAssetClass, source_id: str) -> str:
    return f"lot:{asset_class.value}:{source_id}"


def _mortgage_liability_id(property_id: str) -> str:
    return f"mortgage:{property_id}"


def _accounting_detail_amount_matrix(
    details: event_streams.StreamFrameBuilder, *, rollout_count: int, month_index: np.ndarray, amount_field: str
) -> np.ndarray:
    """Project the in-progress accounting-detail builder into a
    `(rollouts, months)` matrix of `amount_field` summed per
    `(rollout_index, month_index)`. The builder is per-variant so the
    legacy `detail_type` filter is implicit in which builder the caller
    passes."""

    matrix = np.zeros((rollout_count, len(month_index)), dtype="float64")
    frame = details.build()
    if frame.height == 0:
        return matrix
    month_position_by_index = {int(month): position for position, month in enumerate(month_index.tolist())}
    aggregated = (
        frame.lazy().group_by(["rollout_index", "month_index"]).agg(amount=pl.col(amount_field).sum()).collect()
    )
    for row in aggregated.iter_rows(named=True):
        month = int(row["month_index"])
        try:
            month_position = month_position_by_index[month]
        except KeyError as exc:
            raise ValueError(f"accounting detail has month outside result horizon: {month}") from exc
        matrix[int(row["rollout_index"]), month_position] += float(row["amount"])
    return matrix


def _record_property_sale_journal_entries(
    accounting: AccountingTraceBuilder,
    lot_dispositions: event_streams.StreamFrameBuilder,
    *,
    scenario: Scenario,
    disposition: PropertyDispositionArrays,
    tax_usd: np.ndarray,
    net_proceeds_usd: np.ndarray,
) -> None:
    """Record the property sale journal entry and lot disposition row.

    The journal entry covers the sale-event cash flows only (gross, closing,
    debt payoff, sale proceeds). Sale tax is not posted here — it is accrued
    in the per-source-month annual tax allocation and settled at year-end via
    the annual-tax obligation pipeline. `tax_usd` is the per-source-month tax
    attribution recorded on the lot disposition for "tax attributable to this
    sale" reporting; it does not move cash here.
    """
    if disposition.sale_event is None or disposition.sale_month is None:
        return
    sale_event = disposition.sale_event
    property_id = sale_event.property_id or scenario.property_selection.property_id
    if property_id is None:
        return
    actor_id = sale_event.actor_id or _primary_owner_actor_id(scenario)
    month_index = disposition.sale_month
    gross = disposition.column("property_sale_gross_usd")[:, month_index]
    selling_cost = disposition.column("sale_closing_cost_usd")[:, month_index]
    debt_payoff = disposition.column("property_sale_debt_payoff_usd")[:, month_index]
    tax = tax_usd[:, month_index]
    net_proceeds = net_proceeds_usd[:, month_index]
    entry_prefix = f"event:{sale_event.event_id}:property_sale"
    accounting.record_entry_firings(
        schema=posting_schemas.PROPERTY_SALE,
        month_index=month_index,
        cause_id_prefix=entry_prefix,
        actor_id=actor_id,
        policy_id=PROPERTY_SALE_SETTLEMENT_POLICY_ID,
        event_id=sale_event.event_id,
        description="property sale settlement",
        amount_bindings={
            "cash_in": np.maximum(0.0, net_proceeds),
            "selling_cost": selling_cost,
            "debt_payoff": debt_payoff,
            "gross": gross,
            "cash_out": np.maximum(0.0, -net_proceeds),
        },
        leg_chart_account_keys=(
            {"actor_id": actor_id},
            {"actor_id": actor_id, "property_id": property_id},
            {"actor_id": actor_id, "liability_id": _mortgage_liability_id(property_id), "property_id": property_id},
            {"actor_id": actor_id, "property_id": property_id},
            {"actor_id": actor_id},
        ),
    )
    lot_id = _tax_lot_id(LotAssetClass.PROPERTY, property_id)
    mask = gross > 0
    if mask.any():
        rollouts = np.nonzero(mask)[0].astype(np.int64)
        size = int(rollouts.size)
        journal_entry_ids = [
            _trace_row_id(entry_prefix, rollout_index=int(r), month_index=month_index) for r in rollouts
        ]
        basis_col = disposition.column("property_sale_adjusted_basis_usd")[:, month_index]
        realized_col = disposition.column("realized_property_gain_usd")[:, month_index]
        taxable_col = disposition.column("taxable_property_gain_usd")[:, month_index]
        lot_dispositions.extend(
            {
                "rollout_index": rollouts,
                "month_index": np.full(size, month_index, dtype=np.int64),
                "lot_disposition_id": [f"{jid}:lot:{lot_id}" for jid in journal_entry_ids],
                "journal_entry_id": journal_entry_ids,
                "lot_id": [lot_id] * size,
                "asset_class": [LotAssetClass.PROPERTY.value] * size,
                "proceeds_usd": gross[rollouts].astype(np.float64),
                "cost_basis_usd": basis_col[rollouts].astype(np.float64),
                "realized_gain_usd": realized_col[rollouts].astype(np.float64),
                "taxable_gain_usd": taxable_col[rollouts].astype(np.float64),
                "quantity_sold": np.full(size, None, dtype=object),
                "tax_expense_usd": tax[rollouts].astype(np.float64),
            }
        )


def _record_property_sale_accounting_details(
    property_sale_basis_gain_details: event_streams.StreamFrameBuilder,
    *,
    scenario: Scenario,
    disposition: PropertyDispositionArrays,
) -> None:
    if disposition.sale_event is None or disposition.sale_month is None:
        return
    sale_event = disposition.sale_event
    property_id = sale_event.property_id or scenario.property_selection.property_id
    if property_id is None:
        return
    actor_id = sale_event.actor_id or _primary_owner_actor_id(scenario)
    month_position = disposition.sale_month
    gross_col = disposition.column("property_sale_gross_usd")[:, month_position]
    realized_col = disposition.column("realized_property_gain_usd")[:, month_position]
    taxable_col = disposition.column("taxable_property_gain_usd")[:, month_position]
    mask = (gross_col != 0) | (realized_col != 0) | (taxable_col != 0)
    if not mask.any():
        return
    rollouts = np.nonzero(mask)[0].astype(np.int64)
    size = int(rollouts.size)
    property_sale_basis_gain_details.extend(
        {
            "rollout_index": rollouts,
            "month_index": np.full(size, int(disposition.sale_month), dtype=np.int64),
            "actor_id": [actor_id] * size,
            "policy_id": [PROPERTY_SALE_SETTLEMENT_POLICY_ID] * size,
            "event_id": [sale_event.event_id] * size,
            "property_id": [property_id] * size,
            "gross_sale_usd": gross_col[rollouts].astype(np.float64),
            "selling_cost_usd": disposition.column("sale_closing_cost_usd")[rollouts, month_position].astype(
                np.float64
            ),
            "debt_payoff_usd": disposition.column("property_sale_debt_payoff_usd")[rollouts, month_position].astype(
                np.float64
            ),
            "adjusted_basis_usd": disposition.column("property_sale_adjusted_basis_usd")[
                rollouts, month_position
            ].astype(np.float64),
            "realized_gain_usd": realized_col[rollouts].astype(np.float64),
            "depreciation_recapture_usd": disposition.column("depreciation_recapture_usd")[
                rollouts, month_position
            ].astype(np.float64),
            "capital_gain_usd": disposition.column("property_sale_capital_gain_usd")[rollouts, month_position].astype(
                np.float64
            ),
            "capital_gain_exclusion_usd": disposition.column("property_sale_capital_gain_exclusion_usd")[
                rollouts, month_position
            ].astype(np.float64),
            "taxable_capital_gain_usd": disposition.column("taxable_property_capital_gain_usd")[
                rollouts, month_position
            ].astype(np.float64),
            "taxable_gain_usd": taxable_col[rollouts].astype(np.float64),
        }
    )


def _record_tax_payment_allocation_details(
    tax_payment_allocation_details: event_streams.StreamFrameBuilder,
    *,
    scenario: Scenario,
    month_index: np.ndarray,
    annual_tax: AnnualSaleTaxAllocation,
    property_depreciation_recapture_usd: np.ndarray,
    taxable_property_capital_gain_usd: np.ndarray,
    generic_sp500_sale_gain_usd: np.ndarray,
    generic_crypto_sale_gain_usd: np.ndarray,
    private_equity_sale_taxable_gain_usd: np.ndarray,
    net_rental_taxable_income_usd: np.ndarray,
) -> None:
    property_recapture = np.maximum(0.0, property_depreciation_recapture_usd)
    property_capital_gain = np.maximum(0.0, taxable_property_capital_gain_usd)
    sp500_capital_gain = np.maximum(0.0, generic_sp500_sale_gain_usd)
    crypto_capital_gain = np.maximum(0.0, generic_crypto_sale_gain_usd)
    private_equity_capital_gain = np.maximum(0.0, private_equity_sale_taxable_gain_usd)
    rental_taxable = np.maximum(0.0, net_rental_taxable_income_usd)
    total_taxable_income = (
        property_recapture
        + property_capital_gain
        + sp500_capital_gain
        + crypto_capital_gain
        + private_equity_capital_gain
        + rental_taxable
    )
    active_rollouts, active_month_positions = np.nonzero(
        (annual_tax.total_income_tax_usd != 0) | (total_taxable_income != 0)
    )
    if active_rollouts.size == 0:
        return
    actor_id = _primary_owner_actor_id(scenario)
    rollouts = active_rollouts.astype(np.int64)
    months = month_index[active_month_positions].astype(np.int64)
    size = int(rollouts.size)
    rollout_axis = active_rollouts
    month_axis = active_month_positions
    tax_payment_allocation_details.extend(
        {
            "rollout_index": rollouts,
            "month_index": months,
            "actor_id": [actor_id] * size,
            "policy_id": [ANNUAL_TAX_ACCOUNTING_POLICY_ID] * size,
            "event_id": [None] * size,
            "property_id": [None] * size,
            "tax_year_index": (months // MONTHS_PER_YEAR).astype(np.int64),
            "payment_timing": [TaxPaymentTiming.YEAR_END.value] * size,
            "federal_income_tax_usd": annual_tax.federal_income_tax_usd[rollout_axis, month_axis].astype(np.float64),
            "california_income_tax_usd": annual_tax.california_income_tax_usd[rollout_axis, month_axis].astype(
                np.float64
            ),
            "total_income_tax_usd": annual_tax.total_income_tax_usd[rollout_axis, month_axis].astype(np.float64),
            "property_sale_tax_usd": annual_tax.property_sale_tax_usd[rollout_axis, month_axis].astype(np.float64),
            "generic_sp500_sale_tax_usd": annual_tax.generic_sp500_sale_tax_usd[rollout_axis, month_axis].astype(
                np.float64
            ),
            "generic_crypto_sale_tax_usd": annual_tax.generic_crypto_sale_tax_usd[rollout_axis, month_axis].astype(
                np.float64
            ),
            "private_equity_sale_tax_usd": annual_tax.private_equity_sale_tax_usd[rollout_axis, month_axis].astype(
                np.float64
            ),
            "rental_income_tax_usd": annual_tax.rental_income_tax_usd[rollout_axis, month_axis].astype(np.float64),
            "property_depreciation_recapture_usd": property_recapture[rollout_axis, month_axis].astype(np.float64),
            "taxable_property_capital_gain_usd": property_capital_gain[rollout_axis, month_axis].astype(np.float64),
            "generic_sp500_taxable_gain_usd": sp500_capital_gain[rollout_axis, month_axis].astype(np.float64),
            "generic_crypto_taxable_gain_usd": crypto_capital_gain[rollout_axis, month_axis].astype(np.float64),
            "private_equity_taxable_gain_usd": private_equity_capital_gain[rollout_axis, month_axis].astype(np.float64),
            "net_rental_taxable_income_usd": rental_taxable[rollout_axis, month_axis].astype(np.float64),
            "total_taxable_income_usd": total_taxable_income[rollout_axis, month_axis].astype(np.float64),
        }
    )


def _record_sp500_sale_journal_entries(
    accounting: AccountingTraceBuilder,
    lot_dispositions: event_streams.StreamFrameBuilder,
    *,
    month_index: int,
    policy: Policy,
    cause_id_prefix: str,
    amount_usd: np.ndarray,
    basis_usd: np.ndarray,
    tax_usd: np.ndarray,
) -> None:
    entry_prefix = cause_id_prefix
    accounting.record_entry_firings(
        schema=posting_schemas.ASSET_SALE_PUBLIC_SECURITY,
        month_index=month_index,
        cause_id_prefix=entry_prefix,
        actor_id=policy.actor_id,
        policy_id=policy.policy_id,
        description="public security sale",
        amount_bindings={"amount": amount_usd},
        leg_chart_account_keys=({"actor_id": policy.actor_id}, {"actor_id": policy.actor_id}),
    )
    lot_id = _tax_lot_id(LotAssetClass.PUBLIC_SECURITY, "portfolio")
    mask = amount_usd > 0
    if mask.any():
        rollouts = np.nonzero(mask)[0].astype(np.int64)
        size = int(rollouts.size)
        amounts = amount_usd[rollouts].astype(np.float64)
        bases = basis_usd[rollouts].astype(np.float64)
        gains = amounts - bases
        journal_entry_ids = [
            _trace_row_id(entry_prefix, rollout_index=int(r), month_index=month_index) for r in rollouts
        ]
        lot_dispositions.extend(
            {
                "rollout_index": rollouts,
                "month_index": np.full(size, month_index, dtype=np.int64),
                "lot_disposition_id": [f"{jid}:lot:{lot_id}" for jid in journal_entry_ids],
                "journal_entry_id": journal_entry_ids,
                "lot_id": [lot_id] * size,
                "asset_class": [LotAssetClass.PUBLIC_SECURITY.value] * size,
                "proceeds_usd": amounts,
                "cost_basis_usd": np.maximum(0.0, bases),
                "realized_gain_usd": gains,
                "taxable_gain_usd": np.maximum(0.0, gains),
                "quantity_sold": np.full(size, None, dtype=object),
                "tax_expense_usd": tax_usd[rollouts].astype(np.float64),
            }
        )


def _record_private_equity_sale_journal_entries(
    accounting: AccountingTraceBuilder,
    lot_dispositions: event_streams.StreamFrameBuilder,
    *,
    month_index: int,
    instruction: PrivateEquitySaleInstructionBatch,
    sale_application: PrivateEquitySaleApplication,
    tax_usd: np.ndarray,
    source_holding_id: str,
) -> None:
    schema = (
        posting_schemas.ASSET_SALE_PRIVATE_EQUITY_TO_PUBLIC_SECURITY
        if instruction.proceeds_destination is AssetType.GENERIC_SP500_STOCK
        else posting_schemas.ASSET_SALE_PRIVATE_EQUITY_TO_CASH
    )
    entry_prefix = f"policy:{instruction.policy_id}:private_equity_sale"
    accounting.record_entry_firings(
        schema=schema,
        month_index=month_index,
        cause_id_prefix=entry_prefix,
        actor_id=instruction.actor_id,
        policy_id=instruction.policy_id,
        description="private equity sale",
        amount_bindings={"amount": sale_application.sale_usd},
        leg_chart_account_keys=(
            {"actor_id": instruction.actor_id},
            {"actor_id": instruction.actor_id, "source_asset_id": source_holding_id},
        ),
    )
    lot_id = _tax_lot_id(LotAssetClass.PRIVATE_EQUITY, source_holding_id)
    mask = sale_application.sale_usd > 0
    if mask.any():
        rollouts = np.nonzero(mask)[0].astype(np.int64)
        size = int(rollouts.size)
        amounts = sale_application.sale_usd[rollouts].astype(np.float64)
        bases = sale_application.basis_usd[rollouts].astype(np.float64)
        journal_entry_ids = [
            _trace_row_id(entry_prefix, rollout_index=int(r), month_index=month_index) for r in rollouts
        ]
        lot_dispositions.extend(
            {
                "rollout_index": rollouts,
                "month_index": np.full(size, month_index, dtype=np.int64),
                "lot_disposition_id": [f"{jid}:lot:{lot_id}" for jid in journal_entry_ids],
                "journal_entry_id": journal_entry_ids,
                "lot_id": [lot_id] * size,
                "asset_class": [LotAssetClass.PRIVATE_EQUITY.value] * size,
                "proceeds_usd": amounts,
                "cost_basis_usd": np.maximum(0.0, bases),
                "realized_gain_usd": amounts - bases,
                "taxable_gain_usd": sale_application.taxable_gain_usd[rollouts].astype(np.float64),
                "quantity_sold": sale_application.sold_units[rollouts].astype(np.float64).tolist(),
                "tax_expense_usd": tax_usd[rollouts].astype(np.float64),
            }
        )


def _record_partner_agreement_accounting_detail(
    accounting: AccountingTraceBuilder, *, month_index: np.ndarray, partner_equity: PartnerEquityArrays
) -> None:
    if not partner_equity.journal_entries and not partner_equity.balance_snapshots:
        return
    _record_journal_entry_batches(accounting, month_index=month_index, entries=partner_equity.journal_entries)
    _record_balance_snapshot_batches(accounting, month_index=month_index, entries=partner_equity.balance_snapshots)


def _record_property_sale_effects(
    effects: dict[EffectType, event_streams.StreamFrameBuilder],
    *,
    scenario: Scenario,
    disposition: PropertyDispositionArrays,
    tax_usd: np.ndarray | None = None,
    net_proceeds_usd: np.ndarray | None = None,
) -> None:
    if disposition.sale_event is None or disposition.sale_month is None:
        return
    sale_event = disposition.sale_event
    property_id = sale_event.property_id or scenario.property_selection.property_id
    if property_id is None:
        return
    month = disposition.sale_month
    sale_tax = tax_usd if tax_usd is not None else disposition.column("property_sale_tax_usd")
    net_proceeds = (
        net_proceeds_usd if net_proceeds_usd is not None else disposition.column("property_sale_net_proceeds_usd")
    )
    gross = disposition.column("property_sale_gross_usd")[:, month]
    selling_cost = disposition.column("sale_closing_cost_usd")[:, month]
    debt_payoff = disposition.column("property_sale_debt_payoff_usd")[:, month]
    tax_col = sale_tax[:, month]
    net_proceeds_col = net_proceeds[:, month]
    active = (gross != 0) | (selling_cost != 0) | (debt_payoff != 0) | (tax_col != 0) | (net_proceeds_col != 0)
    if not active.any():
        return
    rollouts = np.nonzero(active)[0].astype(np.int64)
    size = int(rollouts.size)
    actor_id = sale_event.actor_id or _primary_owner_actor_id(scenario)
    effects[EffectType.SETTLE_PROPERTY_SALE].extend(
        {
            "rollout_index": rollouts,
            "month_index": np.full(size, month, dtype=np.int64),
            "actor_id": [actor_id] * size,
            "policy_id": [PROPERTY_SALE_SETTLEMENT_POLICY_ID] * size,
            "event_id": [sale_event.event_id] * size,
            "property_id": [property_id] * size,
            "gross_sale_usd": gross[rollouts].astype(np.float64),
            "selling_cost_usd": selling_cost[rollouts].astype(np.float64),
            "debt_payoff_usd": debt_payoff[rollouts].astype(np.float64),
            "adjusted_basis_usd": disposition.column("property_sale_adjusted_basis_usd")[rollouts, month].astype(
                np.float64
            ),
            "realized_gain_usd": disposition.column("realized_property_gain_usd")[rollouts, month].astype(np.float64),
            "depreciation_recapture_usd": disposition.column("depreciation_recapture_usd")[rollouts, month].astype(
                np.float64
            ),
            "capital_gain_usd": disposition.column("property_sale_capital_gain_usd")[rollouts, month].astype(
                np.float64
            ),
            "capital_gain_exclusion_usd": disposition.column("property_sale_capital_gain_exclusion_usd")[
                rollouts, month
            ].astype(np.float64),
            "taxable_capital_gain_usd": disposition.column("taxable_property_capital_gain_usd")[rollouts, month].astype(
                np.float64
            ),
            "taxable_gain_usd": disposition.column("taxable_property_gain_usd")[rollouts, month].astype(np.float64),
            "tax_usd": tax_col[rollouts].astype(np.float64),
            "net_proceeds_usd": net_proceeds_col[rollouts].astype(np.float64),
            "proceeds_destination": [AccountType.CHECKING.value] * size,
        }
    )


def _record_sp500_sale_effects(
    effects: dict[EffectType, event_streams.StreamFrameBuilder],
    *,
    month_index: int,
    policy: Policy,
    amount_usd: np.ndarray,
    basis_usd: np.ndarray,
    tax_usd: np.ndarray,
    shortfall_usd: np.ndarray,
) -> None:
    mask = (amount_usd > 0) | (shortfall_usd > 0)
    if not mask.any():
        return
    rollouts = np.nonzero(mask)[0].astype(np.int64)
    size = int(rollouts.size)
    amounts = amount_usd[rollouts].astype(np.float64)
    bases = basis_usd[rollouts].astype(np.float64)
    taxes = tax_usd[rollouts].astype(np.float64)
    effects[EffectType.SELL_SP500].extend(
        {
            "rollout_index": rollouts,
            "month_index": np.full(size, month_index, dtype=np.int64),
            "actor_id": [policy.actor_id] * size,
            "policy_id": [policy.policy_id] * size,
            "amount_usd": amounts,
            "after_tax_proceeds_usd": np.maximum(0.0, amounts - taxes),
            "basis_usd": bases,
            "gain_usd": amounts - bases,
            "tax_usd": taxes,
            "shortfall_usd": shortfall_usd[rollouts].astype(np.float64),
        }
    )


def _record_crypto_sale_journal_entries(
    accounting: AccountingTraceBuilder,
    lot_dispositions: event_streams.StreamFrameBuilder,
    *,
    month_index: int,
    policy: Policy,
    cause_id_prefix: str,
    source_asset_id: str,
    amount_usd: np.ndarray,
    basis_usd: np.ndarray,
    tax_usd: np.ndarray,
) -> None:
    """Record crypto sale postings + per-rollout LotDisposition rows.

    The realized gain on the LotDisposition contributes to long-term capital
    gain at the annual-tax step. `tax_usd` is the per-rollout tax expense
    attributed to this sale by `annual_sale_tax_allocation` (matching the
    SP500/PE path)."""
    accounting.record_entry_firings(
        schema=posting_schemas.ASSET_SALE_CRYPTO,
        month_index=month_index,
        cause_id_prefix=cause_id_prefix,
        actor_id=policy.actor_id,
        policy_id=policy.policy_id,
        description="crypto sale",
        amount_bindings={"amount": amount_usd},
        leg_chart_account_keys=(
            {"actor_id": policy.actor_id},
            {"actor_id": policy.actor_id, "source_asset_id": source_asset_id},
        ),
    )
    lot_id = _tax_lot_id(LotAssetClass.CRYPTO, source_asset_id)
    mask = amount_usd > 0
    if mask.any():
        rollouts = np.nonzero(mask)[0].astype(np.int64)
        size = int(rollouts.size)
        amounts = amount_usd[rollouts].astype(np.float64)
        bases = basis_usd[rollouts].astype(np.float64)
        gains = amounts - bases
        journal_entry_ids = [
            _trace_row_id(cause_id_prefix, rollout_index=int(r), month_index=month_index) for r in rollouts
        ]
        lot_dispositions.extend(
            {
                "rollout_index": rollouts,
                "month_index": np.full(size, month_index, dtype=np.int64),
                "lot_disposition_id": [f"{jid}:lot:{lot_id}" for jid in journal_entry_ids],
                "journal_entry_id": journal_entry_ids,
                "lot_id": [lot_id] * size,
                "asset_class": [LotAssetClass.CRYPTO.value] * size,
                "proceeds_usd": amounts,
                "cost_basis_usd": np.maximum(0.0, bases),
                "realized_gain_usd": gains,
                "taxable_gain_usd": np.maximum(0.0, gains),
                "quantity_sold": np.full(size, None, dtype=object),
                "tax_expense_usd": tax_usd[rollouts].astype(np.float64),
            }
        )


def _record_crypto_sale_effects(
    effects: dict[EffectType, event_streams.StreamFrameBuilder],
    *,
    month_index: int,
    policy: Policy,
    source_asset_id: str,
    asset_symbol: str,
    amount_usd: np.ndarray,
    basis_usd: np.ndarray,
    quantity_sold: np.ndarray,
    shortfall_usd: np.ndarray,
) -> None:
    mask = (amount_usd > 0) | (shortfall_usd > 0)
    if not mask.any():
        return
    rollouts = np.nonzero(mask)[0].astype(np.int64)
    size = int(rollouts.size)
    amounts = amount_usd[rollouts].astype(np.float64)
    bases = basis_usd[rollouts].astype(np.float64)
    effects[EffectType.SELL_CRYPTO].extend(
        {
            "rollout_index": rollouts,
            "month_index": np.full(size, month_index, dtype=np.int64),
            "actor_id": [policy.actor_id] * size,
            "policy_id": [policy.policy_id] * size,
            "source_asset_id": [source_asset_id] * size,
            "asset_symbol": [asset_symbol] * size,
            "amount_usd": amounts,
            "quantity_sold": quantity_sold[rollouts].astype(np.float64),
            "basis_usd": bases,
            "gain_usd": amounts - bases,
            "shortfall_usd": shortfall_usd[rollouts].astype(np.float64),
        }
    )


def _record_private_equity_sale_effects(
    effects: dict[EffectType, event_streams.StreamFrameBuilder],
    *,
    month_index: int,
    instruction: PrivateEquitySaleInstructionBatch,
    sale_application: PrivateEquitySaleApplication,
    estimated_tax_usd: np.ndarray,
) -> None:
    mask = sale_application.sale_usd > 0
    if not mask.any():
        return
    rollouts = np.nonzero(mask)[0].astype(np.int64)
    size = int(rollouts.size)
    amounts = sale_application.sale_usd[rollouts].astype(np.float64)
    taxes = estimated_tax_usd[rollouts].astype(np.float64)
    after_tax = np.maximum(0.0, amounts - taxes)
    proceeds_destination_value = (
        instruction.proceeds_destination.value
        if hasattr(instruction.proceeds_destination, "value")
        else str(instruction.proceeds_destination)
    )
    effects[EffectType.SELL_PRIVATE_EQUITY].extend(
        {
            "rollout_index": rollouts,
            "month_index": np.full(size, month_index, dtype=np.int64),
            "actor_id": [instruction.actor_id] * size,
            "policy_id": [instruction.policy_id] * size,
            "event_id": [None] * size,
            "event_type": [None] * size,
            "opportunity_id": [instruction.opportunity_id[r] for r in rollouts],
            "opportunity_cause_id": [str(instruction.opportunity_cause_id[r]) for r in rollouts],
            "amount_usd": amounts,
            "after_tax_proceeds_usd": after_tax,
            "basis_usd": sale_application.basis_usd[rollouts].astype(np.float64),
            "taxable_gain_usd": sale_application.taxable_gain_usd[rollouts].astype(np.float64),
            "estimated_tax_usd": taxes,
            "units_sold": sale_application.sold_units[rollouts].astype(np.float64),
            "sold_fraction": sale_application.sold_fraction[rollouts].astype(np.float64),
            "proceeds_destination": [proceeds_destination_value] * size,
        }
    )


def _tax_share_for_sale_action(
    *, source_tax_usd: np.ndarray, action_taxable_income_usd: np.ndarray, source_taxable_income_usd: np.ndarray
) -> np.ndarray:
    tax_share = np.zeros_like(source_tax_usd, dtype="float64")
    np.divide(
        source_tax_usd * action_taxable_income_usd,
        source_taxable_income_usd,
        out=tax_share,
        where=source_taxable_income_usd > 0,
    )
    return tax_share


@dataclass(frozen=True)
class _AnnualTaxObligationKind:
    obligation_type: ObligationType = ObligationType.ANNUAL_TAX_PAYMENT
    required: bool = True


@dataclass(frozen=True)
class _EstimatedTaxObligationKind:
    """Quarterly estimated tax prepayment.

    Posts a single OBLIGATION_SETTLEMENT entry that debits TAX_PAYABLE
    (treating the prepayment as a contra-liability on the same payable
    account the year-end accrual fills) and credits CHECKING_CASH. The
    TAX_ACCRUAL — debit TAX_EXPENSE, credit TAX_PAYABLE — still happens
    once at year-end via `_AnnualTaxObligationKind`, so combined the
    period nets to debit TAX_EXPENSE, credit CHECKING_CASH with no
    double-counting of expense.
    """

    obligation_type: ObligationType = ObligationType.ESTIMATED_TAX_PAYMENT
    required: bool = True


@dataclass(frozen=True)
class _MortgageObligationKind:
    interest_usd: np.ndarray
    principal_usd: np.ndarray
    property_id: str
    obligation_type: ObligationType = ObligationType.MORTGAGE_PAYMENT
    required: bool = True


@dataclass(frozen=True)
class _CashDebitObligationKind:
    """Generic obligation kind for cash demands that settle as a single expense debit
    against cash on the settlement journal entry.

    Used for variants that don't carry their own per-line accounting nuance:
    property tax, HOA dues, insurance, maintenance, outside rent, and special
    assessment.

    `expense_role` is the chart-account role to debit on settlement. The credit
    side is `CHECKING_CASH`. `journal_entry_type` controls how the trace surfaces
    the settlement entry (typically `OBLIGATION_SETTLEMENT`).
    """

    obligation_type: ObligationType
    expense_role: ChartAccountRole
    journal_entry_type: JournalEntryType = JournalEntryType.OBLIGATION_SETTLEMENT
    required: bool = True


@dataclass(frozen=True)
class _PartnerContributionObligationKind:
    """Obligation kind for the contributing actor's monthly equity-building payment.

    The settlement is a cross-actor cash transfer: the contributor's CHECKING_CASH
    is credited (cash leaves) and the recipient owner's CHECKING_CASH is debited
    (cash arrives). The owner's receipt is a downstream effect of the contributor
    funding the obligation — the obligation lives on the contributing actor's
    books, and a contributing-actor shortfall fails the rollout.

    The cash side is balanced on the settlement JE itself; the engine math
    separately credits owner cash via `partner_equity.column("contribution_used_usd")` in
    the month loop, which mirrors the funded amount on the happy path.
    """

    property_id: str
    recipient_actor_id: str
    obligation_type: ObligationType = ObligationType.PARTNER_CONTRIBUTION
    required: bool = True


_ObligationKind = (
    _AnnualTaxObligationKind
    | _EstimatedTaxObligationKind
    | _MortgageObligationKind
    | _CashDebitObligationKind
    | _PartnerContributionObligationKind
)


def _settle_required_cash_obligations(
    *,
    scenario: Scenario,
    market_bundle: MarketBundle,
    month_index: np.ndarray,
    policy_steps: tuple[ActorPolicyStep[Policy], ...],
    obligation_amount_usd: np.ndarray,
    obligation_kind: _ObligationKind,
    creditor_id: str,
    source_policy_id: str,
    cash_usd: np.ndarray,
    generic_sp500_value_usd: np.ndarray,
    remaining_sp500_units_by_month: np.ndarray,
    remaining_sp500_basis_by_month: np.ndarray,
    crypto_value_usd: np.ndarray,
    remaining_crypto_quantity_by_month: np.ndarray,
    remaining_crypto_basis_by_month: np.ndarray,
    crypto_sale_usd: np.ndarray,
    crypto_sale_basis_usd: np.ndarray,
    checking_floor_shortfall_usd: np.ndarray,
    obligations: event_streams.StreamFrameBuilder,
    funding_decisions: event_streams.StreamFrameBuilder,
    accounting: AccountingTraceBuilder,
    sp500_sale_action_records: list[Sp500SaleActionRecord],
    crypto_sale_action_records: list[CryptoSaleActionRecord],
    actor_id: str | None = None,
    pe_state: _PrivateEquityFundingState | None = None,
) -> None:
    resolved_actor_id = actor_id if actor_id is not None else _primary_owner_actor_id(scenario)
    sources = _ObligationFundingSources.for_actor(scenario, actor_id=resolved_actor_id)
    accumulator = _ObligationFundingAccumulator.new(
        obligation_kind=obligation_kind,
        creditor_id=creditor_id,
        source_policy_id=source_policy_id,
        actor_id=resolved_actor_id,
        cash_source_account_id=(
            sources.cash_source_account.account_id if sources.cash_source_account is not None else None
        ),
        rollout_count=int(obligation_amount_usd.shape[0]),
        month_index=month_index,
        amount_due_usd=obligation_amount_usd,
    )
    # The matrices passed in carry per-month state populated by the main engine
    # month loop; each iteration here reads `[:, month_position]` into 1D
    # vectors, calls settlement (1D in, 1D out), writes the post-settlement
    # state back into matrix `[:, month_position]`, and explicitly
    # forward-propagates units/basis (constant going forward) and
    # multiplier-scaled value to `[:, month_position+1:]` so the next
    # iteration — and subsequent settlement calls for other obligation kinds —
    # see the post-sale state. PE state is matrix-backed end-to-end and
    # self-propagates inside the settlement function.
    sp500_multipliers = market_bundle.generic_sp500_multipliers
    crypto_path = (
        market_bundle.crypto_value_multiplier(sources.crypto_source_symbol) if sources.crypto_source_symbol else None
    )
    months_total = remaining_sp500_units_by_month.shape[1]
    for month_position, due_month_index in enumerate(month_index.tolist()):
        if not np.any(obligation_amount_usd[:, month_position] > 0):
            continue
        pre_cash = cash_usd[:, month_position].copy()
        pre_sp500_units = remaining_sp500_units_by_month[:, month_position].copy()
        pre_sp500_basis = remaining_sp500_basis_by_month[:, month_position].copy()
        pre_crypto_quantity = remaining_crypto_quantity_by_month[:, month_position].copy()
        pre_crypto_basis = remaining_crypto_basis_by_month[:, month_position].copy()
        settled = _settle_required_cash_obligation_at_month_position(
            market_bundle=market_bundle,
            month_position=month_position,
            due_month_index=int(due_month_index),
            policy_steps=policy_steps,
            obligation_amount_usd=obligation_amount_usd[:, month_position],
            obligation_kind=obligation_kind,
            creditor_id=creditor_id,
            source_policy_id=source_policy_id,
            actor_id=resolved_actor_id,
            sources=sources,
            cash_usd=pre_cash,
            generic_sp500_value_usd=generic_sp500_value_usd[:, month_position],
            remaining_sp500_units=pre_sp500_units,
            remaining_sp500_basis=pre_sp500_basis,
            crypto_value_usd=crypto_value_usd[:, month_position],
            remaining_crypto_quantity=pre_crypto_quantity,
            remaining_crypto_basis=pre_crypto_basis,
            crypto_sale_usd=crypto_sale_usd,
            crypto_sale_basis_usd=crypto_sale_basis_usd,
            checking_floor_shortfall_usd=checking_floor_shortfall_usd,
            accumulator=accumulator,
            accounting=accounting,
            sp500_sale_action_records=sp500_sale_action_records,
            crypto_sale_action_records=crypto_sale_action_records,
            pe_state=pe_state,
        )
        cash_usd[:, month_position] = settled.cash_usd
        remaining_sp500_units_by_month[:, month_position] = settled.remaining_sp500_units
        remaining_sp500_basis_by_month[:, month_position] = settled.remaining_sp500_basis
        generic_sp500_value_usd[:, month_position] = settled.generic_sp500_value_usd
        remaining_crypto_quantity_by_month[:, month_position] = settled.remaining_crypto_quantity
        remaining_crypto_basis_by_month[:, month_position] = settled.remaining_crypto_basis
        crypto_value_usd[:, month_position] = settled.crypto_value_usd
        # Forward-propagate deltas (not absolute values) so the main-loop's
        # per-month dynamics — e.g. acquisition proceeds at a future
        # event_month, additional main-loop sales, multiplier evolution —
        # are preserved while this iteration's settlement deltas accumulate.
        if month_position + 1 < months_total:
            cash_delta = settled.cash_usd - pre_cash
            sp500_units_delta = settled.remaining_sp500_units - pre_sp500_units
            sp500_basis_delta = settled.remaining_sp500_basis - pre_sp500_basis
            crypto_quantity_delta = settled.remaining_crypto_quantity - pre_crypto_quantity
            crypto_basis_delta = settled.remaining_crypto_basis - pre_crypto_basis
            cash_usd[:, month_position + 1 :] = cash_usd[:, month_position + 1 :] + cash_delta[:, None]
            remaining_sp500_units_by_month[:, month_position + 1 :] = np.maximum(
                0.0, remaining_sp500_units_by_month[:, month_position + 1 :] + sp500_units_delta[:, None]
            )
            remaining_sp500_basis_by_month[:, month_position + 1 :] = np.maximum(
                0.0, remaining_sp500_basis_by_month[:, month_position + 1 :] + sp500_basis_delta[:, None]
            )
            generic_sp500_value_usd[:, month_position + 1 :] = (
                remaining_sp500_units_by_month[:, month_position + 1 :] * sp500_multipliers[:, month_position + 1 :]
            )
            remaining_crypto_quantity_by_month[:, month_position + 1 :] = np.maximum(
                0.0, remaining_crypto_quantity_by_month[:, month_position + 1 :] + crypto_quantity_delta[:, None]
            )
            remaining_crypto_basis_by_month[:, month_position + 1 :] = np.maximum(
                0.0, remaining_crypto_basis_by_month[:, month_position + 1 :] + crypto_basis_delta[:, None]
            )
            if crypto_path is not None:
                crypto_value_usd[:, month_position + 1 :] = (
                    remaining_crypto_quantity_by_month[:, month_position + 1 :] * crypto_path[:, month_position + 1 :]
                )
    accumulator.emit_obligations(obligations)
    accumulator.emit_funding_decisions(funding_decisions)


@dataclass
class _SaleDecisionMatrices:
    """Per-`(policy_step, asset_type)` slot inside one
    `_ObligationFundingAccumulator`. Constants (`policy_id`,
    `policy_sequence_index`, `source_*`) are scalar; per-rollout-per-month
    numbers live in `(rollouts, months)` matrices that the per-month
    inner-loop writes by month-position slice."""

    decision_type: FundingDecisionType
    policy_id: str
    policy_sequence_index: int
    source_type: FundingSourceType
    source_asset_id: str | None
    source_asset_type: AssetType | None
    source_account_id: str | None
    source_account_type: AccountType | None
    requested_sale_usd: np.ndarray
    funded_cash_usd: np.ndarray
    shortfall_usd: np.ndarray

    @classmethod
    def new(
        cls,
        *,
        rollout_count: int,
        months_plus_one: int,
        decision_type: FundingDecisionType,
        policy_id: str,
        policy_sequence_index: int,
        source_type: FundingSourceType,
        source_asset_id: str | None,
        source_asset_type: AssetType | None,
        source_account_id: str | None = None,
        source_account_type: AccountType | None = None,
    ) -> _SaleDecisionMatrices:
        zeros = lambda: np.zeros((rollout_count, months_plus_one), dtype=np.float64)  # noqa: E731
        return cls(
            decision_type=decision_type,
            policy_id=policy_id,
            policy_sequence_index=policy_sequence_index,
            source_type=source_type,
            source_asset_id=source_asset_id,
            source_asset_type=source_asset_type,
            source_account_id=source_account_id,
            source_account_type=source_account_type,
            requested_sale_usd=zeros(),
            funded_cash_usd=zeros(),
            shortfall_usd=zeros(),
        )


@dataclass
class _ObligationFundingAccumulator:
    """Per-`_settle_required_cash_obligations` call state.

    The outer per-month loop is sequential, but every settlement op
    inside it is `(rollouts,)` numpy-vectorized — `paid_from_cash =
    np.minimum(np.maximum(0.0, cash[:, m]), due)`, etc. The legacy
    recording layer broke that vectorization by dispatching into Python
    list comprehensions per `(month, kind, recorder)` to build
    `obligation_id` f-strings and dict-of-columns blocks for
    `StreamFrameBuilder.extend(...)`.

    This accumulator preserves the rollout-vectorization through the
    recording layer: per-month writes from
    `_settle_required_cash_obligation_at_month_position` slice
    `(rollouts,)` vectors into `(rollouts, months)` matrices on this
    object, and a single `emit_*` pass at end-of-call melts each matrix
    family into one `StreamFrameBuilder.extend(...)` per
    `(kind, decision_type, [policy_step])` triple. `obligation_id` /
    `status` are derived at materialize time on the leaner schemas
    (`event_streams.OBLIGATION_LIFECYCLE_SCHEMA` /
    `FUNDING_DECISION_SCHEMA`)."""

    obligation_kind: _ObligationKind
    creditor_id: str
    source_policy_id: str
    actor_id: str
    cash_source_account_id: str | None
    month_index_array: np.ndarray
    amount_due_usd: np.ndarray
    amount_paid_usd: np.ndarray
    cash_available_usd: np.ndarray
    cash_funded_usd: np.ndarray
    sale_decisions: dict[tuple[int, AssetType], _SaleDecisionMatrices]

    @classmethod
    def new(
        cls,
        *,
        obligation_kind: _ObligationKind,
        creditor_id: str,
        source_policy_id: str,
        actor_id: str,
        cash_source_account_id: str | None,
        rollout_count: int,
        month_index: np.ndarray,
        amount_due_usd: np.ndarray,
    ) -> _ObligationFundingAccumulator:
        months_plus_one = int(month_index.size)
        zeros = lambda: np.zeros((rollout_count, months_plus_one), dtype=np.float64)  # noqa: E731
        return cls(
            obligation_kind=obligation_kind,
            creditor_id=creditor_id,
            source_policy_id=source_policy_id,
            actor_id=actor_id,
            cash_source_account_id=cash_source_account_id,
            month_index_array=month_index,
            amount_due_usd=amount_due_usd,
            amount_paid_usd=zeros(),
            cash_available_usd=zeros(),
            cash_funded_usd=zeros(),
            sale_decisions={},
        )

    @property
    def unpaid_amount_usd(self) -> np.ndarray:
        unpaid: np.ndarray = np.maximum(0.0, self.amount_due_usd - self.amount_paid_usd)
        return unpaid

    def record_cash_decision(self, *, month_position: int, available_cash: np.ndarray, funded_cash: np.ndarray) -> None:
        self.cash_available_usd[:, month_position] = available_cash
        self.cash_funded_usd[:, month_position] = funded_cash

    def record_sale_decision(
        self,
        *,
        month_position: int,
        policy_step_index: int,
        asset_type: AssetType,
        decision_type: FundingDecisionType,
        policy_id: str,
        source_type: FundingSourceType,
        source_asset_id: str | None,
        source_asset_type: AssetType | None,
        requested_sale: np.ndarray,
        funded_cash: np.ndarray,
        shortfall: np.ndarray,
        source_account_id: str | None = None,
        source_account_type: AccountType | None = None,
    ) -> None:
        key = (policy_step_index, asset_type)
        slot = self.sale_decisions.get(key)
        if slot is None:
            slot = _SaleDecisionMatrices.new(
                rollout_count=self.amount_due_usd.shape[0],
                months_plus_one=self.amount_due_usd.shape[1],
                decision_type=decision_type,
                policy_id=policy_id,
                policy_sequence_index=policy_step_index,
                source_type=source_type,
                source_asset_id=source_asset_id,
                source_asset_type=source_asset_type,
                source_account_id=source_account_id,
                source_account_type=source_account_type,
            )
            self.sale_decisions[key] = slot
        slot.requested_sale_usd[:, month_position] = requested_sale
        slot.funded_cash_usd[:, month_position] = funded_cash
        slot.shortfall_usd[:, month_position] = shortfall

    def record_settlement(self, *, month_position: int, amount_paid: np.ndarray) -> None:
        self.amount_paid_usd[:, month_position] = amount_paid

    def emit_obligations(self, builder: event_streams.StreamFrameBuilder) -> None:
        mask = self.amount_due_usd > 0
        if not mask.any():
            return
        rollout_axis, month_axis = np.nonzero(mask)
        size = int(rollout_axis.size)
        unpaid = np.maximum(0.0, self.amount_due_usd - self.amount_paid_usd)
        builder.extend(
            {
                "rollout_index": rollout_axis.astype(np.int64),
                "month_index": self.month_index_array[month_axis].astype(np.int64),
                "obligation_type": [self.obligation_kind.obligation_type.value] * size,
                "actor_id": [self.actor_id] * size,
                "creditor_id": [self.creditor_id] * size,
                "due_month_index": self.month_index_array[month_axis].astype(np.int64),
                "amount_due_usd": self.amount_due_usd[rollout_axis, month_axis],
                "amount_paid_usd": self.amount_paid_usd[rollout_axis, month_axis],
                "unpaid_amount_usd": unpaid[rollout_axis, month_axis],
                "source_policy_id": [self.source_policy_id] * size,
                "required": np.full(size, self.obligation_kind.required, dtype=np.bool_),
            }
        )

    def emit_funding_decisions(self, builder: event_streams.StreamFrameBuilder) -> None:
        unpaid = np.maximum(0.0, self.amount_due_usd - self.amount_paid_usd)
        cash_shortfall = np.maximum(0.0, self.amount_due_usd - self.cash_funded_usd)
        # Cash decisions: one row per (rollout, month) where the kind ever had
        # a non-zero amount due. Mirrors the legacy
        # `_record_obligation_cash_funding_decisions` mask.
        cash_mask = self.amount_due_usd > 0
        if cash_mask.any():
            rollout_axis, month_axis = np.nonzero(cash_mask)
            size = int(rollout_axis.size)
            zeros = np.zeros(size, dtype=np.float64)
            builder.extend(
                {
                    "rollout_index": rollout_axis.astype(np.int64),
                    "month_index": self.month_index_array[month_axis].astype(np.int64),
                    "obligation_type": [self.obligation_kind.obligation_type.value] * size,
                    "decision_type": [FundingDecisionType.USE_CASH.value] * size,
                    "actor_id": [self.actor_id] * size,
                    "policy_id": [None] * size,
                    "policy_sequence_index": [None] * size,
                    "source_type": [FundingSourceType.CASH_ACCOUNT.value] * size,
                    "source_account_id": [self.cash_source_account_id] * size,
                    "source_account_type": [AccountType.CHECKING.value] * size,
                    "source_asset_id": [None] * size,
                    "source_asset_type": [None] * size,
                    "available_cash_usd": self.cash_available_usd[rollout_axis, month_axis],
                    "requested_cash_usd": self.amount_due_usd[rollout_axis, month_axis],
                    "requested_sale_usd": zeros,
                    "funded_cash_usd": self.cash_funded_usd[rollout_axis, month_axis],
                    "shortfall_usd": cash_shortfall[rollout_axis, month_axis],
                }
            )
        # Sale decisions: one row per (rollout, month, policy_step, asset_type)
        # where the step's requested_sale or shortfall is non-zero.
        for slot in self.sale_decisions.values():
            mask = (slot.requested_sale_usd > 0) | (slot.shortfall_usd > 0)
            if not mask.any():
                continue
            rollout_axis, month_axis = np.nonzero(mask)
            size = int(rollout_axis.size)
            zeros = np.zeros(size, dtype=np.float64)
            builder.extend(
                {
                    "rollout_index": rollout_axis.astype(np.int64),
                    "month_index": self.month_index_array[month_axis].astype(np.int64),
                    "obligation_type": [self.obligation_kind.obligation_type.value] * size,
                    "decision_type": [slot.decision_type.value] * size,
                    "actor_id": [self.actor_id] * size,
                    "policy_id": [slot.policy_id] * size,
                    "policy_sequence_index": np.full(size, slot.policy_sequence_index, dtype=np.int64),
                    "source_type": [slot.source_type.value] * size,
                    "source_account_id": [slot.source_account_id] * size,
                    "source_account_type": [
                        None if slot.source_account_type is None else slot.source_account_type.value
                    ]
                    * size,
                    "source_asset_id": [slot.source_asset_id] * size,
                    "source_asset_type": [None if slot.source_asset_type is None else slot.source_asset_type.value]
                    * size,
                    "available_cash_usd": zeros,
                    "requested_cash_usd": self.amount_due_usd[rollout_axis, month_axis],
                    "requested_sale_usd": slot.requested_sale_usd[rollout_axis, month_axis],
                    "funded_cash_usd": slot.funded_cash_usd[rollout_axis, month_axis],
                    "shortfall_usd": slot.shortfall_usd[rollout_axis, month_axis],
                }
            )
        # Unfunded decisions: one row per (rollout, month) where settlement
        # left an unpaid balance. Mirrors `_record_unfunded_obligation_decisions`.
        unfunded_mask = unpaid > 0
        if unfunded_mask.any():
            rollout_axis, month_axis = np.nonzero(unfunded_mask)
            size = int(rollout_axis.size)
            zeros = np.zeros(size, dtype=np.float64)
            builder.extend(
                {
                    "rollout_index": rollout_axis.astype(np.int64),
                    "month_index": self.month_index_array[month_axis].astype(np.int64),
                    "obligation_type": [self.obligation_kind.obligation_type.value] * size,
                    "decision_type": [FundingDecisionType.UNFUNDED.value] * size,
                    "actor_id": [self.actor_id] * size,
                    "policy_id": [None] * size,
                    "policy_sequence_index": [None] * size,
                    "source_type": [FundingSourceType.UNFUNDED.value] * size,
                    "source_account_id": [None] * size,
                    "source_account_type": [None] * size,
                    "source_asset_id": [None] * size,
                    "source_asset_type": [None] * size,
                    "available_cash_usd": zeros,
                    "requested_cash_usd": self.amount_due_usd[rollout_axis, month_axis],
                    "requested_sale_usd": zeros,
                    "funded_cash_usd": zeros,
                    "shortfall_usd": unpaid[rollout_axis, month_axis],
                }
            )


@dataclass(frozen=True)
class _ObligationSettlementResult:
    """1D per-rollout state returned by
    `_settle_required_cash_obligation_at_month_position`. All fields are
    `(rollouts,)` numpy vectors. The main month loop threads these back into
    its 1D `current_cash` / `remaining_*` locals; the post-loop wrapper
    `_settle_required_cash_obligations` writes them into matrix `[:, M]`
    snapshots and forward-propagates to later months explicitly."""

    cash_usd: np.ndarray
    remaining_sp500_units: np.ndarray
    remaining_sp500_basis: np.ndarray
    generic_sp500_value_usd: np.ndarray
    remaining_crypto_quantity: np.ndarray
    remaining_crypto_basis: np.ndarray
    crypto_value_usd: np.ndarray


@dataclass
class _PrivateEquityFundingState:
    """Mutable PE state shared with the obligation funding chain.

    The PE state is per-portfolio (the engine aggregates positions into one
    `private_equity_*` path). When a `PublicMarket`-regime PE position funds
    an obligation, this state updates the per-month matrices for downstream
    rows (cash, PE value, sale-tax recording, etc.) and appends a
    `PrivateEquitySaleActionRecord` to the action-record list so the same
    journal-entry / lot-disposition / effect recorder used by
    `PrivateEquitySalePolicy` covers the funding sale.

    All matrices are shaped `(rollout, month)`. The 1D `remaining_units` /
    `remaining_basis_usd` vectors track end-of-month state and are kept in
    sync with the matrices.
    """

    private_equity_value_usd: np.ndarray
    remaining_units_by_month: np.ndarray
    remaining_basis_by_month: np.ndarray
    private_equity_sale_usd: np.ndarray
    private_equity_sale_taxable_gain_usd: np.ndarray
    pe_value_multipliers: np.ndarray
    initial_private_equity: float
    source_holding_id: str
    sale_action_records: list[PrivateEquitySaleActionRecord]


@dataclass
class _PartnerSettlementContext:
    """Per-agreement state for inline partner-contribution settlement.

    Each `PartnerEquityAgreementArrays` has an obligation accumulator + per-rollout
    1D state (the contributing actor's cash, SP500 units/basis, crypto
    quantity/basis). The main month loop threads this state forward across
    iterations; post-loop the accumulator's row blocks are flushed to the
    obligation + funding-decision event streams."""

    agreement: PartnerEquityAgreementArrays
    sources: _ObligationFundingSources
    accumulator: _ObligationFundingAccumulator
    current_cash: np.ndarray  # (rollouts,) — contributing actor's checking cash
    remaining_sp500_units: np.ndarray
    remaining_sp500_basis: np.ndarray
    remaining_crypto_quantity: np.ndarray
    remaining_crypto_basis: np.ndarray
    # Per-month decision matrices (sale amounts/basis/checking-floor shortfall).
    crypto_sale_usd: np.ndarray
    crypto_sale_basis_usd: np.ndarray
    checking_floor_shortfall_usd: np.ndarray

    @classmethod
    def new(
        cls,
        *,
        scenario: Scenario,
        agreement: PartnerEquityAgreementArrays,
        owner_actor_id: str,
        rollout_count: int,
        month_count: int,
        month_index: np.ndarray,
    ) -> _PartnerSettlementContext:
        contributing_actor_id = agreement.policy.actor_id
        sources = _ObligationFundingSources.for_actor(scenario, actor_id=contributing_actor_id)
        accumulator = _ObligationFundingAccumulator.new(
            obligation_kind=_PartnerContributionObligationKind(
                property_id=agreement.property_id, recipient_actor_id=owner_actor_id
            ),
            creditor_id=owner_actor_id,
            source_policy_id=agreement.policy.policy_id,
            actor_id=contributing_actor_id,
            cash_source_account_id=(
                sources.cash_source_account.account_id if sources.cash_source_account is not None else None
            ),
            rollout_count=rollout_count,
            month_index=month_index,
            amount_due_usd=agreement.column("contribution_usd"),
        )
        partner_initial_cash = _partner_initial_funding_cash_usd(
            scenario,
            actor_id=contributing_actor_id,
            configured_contribution_total_usd=float(np.max(np.sum(agreement.column("contribution_usd"), axis=1))),
        )
        return cls(
            agreement=agreement,
            sources=sources,
            accumulator=accumulator,
            current_cash=np.full(rollout_count, partner_initial_cash, dtype="float64"),
            remaining_sp500_units=np.zeros(rollout_count, dtype="float64"),
            remaining_sp500_basis=np.zeros(rollout_count, dtype="float64"),
            remaining_crypto_quantity=np.zeros(rollout_count, dtype="float64"),
            remaining_crypto_basis=np.zeros(rollout_count, dtype="float64"),
            crypto_sale_usd=np.zeros((rollout_count, month_count), dtype="float64"),
            crypto_sale_basis_usd=np.zeros((rollout_count, month_count), dtype="float64"),
            checking_floor_shortfall_usd=np.zeros((rollout_count, month_count), dtype="float64"),
        )


@dataclass(frozen=True)
class _ObligationFundingSources:
    """Per-actor lookups for cash/SP500/crypto/PE sources used in obligation settlement.

    Cached once at the top of `_settle_required_cash_obligations` so the per-month
    helper does not re-scan the balance sheet for every month/obligation pair.
    `pe_public_market_regime` is set only when the scenario's PE positions share a
    `PublicMarket` regime, which is the only regime that can fund obligations via
    the funding-policy chain; otherwise it is `None`.
    """

    cash_source_account: AccountBalance | None
    sp500_source_asset: GenericSp500StockPosition | None
    crypto_source_id: str
    crypto_source_account_id: str | None
    crypto_source_symbol: str
    pe_source_holding_id: str
    pe_public_market_regime: PublicMarket | None

    @classmethod
    def for_actor(cls, scenario: Scenario, *, actor_id: str) -> _ObligationFundingSources:
        crypto_source_assets = _crypto_asset_sources(scenario, actor_id=actor_id)
        pe_regime = _effective_pe_liquidity_regime(scenario)
        return cls(
            cash_source_account=_single_checking_account_source(scenario, actor_id=actor_id),
            sp500_source_asset=_single_sp500_asset_source(scenario, actor_id=actor_id),
            crypto_source_id=_crypto_source_holding_id(scenario, actor_id=actor_id),
            crypto_source_account_id=crypto_source_assets[0].source_account_id if crypto_source_assets else None,
            # With explicit per-symbol bundle keying there is no aggregated
            # `"crypto_portfolio"` path; the engine reads the first symbol's path
            # for multi-symbol scenarios (placeholder paths are identical across
            # symbols today). Empty when the actor holds no crypto.
            crypto_source_symbol=(crypto_source_assets[0].asset_symbol if crypto_source_assets else ""),
            pe_source_holding_id=_private_equity_source_holding_id(scenario),
            pe_public_market_regime=pe_regime if isinstance(pe_regime, PublicMarket) else None,
        )


def _settle_required_cash_obligation_at_month_position(
    *,
    market_bundle: MarketBundle,
    month_position: int,
    due_month_index: int,
    policy_steps: tuple[ActorPolicyStep[Policy], ...],
    obligation_amount_usd: np.ndarray,
    obligation_kind: _ObligationKind,
    creditor_id: str,
    source_policy_id: str,
    actor_id: str,
    sources: _ObligationFundingSources,
    cash_usd: np.ndarray,
    generic_sp500_value_usd: np.ndarray,
    remaining_sp500_units: np.ndarray,
    remaining_sp500_basis: np.ndarray,
    crypto_value_usd: np.ndarray,
    remaining_crypto_quantity: np.ndarray,
    remaining_crypto_basis: np.ndarray,
    crypto_sale_usd: np.ndarray,
    crypto_sale_basis_usd: np.ndarray,
    checking_floor_shortfall_usd: np.ndarray,
    accumulator: _ObligationFundingAccumulator,
    accounting: AccountingTraceBuilder,
    sp500_sale_action_records: list[Sp500SaleActionRecord],
    crypto_sale_action_records: list[CryptoSaleActionRecord],
    pe_state: _PrivateEquityFundingState | None = None,
) -> _ObligationSettlementResult:
    """Settle one obligation at a single month position.

    Takes 1D `(rollouts,)` state vectors for cash/units/basis/value as the
    pre-settlement balance at `month_position`; returns the post-settlement
    balance as `_ObligationSettlementResult`. The caller threads the result
    back into its working state (1D locals in the main month loop; matrix
    columns + explicit forward propagation in
    `_settle_required_cash_obligations`).

    Single-month accumulator matrices (`crypto_sale_usd`,
    `crypto_sale_basis_usd`, `checking_floor_shortfall_usd`,
    `accumulator.*`, and `pe_state.private_equity_sale_*`) are still
    written `[:, month_position]` in place — they record per-month decisions,
    not state that flows forward. The PE branch additionally forward-propagates
    `pe_state.remaining_*` and `pe_state.private_equity_value_usd` so the
    next iteration / next settlement call sees the post-sale state.
    """
    obligation_type = obligation_kind.obligation_type
    due = obligation_amount_usd
    paid_from_cash = np.minimum(np.maximum(0.0, cash_usd), due)
    remaining_due = np.maximum(0.0, due - paid_from_cash)
    accumulator.record_cash_decision(month_position=month_position, available_cash=cash_usd, funded_cash=paid_from_cash)

    for policy_step in policy_steps:
        if not np.any(remaining_due > 0):
            break
        policy = policy_step.policy
        if not isinstance(policy, CheckingFloorSellPublicStockPolicy):
            continue
        for asset_type in policy.sale_asset_preference:
            if not np.any(remaining_due > 0):
                break
            if asset_type is AssetType.GENERIC_SP500_STOCK:
                application = _apply_checking_floor_obligation_funding_policy(
                    policy_step,
                    due_usd=due,
                    remaining_due_usd=remaining_due,
                    cash_usd=cash_usd,
                    remaining_units=remaining_sp500_units,
                    remaining_basis_usd=remaining_sp500_basis,
                    sp500_unit_price_usd=market_bundle.generic_sp500_multipliers[:, month_position],
                    source_asset=sources.sp500_source_asset,
                )
                if application is None:
                    continue
                units_sold = np.maximum(0.0, remaining_sp500_units - application.remaining_units)
                basis_sold = np.maximum(0.0, remaining_sp500_basis - application.remaining_basis_usd)
                remaining_sp500_units = np.maximum(0.0, remaining_sp500_units - units_sold)
                remaining_sp500_basis = np.maximum(0.0, remaining_sp500_basis - basis_sold)
                generic_sp500_value_usd = (
                    remaining_sp500_units * market_bundle.generic_sp500_multipliers[:, month_position]
                )
                cash_usd = cash_usd + application.sale_usd
                remaining_due = application.remaining_due_usd
                checking_floor_shortfall_usd[:, month_position] = np.maximum(
                    checking_floor_shortfall_usd[:, month_position], remaining_due
                )
                accumulator.record_sale_decision(
                    month_position=month_position,
                    policy_step_index=application.policy_step.sequence_index,
                    asset_type=AssetType.GENERIC_SP500_STOCK,
                    decision_type=FundingDecisionType.SELL_PUBLIC_STOCK,
                    policy_id=application.policy_step.policy.policy_id,
                    source_type=FundingSourceType.PUBLIC_MARKET_ASSET,
                    source_asset_id=(
                        sources.sp500_source_asset.asset_id if sources.sp500_source_asset is not None else None
                    ),
                    source_asset_type=AssetType.GENERIC_SP500_STOCK,
                    requested_sale=application.instruction.requested_amount_usd,
                    funded_cash=application.funded_cash_usd,
                    shortfall=application.shortfall_usd,
                )
                sp500_sale_action_records.append(
                    Sp500SaleActionRecord(
                        month_position=month_position,
                        month_index=due_month_index,
                        policy=application.policy_step.policy,
                        cause_id_prefix=(
                            f"policy:{application.policy_step.policy.policy_id}:{obligation_type.value}:funding_sale"
                        ),
                        numerics=_build_sale_action_frame(
                            {
                                "amount_usd": application.sale_usd,
                                "basis_usd": basis_sold,
                                "shortfall_usd": remaining_due,
                            },
                            _SP500_SALE_ACTION_COLUMNS,
                        ),
                    )
                )
            elif asset_type is AssetType.CRYPTO:
                # With explicit per-symbol bundle keying we read the actor's first
                # crypto symbol's path; the engine aggregates crypto state, so
                # heterogeneous symbols all read the same placeholder path in this
                # slice (per-symbol state splitting is a follow-on). Skip when the
                # actor holds no crypto — there is no symbol to look up.
                if not sources.crypto_source_symbol:
                    continue
                crypto_path = market_bundle.crypto_value_multiplier(sources.crypto_source_symbol)
                crypto_application = _apply_crypto_checking_floor_obligation_funding_policy(
                    policy_step,
                    due_usd=due,
                    remaining_due_usd=remaining_due,
                    cash_usd=cash_usd,
                    remaining_quantity=remaining_crypto_quantity,
                    remaining_basis_usd=remaining_crypto_basis,
                    crypto_unit_price_usd=crypto_path[:, month_position],
                    source_asset_id=sources.crypto_source_id,
                )
                if crypto_application is None:
                    continue
                quantity_sold = np.maximum(0.0, remaining_crypto_quantity - crypto_application.remaining_quantity)
                basis_sold = np.maximum(0.0, remaining_crypto_basis - crypto_application.remaining_basis_usd)
                remaining_crypto_quantity = np.maximum(0.0, remaining_crypto_quantity - quantity_sold)
                remaining_crypto_basis = np.maximum(0.0, remaining_crypto_basis - basis_sold)
                crypto_value_usd = remaining_crypto_quantity * crypto_path[:, month_position]
                cash_usd = cash_usd + crypto_application.sale_usd
                crypto_sale_usd[:, month_position] = crypto_sale_usd[:, month_position] + crypto_application.sale_usd
                crypto_sale_basis_usd[:, month_position] = crypto_sale_basis_usd[:, month_position] + basis_sold
                remaining_due = crypto_application.remaining_due_usd
                checking_floor_shortfall_usd[:, month_position] = np.maximum(
                    checking_floor_shortfall_usd[:, month_position], remaining_due
                )
                accumulator.record_sale_decision(
                    month_position=month_position,
                    policy_step_index=policy_step.sequence_index,
                    asset_type=AssetType.CRYPTO,
                    decision_type=FundingDecisionType.SELL_CRYPTO,
                    policy_id=policy.policy_id,
                    source_type=FundingSourceType.CRYPTO_ASSET,
                    source_asset_id=sources.crypto_source_id,
                    source_asset_type=AssetType.CRYPTO,
                    source_account_id=sources.crypto_source_account_id,
                    source_account_type=(
                        AccountType.CRYPTO_EXCHANGE if sources.crypto_source_account_id is not None else None
                    ),
                    requested_sale=crypto_application.instruction.requested_amount_usd,
                    funded_cash=crypto_application.funded_cash_usd,
                    shortfall=crypto_application.shortfall_usd,
                )
                crypto_sale_action_records.append(
                    CryptoSaleActionRecord(
                        month_position=month_position,
                        month_index=due_month_index,
                        policy=policy,
                        cause_id_prefix=(f"policy:{policy.policy_id}:{obligation_type.value}:funding_crypto_sale"),
                        source_asset_id=sources.crypto_source_id,
                        asset_symbol=sources.crypto_source_symbol,
                        numerics=_build_sale_action_frame(
                            {
                                "amount_usd": crypto_application.sale_usd,
                                "basis_usd": basis_sold,
                                "quantity_sold": quantity_sold,
                                "shortfall_usd": remaining_due.copy(),
                            },
                            _CRYPTO_SALE_ACTION_COLUMNS,
                        ),
                    )
                )
            elif asset_type is AssetType.PRIVATE_EQUITY:
                if sources.pe_public_market_regime is None or pe_state is None:
                    # A scenario opted into PE funding by listing PRIVATE_EQUITY in
                    # sale_asset_preference, but the position is not PublicMarket
                    # (or the engine code path has no PE state available). Skip
                    # rather than raise: the preference is a soft fallback chain.
                    continue
                # Unit price = current month's value / current month's units (whenever
                # units > 0). This is the same spot mark the LiquidityEventOnly tender
                # path uses and accurately reflects the market multiplier at this month.
                pe_units_now = pe_state.remaining_units_by_month[:, month_position]
                pe_value_now = pe_state.private_equity_value_usd[:, month_position]
                pe_unit_price_now = np.where(pe_units_now > 0, pe_value_now / np.maximum(pe_units_now, 1e-12), 0.0)
                pe_application = _apply_pe_checking_floor_obligation_funding_policy(
                    policy_step,
                    due_usd=due,
                    remaining_due_usd=remaining_due,
                    cash_usd=cash_usd,
                    remaining_units=pe_units_now,
                    remaining_basis_usd=pe_state.remaining_basis_by_month[:, month_position],
                    pe_unit_price_usd=pe_unit_price_now,
                    pe_regime=sources.pe_public_market_regime,
                    current_month_index=int(due_month_index),
                    source_holding_id=sources.pe_source_holding_id,
                )
                if pe_application is None:
                    continue
                units_sold = np.maximum(0.0, pe_units_now - pe_application.remaining_units)
                basis_sold = np.maximum(
                    0.0, pe_state.remaining_basis_by_month[:, month_position] - pe_application.remaining_basis_usd
                )
                # PE state stays matrix-backed because the post-loop
                # `_settle_required_cash_obligations` wrapper reads
                # `pe_state.remaining_*_by_month[:, M+1]` at the next iteration
                # (and successive settlement calls for other obligation kinds
                # read these matrices too). Forward-propagate units/basis and
                # multiplier-scaled value here so the next consumer sees the
                # post-sale state.
                pe_state.remaining_units_by_month[:, month_position:] = np.maximum(
                    0.0, pe_state.remaining_units_by_month[:, month_position:] - units_sold[:, None]
                )
                pe_state.remaining_basis_by_month[:, month_position:] = np.maximum(
                    0.0, pe_state.remaining_basis_by_month[:, month_position:] - basis_sold[:, None]
                )
                pe_state.private_equity_value_usd[:, month_position] = np.maximum(
                    0.0, pe_value_now - pe_application.sale_usd
                )
                if month_position + 1 < pe_state.private_equity_value_usd.shape[1]:
                    multiplier_at_month = pe_state.pe_value_multipliers[:, month_position]
                    forward_ratios = np.where(
                        multiplier_at_month[:, None] > 0,
                        pe_state.pe_value_multipliers[:, month_position + 1 :]
                        / np.maximum(multiplier_at_month[:, None], 1e-12),
                        0.0,
                    )
                    pe_state.private_equity_value_usd[:, month_position + 1 :] = np.maximum(
                        0.0, pe_state.private_equity_value_usd[:, month_position, None] * forward_ratios
                    )
                cash_usd = cash_usd + pe_application.sale_usd
                pe_state.private_equity_sale_usd[:, month_position] = (
                    pe_state.private_equity_sale_usd[:, month_position] + pe_application.sale_usd
                )
                # `pe_state.private_equity_sale_taxable_gain_usd[:, M] +=`
                # used to be done here for the post-loop tax settlement
                # path; the PE gain matrix is now derived from the unified
                # `asset_change_log` (which already records every PE sale
                # via `sp500_sale_action_records`-style `sale_action_records`
                # append below). Removed to keep one source of truth.
                remaining_due = pe_application.remaining_due_usd
                checking_floor_shortfall_usd[:, month_position] = np.maximum(
                    checking_floor_shortfall_usd[:, month_position], remaining_due
                )
                accumulator.record_sale_decision(
                    month_position=month_position,
                    policy_step_index=policy_step.sequence_index,
                    asset_type=AssetType.PRIVATE_EQUITY,
                    decision_type=FundingDecisionType.SELL_PRIVATE_EQUITY,
                    policy_id=policy.policy_id,
                    source_type=FundingSourceType.PRIVATE_EQUITY_ASSET,
                    source_asset_id=sources.pe_source_holding_id,
                    source_asset_type=AssetType.PRIVATE_EQUITY,
                    requested_sale=pe_application.instruction.requested_amount_usd,
                    funded_cash=pe_application.funded_cash_usd,
                    shortfall=pe_application.shortfall_usd,
                )
                # Mirror PrivateEquitySaleApplication shape for the existing
                # journal-entry/lot-disposition/effect recorder.
                pe_state.sale_action_records.append(
                    PrivateEquitySaleActionRecord(
                        month_position=month_position,
                        month_index=due_month_index,
                        instruction=PrivateEquitySaleInstructionBatch(
                            actor_id=policy.actor_id,
                            policy_id=policy.policy_id,
                            requested_amount_usd=pe_application.instruction.requested_amount_usd,
                            proceeds_destination=AccountType.CHECKING,
                            opportunity_id=np.array([None] * cash_usd.shape[0], dtype=object),
                            opportunity_cause_id=np.array(
                                [
                                    f"policy:{policy.policy_id}:{obligation_type.value}:funding_pe_sale:rollout:{i}:"
                                    f"month:{due_month_index}"
                                    for i in range(cash_usd.shape[0])
                                ],
                                dtype=object,
                            ),
                        ),
                        sale_application=PrivateEquitySaleApplication(
                            sale_usd=pe_application.sale_usd,
                            basis_usd=pe_application.basis_usd,
                            taxable_gain_usd=pe_application.taxable_gain_usd,
                            sold_units=pe_application.sold_units,
                            sold_fraction=pe_application.sold_fraction,
                            remaining_units=pe_application.remaining_units,
                            remaining_basis_usd=pe_application.remaining_basis_usd,
                            remaining_fraction=np.zeros_like(pe_application.sold_fraction),
                            journal_entries=(),
                        ),
                    )
                )
            else:
                raise ValueError(
                    f"unsupported sale_asset_preference entry {asset_type} for CheckingFloorSellPublicStockPolicy"
                )

    amount_paid = np.minimum(due, np.maximum(0.0, cash_usd))
    cash_usd = cash_usd - amount_paid
    _record_obligation_accrual_and_settlement_entries(
        accounting,
        obligation_kind=obligation_kind,
        month_position=month_position,
        month_index=due_month_index,
        actor_id=actor_id,
        source_policy_id=source_policy_id,
        due_usd=due,
        amount_paid_usd=amount_paid,
    )
    accumulator.record_settlement(month_position=month_position, amount_paid=amount_paid)
    return _ObligationSettlementResult(
        cash_usd=cash_usd,
        remaining_sp500_units=remaining_sp500_units,
        remaining_sp500_basis=remaining_sp500_basis,
        generic_sp500_value_usd=generic_sp500_value_usd,
        remaining_crypto_quantity=remaining_crypto_quantity,
        remaining_crypto_basis=remaining_crypto_basis,
        crypto_value_usd=crypto_value_usd,
    )


def _record_obligation_accrual_and_settlement_entries(
    accounting: AccountingTraceBuilder,
    *,
    obligation_kind: _ObligationKind,
    month_position: int,
    month_index: int,
    actor_id: str,
    source_policy_id: str,
    due_usd: np.ndarray,
    amount_paid_usd: np.ndarray,
) -> None:
    obligation_type = obligation_kind.obligation_type
    if isinstance(obligation_kind, _AnnualTaxObligationKind):
        tax_liability_id = f"tax:{obligation_type.value}"
        accounting.record_entry_firings(
            schema=posting_schemas.TAX_ACCRUAL,
            month_index=month_index,
            cause_id_prefix=f"policy:{source_policy_id}:{obligation_type.value}:accrual",
            obligation_id_prefix=obligation_type.value,
            actor_id=actor_id,
            policy_id=source_policy_id,
            description=obligation_type.value,
            amount_bindings={"amount": due_usd},
            leg_chart_account_keys=({"actor_id": actor_id}, {"actor_id": actor_id, "liability_id": tax_liability_id}),
        )
        accounting.record_entry_firings(
            schema=posting_schemas.TAX_PAYMENT_SETTLEMENT,
            month_index=month_index,
            cause_id_prefix=f"policy:{source_policy_id}:{obligation_type.value}:settlement",
            obligation_id_prefix=obligation_type.value,
            actor_id=actor_id,
            policy_id=source_policy_id,
            description=obligation_type.value,
            amount_bindings={"amount": amount_paid_usd},
            leg_chart_account_keys=({"actor_id": actor_id, "liability_id": tax_liability_id}, {"actor_id": actor_id}),
        )
        return
    if isinstance(obligation_kind, _EstimatedTaxObligationKind):
        # Estimated payments are tax prepayments. Post only the settlement leg
        # (debit TAX_PAYABLE, credit CHECKING_CASH). The year-end TAX_ACCRUAL
        # entry will later credit TAX_PAYABLE for the full year tax; combined,
        # TAX_PAYABLE nets to (full year tax - estimated paid) before the
        # year-end settlement debits the residual back to zero. The liability
        # is keyed against the annual-tax-payment family so estimated payments
        # net against the same TAX_PAYABLE balance the year-end accrual builds.
        accounting.record_entry_firings(
            schema=posting_schemas.TAX_PAYMENT_SETTLEMENT,
            month_index=month_index,
            cause_id_prefix=f"policy:{source_policy_id}:{obligation_type.value}:settlement",
            obligation_id_prefix=obligation_type.value,
            actor_id=actor_id,
            policy_id=source_policy_id,
            description=obligation_type.value,
            amount_bindings={"amount": amount_paid_usd},
            leg_chart_account_keys=(
                {"actor_id": actor_id, "liability_id": f"tax:{ObligationType.ANNUAL_TAX_PAYMENT.value}"},
                {"actor_id": actor_id},
            ),
        )
        return
    if isinstance(obligation_kind, _CashDebitObligationKind):
        accounting.record_entry_firings(
            schema=posting_schemas.CASH_DEBIT_SETTLEMENT_BY_EXPENSE_ROLE[obligation_kind.expense_role],
            month_index=month_index,
            cause_id_prefix=f"policy:{source_policy_id}:{obligation_type.value}:settlement",
            obligation_id_prefix=obligation_type.value,
            actor_id=actor_id,
            policy_id=source_policy_id,
            description=obligation_type.value,
            amount_bindings={"amount": amount_paid_usd},
            leg_chart_account_keys=({"actor_id": actor_id}, {"actor_id": actor_id}),
        )
        return
    if isinstance(obligation_kind, _PartnerContributionObligationKind):
        # Settlement is a balanced cross-actor cash transfer: the contributing
        # actor's cash is credited (cash leaves) and the recipient owner's cash
        # is debited (cash arrives). Each posting carries a counterparty actor
        # and the property id so the ledger explains the transfer.
        accounting.record_entry_firings(
            schema=posting_schemas.PARTNER_CONTRIBUTION_TRANSFER,
            month_index=month_index,
            cause_id_prefix=f"policy:{source_policy_id}:{obligation_type.value}:settlement",
            obligation_id_prefix=obligation_type.value,
            actor_id=actor_id,
            policy_id=source_policy_id,
            description=obligation_type.value,
            amount_bindings={"amount": amount_paid_usd},
            leg_chart_account_keys=(
                {
                    "actor_id": obligation_kind.recipient_actor_id,
                    "counterparty_actor_id": actor_id,
                    "property_id": obligation_kind.property_id,
                },
                {
                    "actor_id": actor_id,
                    "counterparty_actor_id": obligation_kind.recipient_actor_id,
                    "property_id": obligation_kind.property_id,
                },
            ),
        )
        return
    interest_due = obligation_kind.interest_usd[:, month_position]
    principal_due = obligation_kind.principal_usd[:, month_position]
    paid_fraction = np.divide(
        amount_paid_usd, due_usd, out=np.zeros_like(amount_paid_usd, dtype="float64"), where=due_usd > 0
    )
    interest_paid = interest_due * paid_fraction
    principal_paid = principal_due * paid_fraction
    liability_id = _mortgage_liability_id(obligation_kind.property_id)
    accounting.record_entry_firings(
        schema=posting_schemas.MORTGAGE_PAYMENT,
        month_index=month_index,
        cause_id_prefix=f"policy:{source_policy_id}:{obligation_type.value}:settlement",
        obligation_id_prefix=obligation_type.value,
        actor_id=actor_id,
        policy_id=source_policy_id,
        description=obligation_type.value,
        amount_bindings={
            "interest_paid": interest_paid,
            "principal_paid": principal_paid,
            "amount_paid": amount_paid_usd,
        },
        leg_chart_account_keys=(
            {"actor_id": actor_id},
            {"actor_id": actor_id, "liability_id": liability_id},
            {"actor_id": actor_id},
        ),
    )


def _apply_checking_floor_obligation_funding_policy(
    policy_step: ActorPolicyStep[Policy],
    *,
    due_usd: np.ndarray,
    remaining_due_usd: np.ndarray,
    cash_usd: np.ndarray,
    remaining_units: np.ndarray,
    remaining_basis_usd: np.ndarray,
    sp500_unit_price_usd: np.ndarray,
    source_asset: GenericSp500StockPosition | None,
) -> ObligationFundingPolicyApplication | None:
    policy = policy_step.policy
    if not isinstance(policy, CheckingFloorSellPublicStockPolicy):
        raise TypeError(f"checking-floor obligation funding handler received {type(policy).__name__}")
    projected_cash_after_obligation = cash_usd - due_usd
    requested_sale = np.where(
        (remaining_due_usd > 0) & (projected_cash_after_obligation < float(policy.floor_usd)),
        float(policy.sale_amount_usd),
        0.0,
    )
    instruction = SellAssetInstructionBatch(
        actor_id=policy.actor_id,
        policy_id=policy.policy_id,
        asset_type=AssetType.GENERIC_SP500_STOCK,
        requested_amount_usd=requested_sale,
        target_cash_floor_usd=float(policy.floor_usd),
        source_asset_id=source_asset.asset_id if source_asset is not None else None,
    )
    sale_application = apply_generic_sp500_sale_instruction(
        instruction,
        current_cash_usd=cash_usd,
        remaining_units=remaining_units,
        remaining_basis_usd=remaining_basis_usd,
        sp500_unit_price_usd=sp500_unit_price_usd,
    )
    if not np.any((requested_sale > 0) | (sale_application.shortfall_usd > 0)):
        return None
    funded_cash = np.minimum(remaining_due_usd, sale_application.sale_usd)
    remaining_due_after_sale = np.maximum(0.0, remaining_due_usd - funded_cash)
    return ObligationFundingPolicyApplication(
        policy_step=policy_step,
        instruction=instruction,
        sale_usd=sale_application.sale_usd,
        basis_usd=sale_application.basis_usd,
        funded_cash_usd=funded_cash,
        shortfall_usd=remaining_due_after_sale,
        remaining_due_usd=remaining_due_after_sale,
        remaining_units=sale_application.remaining_units,
        remaining_basis_usd=sale_application.remaining_basis_usd,
    )


def _apply_pe_checking_floor_obligation_funding_policy(
    policy_step: ActorPolicyStep[Policy],
    *,
    due_usd: np.ndarray,
    remaining_due_usd: np.ndarray,
    cash_usd: np.ndarray,
    remaining_units: np.ndarray,
    remaining_basis_usd: np.ndarray,
    pe_unit_price_usd: np.ndarray,
    pe_regime: PublicMarket,
    current_month_index: int,
    source_holding_id: str,
) -> PrivateEquityObligationFundingPolicyApplication | None:
    """Sell a `PublicMarket` PE position to fund a cash obligation.

    Returns `None` if the lockup has not expired or the policy did not request
    a sale (no rollouts both short of the floor and unfunded). The caller
    threads `remaining_units`/`remaining_basis_usd` (1D per-rollout arrays) and
    updates them with the application's `remaining_*` fields.
    """
    policy = policy_step.policy
    if not isinstance(policy, CheckingFloorSellPublicStockPolicy):
        raise TypeError(f"checking-floor PE obligation funding handler received {type(policy).__name__}")
    if pe_regime.lockup_end_month is not None and current_month_index < int(pe_regime.lockup_end_month):
        return None
    projected_cash_after_obligation = cash_usd - due_usd
    requested_sale = np.where(
        (remaining_due_usd > 0) & (projected_cash_after_obligation < float(policy.floor_usd)),
        # As with crypto: cap the requested sale at remaining_due. PE is a
        # discretionary funding source; selling more than needed leaks tax
        # liability without further benefit.
        np.minimum(float(policy.sale_amount_usd), remaining_due_usd),
        0.0,
    )
    instruction = SellAssetInstructionBatch(
        actor_id=policy.actor_id,
        policy_id=policy.policy_id,
        asset_type=AssetType.PRIVATE_EQUITY,
        requested_amount_usd=requested_sale,
        target_cash_floor_usd=float(policy.floor_usd),
        source_asset_id=source_holding_id,
    )
    value_usd = remaining_units * pe_unit_price_usd
    sale_usd = np.minimum(requested_sale, value_usd)
    sold_fraction = np.divide(sale_usd, value_usd, out=np.zeros_like(sale_usd), where=value_usd > 0)
    basis_usd = remaining_basis_usd * sold_fraction
    taxable_gain_usd = np.maximum(0.0, sale_usd - basis_usd)
    sold_units = remaining_units * sold_fraction
    cash_after_sale = cash_usd + sale_usd
    shortfall_usd = np.maximum(0.0, float(policy.floor_usd) - cash_after_sale)
    if not np.any((requested_sale > 0) | (shortfall_usd > 0)):
        return None
    funded_cash = np.minimum(remaining_due_usd, sale_usd)
    remaining_due_after_sale = np.maximum(0.0, remaining_due_usd - funded_cash)
    return PrivateEquityObligationFundingPolicyApplication(
        policy_step=policy_step,
        instruction=instruction,
        sale_usd=sale_usd,
        basis_usd=basis_usd,
        taxable_gain_usd=taxable_gain_usd,
        funded_cash_usd=funded_cash,
        shortfall_usd=remaining_due_after_sale,
        remaining_due_usd=remaining_due_after_sale,
        remaining_units=np.maximum(0.0, remaining_units - sold_units),
        remaining_basis_usd=np.maximum(0.0, remaining_basis_usd - basis_usd),
        sold_units=sold_units,
        sold_fraction=sold_fraction,
    )


def _apply_crypto_checking_floor_obligation_funding_policy(
    policy_step: ActorPolicyStep[Policy],
    *,
    due_usd: np.ndarray,
    remaining_due_usd: np.ndarray,
    cash_usd: np.ndarray,
    remaining_quantity: np.ndarray,
    remaining_basis_usd: np.ndarray,
    crypto_unit_price_usd: np.ndarray,
    source_asset_id: str,
) -> CryptoObligationFundingPolicyApplication | None:
    policy = policy_step.policy
    if not isinstance(policy, CheckingFloorSellPublicStockPolicy):
        raise TypeError(f"checking-floor crypto obligation funding handler received {type(policy).__name__}")
    projected_cash_after_obligation = cash_usd - due_usd
    requested_sale = np.where(
        (remaining_due_usd > 0) & (projected_cash_after_obligation < float(policy.floor_usd)),
        # Crypto sale amount is the smaller of the policy sale_amount_usd and the
        # remaining_due (it is wasteful to sell more crypto than needed to clear the
        # obligation when SP500 has already been exhausted). The instruction applier
        # then clamps to current crypto value.
        np.minimum(float(policy.sale_amount_usd), remaining_due_usd),
        0.0,
    )
    instruction = SellAssetInstructionBatch(
        actor_id=policy.actor_id,
        policy_id=policy.policy_id,
        asset_type=AssetType.CRYPTO,
        requested_amount_usd=requested_sale,
        target_cash_floor_usd=float(policy.floor_usd),
        source_asset_id=source_asset_id,
    )
    sale_application = apply_crypto_sale_instruction(
        instruction,
        current_cash_usd=cash_usd,
        remaining_quantity=remaining_quantity,
        remaining_basis_usd=remaining_basis_usd,
        crypto_unit_price_usd=crypto_unit_price_usd,
    )
    if not np.any((requested_sale > 0) | (sale_application.shortfall_usd > 0)):
        return None
    funded_cash = np.minimum(remaining_due_usd, sale_application.sale_usd)
    remaining_due_after_sale = np.maximum(0.0, remaining_due_usd - funded_cash)
    return CryptoObligationFundingPolicyApplication(
        policy_step=policy_step,
        instruction=instruction,
        sale_usd=sale_application.sale_usd,
        basis_usd=sale_application.basis_usd,
        funded_cash_usd=funded_cash,
        shortfall_usd=remaining_due_after_sale,
        remaining_due_usd=remaining_due_after_sale,
        remaining_quantity=sale_application.remaining_quantity,
        remaining_basis_usd=sale_application.remaining_basis_usd,
    )


def _property_cash_flow_arrays(
    scenario: Scenario,
    market_bundle: MarketBundle,
    *,
    location_id: str | None,
    property_value_usd: np.ndarray,
    mortgage_interest_usd: np.ndarray,
    mortgage_principal_usd: np.ndarray,
) -> PropertyCashFlowArrays:
    rollout_count, n_months_plus_one = property_value_usd.shape
    horizon_months = n_months_plus_one - 1
    zeros = np.zeros_like(property_value_usd, dtype="float64")
    mortgage_payment = mortgage_interest_usd + mortgage_principal_usd
    # Mortgage payments are settled through the obligation pipeline in
    # _settle_required_cash_obligations, not directly through net_property_cash_flow_usd.
    # The mortgage_payment_usd is retained on this array for reporting parity, but the
    # operating cash flow stops at carrying cost minus rental income.
    if scenario.property_selection.property_id is None:
        return PropertyCashFlowArrays(
            rollout_count=rollout_count,
            horizon_months=horizon_months,
            numerics=_build_property_cash_flow_frame(
                {
                    "mortgage_payment_usd": mortgage_payment,
                    "property_tax_usd": zeros,
                    "hoa_usd": zeros,
                    "insurance_usd": zeros,
                    "maintenance_usd": zeros,
                    "rental_income_usd": zeros,
                    "rental_management_fee_usd": zeros,
                    "rental_leasing_fee_usd": zeros,
                    "property_carrying_cost_usd": zeros,
                    "net_property_cash_flow_usd": zeros,
                }
            ),
            journal_entries=(),
        )

    property_tax = monthly_property_tax_usd(
        purchase_price_usd=_purchase_price_usd(scenario),
        local_regulation=_required_local_regulation(scenario),
        market_bundle=market_bundle,
    )
    expense_multiplier = market_bundle.inflation_multipliers.copy()
    expense_multiplier[:, 0] = 0.0
    hoa = _scenario_hoa_monthly_usd(scenario) * expense_multiplier
    property_assumptions = scenario.property_assumptions
    insurance = (property_assumptions.insurance_annual_usd / MONTHS_PER_YEAR) * expense_multiplier
    maintenance = property_value_usd * (property_assumptions.maintenance_pct / 100) / MONTHS_PER_YEAR
    maintenance[:, 0] = 0.0
    (rental_income, rental_management_fee, rental_leasing_fee) = _rental_cash_flow_arrays(
        scenario, market_bundle, location_id=location_id
    )
    operating_cash_flow = apply_property_operating_cash_flows(
        actor_id=_primary_owner_actor_id(scenario),
        policy_id=PROPERTY_OPERATING_CASH_FLOW_POLICY_ID,
        property_tax_usd=property_tax,
        hoa_usd=hoa,
        insurance_usd=insurance,
        maintenance_usd=maintenance,
        rental_income_usd=rental_income,
        rental_management_fee_usd=rental_management_fee,
        rental_leasing_fee_usd=rental_leasing_fee,
    )
    return PropertyCashFlowArrays(
        rollout_count=rollout_count,
        horizon_months=horizon_months,
        numerics=_build_property_cash_flow_frame(
            {
                "mortgage_payment_usd": mortgage_payment,
                "property_tax_usd": operating_cash_flow.property_tax_usd,
                "hoa_usd": operating_cash_flow.hoa_usd,
                "insurance_usd": operating_cash_flow.insurance_usd,
                "maintenance_usd": operating_cash_flow.maintenance_usd,
                "rental_income_usd": operating_cash_flow.rental_income_usd,
                "rental_management_fee_usd": operating_cash_flow.rental_management_fee_usd,
                "rental_leasing_fee_usd": operating_cash_flow.rental_leasing_fee_usd,
                "property_carrying_cost_usd": operating_cash_flow.property_carrying_cost_usd,
                "net_property_cash_flow_usd": operating_cash_flow.net_operating_cash_flow_usd,
            }
        ),
        journal_entries=operating_cash_flow.journal_entries,
    )


def _rental_cash_flow_arrays(
    scenario: Scenario, market_bundle: MarketBundle, *, location_id: str | None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    shape = (market_bundle.rollout_count, market_bundle.horizon_months + 1)
    income = np.zeros(shape, dtype="float64")
    management_fee = np.zeros(shape, dtype="float64")
    leasing_fee = np.zeros(shape, dtype="float64")
    rental = scenario.rental_plan
    if rental.rental_mode is RentalMode.NOT_RENTED:
        return income, management_fee, leasing_fee

    active = rental_active_mask(scenario, market_bundle)
    # Active rental requires a property which requires `location_id`; the validator
    # in `augur.core.api` enforces both up front, so `location_id` is non-None here.
    assert location_id is not None
    rent_multiplier = market_bundle.rent_multipliers(location_id)
    if rental.rental_mode is RentalMode.RENT_ROOMS_WHILE_OWNER_LIVES_THERE:
        base_rent = float(rental.rooms_rented) * float(rental.room_rent_monthly_usd or 0.0)
        vacancy_fraction = _pct_fraction(float(rental.room_vacancy_pct), "room_vacancy_pct")
        applies_management = False
    else:
        base_rent = float(rental.monthly_rent_usd or 0.0)
        vacancy_fraction = _pct_fraction(float(rental.vacancy_pct), "vacancy_pct")
        applies_management = True

    # vacancy_pct is a multiplier on rent collected: actual rent = base * (1 - vacancy_pct).
    # Vacancy is the absence of a rent credit, not an expense to record.
    income = base_rent * (1.0 - vacancy_fraction) * rent_multiplier * active[None, :]
    if applies_management:
        management_fee = income * _pct_fraction(float(rental.management_fee_pct), "management_fee_pct")
        leasing_fee = income * _pct_fraction(float(rental.leasing_fee_pct), "leasing_fee_pct") / MONTHS_PER_YEAR
    return income, management_fee, leasing_fee


def _partner_equity_arrays(
    scenario: Scenario,
    market_bundle: MarketBundle,
    *,
    policy_steps: tuple[ActorPolicyStep[Policy], ...],
    owner_initial_equity_usd: float,
    home_equity_usd: np.ndarray,
    mortgage_interest_usd: np.ndarray,
    mortgage_principal_usd: np.ndarray,
    property_tax_usd: np.ndarray,
    hoa_usd: np.ndarray,
    insurance_usd: np.ndarray,
    maintenance_usd: np.ndarray,
) -> PartnerEquityArrays:
    rollout_count, n_months_plus_one = home_equity_usd.shape
    horizon_months = n_months_plus_one - 1
    zeros = np.zeros_like(home_equity_usd, dtype="float64")
    owner_equity_without_partners = float(owner_initial_equity_usd) + np.cumsum(mortgage_principal_usd, axis=1)
    empty = PartnerEquityArrays(
        rollout_count=rollout_count,
        horizon_months=horizon_months,
        numerics=_build_partner_equity_frame(
            {
                "contribution_usd": zeros,
                "contribution_used_usd": zeros,
                "unallocated_excess_usd": zeros,
                "house_costs_usd": zeros,
                "mortgage_payment_usd": zeros,
                "mortgage_interest_usd": zeros,
                "mortgage_principal_usd": zeros,
                "principal_credit_usd": zeros,
                "owner_principal_usd": mortgage_principal_usd,
                "house_cost_share": zeros,
                "partner_equity_ledger_usd": zeros,
                "owner_equity_ledger_usd": owner_equity_without_partners,
                "ownership_pct": zeros,
                "home_equity_claim_usd": zeros,
                "owner_home_equity_claim_usd": home_equity_usd,
            }
        ),
        agreements=(),
        journal_entries=(),
        balance_snapshots=(),
    )
    if not _has_partner(scenario):
        return empty

    month_matrix = np.broadcast_to(market_bundle.month_index[None, :], home_equity_usd.shape)
    mortgage_payment = mortgage_interest_usd + mortgage_principal_usd
    house_uses = (
        mortgage_interest_usd + mortgage_principal_usd + property_tax_usd + hoa_usd + insurance_usd + maintenance_usd
    )
    owner_actor_id = _primary_owner_actor_id(scenario)
    contribution_inputs = []
    remaining_house_uses = house_uses.copy()
    remaining_principal = mortgage_principal_usd.copy()
    for partner_equity_step in policy_steps:
        policy = partner_equity_step.policy
        if not isinstance(policy, PartnerEquityAccrualPolicy):
            continue
        property_id = _partner_equity_property_id(scenario, policy)
        if property_id is None:
            continue
        occupied_months = _partner_occupied_months(scenario, policy, market_bundle.horizon_months)
        active = (month_matrix > 0) & (month_matrix <= occupied_months)
        configured_payment = np.where(
            active, float(policy.base_monthly_payment_usd) * _partner_payment_growth(policy, market_bundle), 0.0
        )
        contribution_instruction = partner_contribution_instruction(
            policy, recipient_actor_id=owner_actor_id, contribution_usd=configured_payment
        )
        principal_available = remaining_principal.copy()
        contribution_application = apply_partner_house_cost_contribution(
            contribution_instruction,
            property_id=property_id,
            house_costs_usd=remaining_house_uses,
            mortgage_principal_usd=principal_available,
        )
        freeze_after_month = _partner_freeze_after_month(
            scenario, policy, occupied_months, market_bundle.horizon_months
        )
        contribution_inputs.append(
            (
                policy,
                partner_equity_step.sequence_index,
                property_id,
                contribution_instruction,
                contribution_application,
                principal_available,
                freeze_after_month,
            )
        )
        remaining_house_uses = np.maximum(0.0, remaining_house_uses - contribution_application.contribution_used_usd)
        remaining_principal = np.maximum(0.0, remaining_principal - contribution_application.principal_credit_usd)
    if not contribution_inputs:
        return empty

    principal_credit = sum(
        (application.principal_credit_usd for _, _, _, _, application, _, _ in contribution_inputs), start=zeros.copy()
    )
    owner_principal = np.maximum(0.0, mortgage_principal_usd - principal_credit)
    owner_equity_ledger = float(owner_initial_equity_usd) + np.cumsum(owner_principal, axis=1)
    total_partner_equity_ledger = sum(
        (np.cumsum(application.principal_credit_usd, axis=1) for _, _, _, _, application, _, _ in contribution_inputs),
        start=zeros.copy(),
    )
    agreements = []
    for (
        policy,
        policy_sequence_index,
        property_id,
        contribution_instruction,
        contribution_application,
        principal_available,
        freeze_after_month,
    ) in contribution_inputs:
        ownership_application = apply_partner_ownership_accrual(
            contribution_instruction,
            property_id=property_id,
            owner_initial_equity_usd=owner_initial_equity_usd,
            home_equity_usd=home_equity_usd,
            owner_principal_usd=owner_principal,
            partner_principal_credit_usd=contribution_application.principal_credit_usd,
            month_index=market_bundle.month_index,
            freeze_after_month=freeze_after_month,
            owner_equity_ledger_usd=owner_equity_ledger,
            total_partner_equity_ledger_usd=total_partner_equity_ledger,
        )
        agreements.append(
            PartnerEquityAgreementArrays(
                policy_sequence_index=policy_sequence_index,
                policy=policy,
                property_id=property_id,
                recipient_actor_id=owner_actor_id,
                rollout_count=rollout_count,
                horizon_months=horizon_months,
                numerics=_build_partner_equity_frame(
                    {
                        "contribution_usd": contribution_instruction.amount_usd,
                        "contribution_used_usd": contribution_application.contribution_used_usd,
                        "unallocated_excess_usd": contribution_application.unallocated_excess_usd,
                        "house_costs_usd": contribution_application.house_costs_usd,
                        "mortgage_payment_usd": mortgage_payment,
                        "mortgage_interest_usd": mortgage_interest_usd,
                        "mortgage_principal_usd": principal_available,
                        "principal_credit_usd": contribution_application.principal_credit_usd,
                        "owner_principal_usd": owner_principal,
                        "house_cost_share": contribution_application.house_cost_share,
                        "partner_equity_ledger_usd": ownership_application.partner_equity_ledger_usd,
                        "owner_equity_ledger_usd": owner_equity_ledger,
                        "ownership_pct": ownership_application.ownership_pct,
                        "home_equity_claim_usd": ownership_application.home_equity_claim_usd,
                        "owner_home_equity_claim_usd": ownership_application.owner_home_equity_claim_usd,
                    }
                ),
                journal_entries=contribution_application.journal_entries + ownership_application.journal_entries,
                balance_snapshots=ownership_application.balance_snapshots,
            )
        )

    contribution_usd = sum((agreement.column("contribution_usd") for agreement in agreements), start=zeros.copy())
    contribution_used = sum((agreement.column("contribution_used_usd") for agreement in agreements), start=zeros.copy())
    unallocated_excess = sum(
        (agreement.column("unallocated_excess_usd") for agreement in agreements), start=zeros.copy()
    )
    home_equity_claim = sum((agreement.column("home_equity_claim_usd") for agreement in agreements), start=zeros.copy())
    owner_home_equity_claim = home_equity_usd - home_equity_claim
    property_id = contribution_inputs[0][2]
    owner_aggregate = apply_partner_ownership_aggregate(
        property_id=property_id,
        owner_actor_id=owner_actor_id,
        owner_principal_usd=owner_principal,
        owner_equity_ledger_usd=owner_equity_ledger,
        owner_home_equity_claim_usd=owner_home_equity_claim,
    )
    positive_home_equity = np.maximum(home_equity_usd, 0.0)
    ownership_pct = np.divide(
        home_equity_claim, positive_home_equity, out=np.zeros_like(home_equity_claim), where=positive_home_equity > 0
    )
    return PartnerEquityArrays(
        rollout_count=rollout_count,
        horizon_months=horizon_months,
        numerics=_build_partner_equity_frame(
            {
                "contribution_usd": contribution_usd,
                "contribution_used_usd": contribution_used,
                "unallocated_excess_usd": unallocated_excess,
                "house_costs_usd": house_uses,
                "mortgage_payment_usd": mortgage_payment,
                "mortgage_interest_usd": mortgage_interest_usd,
                "mortgage_principal_usd": mortgage_principal_usd,
                "principal_credit_usd": principal_credit,
                "owner_principal_usd": owner_principal,
                "house_cost_share": np.divide(
                    contribution_used, house_uses, out=np.zeros_like(contribution_used), where=house_uses > 0
                ),
                "partner_equity_ledger_usd": total_partner_equity_ledger,
                "owner_equity_ledger_usd": owner_equity_ledger,
                "ownership_pct": ownership_pct,
                "home_equity_claim_usd": home_equity_claim,
                "owner_home_equity_claim_usd": owner_home_equity_claim,
            }
        ),
        agreements=tuple(agreements),
        journal_entries=owner_aggregate.journal_entries
        + tuple(entry for agreement in agreements for entry in agreement.journal_entries),
        balance_snapshots=owner_aggregate.balance_snapshots
        + tuple(snapshot for agreement in agreements for snapshot in agreement.balance_snapshots),
    )


def _settle_partner_equity_on_property_sale(
    partner_equity: PartnerEquityArrays, *, sale_month: int | None, property_sale_net_proceeds_usd: np.ndarray
) -> PartnerEquityArrays:
    if sale_month is None or not partner_equity.agreements:
        return partner_equity

    agreements = tuple(
        _settle_partner_equity_agreement_on_property_sale(
            agreement, sale_month=sale_month, property_sale_net_proceeds_usd=property_sale_net_proceeds_usd
        )
        for agreement in partner_equity.agreements
    )
    partner_home_equity_claim_usd = sum(
        (agreement.column("home_equity_claim_usd") for agreement in agreements),
        start=np.zeros_like(partner_equity.column("home_equity_claim_usd")),
    )
    owner_home_equity_claim_usd = partner_equity.column("owner_home_equity_claim_usd").copy()
    sale_net_proceeds = property_sale_net_proceeds_usd[:, sale_month]
    owner_home_equity_claim_usd[:, sale_month:] = (
        sale_net_proceeds[:, None] - partner_home_equity_claim_usd[:, sale_month:]
    )
    owner_balance_snapshots = tuple(
        replace(snapshot, amount_usd=owner_home_equity_claim_usd)
        if snapshot.role is ChartAccountRole.OWNER_HOME_EQUITY_CLAIM
        else snapshot
        for snapshot in partner_equity.balance_snapshots
        if snapshot.role in {ChartAccountRole.OWNER_EQUITY_LEDGER, ChartAccountRole.OWNER_HOME_EQUITY_CLAIM}
    )
    return replace(
        partner_equity.with_numerics(
            home_equity_claim_usd=partner_home_equity_claim_usd, owner_home_equity_claim_usd=owner_home_equity_claim_usd
        ),
        agreements=agreements,
        balance_snapshots=owner_balance_snapshots
        + tuple(snapshot for agreement in agreements for snapshot in agreement.balance_snapshots),
    )


def _settle_partner_equity_agreement_on_property_sale(
    agreement: PartnerEquityAgreementArrays, *, sale_month: int, property_sale_net_proceeds_usd: np.ndarray
) -> PartnerEquityAgreementArrays:
    home_equity_claim_usd = agreement.column("home_equity_claim_usd").copy()
    owner_home_equity_claim_usd = agreement.column("owner_home_equity_claim_usd").copy()
    sale_net_proceeds = property_sale_net_proceeds_usd[:, sale_month]
    partner_sale_claim = np.maximum(0.0, sale_net_proceeds) * agreement.column("ownership_pct")[:, sale_month]
    home_equity_claim_usd[:, sale_month:] = partner_sale_claim[:, None]
    owner_home_equity_claim_usd[:, sale_month:] = sale_net_proceeds[:, None] - partner_sale_claim[:, None]
    balance_snapshots = tuple(
        replace(snapshot, amount_usd=home_equity_claim_usd)
        if snapshot.role is ChartAccountRole.PARTNER_HOME_EQUITY_CLAIM
        else snapshot
        for snapshot in agreement.balance_snapshots
    )
    return replace(
        agreement.with_numerics(
            home_equity_claim_usd=home_equity_claim_usd, owner_home_equity_claim_usd=owner_home_equity_claim_usd
        ),
        balance_snapshots=balance_snapshots,
    )


def _partner_equity_property_id(scenario: Scenario, policy: PartnerEquityAccrualPolicy | None) -> str | None:
    if policy is None:
        return None
    return policy.property_id or scenario.property_selection.property_id


def _partner_payment_growth(policy: PartnerEquityAccrualPolicy, market_bundle: MarketBundle) -> np.ndarray:
    if policy.grow_with_inflation:
        return market_bundle.inflation_multipliers
    growth_pct = policy.payment_growth_annual_pct
    month_index = market_bundle.month_index.astype("float64")
    growth = (1 + growth_pct / 100) ** (month_index / MONTHS_PER_YEAR)
    return np.broadcast_to(growth[None, :], (market_bundle.rollout_count, market_bundle.horizon_months + 1)).copy()


def _partner_occupied_months(scenario: Scenario, policy: PartnerEquityAccrualPolicy, horizon_months: int) -> int:
    if policy.occupied_months is not None:
        occupied_months = int(policy.occupied_months)
    elif scenario.occupancy_plan.occupancy_mode is OccupancyMode.NO_OWNER_OCCUPANCY:
        occupied_months = 0
    elif scenario.occupancy_plan.end_month is not None:
        occupied_months = int(scenario.occupancy_plan.end_month)
    else:
        occupied_months = horizon_months
    return max(0, min(occupied_months, horizon_months))


def _partner_freeze_after_month(
    scenario: Scenario, policy: PartnerEquityAccrualPolicy, occupied_months: int, horizon_months: int
) -> int | None:
    if policy.freeze_ownership_after_month is not None:
        return max(0, min(int(policy.freeze_ownership_after_month), horizon_months))
    if occupied_months < horizon_months:
        return occupied_months
    if scenario.rental_plan.rental_mode is RentalMode.TRANSITION_TO_WHOLE_PROPERTY_RENTAL:
        return occupied_months
    return None


def _property_and_mortgage_arrays(
    scenario: Scenario, market_bundle: MarketBundle, *, location_id: str | None
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rollout_count = market_bundle.rollout_count
    month_count = market_bundle.horizon_months + 1
    property_value = np.zeros((rollout_count, month_count), dtype="float64")
    mortgage_balance = np.zeros((rollout_count, month_count), dtype="float64")
    mortgage_interest = np.zeros((rollout_count, month_count), dtype="float64")
    mortgage_principal = np.zeros((rollout_count, month_count), dtype="float64")
    if scenario.property_selection.property_id is None:
        return property_value, mortgage_balance, mortgage_interest, mortgage_principal

    # Selecting a property requires `location_id` (enforced by the api validator).
    assert location_id is not None
    purchase_price = _purchase_price_usd(scenario)
    property_value = purchase_price * market_bundle.home_value_multipliers(location_id)
    loan_amount, annual_rate_pct, term_months = _loan_terms(scenario, market_bundle, purchase_price)
    if np.allclose(loan_amount, 0):
        return property_value, mortgage_balance, mortgage_interest, mortgage_principal

    mortgage_balance, mortgage_interest, mortgage_principal = _amortization_arrays(
        loan_amount=loan_amount,
        annual_rate_pct=annual_rate_pct,
        term_months=term_months,
        horizon_months=market_bundle.horizon_months,
    )
    return property_value, mortgage_balance, mortgage_interest, mortgage_principal


def _amortization_arrays(
    *, loan_amount: np.ndarray, annual_rate_pct: np.ndarray, term_months: int, horizon_months: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rollout_count = loan_amount.shape[0]
    month_count = horizon_months + 1
    balance = np.zeros((rollout_count, month_count), dtype="float64")
    interest = np.zeros((rollout_count, month_count), dtype="float64")
    principal = np.zeros((rollout_count, month_count), dtype="float64")
    current_balance = loan_amount.astype("float64").copy()
    balance[:, 0] = current_balance
    monthly_rate = annual_rate_pct / 100 / MONTHS_PER_YEAR
    payment = np.zeros_like(loan_amount)
    zero_rate = monthly_rate == 0
    payment[zero_rate] = loan_amount[zero_rate] / term_months
    nonzero_rate = ~zero_rate
    payment[nonzero_rate] = (
        loan_amount[nonzero_rate]
        * monthly_rate[nonzero_rate]
        * (1 + monthly_rate[nonzero_rate]) ** term_months
        / ((1 + monthly_rate[nonzero_rate]) ** term_months - 1)
    )
    for month in range(1, month_count):
        active = (month <= term_months) & (current_balance > 0)
        month_interest = np.where(active, current_balance * monthly_rate, 0.0)
        month_principal = np.where(active, np.minimum(payment - month_interest, current_balance), 0.0)
        current_balance = np.maximum(0.0, current_balance - month_principal)
        interest[:, month] = month_interest
        principal[:, month] = month_principal
        balance[:, month] = current_balance
    return balance, interest, principal


def _loan_terms(
    scenario: Scenario, market_bundle: MarketBundle, purchase_price_usd: float
) -> tuple[np.ndarray, np.ndarray, int]:
    financing = scenario.financing
    rollout_count = market_bundle.rollout_count
    if financing.financing_mode is FinancingMode.CASH:
        return (np.zeros(rollout_count, dtype="float64"), np.zeros(rollout_count, dtype="float64"), 1)
    if financing.down_payment_pct > 100:
        raise ValueError("down_payment_pct must be <= 100")
    if financing.loan_amount_usd is not None:
        loan_amount_value = float(financing.loan_amount_usd)
        if loan_amount_value > purchase_price_usd:
            raise ValueError("loan_amount_usd must not exceed purchase_price_usd")
    else:
        loan_amount_value = purchase_price_usd * (1 - financing.down_payment_pct / 100)
    loan_amount = np.full(rollout_count, loan_amount_value, dtype="float64")
    rate_pct = (
        np.full(rollout_count, float(financing.mortgage_rate_pct), dtype="float64")
        if financing.mortgage_rate_pct is not None
        else market_bundle.mortgage_30y_rate_pct[:, 0]
    )
    term_years = financing.mortgage_term_years
    if term_years is None:
        term_years = 15 if financing.financing_mode is FinancingMode.FIXED_15 else 30
    return loan_amount, rate_pct, int(term_years) * MONTHS_PER_YEAR


def _initial_property_cash_outlay_usd(scenario: Scenario) -> float:
    if scenario.property_selection.property_id is None:
        return 0.0
    purchase_price = _purchase_price_usd(scenario)
    financing = scenario.financing
    if financing.financing_mode is FinancingMode.CASH:
        return purchase_price
    if financing.loan_amount_usd is not None:
        return purchase_price - float(financing.loan_amount_usd)
    return purchase_price * (financing.down_payment_pct / 100)


def _purchase_price_usd(scenario: Scenario) -> float:
    purchase_price = scenario.property_selection.purchase_price_usd
    if purchase_price is None:
        property_id = scenario.property_selection.property_id
        if property_id is None:
            return 0.0
        raise ValueError(f"scenario {scenario.scenario_id!r} selects {property_id} without purchase_price_usd")
    return float(purchase_price)


def _initial_cash_usd(scenario: Scenario) -> float:
    return sum(
        account.balance_usd
        for account in scenario.initial_balance_sheet.accounts
        if account.account_type.value == "checking"
    )


def _single_checking_account_source(scenario: Scenario, *, actor_id: str) -> AccountBalance | None:
    accounts = tuple(
        account
        for account in scenario.initial_balance_sheet.accounts
        if account.account_type is AccountType.CHECKING and account.owner_actor_id == actor_id
    )
    if len(accounts) == 1:
        return accounts[0]
    return None


def _initial_sp500_value_usd(scenario: Scenario) -> float:
    return sum(
        asset.value_usd
        for asset in scenario.initial_balance_sheet.assets
        if isinstance(asset, GenericSp500StockPosition)
    )


def _initial_sp500_cost_basis_usd(scenario: Scenario) -> float:
    total = 0.0
    for asset in scenario.initial_balance_sheet.assets:
        if not isinstance(asset, GenericSp500StockPosition):
            continue
        if asset.cost_basis_usd is None:
            raise ValueError(
                f"GenericSp500StockPosition {asset.asset_id!r} has no cost_basis_usd; the simulator "
                "needs an explicit basis to seed tax lots. Supply it on the portfolio statement "
                "(public_securities[*].cost_basis.amount_usd) or on the constructed position."
            )
        total += asset.cost_basis_usd
    return total


def _single_sp500_asset_source(scenario: Scenario, *, actor_id: str) -> GenericSp500StockPosition | None:
    positions = tuple(
        asset
        for asset in scenario.initial_balance_sheet.assets
        if isinstance(asset, GenericSp500StockPosition) and asset.owner_actor_id == actor_id
    )
    if len(positions) == 1:
        return positions[0]
    return None


def _initial_crypto_value_usd(scenario: Scenario) -> float:
    return sum(
        asset.value_usd for asset in scenario.initial_balance_sheet.assets if isinstance(asset, CryptoAssetPosition)
    )


def _initial_crypto_cost_basis_usd(scenario: Scenario) -> float:
    total = 0.0
    for asset in scenario.initial_balance_sheet.assets:
        if not isinstance(asset, CryptoAssetPosition):
            continue
        if asset.cost_basis_usd is None:
            raise ValueError(
                f"CryptoAssetPosition {asset.asset_id!r} has no cost_basis_usd; the simulator "
                "needs an explicit basis to seed tax lots. Supply it on the portfolio statement "
                "(crypto_holdings[*].cost_basis.amount_usd) or on the constructed position."
            )
        total += asset.cost_basis_usd
    return total


def _crypto_asset_sources(scenario: Scenario, *, actor_id: str) -> tuple[CryptoAssetPosition, ...]:
    return tuple(
        asset
        for asset in scenario.initial_balance_sheet.assets
        if isinstance(asset, CryptoAssetPosition) and asset.owner_actor_id == actor_id
    )


def _crypto_source_holding_id(scenario: Scenario, *, actor_id: str) -> str:
    sources = _crypto_asset_sources(scenario, actor_id=actor_id)
    if len(sources) == 1:
        return sources[0].asset_id
    return "crypto_portfolio"


def _crypto_symbol_routing_keys(scenario: Scenario) -> tuple[str, ...]:
    """Distinct crypto symbols across all positions, in scenario order."""
    seen: list[str] = []
    for asset in scenario.initial_balance_sheet.assets:
        if not isinstance(asset, CryptoAssetPosition):
            continue
        if asset.asset_symbol not in seen:
            seen.append(asset.asset_symbol)
    return tuple(seen)


def _crypto_engine_routing_key(scenario: Scenario) -> str | None:
    """Single symbol key for the aggregated crypto state, or None when no positions.

    The engine aggregates all crypto positions into one state path. With one
    symbol the aggregate rides that symbol's path; with multiple symbols, the
    first symbol is chosen (the per-symbol joint model is a future slice, so
    placeholder paths are currently identical anyway). Scenarios with no
    crypto positions return `None` so the engine skips the lookup entirely.
    """
    keys = _crypto_symbol_routing_keys(scenario)
    return keys[0] if keys else None


def _private_equity_position_value_usd(asset: PrivateEquityPosition, *, current_unit_price_usd: float) -> float:
    """Resolve a PE position's opening mark.

    Honors an explicit `value_usd` (statement mark or manual override) when set;
    otherwise derives `units × current_unit_price_usd`. A position with neither field
    is rejected by `PrivateEquityPosition`'s validator before reaching the engine.
    """
    if asset.value_usd is not None:
        return float(asset.value_usd)
    if current_unit_price_usd <= 0.0:
        raise ValueError(
            f"PrivateEquityPosition {asset.asset_id!r} has units only but the active "
            "MarketBundleMetadata.current_private_equity_price_usd is 0; either supply "
            "value_usd or use a market provider that publishes a PE unit price."
        )
    return float(asset.units or 0.0) * current_unit_price_usd


def _initial_private_equity_value_usd(scenario: Scenario, *, current_unit_price_usd: float) -> float:
    return sum(
        _private_equity_position_value_usd(asset, current_unit_price_usd=current_unit_price_usd)
        for asset in scenario.initial_balance_sheet.assets
        if isinstance(asset, PrivateEquityPosition)
    )


def _initial_private_equity_cost_basis_usd(scenario: Scenario) -> float:
    total = 0.0
    for asset in scenario.initial_balance_sheet.assets:
        if not isinstance(asset, PrivateEquityPosition):
            continue
        if asset.cost_basis_usd is None:
            raise ValueError(
                f"PrivateEquityPosition {asset.asset_id!r} has no cost_basis_usd; the simulator "
                "needs an explicit basis to seed tax lots. Supply it on the portfolio statement "
                "(private_equity_lots[*].cost_basis.amount_usd) or on the constructed position."
            )
        total += asset.cost_basis_usd
    return total


def _initial_private_equity_units(scenario: Scenario) -> float:
    return sum(
        asset.units or 0.0
        for asset in scenario.initial_balance_sheet.assets
        if isinstance(asset, PrivateEquityPosition)
    )


def _effective_pe_liquidity_regime(scenario: Scenario) -> LiquidityEventOnly | PublicMarket | Acquisition:
    """Collapse the per-position liquidity regimes into a single portfolio-level regime.

    The engine aggregates all PE positions into one `private_equity_*` state path
    (units, basis, fraction). All positions must therefore share the same regime
    type (and the same regime parameters), or the engine cannot dispatch correctly.
    With no PE positions, defaults to `LiquidityEventOnly()`.
    """
    positions = tuple(
        asset for asset in scenario.initial_balance_sheet.assets if isinstance(asset, PrivateEquityPosition)
    )
    if not positions:
        return LiquidityEventOnly()
    regimes = {position.liquidity_regime for position in positions}
    if len(regimes) > 1:
        raise ValueError(
            "PrivateEquityPosition entries in initial_balance_sheet.assets must share a single "
            f"liquidity_regime (the engine aggregates them); got {regimes}"
        )
    return positions[0].liquidity_regime


def _private_equity_source_holding_id(scenario: Scenario) -> str:
    positions = tuple(
        asset for asset in scenario.initial_balance_sheet.assets if isinstance(asset, PrivateEquityPosition)
    )
    if len(positions) == 1:
        return positions[0].asset_id
    return "private_equity_portfolio"


def _private_equity_issuer_routing_keys(scenario: Scenario) -> tuple[str, ...]:
    """Distinct per-issuer routing keys for PE positions in scenario order.

    Each key is `position.market_routing_key` (the issuer_id or, when absent,
    the asset_id). Empty tuple when the scenario has no PE positions.
    Multiple lots sharing one issuer collapse to one key.
    """
    seen: list[str] = []
    for asset in scenario.initial_balance_sheet.assets:
        if not isinstance(asset, PrivateEquityPosition):
            continue
        key = asset.market_routing_key
        if key not in seen:
            seen.append(key)
    return tuple(seen)


def _private_equity_issuer_observation_keys(scenario: Scenario) -> tuple[tuple[str, str], ...]:
    """`(observation_source_asset_id, routing_key)` pairs for emission.

    For a single-issuer scenario, returns `((source_holding_id, only_key),)` so the
    emitted observation's `source_asset_id` matches the legacy aggregated value
    (e.g. `"pe"` for one position, `"private_equity_portfolio"` for the merged path).
    For multi-issuer scenarios, returns one pair per issuer with the issuer key
    as `source_asset_id` so downstream consumers can split by issuer.
    """
    keys = _private_equity_issuer_routing_keys(scenario)
    if len(keys) <= 1:
        return tuple((_private_equity_source_holding_id(scenario), key) for key in keys)
    return tuple((key, key) for key in keys)


def _has_partner(scenario: Scenario) -> bool:
    return any(actor.role is ActorRole.EQUITY_BUILDING_OCCUPANT for actor in scenario.actors)


def _primary_owner_actor_id(scenario: Scenario) -> str:
    for actor in scenario.actors:
        if actor.role is ActorRole.PRIMARY_OWNER:
            return actor.actor_id
    return "owner"


def _scenario_hoa_monthly_usd(scenario: Scenario) -> float:
    for event in scenario.events:
        if isinstance(event, PropertyPurchaseEvent) and event.hoa_monthly_usd is not None:
            return float(event.hoa_monthly_usd)
    return 0.0


def _settle_partner_contribution_obligations(
    *,
    scenario: Scenario,
    market_bundle: MarketBundle,
    month_index: np.ndarray,
    rollout_count: int,
    month_count: int,
    policy_steps: tuple[ActorPolicyStep[Policy], ...],
    partner_equity: PartnerEquityArrays,
    owner_actor_id: str,
    obligations: event_streams.StreamFrameBuilder,
    funding_decisions: event_streams.StreamFrameBuilder,
    accounting: AccountingTraceBuilder,
    sp500_sale_action_records: list[Sp500SaleActionRecord],
    crypto_sale_action_records: list[CryptoSaleActionRecord],
) -> None:
    """Settle each PartnerEquityAccrualPolicy's monthly contribution as an obligation
    on the contributing actor.

    Each agreement runs an independent obligation pass keyed on the contributing
    actor's CHECKING_CASH balance. The settlement journal entry debits the
    recipient owner's CHECKING_CASH and credits the contributor's CHECKING_CASH
    (a balanced cross-actor transfer). The contributing actor's failure to fund
    flips the rollout to FAILED via FailureEvent.

    The owner's cash trajectory is driven by `partner_equity.column("contribution_used_usd")`
    added in the main month loop; on the happy path the obligation pipeline pays
    the configured amount in full and the JE cash debit matches the owner-side
    receipt.
    """
    if not partner_equity.agreements:
        return
    for agreement in partner_equity.agreements:
        contributing_actor_id = agreement.policy.actor_id
        partner_initial_cash = _partner_initial_funding_cash_usd(
            scenario,
            actor_id=contributing_actor_id,
            configured_contribution_total_usd=float(np.max(np.sum(agreement.column("contribution_usd"), axis=1))),
        )
        partner_cash = np.full((rollout_count, month_count), partner_initial_cash, dtype="float64")
        # The contribution_usd matrix carries the configured monthly payment per
        # month (zero where no payment is configured). Auxiliary state matrices
        # (sp500 / crypto units / basis / sale tracking, checking-floor
        # shortfall) are per-partner because they describe the partner's funding
        # path, not the owner's.
        partner_sp500_units_by_month = np.zeros((rollout_count, month_count), dtype="float64")
        partner_sp500_basis_by_month = np.zeros((rollout_count, month_count), dtype="float64")
        partner_sp500_value_usd = np.zeros((rollout_count, month_count), dtype="float64")
        partner_crypto_quantity_by_month = np.zeros((rollout_count, month_count), dtype="float64")
        partner_crypto_basis_by_month = np.zeros((rollout_count, month_count), dtype="float64")
        partner_crypto_value_usd = np.zeros((rollout_count, month_count), dtype="float64")
        partner_crypto_sale_usd = np.zeros((rollout_count, month_count), dtype="float64")
        partner_crypto_sale_basis_usd = np.zeros((rollout_count, month_count), dtype="float64")
        partner_checking_floor_shortfall = np.zeros((rollout_count, month_count), dtype="float64")
        obligation_kind = _PartnerContributionObligationKind(
            property_id=agreement.property_id, recipient_actor_id=owner_actor_id
        )
        _settle_required_cash_obligations(
            scenario=scenario,
            market_bundle=market_bundle,
            month_index=month_index,
            policy_steps=policy_steps,
            obligation_amount_usd=agreement.column("contribution_usd"),
            obligation_kind=obligation_kind,
            creditor_id=owner_actor_id,
            source_policy_id=agreement.policy.policy_id,
            cash_usd=partner_cash,
            generic_sp500_value_usd=partner_sp500_value_usd,
            remaining_sp500_units_by_month=partner_sp500_units_by_month,
            remaining_sp500_basis_by_month=partner_sp500_basis_by_month,
            crypto_value_usd=partner_crypto_value_usd,
            remaining_crypto_quantity_by_month=partner_crypto_quantity_by_month,
            remaining_crypto_basis_by_month=partner_crypto_basis_by_month,
            crypto_sale_usd=partner_crypto_sale_usd,
            crypto_sale_basis_usd=partner_crypto_sale_basis_usd,
            checking_floor_shortfall_usd=partner_checking_floor_shortfall,
            obligations=obligations,
            funding_decisions=funding_decisions,
            accounting=accounting,
            sp500_sale_action_records=sp500_sale_action_records,
            crypto_sale_action_records=crypto_sale_action_records,
            actor_id=contributing_actor_id,
        )


def _actor_initial_checking_cash_usd(scenario: Scenario, *, actor_id: str) -> float:
    return sum(
        account.balance_usd
        for account in scenario.initial_balance_sheet.accounts
        if account.account_type is AccountType.CHECKING and account.owner_actor_id == actor_id
    )


def _partner_initial_funding_cash_usd(
    scenario: Scenario, *, actor_id: str, configured_contribution_total_usd: float
) -> float:
    """Resolve the contributing partner's starting cash for obligation settlement.

    A partner with an explicitly configured CHECKING account funds the
    obligation strictly from that balance — running out fails the rollout. A
    partner with no configured account is assumed to fund off-trace (their
    external income is not modeled), so default to the total configured
    contribution amount: enough to pay every scheduled month in full. Tests
    that want to exercise the failure path should configure a partner account
    with an insufficient balance.
    """
    explicit_cash = _actor_initial_checking_cash_usd(scenario, actor_id=actor_id)
    if any(
        account.account_type is AccountType.CHECKING and account.owner_actor_id == actor_id
        for account in scenario.initial_balance_sheet.accounts
    ):
        return explicit_cash
    return configured_contribution_total_usd


def _outside_rent_obligation_due_usd(scenario: Scenario, *, rollout_count: int, month_index: np.ndarray) -> np.ndarray:
    """Build a (rollout, month) matrix of outside-rent dues from the occupancy plan.

    Each month in [start_month, end_month or last in-horizon month] contributes
    `outside_rent_monthly_usd` when `occupancy_mode is OWNER_RENTS_ELSEWHERE`.
    Zero rent or any other occupancy mode returns an all-zero matrix (callers can
    short-circuit on `np.any(... > 0)`). Every rollout pays the same flat rent —
    outside rent is a deterministic occupancy choice today, not a stochastic input.
    Inflation indexing is a deliberate follow-up.
    """
    matrix = np.zeros((rollout_count, month_index.size), dtype="float64")
    plan = scenario.occupancy_plan
    if (
        plan.occupancy_mode is not OccupancyMode.OWNER_RENTS_ELSEWHERE
        or plan.outside_rent_monthly_usd <= 0
        or month_index.size == 0
    ):
        return matrix
    end_inclusive = plan.end_month if plan.end_month is not None else int(month_index.max())
    span_mask = (month_index >= int(plan.start_month)) & (month_index <= int(end_inclusive))
    matrix[:, span_mask] = float(plan.outside_rent_monthly_usd)
    return matrix


def _special_assessment_obligation_due_usd(
    scenario: Scenario, *, rollout_count: int, month_index: np.ndarray
) -> np.ndarray:
    """Build a (rollout, month) matrix of special-assessment dues from scenario events.

    Each `SpecialAssessmentEvent` in `scenario.events` contributes its `amount_usd` to
    the matrix column corresponding to its `month_index`. Events whose `month_index`
    falls outside the simulation horizon are clamped to the last in-horizon month so
    the cash impact lands within the simulation (mirrors the year-end tax fallback).
    Each row of the matrix is identical across rollouts: a special assessment is a
    deterministic scheduled event, not a per-rollout stochastic input.
    """
    matrix = np.zeros((rollout_count, month_index.size), dtype="float64")
    if month_index.size == 0:
        return matrix
    last_position = month_index.size - 1
    for event in scenario.events:
        if not isinstance(event, SpecialAssessmentEvent):
            continue
        event_positions = np.nonzero(month_index == int(event.month_index))[0]
        position = int(event_positions[0]) if event_positions.size > 0 else last_position
        matrix[:, position] = matrix[:, position] + float(event.amount_usd)
    return matrix


def _required_local_regulation(scenario: Scenario) -> LocalRegulation:
    if scenario.property_selection.local_regulation is None:
        raise ValueError(
            f"scenario {scenario.scenario_id!r} has real estate but no resolved local_regulation; "
            "the caller must populate `property_selection.local_regulation` (e.g. via the "
            "ScenarioEngine or `scenario_with_location_tax_defaults`) before the engine runs"
        )
    return scenario.property_selection.local_regulation


def _pct_fraction(value: float, name: str) -> float:
    if value < 0 or value > 100:
        raise ValueError(f"{name} must be in [0, 100]")
    return value / 100


_FAN_PERCENTILES: tuple[int, ...] = (
    1,
    2,
    5,
    10,
    15,
    20,
    25,
    30,
    35,
    40,
    45,
    50,
    55,
    60,
    65,
    70,
    75,
    80,
    85,
    90,
    95,
    98,
    99,
)
_FAN_QUANTILE_LEVELS: np.ndarray = np.array(_FAN_PERCENTILES, dtype="float64") / 100.0


def _fan_columns(values: np.ndarray) -> ColumnarTable:
    matrix = np.asarray(values, dtype="float64")
    if matrix.ndim != 2:
        raise ValueError("fan values must be shaped (rollout, month)")
    month_index = np.arange(matrix.shape[1], dtype="int64")
    # `np.quantile` here instead of `np.nanpercentile` — fan-metric data is
    # always defined (no NaN sources) so the nan-safety dispatch into
    # per-column apply_along_axis isn't earning its keep. Direct quantile
    # is a vectorized native call that profiled as ~10× faster on the bench
    # (materialize 0.9s -> ~0.4s post-change). If the engine ever starts
    # emitting NaN into a fan metric, that's a bug to catch — the
    # propagated NaN here will make it visible.
    percentile_values = np.quantile(matrix, _FAN_QUANTILE_LEVELS, axis=0, method="linear")
    columns: dict[str, list[Any]] = {
        "month_index": month_index.tolist(),
        "year": (month_index / MONTHS_PER_YEAR).tolist(),
    }
    for index, percentile in enumerate(_FAN_PERCENTILES):
        columns[f"p{percentile:02d}"] = percentile_values[index].tolist()
    return ColumnarTable(row_count=int(matrix.shape[1]), columns=columns)
