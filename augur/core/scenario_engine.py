from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, cast

import numpy as np

from augur.core.accounting import (
    AccountingCause,
    AccountingCauseType,
    ChartAccount,
    ChartAccountRole,
    JournalEntryType,
    LiabilityState,
    LiabilityType,
    LotAssetClass,
    LotDisposition,
    PostingSide,
    SimulationBalanceSnapshot,
    SimulationJournalEntry,
    SimulationPosting,
    TaxLot,
    chart_account_id,
    chart_account_type_for_role,
    validate_accounting_trace,
)
from augur.core.annual_tax import AnnualSaleTaxAllocation, annual_sale_tax_allocation
from augur.core.local_regulation import LocalRegulation, local_regulation_for_location
from augur.core.market_bundle import MarketBundle
from augur.core.policy_runtime import (
    ActorPolicyStep,
    BalanceSnapshotBatch,
    JournalEntryBatch,
    PostingBatch,
    PrivateEquitySaleApplication,
    PrivateEquitySaleInstructionBatch,
    PrivateEquitySaleOpportunityBatch,
    SellAssetInstructionBatch,
    actor_policy_programs,
    actor_policy_steps,
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
    AccountingDetailType,
    AccountType,
    ActorRole,
    AssetType,
    CheckingFloorSellPublicStockPolicy,
    FailureEventType,
    FinancingMode,
    FixedAmountPrivateEquitySaleRule,
    FundingDecisionType,
    FundingSourceType,
    GenericSp500StockPosition,
    LiquidNetWorthFloorPrivateEquitySaleRule,
    MarketPathObservation,
    MonthlySpendDecision,
    MonthlySpendPolicy,
    ObligationStatus,
    ObligationType,
    OccupancyMode,
    PartnerContributionDecision,
    PartnerEquityAccrualPolicy,
    Policy,
    PrivateEquityPosition,
    PrivateEquitySaleDecision,
    PrivateEquitySaleDecisionReason,
    PrivateEquitySaleOpportunityObservation,
    PrivateEquitySalePolicy,
    PrivateEquitySaleRule,
    PropertyPurchaseEvent,
    PropertySaleBasisGainDetail,
    RentalMode,
    ReportMetric,
    RolloutStatus,
    RolloutStatusType,
    Scenario,
    SellPrivateEquityAction,
    SellPublicStockDecision,
    SellSp500Action,
    SettlementStatus,
    SettlePropertySaleAction,
    SimulationAccountingDetail,
    SimulationAction,
    SimulationFailureEvent,
    SimulationFundingDecision,
    SimulationMarketObservation,
    SimulationObligation,
    SimulationPolicyDecision,
    SimulationSettlementResult,
    TaxPaymentAllocationDetail,
)
from augur.core.schemas import ColumnarTable

MONTHS_PER_YEAR = 12
MORTGAGE_SERVICING_POLICY_ID = "mortgage_servicing"
PROPERTY_OPERATING_CASH_FLOW_POLICY_ID = "property_operating_cash_flow"
PROPERTY_SALE_SETTLEMENT_POLICY_ID = "property_sale_settlement"
ANNUAL_TAX_ACCOUNTING_POLICY_ID = "annual_tax_accounting"


@dataclass(frozen=True)
class ScenarioRunArrays:
    scenario_id: str
    scenario_label: str
    month_index: np.ndarray
    cash_usd: np.ndarray
    generic_sp500_value_usd: np.ndarray
    generic_sp500_sale_usd: np.ndarray
    generic_sp500_sale_basis_usd: np.ndarray
    generic_sp500_sale_gain_usd: np.ndarray
    generic_sp500_sale_tax_usd: np.ndarray
    checking_floor_action_usd: np.ndarray
    checking_floor_shortfall_usd: np.ndarray
    private_equity_value_usd: np.ndarray
    private_equity_sale_opportunity_value_usd: np.ndarray
    private_equity_sale_usd: np.ndarray
    private_equity_sale_basis_usd: np.ndarray
    private_equity_sale_tax_usd: np.ndarray
    rental_income_tax_usd: np.ndarray
    federal_income_tax_usd: np.ndarray
    california_income_tax_usd: np.ndarray
    total_income_tax_usd: np.ndarray
    private_equity_sale_opportunity_event: np.ndarray
    property_value_usd: np.ndarray
    mortgage_balance_usd: np.ndarray
    mortgage_interest_usd: np.ndarray
    mortgage_principal_usd: np.ndarray
    mortgage_payment_usd: np.ndarray
    property_tax_usd: np.ndarray
    hoa_usd: np.ndarray
    insurance_usd: np.ndarray
    maintenance_usd: np.ndarray
    rental_income_usd: np.ndarray
    rental_management_fee_usd: np.ndarray
    rental_leasing_fee_usd: np.ndarray
    property_carrying_cost_usd: np.ndarray
    net_property_cash_flow_usd: np.ndarray
    purchase_closing_cost_usd: np.ndarray
    sale_closing_cost_usd: np.ndarray
    property_depreciation_usd: np.ndarray
    cumulative_property_depreciation_usd: np.ndarray
    property_sale_gross_usd: np.ndarray
    property_sale_net_proceeds_usd: np.ndarray
    property_sale_tax_usd: np.ndarray
    property_sale_debt_payoff_usd: np.ndarray
    property_sale_adjusted_basis_usd: np.ndarray
    realized_property_gain_usd: np.ndarray
    property_sale_capital_gain_usd: np.ndarray
    property_sale_capital_gain_exclusion_usd: np.ndarray
    taxable_property_capital_gain_usd: np.ndarray
    taxable_property_gain_usd: np.ndarray
    depreciation_recapture_usd: np.ndarray
    net_property_sale_cash_flow_usd: np.ndarray
    home_equity_usd: np.ndarray
    owner_home_equity_claim_usd: np.ndarray
    partner_home_equity_claim_usd: np.ndarray
    partner_contribution_usd: np.ndarray
    partner_contribution_used_usd: np.ndarray
    partner_unallocated_excess_usd: np.ndarray
    partner_house_costs_usd: np.ndarray
    partner_principal_credit_usd: np.ndarray
    owner_principal_credit_usd: np.ndarray
    partner_house_cost_share: np.ndarray
    partner_equity_ledger_usd: np.ndarray
    owner_equity_ledger_usd: np.ndarray
    partner_ownership_pct: np.ndarray
    liquid_net_worth_usd: np.ndarray
    net_worth_usd: np.ndarray
    partner_present: np.ndarray
    monthly_spend_usd: np.ndarray
    actions: tuple[SimulationAction, ...]
    policy_decisions: tuple[SimulationPolicyDecision, ...]
    market_observations: tuple[SimulationMarketObservation, ...]
    chart_accounts: tuple[ChartAccount, ...]
    journal_entries: tuple[SimulationJournalEntry, ...]
    postings: tuple[SimulationPosting, ...]
    balance_snapshots: tuple[SimulationBalanceSnapshot, ...]
    tax_lots: tuple[TaxLot, ...]
    lot_dispositions: tuple[LotDisposition, ...]
    liabilities: tuple[LiabilityState, ...]
    accounting_details: tuple[SimulationAccountingDetail, ...]
    obligations: tuple[SimulationObligation, ...]
    funding_decisions: tuple[SimulationFundingDecision, ...]
    settlement_results: tuple[SimulationSettlementResult, ...]
    failure_events: tuple[SimulationFailureEvent, ...]

    @property
    def rollout_count(self) -> int:
        return int(self.cash_usd.shape[0])

    @property
    def horizon_months(self) -> int:
        return int(self.cash_usd.shape[1] - 1)

    def rollout_statuses(self) -> tuple[RolloutStatus, ...]:
        statuses: list[RolloutStatus] = []
        for rollout_index in range(self.rollout_count):
            cash_path = self.cash_usd[rollout_index, :]
            negative_positions = np.nonzero(cash_path < 0)[0]
            min_cash_usd = float(np.min(cash_path))
            failed_events = tuple(event for event in self.failure_events if event.rollout_index == rollout_index)
            if failed_events:
                first_failed_month = min(event.month_index for event in failed_events)
                statuses.append(
                    RolloutStatus(
                        rollout_index=rollout_index,
                        status=RolloutStatusType.FAILED,
                        min_cash_usd=min_cash_usd,
                        first_negative_cash_month_index=(
                            int(self.month_index[int(negative_positions[0])]) if negative_positions.size else None
                        ),
                        first_failed_obligation_month_index=first_failed_month,
                        failed_obligation_count=len(failed_events),
                        unpaid_obligation_usd=sum(event.unpaid_amount_usd for event in failed_events),
                    )
                )
                continue
            if negative_positions.size == 0:
                statuses.append(
                    RolloutStatus(
                        rollout_index=rollout_index, status=RolloutStatusType.ACTIVE, min_cash_usd=min_cash_usd
                    )
                )
                continue
            first_negative_position = int(negative_positions[0])
            statuses.append(
                RolloutStatus(
                    rollout_index=rollout_index,
                    status=RolloutStatusType.CASH_NEGATIVE,
                    min_cash_usd=min_cash_usd,
                    first_negative_cash_month_index=int(self.month_index[first_negative_position]),
                )
            )
        return tuple(statuses)

    def monthly_columns(self) -> ColumnarTable:
        row_count = self.rollout_count * (self.horizon_months + 1)
        rollout_index = np.repeat(np.arange(self.rollout_count, dtype="int64"), self.horizon_months + 1)
        month_index = np.tile(self.month_index, self.rollout_count)
        scenario_ids = [self.scenario_id] * row_count
        scenario_labels = [self.scenario_label] * row_count
        return ColumnarTable(
            row_count=row_count,
            columns={
                "scenario_id": scenario_ids,
                "scenario_label": scenario_labels,
                "rollout_index": rollout_index.tolist(),
                "month_index": month_index.tolist(),
                **_monthly_metric_columns(self),
            },
        )

    def terminal_columns(self) -> ColumnarTable:
        final = -1
        return ColumnarTable(
            row_count=self.rollout_count,
            columns={
                "scenario_id": [self.scenario_id] * self.rollout_count,
                "scenario_label": [self.scenario_label] * self.rollout_count,
                "rollout_index": np.arange(self.rollout_count, dtype="int64").tolist(),
                "month_index": [int(self.month_index[final])] * self.rollout_count,
                "final_cash_usd": self.cash_usd[:, final].tolist(),
                "final_generic_sp500_value_usd": self.generic_sp500_value_usd[:, final].tolist(),
                "total_generic_sp500_sale_usd": np.sum(self.generic_sp500_sale_usd, axis=1).tolist(),
                "total_generic_sp500_sale_basis_usd": np.sum(self.generic_sp500_sale_basis_usd, axis=1).tolist(),
                "total_generic_sp500_sale_gain_usd": np.sum(self.generic_sp500_sale_gain_usd, axis=1).tolist(),
                "total_generic_sp500_sale_tax_usd": np.sum(self.generic_sp500_sale_tax_usd, axis=1).tolist(),
                "final_checking_floor_shortfall_usd": self.checking_floor_shortfall_usd[:, final].tolist(),
                "final_private_equity_value_usd": self.private_equity_value_usd[:, final].tolist(),
                "final_private_equity_sale_opportunity_value_usd": (
                    self.private_equity_sale_opportunity_value_usd[:, final].tolist()
                ),
                "total_private_equity_sale_usd": np.sum(self.private_equity_sale_usd, axis=1).tolist(),
                "total_private_equity_sale_basis_usd": np.sum(self.private_equity_sale_basis_usd, axis=1).tolist(),
                "total_private_equity_sale_tax_usd": np.sum(self.private_equity_sale_tax_usd, axis=1).tolist(),
                "total_federal_income_tax_usd": np.sum(self.federal_income_tax_usd, axis=1).tolist(),
                "total_california_income_tax_usd": np.sum(self.california_income_tax_usd, axis=1).tolist(),
                "total_income_tax_usd": np.sum(self.total_income_tax_usd, axis=1).tolist(),
                "final_property_value_usd": self.property_value_usd[:, final].tolist(),
                "final_mortgage_balance_usd": self.mortgage_balance_usd[:, final].tolist(),
                "final_home_equity_usd": self.home_equity_usd[:, final].tolist(),
                "final_owner_home_equity_claim_usd": self.owner_home_equity_claim_usd[:, final].tolist(),
                "final_partner_home_equity_claim_usd": self.partner_home_equity_claim_usd[:, final].tolist(),
                "final_partner_ownership_pct": self.partner_ownership_pct[:, final].tolist(),
                "total_partner_contribution_used_usd": np.sum(self.partner_contribution_used_usd, axis=1).tolist(),
                "total_partner_principal_credit_usd": np.sum(self.partner_principal_credit_usd, axis=1).tolist(),
                "total_owner_principal_credit_usd": np.sum(self.owner_principal_credit_usd, axis=1).tolist(),
                "final_partner_equity_ledger_usd": self.partner_equity_ledger_usd[:, final].tolist(),
                "final_owner_equity_ledger_usd": self.owner_equity_ledger_usd[:, final].tolist(),
                "total_rental_income_usd": np.sum(self.rental_income_usd, axis=1).tolist(),
                "total_property_carrying_cost_usd": np.sum(self.property_carrying_cost_usd, axis=1).tolist(),
                "total_net_property_cash_flow_usd": np.sum(self.net_property_cash_flow_usd, axis=1).tolist(),
                "total_purchase_closing_cost_usd": np.sum(self.purchase_closing_cost_usd, axis=1).tolist(),
                "total_sale_closing_cost_usd": np.sum(self.sale_closing_cost_usd, axis=1).tolist(),
                "total_property_depreciation_usd": np.sum(self.property_depreciation_usd, axis=1).tolist(),
                "final_cumulative_property_depreciation_usd": self.cumulative_property_depreciation_usd[
                    :, final
                ].tolist(),
                "total_property_sale_gross_usd": np.sum(self.property_sale_gross_usd, axis=1).tolist(),
                "total_property_sale_net_proceeds_usd": np.sum(self.property_sale_net_proceeds_usd, axis=1).tolist(),
                "total_property_sale_tax_usd": np.sum(self.property_sale_tax_usd, axis=1).tolist(),
                "total_property_sale_debt_payoff_usd": np.sum(self.property_sale_debt_payoff_usd, axis=1).tolist(),
                "total_property_sale_adjusted_basis_usd": np.sum(
                    self.property_sale_adjusted_basis_usd, axis=1
                ).tolist(),
                "total_realized_property_gain_usd": np.sum(self.realized_property_gain_usd, axis=1).tolist(),
                "total_property_sale_capital_gain_usd": np.sum(self.property_sale_capital_gain_usd, axis=1).tolist(),
                "total_property_sale_capital_gain_exclusion_usd": np.sum(
                    self.property_sale_capital_gain_exclusion_usd, axis=1
                ).tolist(),
                "total_taxable_property_capital_gain_usd": np.sum(
                    self.taxable_property_capital_gain_usd, axis=1
                ).tolist(),
                "total_taxable_property_gain_usd": np.sum(self.taxable_property_gain_usd, axis=1).tolist(),
                "total_depreciation_recapture_usd": np.sum(self.depreciation_recapture_usd, axis=1).tolist(),
                "total_net_property_sale_cash_flow_usd": np.sum(self.net_property_sale_cash_flow_usd, axis=1).tolist(),
                "final_liquid_net_worth_usd": self.liquid_net_worth_usd[:, final].tolist(),
                "final_net_worth_usd": self.net_worth_usd[:, final].tolist(),
            },
        )

    def metric_fan_columns(self) -> dict[str, ColumnarTable]:
        return {
            "cash_usd": _fan_columns(self.cash_usd),
            "net_worth_usd": _fan_columns(self.net_worth_usd),
            "liquid_net_worth_usd": _fan_columns(self.liquid_net_worth_usd),
            "generic_sp500_value_usd": _fan_columns(self.generic_sp500_value_usd),
            "checking_floor_shortfall_usd": _fan_columns(self.checking_floor_shortfall_usd),
            "property_value_usd": _fan_columns(self.property_value_usd),
            "home_equity_usd": _fan_columns(self.home_equity_usd),
            "owner_home_equity_claim_usd": _fan_columns(self.owner_home_equity_claim_usd),
            "partner_home_equity_claim_usd": _fan_columns(self.partner_home_equity_claim_usd),
            "partner_principal_credit_usd": _fan_columns(self.partner_principal_credit_usd),
            "partner_equity_ledger_usd": _fan_columns(self.partner_equity_ledger_usd),
            "owner_equity_ledger_usd": _fan_columns(self.owner_equity_ledger_usd),
            "partner_ownership_pct": _fan_columns(self.partner_ownership_pct),
            "mortgage_balance_usd": _fan_columns(self.mortgage_balance_usd),
            "rental_income_usd": _fan_columns(self.rental_income_usd),
            "net_property_cash_flow_usd": _fan_columns(self.net_property_cash_flow_usd),
            "property_sale_net_proceeds_usd": _fan_columns(self.property_sale_net_proceeds_usd),
            "net_property_sale_cash_flow_usd": _fan_columns(self.net_property_sale_cash_flow_usd),
            "private_equity_value_usd": _fan_columns(self.private_equity_value_usd),
            "private_equity_sale_opportunity_value_usd": _fan_columns(self.private_equity_sale_opportunity_value_usd),
        }


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
    MonthlyColumnSpec(ReportMetric.LIQUID_NET_WORTH_USD, MonthlyColumnSource.REPORT_PROJECTION, "cash + public stock"),
    MonthlyColumnSpec(
        ReportMetric.NET_WORTH_USD, MonthlyColumnSource.REPORT_PROJECTION, "cash + public stock + private equity + home"
    ),
    MonthlyColumnSpec(ReportMetric.PARTNER_PRESENT, MonthlyColumnSource.TRAJECTORY_STATE, "scenario actor state"),
    MonthlyColumnSpec(ReportMetric.MONTHLY_SPEND_USD, MonthlyColumnSource.LEDGER_ENTRY, "cash/monthly_spend"),
)


def monthly_column_specs() -> tuple[MonthlyColumnSpec, ...]:
    return _MONTHLY_COLUMN_SPECS


def available_report_metrics() -> tuple[ReportMetric, ...]:
    return tuple(ReportMetric)


def report_metric_array(arrays: ScenarioRunArrays, metric: ReportMetric) -> np.ndarray:
    return _report_metric_arrays(arrays)[metric]


def _monthly_metric_columns(arrays: ScenarioRunArrays) -> dict[str, list[Any]]:
    columns: dict[str, list[Any]] = {}
    for spec in _MONTHLY_COLUMN_SPECS:
        values = report_metric_array(arrays, spec.metric)
        columns[spec.metric.value] = _flat_bool(values) if values.dtype == np.bool_ else _flat(values)
    return columns


def _report_metric_arrays(arrays: ScenarioRunArrays) -> dict[ReportMetric, np.ndarray]:
    return {
        ReportMetric.MONTH_INDEX: arrays.month_index,
        ReportMetric.CASH_USD: arrays.cash_usd,
        ReportMetric.GENERIC_SP500_VALUE_USD: arrays.generic_sp500_value_usd,
        ReportMetric.GENERIC_SP500_SALE_USD: arrays.generic_sp500_sale_usd,
        ReportMetric.GENERIC_SP500_SALE_BASIS_USD: arrays.generic_sp500_sale_basis_usd,
        ReportMetric.GENERIC_SP500_SALE_GAIN_USD: arrays.generic_sp500_sale_gain_usd,
        ReportMetric.GENERIC_SP500_SALE_TAX_USD: arrays.generic_sp500_sale_tax_usd,
        ReportMetric.CHECKING_FLOOR_ACTION_USD: arrays.checking_floor_action_usd,
        ReportMetric.CHECKING_FLOOR_SHORTFALL_USD: arrays.checking_floor_shortfall_usd,
        ReportMetric.PRIVATE_EQUITY_VALUE_USD: arrays.private_equity_value_usd,
        ReportMetric.PRIVATE_EQUITY_SALE_OPPORTUNITY_VALUE_USD: arrays.private_equity_sale_opportunity_value_usd,
        ReportMetric.PRIVATE_EQUITY_SALE_USD: arrays.private_equity_sale_usd,
        ReportMetric.PRIVATE_EQUITY_SALE_BASIS_USD: arrays.private_equity_sale_basis_usd,
        ReportMetric.PRIVATE_EQUITY_SALE_TAX_USD: arrays.private_equity_sale_tax_usd,
        ReportMetric.RENTAL_INCOME_TAX_USD: arrays.rental_income_tax_usd,
        ReportMetric.FEDERAL_INCOME_TAX_USD: arrays.federal_income_tax_usd,
        ReportMetric.CALIFORNIA_INCOME_TAX_USD: arrays.california_income_tax_usd,
        ReportMetric.TOTAL_INCOME_TAX_USD: arrays.total_income_tax_usd,
        ReportMetric.PRIVATE_EQUITY_SALE_OPPORTUNITY_EVENT: arrays.private_equity_sale_opportunity_event,
        ReportMetric.PROPERTY_VALUE_USD: arrays.property_value_usd,
        ReportMetric.MORTGAGE_BALANCE_USD: arrays.mortgage_balance_usd,
        ReportMetric.MORTGAGE_INTEREST_USD: arrays.mortgage_interest_usd,
        ReportMetric.MORTGAGE_PRINCIPAL_USD: arrays.mortgage_principal_usd,
        ReportMetric.MORTGAGE_PAYMENT_USD: arrays.mortgage_payment_usd,
        ReportMetric.PROPERTY_TAX_USD: arrays.property_tax_usd,
        ReportMetric.HOA_USD: arrays.hoa_usd,
        ReportMetric.INSURANCE_USD: arrays.insurance_usd,
        ReportMetric.MAINTENANCE_USD: arrays.maintenance_usd,
        ReportMetric.RENTAL_INCOME_USD: arrays.rental_income_usd,
        ReportMetric.RENTAL_MANAGEMENT_FEE_USD: arrays.rental_management_fee_usd,
        ReportMetric.RENTAL_LEASING_FEE_USD: arrays.rental_leasing_fee_usd,
        ReportMetric.PROPERTY_CARRYING_COST_USD: arrays.property_carrying_cost_usd,
        ReportMetric.NET_PROPERTY_CASH_FLOW_USD: arrays.net_property_cash_flow_usd,
        ReportMetric.PURCHASE_CLOSING_COST_USD: arrays.purchase_closing_cost_usd,
        ReportMetric.SALE_CLOSING_COST_USD: arrays.sale_closing_cost_usd,
        ReportMetric.PROPERTY_DEPRECIATION_USD: arrays.property_depreciation_usd,
        ReportMetric.CUMULATIVE_PROPERTY_DEPRECIATION_USD: arrays.cumulative_property_depreciation_usd,
        ReportMetric.PROPERTY_SALE_GROSS_USD: arrays.property_sale_gross_usd,
        ReportMetric.PROPERTY_SALE_NET_PROCEEDS_USD: arrays.property_sale_net_proceeds_usd,
        ReportMetric.PROPERTY_SALE_TAX_USD: arrays.property_sale_tax_usd,
        ReportMetric.PROPERTY_SALE_DEBT_PAYOFF_USD: arrays.property_sale_debt_payoff_usd,
        ReportMetric.PROPERTY_SALE_ADJUSTED_BASIS_USD: arrays.property_sale_adjusted_basis_usd,
        ReportMetric.REALIZED_PROPERTY_GAIN_USD: arrays.realized_property_gain_usd,
        ReportMetric.PROPERTY_SALE_CAPITAL_GAIN_USD: arrays.property_sale_capital_gain_usd,
        ReportMetric.PROPERTY_SALE_CAPITAL_GAIN_EXCLUSION_USD: arrays.property_sale_capital_gain_exclusion_usd,
        ReportMetric.TAXABLE_PROPERTY_CAPITAL_GAIN_USD: arrays.taxable_property_capital_gain_usd,
        ReportMetric.TAXABLE_PROPERTY_GAIN_USD: arrays.taxable_property_gain_usd,
        ReportMetric.DEPRECIATION_RECAPTURE_USD: arrays.depreciation_recapture_usd,
        ReportMetric.NET_PROPERTY_SALE_CASH_FLOW_USD: arrays.net_property_sale_cash_flow_usd,
        ReportMetric.HOME_EQUITY_USD: arrays.home_equity_usd,
        ReportMetric.OWNER_HOME_EQUITY_CLAIM_USD: arrays.owner_home_equity_claim_usd,
        ReportMetric.PARTNER_HOME_EQUITY_CLAIM_USD: arrays.partner_home_equity_claim_usd,
        ReportMetric.PARTNER_CONTRIBUTION_USD: arrays.partner_contribution_usd,
        ReportMetric.PARTNER_CONTRIBUTION_USED_USD: arrays.partner_contribution_used_usd,
        ReportMetric.PARTNER_UNALLOCATED_EXCESS_USD: arrays.partner_unallocated_excess_usd,
        ReportMetric.PARTNER_HOUSE_COSTS_USD: arrays.partner_house_costs_usd,
        ReportMetric.PARTNER_PRINCIPAL_CREDIT_USD: arrays.partner_principal_credit_usd,
        ReportMetric.OWNER_PRINCIPAL_CREDIT_USD: arrays.owner_principal_credit_usd,
        ReportMetric.PARTNER_HOUSE_COST_SHARE: arrays.partner_house_cost_share,
        ReportMetric.PARTNER_EQUITY_LEDGER_USD: arrays.partner_equity_ledger_usd,
        ReportMetric.OWNER_EQUITY_LEDGER_USD: arrays.owner_equity_ledger_usd,
        ReportMetric.PARTNER_OWNERSHIP_PCT: arrays.partner_ownership_pct,
        ReportMetric.LIQUID_NET_WORTH_USD: arrays.liquid_net_worth_usd,
        ReportMetric.NET_WORTH_USD: arrays.net_worth_usd,
        ReportMetric.PARTNER_PRESENT: arrays.partner_present,
        ReportMetric.MONTHLY_SPEND_USD: arrays.monthly_spend_usd,
    }


@dataclass(frozen=True)
class PropertyCashFlowArrays:
    mortgage_payment_usd: np.ndarray
    property_tax_usd: np.ndarray
    hoa_usd: np.ndarray
    insurance_usd: np.ndarray
    maintenance_usd: np.ndarray
    rental_income_usd: np.ndarray
    rental_management_fee_usd: np.ndarray
    rental_leasing_fee_usd: np.ndarray
    property_carrying_cost_usd: np.ndarray
    net_property_cash_flow_usd: np.ndarray
    journal_entries: tuple[JournalEntryBatch, ...]


@dataclass(frozen=True)
class PartnerEquityAgreementArrays:
    policy_sequence_index: int
    policy: PartnerEquityAccrualPolicy
    property_id: str
    recipient_actor_id: str
    contribution_usd: np.ndarray
    contribution_used_usd: np.ndarray
    unallocated_excess_usd: np.ndarray
    house_costs_usd: np.ndarray
    mortgage_payment_usd: np.ndarray
    mortgage_interest_usd: np.ndarray
    mortgage_principal_usd: np.ndarray
    principal_credit_usd: np.ndarray
    owner_principal_usd: np.ndarray
    house_cost_share: np.ndarray
    partner_equity_ledger_usd: np.ndarray
    owner_equity_ledger_usd: np.ndarray
    ownership_pct: np.ndarray
    home_equity_claim_usd: np.ndarray
    owner_home_equity_claim_usd: np.ndarray
    journal_entries: tuple[JournalEntryBatch, ...]
    balance_snapshots: tuple[BalanceSnapshotBatch, ...]


@dataclass(frozen=True)
class PartnerEquityArrays:
    contribution_usd: np.ndarray
    contribution_used_usd: np.ndarray
    unallocated_excess_usd: np.ndarray
    house_costs_usd: np.ndarray
    mortgage_payment_usd: np.ndarray
    mortgage_interest_usd: np.ndarray
    mortgage_principal_usd: np.ndarray
    principal_credit_usd: np.ndarray
    owner_principal_usd: np.ndarray
    house_cost_share: np.ndarray
    partner_equity_ledger_usd: np.ndarray
    owner_equity_ledger_usd: np.ndarray
    ownership_pct: np.ndarray
    home_equity_claim_usd: np.ndarray
    owner_home_equity_claim_usd: np.ndarray
    agreements: tuple[PartnerEquityAgreementArrays, ...]
    journal_entries: tuple[JournalEntryBatch, ...]
    balance_snapshots: tuple[BalanceSnapshotBatch, ...]


@dataclass(frozen=True)
class Sp500SaleActionRecord:
    month_position: int
    month_index: int
    policy: Policy
    cause_id_prefix: str
    amount_usd: np.ndarray
    basis_usd: np.ndarray
    shortfall_usd: np.ndarray


@dataclass(frozen=True)
class PrivateEquitySaleActionRecord:
    month_position: int
    month_index: int
    instruction: PrivateEquitySaleInstructionBatch
    sale_application: PrivateEquitySaleApplication


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


class AccountingTraceBuilder:
    def __init__(self) -> None:
        self.chart_accounts_by_id: dict[str, ChartAccount] = {}
        self.journal_entries: list[SimulationJournalEntry] = []
        self.postings: list[SimulationPosting] = []
        self.balance_snapshots: list[SimulationBalanceSnapshot] = []

    def record_entry(
        self, *, month_index: int | np.ndarray, entry: JournalEntryBatch, amount_multiplier: np.ndarray | None = None
    ) -> None:
        month_values, posting_amounts = _normalized_posting_amounts(
            month_index=month_index, postings=entry.postings, amount_multiplier=amount_multiplier
        )
        if not posting_amounts:
            return
        active = np.zeros(posting_amounts[0][1].shape, dtype=np.bool_)
        for _, amount_usd in posting_amounts:
            active |= amount_usd > 0
        rollout_indexes, month_positions = np.nonzero(active)
        for rollout_index, month_position in zip(rollout_indexes.tolist(), month_positions.tolist(), strict=True):
            month = int(month_values[month_position])
            journal_entry_id = _trace_row_id(entry.cause_id_prefix, rollout_index=rollout_index, month_index=month)
            obligation_id = (
                _trace_row_id(entry.obligation_id_prefix, rollout_index=rollout_index, month_index=month)
                if entry.obligation_id_prefix is not None
                else None
            )
            self.journal_entries.append(
                SimulationJournalEntry(
                    journal_entry_id=journal_entry_id,
                    rollout_index=rollout_index,
                    month_index=month,
                    journal_entry_type=entry.journal_entry_type,
                    actor_id=entry.actor_id,
                    policy_id=entry.policy_id,
                    event_id=entry.event_id,
                    obligation_id=obligation_id,
                    description=entry.description,
                    cause=AccountingCause(
                        cause_type=entry.cause_type,
                        cause_id=journal_entry_id,
                        policy_id=entry.policy_id,
                        event_id=entry.event_id,
                        obligation_id=obligation_id,
                    ),
                )
            )
            for posting_index, (posting, amount_matrix) in enumerate(posting_amounts):
                posting_amount_usd = float(amount_matrix[rollout_index, month_position])
                if posting_amount_usd <= 0:
                    continue
                chart_account = self._chart_account(posting)
                self.postings.append(
                    SimulationPosting(
                        posting_id=f"{journal_entry_id}:posting:{posting_index}:{posting.side.value}",
                        journal_entry_id=journal_entry_id,
                        rollout_index=rollout_index,
                        month_index=month,
                        chart_account_id=chart_account.chart_account_id,
                        side=posting.side,
                        amount_usd=posting_amount_usd,
                        liability_id=posting.liability_id,
                    )
                )

    def record_snapshot(self, *, month_index: np.ndarray, snapshot: BalanceSnapshotBatch) -> None:
        amount_usd = np.asarray(snapshot.amount_usd, dtype="float64")
        if amount_usd.ndim != 2:
            raise ValueError("balance snapshot amount_usd must be rollout/month shaped")
        chart_account = self._chart_account(
            PostingBatch(
                role=snapshot.role,
                side=PostingSide.DEBIT,
                amount_usd=amount_usd,
                actor_id=snapshot.actor_id,
                source_account_id=snapshot.source_account_id,
                source_asset_id=snapshot.source_asset_id,
                liability_id=snapshot.liability_id,
                property_id=snapshot.property_id,
                counterparty_actor_id=snapshot.counterparty_actor_id,
            )
        )
        rollout_indexes, month_positions = np.nonzero(amount_usd != 0)
        for rollout_index, month_position in zip(rollout_indexes.tolist(), month_positions.tolist(), strict=True):
            self.balance_snapshots.append(
                SimulationBalanceSnapshot(
                    rollout_index=rollout_index,
                    month_index=int(month_index[month_position]),
                    chart_account_id=chart_account.chart_account_id,
                    balance_usd=float(amount_usd[rollout_index, month_position]),
                )
            )

    def _chart_account(self, posting: PostingBatch) -> ChartAccount:
        account_id = chart_account_id(
            posting.role,
            actor_id=posting.actor_id,
            source_account_id=posting.source_account_id,
            source_asset_id=posting.source_asset_id,
            liability_id=posting.liability_id,
            property_id=posting.property_id,
            counterparty_actor_id=posting.counterparty_actor_id,
        )
        account = self.chart_accounts_by_id.get(account_id)
        if account is None:
            account = ChartAccount(
                chart_account_id=account_id,
                account_type=chart_account_type_for_role(posting.role),
                role=posting.role,
                actor_id=posting.actor_id,
                source_account_id=posting.source_account_id,
                source_asset_id=posting.source_asset_id,
                liability_id=posting.liability_id,
                property_id=posting.property_id,
                counterparty_actor_id=posting.counterparty_actor_id,
            )
            self.chart_accounts_by_id[account_id] = account
        return account

    def validate(self) -> None:
        validate_accounting_trace(
            chart_accounts=tuple(self.chart_accounts_by_id.values()),
            journal_entries=tuple(self.journal_entries),
            postings=tuple(self.postings),
        )


def _normalized_posting_amounts(
    *, month_index: int | np.ndarray, postings: tuple[PostingBatch, ...], amount_multiplier: np.ndarray | None = None
) -> tuple[np.ndarray, list[tuple[PostingBatch, np.ndarray]]]:
    month_values = np.asarray([month_index], dtype="int64") if isinstance(month_index, int) else month_index
    normalized: list[tuple[PostingBatch, np.ndarray]] = []
    for posting in postings:
        amount_usd = np.asarray(posting.amount_usd, dtype="float64")
        if amount_usd.ndim == 1:
            amount_usd = amount_usd[:, None]
        if amount_usd.ndim != 2:
            raise ValueError("posting amount_usd must be rollout or rollout/month shaped")
        if amount_multiplier is not None:
            multiplier = np.asarray(amount_multiplier, dtype="float64")
            if multiplier.ndim == 1:
                multiplier = multiplier[:, None]
            amount_usd = amount_usd * multiplier
        if amount_usd.shape[1] != len(month_values):
            raise ValueError(
                f"posting month dimension {amount_usd.shape[1]} does not match month_index length {len(month_values)}"
            )
        normalized.append((posting, amount_usd))
    return month_values, normalized


def _trace_row_id(prefix: str, *, rollout_index: int, month_index: int) -> str:
    return f"{prefix}:rollout:{rollout_index}:month:{month_index}"


def _record_opening_accounting_state(
    accounting: AccountingTraceBuilder,
    tax_lots: list[TaxLot],
    liabilities: list[LiabilityState],
    *,
    scenario: Scenario,
    rollout_count: int,
    initial_cash_usd: float,
    initial_sp500_value_usd: float,
    initial_sp500_basis_usd: float,
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
        accounting.record_entry(
            month_index=month_zero,
            entry=JournalEntryBatch(
                journal_entry_type=JournalEntryType.OPENING_BALANCE,
                cause_type=AccountingCauseType.OPENING_BALANCE,
                cause_id_prefix="opening:checking_cash",
                actor_id=actor_id,
                description="opening checking cash",
                postings=(
                    PostingBatch(
                        role=ChartAccountRole.CHECKING_CASH,
                        side=PostingSide.DEBIT,
                        amount_usd=amount,
                        actor_id=actor_id,
                        source_account_id=cash_source.account_id if cash_source is not None else None,
                    ),
                    PostingBatch(
                        role=ChartAccountRole.OPENING_EQUITY,
                        side=PostingSide.CREDIT,
                        amount_usd=amount,
                        actor_id=actor_id,
                    ),
                ),
            ),
        )

    if initial_sp500_value_usd > 0:
        amount = np.full(rollout_count, initial_sp500_value_usd, dtype="float64")
        lot_id = _tax_lot_id(LotAssetClass.PUBLIC_SECURITY, sp500_source.asset_id if sp500_source else "portfolio")
        accounting.record_entry(
            month_index=month_zero,
            entry=JournalEntryBatch(
                journal_entry_type=JournalEntryType.OPENING_BALANCE,
                cause_type=AccountingCauseType.OPENING_BALANCE,
                cause_id_prefix="opening:public_security",
                actor_id=actor_id,
                description="opening public security holdings",
                postings=(
                    PostingBatch(
                        role=ChartAccountRole.PUBLIC_SECURITY,
                        side=PostingSide.DEBIT,
                        amount_usd=amount,
                        actor_id=actor_id,
                        source_asset_id=sp500_source.asset_id if sp500_source is not None else None,
                    ),
                    PostingBatch(
                        role=ChartAccountRole.OPENING_EQUITY,
                        side=PostingSide.CREDIT,
                        amount_usd=amount,
                        actor_id=actor_id,
                    ),
                ),
            ),
        )
        tax_lots.append(
            TaxLot(
                lot_id=lot_id,
                asset_class=LotAssetClass.PUBLIC_SECURITY,
                owner_actor_id=actor_id,
                source_asset_id=sp500_source.asset_id if sp500_source is not None else None,
                cost_basis_usd=max(0.0, float(initial_sp500_basis_usd)),
                acquisition_month_index=0,
            )
        )

    if initial_private_equity_value_usd > 0:
        amount = np.full(rollout_count, initial_private_equity_value_usd, dtype="float64")
        lot_id = _tax_lot_id(LotAssetClass.PRIVATE_EQUITY, private_equity_source_id)
        accounting.record_entry(
            month_index=month_zero,
            entry=JournalEntryBatch(
                journal_entry_type=JournalEntryType.OPENING_BALANCE,
                cause_type=AccountingCauseType.OPENING_BALANCE,
                cause_id_prefix="opening:private_equity",
                actor_id=actor_id,
                description="opening private equity holdings",
                postings=(
                    PostingBatch(
                        role=ChartAccountRole.PRIVATE_EQUITY,
                        side=PostingSide.DEBIT,
                        amount_usd=amount,
                        actor_id=actor_id,
                        source_asset_id=private_equity_source_id,
                    ),
                    PostingBatch(
                        role=ChartAccountRole.OPENING_EQUITY,
                        side=PostingSide.CREDIT,
                        amount_usd=amount,
                        actor_id=actor_id,
                    ),
                ),
            ),
        )
        tax_lots.append(
            TaxLot(
                lot_id=lot_id,
                asset_class=LotAssetClass.PRIVATE_EQUITY,
                owner_actor_id=actor_id,
                source_asset_id=private_equity_source_id,
                cost_basis_usd=max(0.0, float(initial_private_equity_basis_usd)),
                quantity=_initial_private_equity_units(scenario) or None,
                acquisition_month_index=0,
            )
        )

    property_id = scenario.property_selection.property_id
    if property_id is None or purchase_price_usd <= 0:
        return

    purchase = np.full(rollout_count, purchase_price_usd, dtype="float64")
    closing = np.asarray(purchase_closing_cost_usd, dtype="float64")
    mortgage = np.asarray(mortgage_balance_usd, dtype="float64")
    cash_outlay = np.full(rollout_count, down_payment_usd, dtype="float64") + closing
    liability_id = _mortgage_liability_id(property_id)
    accounting.record_entry(
        month_index=month_zero,
        entry=JournalEntryBatch(
            journal_entry_type=JournalEntryType.OPENING_BALANCE,
            cause_type=AccountingCauseType.OPENING_BALANCE,
            cause_id_prefix=f"opening:property:{property_id}",
            actor_id=actor_id,
            description="opening property purchase",
            postings=(
                PostingBatch(
                    role=ChartAccountRole.PROPERTY,
                    side=PostingSide.DEBIT,
                    amount_usd=purchase,
                    actor_id=actor_id,
                    property_id=property_id,
                ),
                PostingBatch(
                    role=ChartAccountRole.PROPERTY_PURCHASE_CLOSING_EXPENSE,
                    side=PostingSide.DEBIT,
                    amount_usd=closing,
                    actor_id=actor_id,
                    property_id=property_id,
                ),
                PostingBatch(
                    role=ChartAccountRole.CHECKING_CASH,
                    side=PostingSide.CREDIT,
                    amount_usd=cash_outlay,
                    actor_id=actor_id,
                    source_account_id=cash_source.account_id if cash_source is not None else None,
                ),
                PostingBatch(
                    role=ChartAccountRole.MORTGAGE_PAYABLE,
                    side=PostingSide.CREDIT,
                    amount_usd=mortgage,
                    actor_id=actor_id,
                    liability_id=liability_id,
                    property_id=property_id,
                ),
            ),
        ),
    )
    tax_lots.append(
        TaxLot(
            lot_id=_tax_lot_id(LotAssetClass.PROPERTY, property_id),
            asset_class=LotAssetClass.PROPERTY,
            owner_actor_id=actor_id,
            property_id=property_id,
            cost_basis_usd=max(0.0, purchase_price_usd + float(np.mean(closing))),
            acquisition_month_index=0,
        )
    )
    initial_mortgage = float(np.max(mortgage))
    if initial_mortgage > 0:
        liabilities.append(
            LiabilityState(
                liability_id=liability_id,
                liability_type=LiabilityType.MORTGAGE,
                actor_id=actor_id,
                creditor_id="mortgage_lender",
                property_id=property_id,
                balance_usd=initial_mortgage,
            )
        )


def _record_state_balance_snapshots(
    accounting: AccountingTraceBuilder,
    *,
    scenario: Scenario,
    month_index: np.ndarray,
    cash_usd: np.ndarray,
    generic_sp500_value_usd: np.ndarray,
    private_equity_value_usd: np.ndarray,
    property_value_usd: np.ndarray,
    mortgage_balance_usd: np.ndarray,
    property_balance_mask: np.ndarray,
) -> None:
    actor_id = _primary_owner_actor_id(scenario)
    cash_source = _single_checking_account_source(scenario, actor_id=actor_id)
    sp500_source = _single_sp500_asset_source(scenario, actor_id=actor_id)
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
    initial_private_equity = _initial_private_equity_value_usd(scenario)
    initial_private_equity_basis = _initial_private_equity_cost_basis_usd(scenario)
    initial_private_equity_units = _initial_private_equity_units(scenario)
    private_equity_source_holding_id = _private_equity_source_holding_id(scenario)
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
    net_property_cash_flow = property_cash_flow.net_property_cash_flow_usd * property_live_mask
    home_equity = property_value - mortgage_balance
    partner_equity = _partner_equity_arrays(
        scenario,
        market_bundle,
        policy_steps=policy_steps,
        owner_initial_equity_usd=down_payment,
        home_equity_usd=home_equity,
        mortgage_interest_usd=mortgage_interest,
        mortgage_principal_usd=mortgage_principal,
        property_tax_usd=property_cash_flow.property_tax_usd * property_live_mask,
        hoa_usd=property_cash_flow.hoa_usd * property_live_mask,
        insurance_usd=property_cash_flow.insurance_usd * property_live_mask,
        maintenance_usd=property_cash_flow.maintenance_usd * property_live_mask,
    )
    generic_sp500_value = np.zeros((rollout_count, month_count), dtype="float64")
    generic_sp500_sale_gain = np.zeros((rollout_count, month_count), dtype="float64")
    generic_sp500_sale_tax = np.zeros((rollout_count, month_count), dtype="float64")
    checking_floor_shortfall = np.zeros((rollout_count, month_count), dtype="float64")
    private_equity_value = np.zeros((rollout_count, month_count), dtype="float64")
    private_equity_sale_opportunity_value = np.zeros((rollout_count, month_count), dtype="float64")
    private_equity_sale_taxable_gain = np.zeros((rollout_count, month_count), dtype="float64")
    private_equity_sale_tax = np.zeros((rollout_count, month_count), dtype="float64")
    cash = np.zeros((rollout_count, month_count), dtype="float64")
    remaining_sp500_units_by_month = np.zeros((rollout_count, month_count), dtype="float64")
    remaining_sp500_basis_by_month = np.zeros((rollout_count, month_count), dtype="float64")
    private_equity_sale_opportunity_event = market_bundle.private_equity_sale_opportunity_mask.copy()
    remaining_private_equity_fraction = np.ones(rollout_count, dtype="float64")
    remaining_sp500_units = np.divide(
        initial_sp500,
        market_bundle.generic_sp500_multipliers[:, 0],
        out=np.zeros(rollout_count, dtype="float64"),
        where=market_bundle.generic_sp500_multipliers[:, 0] > 0,
    )
    remaining_sp500_basis = np.full(rollout_count, initial_sp500_basis, dtype="float64")
    remaining_private_equity_basis = np.full(rollout_count, initial_private_equity_basis, dtype="float64")
    remaining_private_equity_units = np.full(rollout_count, initial_private_equity_units, dtype="float64")
    current_cash = (
        np.full(rollout_count, initial_cash - down_payment, dtype="float64")
        - disposition.purchase_closing_cost_usd[:, 0]
    )
    actions: list[SimulationAction] = []
    policy_decisions: list[SimulationPolicyDecision] = []
    market_observations: list[SimulationMarketObservation] = list(_market_path_observations(scenario, market_bundle))
    accounting = AccountingTraceBuilder()
    tax_lots: list[TaxLot] = []
    lot_dispositions: list[LotDisposition] = []
    liabilities: list[LiabilityState] = []
    accounting_details: list[SimulationAccountingDetail] = []
    obligations: list[SimulationObligation] = []
    funding_decisions: list[SimulationFundingDecision] = []
    settlement_results: list[SimulationSettlementResult] = []
    failure_events: list[SimulationFailureEvent] = []
    sp500_sale_action_records: list[Sp500SaleActionRecord] = []
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
        initial_private_equity_value_usd=initial_private_equity,
        initial_private_equity_basis_usd=initial_private_equity_basis,
        purchase_price_usd=purchase_price,
        down_payment_usd=down_payment,
        purchase_closing_cost_usd=disposition.purchase_closing_cost_usd[:, 0],
        mortgage_balance_usd=mortgage_balance[:, 0],
    )

    for month in range(month_count):
        current_cash = current_cash + disposition.net_property_sale_cash_flow_usd[:, month]
        if month > 0:
            current_cash = (
                current_cash + net_property_cash_flow[:, month] + partner_equity.contribution_used_usd[:, month]
            )

        private_equity_value_before_sale = (
            initial_private_equity
            * remaining_private_equity_fraction
            * market_bundle.private_equity_value_multipliers[:, month]
        )
        market_opportunity = private_equity_sale_opportunity(
            sale_opportunity_mask=market_bundle.private_equity_sale_opportunity_mask[:, month],
            private_equity_value_before_sale_usd=private_equity_value_before_sale,
            path_set_id=market_bundle.metadata.path_set_id,
            month_index=int(month_index[month]),
            source_holding_id=private_equity_source_holding_id,
        )
        _record_private_equity_sale_opportunity_observations(
            market_observations,
            month_index=int(month_index[month]),
            source_asset_id=private_equity_source_holding_id,
            opportunity=market_opportunity,
        )
        market_sale_opportunity_value = market_opportunity.sale_opportunity_value_usd
        private_equity_sale_month = np.zeros(rollout_count, dtype="float64")
        private_equity_sale_taxable_gain_month = np.zeros(rollout_count, dtype="float64")
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
                    initial_private_equity
                    * remaining_private_equity_fraction
                    * market_bundle.private_equity_value_multipliers[:, month]
                )
                current_opportunity = private_equity_sale_opportunity(
                    sale_opportunity_mask=market_bundle.private_equity_sale_opportunity_mask[:, month],
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
                        amount_usd=sp500_sale_application.sale_usd,
                        basis_usd=sp500_sale_application.basis_usd,
                        shortfall_usd=sp500_sale_application.shortfall_usd,
                    )
                )
        sp500_value_after_sale = remaining_sp500_units * sp500_multiplier

        cash[:, month] = current_cash
        generic_sp500_value[:, month] = sp500_value_after_sale
        remaining_sp500_units_by_month[:, month] = remaining_sp500_units
        remaining_sp500_basis_by_month[:, month] = remaining_sp500_basis
        generic_sp500_sale_gain[:, month] = sp500_sale - sp500_basis
        checking_floor_shortfall[:, month] = sp500_shortfall
        private_equity_sale_taxable_gain[:, month] = private_equity_sale_taxable_gain_month
        private_equity_sale_tax[:, month] = private_equity_sale_tax_month
        private_equity_sale_opportunity_value[:, month] = np.maximum(
            0.0, market_sale_opportunity_value - private_equity_sale_month
        )
        private_equity_value[:, month] = private_equity_value_before_sale - private_equity_sale_month

    property_tax_for_tax_allocation = property_cash_flow.property_tax_usd * property_live_mask
    net_rental_taxable_income = (
        property_cash_flow.rental_income_usd
        - property_cash_flow.rental_management_fee_usd
        - property_cash_flow.rental_leasing_fee_usd
        - property_cash_flow.property_tax_usd
        - property_cash_flow.hoa_usd
        - property_cash_flow.insurance_usd
        - property_cash_flow.maintenance_usd
        - mortgage_interest
        - disposition.property_depreciation_usd
    ) * property_live_mask
    annual_tax = annual_sale_tax_allocation(
        scenario.tax_profile,
        month_index=month_index,
        property_depreciation_recapture_usd=disposition.depreciation_recapture_usd,
        taxable_property_capital_gain_usd=disposition.taxable_property_capital_gain_usd,
        generic_sp500_sale_gain_usd=generic_sp500_sale_gain,
        private_equity_sale_taxable_gain_usd=private_equity_sale_taxable_gain,
        property_tax_usd=property_tax_for_tax_allocation,
        mortgage_interest_usd=mortgage_interest,
        mortgage_principal_balance_usd=mortgage_balance * property_live_mask,
        net_rental_taxable_income_usd=net_rental_taxable_income,
    )
    generic_sp500_sale_tax = annual_tax.generic_sp500_sale_tax_usd
    private_equity_sale_tax = annual_tax.private_equity_sale_tax_usd
    property_sale_tax = annual_tax.property_sale_tax_usd
    # Property sale net proceeds reflect the cash actually received at sale. Tax is
    # accrued in the source month but settled at year-end via the annual-tax
    # obligation path, so it does not reduce the sale-event cash inflow.
    property_sale_net_proceeds = (
        disposition.property_sale_gross_usd
        - disposition.sale_closing_cost_usd
        - disposition.property_sale_debt_payoff_usd
    )
    partner_equity = _settle_partner_equity_on_property_sale(
        partner_equity, sale_month=disposition.sale_month, property_sale_net_proceeds_usd=property_sale_net_proceeds
    )
    obligation_tax_due = _year_end_tax_obligation_due_usd(
        month_index=month_index, source_month_tax_due_usd=annual_tax.total_income_tax_usd
    )
    _settle_required_cash_obligations(
        scenario=scenario,
        market_bundle=market_bundle,
        month_index=month_index,
        policy_steps=policy_steps,
        obligation_amount_usd=obligation_tax_due,
        obligation_kind=_AnnualTaxObligationKind(),
        creditor_id="tax_authority",
        source_policy_id=ANNUAL_TAX_ACCOUNTING_POLICY_ID,
        cash_usd=cash,
        generic_sp500_value_usd=generic_sp500_value,
        remaining_sp500_units_by_month=remaining_sp500_units_by_month,
        remaining_sp500_basis_by_month=remaining_sp500_basis_by_month,
        checking_floor_shortfall_usd=checking_floor_shortfall,
        obligations=obligations,
        funding_decisions=funding_decisions,
        settlement_results=settlement_results,
        failure_events=failure_events,
        accounting=accounting,
        sp500_sale_action_records=sp500_sale_action_records,
    )

    partner_present = np.full((rollout_count, month_count), _has_partner(scenario), dtype=np.bool_)
    owner_home_equity_claim = partner_equity.owner_home_equity_claim_usd
    if disposition.sale_month is None:
        owner_home_equity_claim_for_net_worth = owner_home_equity_claim
    else:
        unsold_mask = (month_index < disposition.sale_month).astype("float64")
        unsold_mask = np.broadcast_to(unsold_mask[None, :], (rollout_count, month_count))
        owner_home_equity_claim_for_net_worth = owner_home_equity_claim * unsold_mask
    liquid_net_worth = cash + generic_sp500_value
    net_worth = cash + generic_sp500_value + private_equity_value + owner_home_equity_claim_for_net_worth
    _record_property_sale_actions(
        actions,
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
    _record_property_sale_accounting_details(accounting_details, scenario=scenario, disposition=disposition)
    _record_tax_payment_allocation_details(
        accounting_details,
        scenario=scenario,
        month_index=month_index,
        annual_tax=annual_tax,
        property_depreciation_recapture_usd=disposition.depreciation_recapture_usd,
        taxable_property_capital_gain_usd=disposition.taxable_property_capital_gain_usd,
        generic_sp500_sale_gain_usd=generic_sp500_sale_gain,
        private_equity_sale_taxable_gain_usd=private_equity_sale_taxable_gain,
        net_rental_taxable_income_usd=net_rental_taxable_income,
    )
    for sp500_sale_action_record in sp500_sale_action_records:
        source_tax = _tax_share_for_sale_action(
            source_tax_usd=generic_sp500_sale_tax[:, sp500_sale_action_record.month_position],
            action_taxable_income_usd=np.maximum(
                0.0, sp500_sale_action_record.amount_usd - sp500_sale_action_record.basis_usd
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
            amount_usd=sp500_sale_action_record.amount_usd,
            basis_usd=sp500_sale_action_record.basis_usd,
            tax_usd=source_tax,
        )
        _record_sp500_sale_actions(
            actions,
            month_index=sp500_sale_action_record.month_index,
            policy=sp500_sale_action_record.policy,
            amount_usd=sp500_sale_action_record.amount_usd,
            basis_usd=sp500_sale_action_record.basis_usd,
            tax_usd=source_tax,
            shortfall_usd=sp500_sale_action_record.shortfall_usd,
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
        _record_private_equity_sale_actions(
            actions,
            month_index=private_equity_sale_action_record.month_index,
            instruction=private_equity_sale_action_record.instruction,
            sale_application=private_equity_sale_action_record.sale_application,
            estimated_tax_usd=source_tax,
        )
    _record_partner_contribution_decisions(policy_decisions, month_index=month_index, partner_equity=partner_equity)
    _record_partner_agreement_accounting_detail(accounting, month_index=month_index, partner_equity=partner_equity)
    mortgage_payment_due = property_cash_flow.mortgage_payment_usd * property_live_mask
    if scenario.property_selection.property_id is not None:
        _settle_required_cash_obligations(
            scenario=scenario,
            market_bundle=market_bundle,
            month_index=month_index,
            policy_steps=policy_steps,
            obligation_amount_usd=mortgage_payment_due,
            obligation_kind=_MortgageObligationKind(
                interest_usd=mortgage_interest,
                principal_usd=mortgage_principal,
                property_id=scenario.property_selection.property_id,
            ),
            creditor_id="mortgage_lender",
            source_policy_id=MORTGAGE_SERVICING_POLICY_ID,
            cash_usd=cash,
            generic_sp500_value_usd=generic_sp500_value,
            remaining_sp500_units_by_month=remaining_sp500_units_by_month,
            remaining_sp500_basis_by_month=remaining_sp500_basis_by_month,
            checking_floor_shortfall_usd=checking_floor_shortfall,
            obligations=obligations,
            funding_decisions=funding_decisions,
            settlement_results=settlement_results,
            failure_events=failure_events,
            accounting=accounting,
            sp500_sale_action_records=sp500_sale_action_records,
        )
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
        private_equity_value_usd=private_equity_value,
        property_value_usd=property_value,
        mortgage_balance_usd=mortgage_balance,
        property_balance_mask=property_balance_mask,
    )
    accounting.validate()
    monthly_spend_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.MONTHLY_LIVING_EXPENSE,
        side=PostingSide.DEBIT,
    )
    mortgage_interest_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.MORTGAGE_INTEREST_EXPENSE,
        side=PostingSide.DEBIT,
    )
    mortgage_principal_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.MORTGAGE_PAYABLE,
        side=PostingSide.DEBIT,
        journal_entry_type=JournalEntryType.MORTGAGE_PAYMENT,
    )
    mortgage_payment_from_accounting = mortgage_interest_from_accounting + mortgage_principal_from_accounting
    property_tax_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PROPERTY_TAX_EXPENSE,
        side=PostingSide.DEBIT,
    )
    hoa_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.HOA_EXPENSE,
        side=PostingSide.DEBIT,
    )
    insurance_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.INSURANCE_EXPENSE,
        side=PostingSide.DEBIT,
    )
    maintenance_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.MAINTENANCE_EXPENSE,
        side=PostingSide.DEBIT,
    )
    rental_income_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.RENTAL_INCOME,
        side=PostingSide.CREDIT,
    )
    rental_management_fee_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.RENTAL_MANAGEMENT_FEE_EXPENSE,
        side=PostingSide.DEBIT,
    )
    rental_leasing_fee_from_accounting = _posting_amount_matrix(
        accounting,
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
        accounting,
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
    private_equity_sale_from_accounting = _posting_amount_matrix(
        accounting,
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
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PROPERTY,
        side=PostingSide.CREDIT,
        journal_entry_type=JournalEntryType.PROPERTY_SALE,
    )
    sale_closing_cost_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PROPERTY_SALE_CLOSING_EXPENSE,
        side=PostingSide.DEBIT,
        journal_entry_type=JournalEntryType.PROPERTY_SALE,
    )
    property_sale_debt_payoff_from_accounting = _posting_amount_matrix(
        accounting,
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
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.CHECKING_CASH,
        side=PostingSide.DEBIT,
        journal_entry_type=JournalEntryType.PROPERTY_SALE,
    )
    property_sale_cash_out_from_accounting = _posting_amount_matrix(
        accounting,
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
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PARTNER_CONTRIBUTION_TRANSFER,
        side=PostingSide.CREDIT,
    )
    partner_contribution_used_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PARTNER_CONTRIBUTION_USED,
        side=PostingSide.DEBIT,
    )
    partner_unallocated_excess_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PARTNER_UNALLOCATED_CLAIM,
        side=PostingSide.DEBIT,
    )
    partner_principal_credit_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PARTNER_PRINCIPAL_CREDIT,
        side=PostingSide.DEBIT,
    )
    owner_principal_credit_from_accounting = _posting_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.OWNER_PRINCIPAL_CREDIT,
        side=PostingSide.DEBIT,
    )
    partner_equity_ledger_from_snapshot = _balance_snapshot_amount_matrix(
        accounting, rollout_count=rollout_count, month_index=month_index, role=ChartAccountRole.PARTNER_EQUITY_LEDGER
    )
    owner_equity_ledger_from_snapshot = _balance_snapshot_amount_matrix(
        accounting, rollout_count=rollout_count, month_index=month_index, role=ChartAccountRole.OWNER_EQUITY_LEDGER
    )
    partner_home_equity_claim_from_snapshot = _balance_snapshot_amount_matrix(
        accounting,
        rollout_count=rollout_count,
        month_index=month_index,
        role=ChartAccountRole.PARTNER_HOME_EQUITY_CLAIM,
    )
    owner_home_equity_claim_from_snapshot = _balance_snapshot_amount_matrix(
        accounting, rollout_count=rollout_count, month_index=month_index, role=ChartAccountRole.OWNER_HOME_EQUITY_CLAIM
    )
    if not partner_equity.agreements:
        owner_principal_credit_from_accounting = partner_equity.owner_principal_usd
        partner_equity_ledger_from_snapshot = partner_equity.partner_equity_ledger_usd
        owner_equity_ledger_from_snapshot = partner_equity.owner_equity_ledger_usd
        partner_home_equity_claim_from_snapshot = partner_equity.home_equity_claim_usd
        owner_home_equity_claim_from_snapshot = partner_equity.owner_home_equity_claim_usd
    federal_income_tax_from_accounting = _accounting_detail_amount_matrix(
        accounting_details,
        rollout_count=rollout_count,
        month_index=month_index,
        detail_type=AccountingDetailType.TAX_PAYMENT_ALLOCATION,
        amount_field="federal_income_tax_usd",
    )
    california_income_tax_from_accounting = _accounting_detail_amount_matrix(
        accounting_details,
        rollout_count=rollout_count,
        month_index=month_index,
        detail_type=AccountingDetailType.TAX_PAYMENT_ALLOCATION,
        amount_field="california_income_tax_usd",
    )
    total_income_tax_from_accounting = _accounting_detail_amount_matrix(
        accounting_details,
        rollout_count=rollout_count,
        month_index=month_index,
        detail_type=AccountingDetailType.TAX_PAYMENT_ALLOCATION,
        amount_field="total_income_tax_usd",
    )
    rental_income_tax_from_accounting = _accounting_detail_amount_matrix(
        accounting_details,
        rollout_count=rollout_count,
        month_index=month_index,
        detail_type=AccountingDetailType.TAX_PAYMENT_ALLOCATION,
        amount_field="rental_income_tax_usd",
    )
    property_sale_adjusted_basis_from_accounting = _accounting_detail_amount_matrix(
        accounting_details,
        rollout_count=rollout_count,
        month_index=month_index,
        detail_type=AccountingDetailType.PROPERTY_SALE_BASIS_GAIN,
        amount_field="adjusted_basis_usd",
    )
    realized_property_gain_from_accounting = _accounting_detail_amount_matrix(
        accounting_details,
        rollout_count=rollout_count,
        month_index=month_index,
        detail_type=AccountingDetailType.PROPERTY_SALE_BASIS_GAIN,
        amount_field="realized_gain_usd",
    )
    property_sale_capital_gain_from_accounting = _accounting_detail_amount_matrix(
        accounting_details,
        rollout_count=rollout_count,
        month_index=month_index,
        detail_type=AccountingDetailType.PROPERTY_SALE_BASIS_GAIN,
        amount_field="capital_gain_usd",
    )
    property_sale_capital_gain_exclusion_from_accounting = _accounting_detail_amount_matrix(
        accounting_details,
        rollout_count=rollout_count,
        month_index=month_index,
        detail_type=AccountingDetailType.PROPERTY_SALE_BASIS_GAIN,
        amount_field="capital_gain_exclusion_usd",
    )
    taxable_property_capital_gain_from_accounting = _accounting_detail_amount_matrix(
        accounting_details,
        rollout_count=rollout_count,
        month_index=month_index,
        detail_type=AccountingDetailType.PROPERTY_SALE_BASIS_GAIN,
        amount_field="taxable_capital_gain_usd",
    )
    taxable_property_gain_from_accounting = _accounting_detail_amount_matrix(
        accounting_details,
        rollout_count=rollout_count,
        month_index=month_index,
        detail_type=AccountingDetailType.PROPERTY_SALE_BASIS_GAIN,
        amount_field="taxable_gain_usd",
    )
    depreciation_recapture_from_accounting = _accounting_detail_amount_matrix(
        accounting_details,
        rollout_count=rollout_count,
        month_index=month_index,
        detail_type=AccountingDetailType.PROPERTY_SALE_BASIS_GAIN,
        amount_field="depreciation_recapture_usd",
    )
    trace_identity_by_rollout = _trace_identity_by_rollout(scenario, market_bundle)
    return ScenarioRunArrays(
        scenario_id=scenario.scenario_id,
        scenario_label=scenario.label,
        month_index=month_index,
        cash_usd=cash,
        generic_sp500_value_usd=generic_sp500_value,
        generic_sp500_sale_usd=generic_sp500_sale_from_accounting,
        generic_sp500_sale_basis_usd=generic_sp500_sale_basis_from_accounting,
        generic_sp500_sale_gain_usd=generic_sp500_sale_from_accounting - generic_sp500_sale_basis_from_accounting,
        generic_sp500_sale_tax_usd=generic_sp500_sale_tax_from_accounting,
        checking_floor_action_usd=generic_sp500_sale_from_accounting,
        checking_floor_shortfall_usd=checking_floor_shortfall,
        private_equity_value_usd=private_equity_value,
        private_equity_sale_opportunity_value_usd=private_equity_sale_opportunity_value,
        private_equity_sale_usd=private_equity_sale_from_accounting,
        private_equity_sale_basis_usd=private_equity_sale_basis_from_accounting,
        private_equity_sale_tax_usd=private_equity_sale_tax_from_accounting,
        rental_income_tax_usd=rental_income_tax_from_accounting,
        federal_income_tax_usd=federal_income_tax_from_accounting,
        california_income_tax_usd=california_income_tax_from_accounting,
        total_income_tax_usd=total_income_tax_from_accounting,
        private_equity_sale_opportunity_event=private_equity_sale_opportunity_event,
        property_value_usd=property_value,
        mortgage_balance_usd=mortgage_balance,
        mortgage_interest_usd=mortgage_interest_from_accounting,
        mortgage_principal_usd=mortgage_principal_from_accounting,
        mortgage_payment_usd=mortgage_payment_from_accounting,
        property_tax_usd=property_tax_from_accounting,
        hoa_usd=hoa_from_accounting,
        insurance_usd=insurance_from_accounting,
        maintenance_usd=maintenance_from_accounting,
        rental_income_usd=rental_income_from_accounting,
        rental_management_fee_usd=rental_management_fee_from_accounting,
        rental_leasing_fee_usd=rental_leasing_fee_from_accounting,
        property_carrying_cost_usd=property_carrying_cost_from_accounting,
        net_property_cash_flow_usd=net_property_cash_flow_from_accounting,
        purchase_closing_cost_usd=disposition.purchase_closing_cost_usd,
        sale_closing_cost_usd=sale_closing_cost_from_accounting,
        property_depreciation_usd=disposition.property_depreciation_usd,
        cumulative_property_depreciation_usd=disposition.cumulative_property_depreciation_usd,
        property_sale_gross_usd=property_sale_gross_from_accounting,
        property_sale_net_proceeds_usd=property_sale_net_proceeds_from_accounting,
        property_sale_tax_usd=property_sale_tax_from_accounting,
        property_sale_debt_payoff_usd=property_sale_debt_payoff_from_accounting,
        property_sale_adjusted_basis_usd=property_sale_adjusted_basis_from_accounting,
        realized_property_gain_usd=realized_property_gain_from_accounting,
        property_sale_capital_gain_usd=property_sale_capital_gain_from_accounting,
        property_sale_capital_gain_exclusion_usd=property_sale_capital_gain_exclusion_from_accounting,
        taxable_property_capital_gain_usd=taxable_property_capital_gain_from_accounting,
        taxable_property_gain_usd=taxable_property_gain_from_accounting,
        depreciation_recapture_usd=depreciation_recapture_from_accounting,
        net_property_sale_cash_flow_usd=property_sale_net_proceeds_from_accounting,
        home_equity_usd=home_equity,
        owner_home_equity_claim_usd=owner_home_equity_claim_from_snapshot,
        partner_home_equity_claim_usd=partner_home_equity_claim_from_snapshot,
        partner_contribution_usd=partner_contribution_from_accounting,
        partner_contribution_used_usd=partner_contribution_used_from_accounting,
        partner_unallocated_excess_usd=partner_unallocated_excess_from_accounting,
        partner_house_costs_usd=partner_equity.house_costs_usd,
        partner_principal_credit_usd=partner_principal_credit_from_accounting,
        owner_principal_credit_usd=owner_principal_credit_from_accounting,
        partner_house_cost_share=partner_equity.house_cost_share,
        partner_equity_ledger_usd=partner_equity_ledger_from_snapshot,
        owner_equity_ledger_usd=owner_equity_ledger_from_snapshot,
        partner_ownership_pct=partner_equity.ownership_pct,
        liquid_net_worth_usd=liquid_net_worth,
        net_worth_usd=net_worth,
        partner_present=partner_present,
        monthly_spend_usd=monthly_spend_from_accounting,
        actions=_sorted_actions(_with_trajectory_identity(actions, trace_identity_by_rollout)),
        policy_decisions=_sorted_policy_decisions(
            _with_trajectory_identity(policy_decisions, trace_identity_by_rollout)
        ),
        market_observations=_sorted_market_observations(
            _with_trajectory_identity(market_observations, trace_identity_by_rollout)
        ),
        chart_accounts=_sorted_chart_accounts(list(accounting.chart_accounts_by_id.values())),
        journal_entries=_sorted_journal_entries(
            _with_trajectory_identity(accounting.journal_entries, trace_identity_by_rollout)
        ),
        postings=_sorted_postings(_with_trajectory_identity(accounting.postings, trace_identity_by_rollout)),
        balance_snapshots=_sorted_balance_snapshots(
            _with_trajectory_identity(accounting.balance_snapshots, trace_identity_by_rollout)
        ),
        tax_lots=_sorted_tax_lots(tax_lots),
        lot_dispositions=_sorted_lot_dispositions(
            _with_trajectory_identity(lot_dispositions, trace_identity_by_rollout)
        ),
        liabilities=_sorted_liabilities(liabilities),
        accounting_details=_sorted_accounting_details(
            _with_trajectory_identity(accounting_details, trace_identity_by_rollout)
        ),
        obligations=_sorted_obligations(_with_trajectory_identity(obligations, trace_identity_by_rollout)),
        funding_decisions=_sorted_funding_decisions(
            _with_trajectory_identity(funding_decisions, trace_identity_by_rollout)
        ),
        settlement_results=_sorted_settlement_results(
            _with_trajectory_identity(settlement_results, trace_identity_by_rollout)
        ),
        failure_events=_sorted_failure_events(_with_trajectory_identity(failure_events, trace_identity_by_rollout)),
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


def _with_trajectory_identity[TraceRecordT](
    records: list[TraceRecordT], identity_by_rollout: Mapping[int, dict[str, str]]
) -> list[TraceRecordT]:
    return [cast(TraceRecordT, _copy_with_trajectory_identity(record, identity_by_rollout)) for record in records]


def _copy_with_trajectory_identity(record: Any, identity_by_rollout: Mapping[int, dict[str, str]]) -> Any:
    return record.model_copy(update=identity_by_rollout[int(record.rollout_index)])


def _market_path_observations(
    scenario: Scenario, market_bundle: MarketBundle
) -> tuple[SimulationMarketObservation, ...]:
    home_multiplier = market_bundle.home_value_multipliers(scenario.location_id)
    rent_multiplier = market_bundle.rent_multipliers(scenario.location_id)
    observations: list[SimulationMarketObservation] = []
    rollout_indexes, month_positions = np.indices(
        (market_bundle.rollout_count, market_bundle.horizon_months + 1), sparse=False
    )
    for rollout_index, month_position in zip(
        rollout_indexes.ravel().tolist(), month_positions.ravel().tolist(), strict=True
    ):
        observations.append(
            MarketPathObservation(
                rollout_index=rollout_index,
                month_index=int(market_bundle.month_index[month_position]),
                location_id=scenario.location_id,
                inflation_multiplier=float(market_bundle.inflation_multipliers[rollout_index, month_position]),
                sp500_multiplier=float(market_bundle.generic_sp500_multipliers[rollout_index, month_position]),
                private_equity_value_multiplier=float(
                    market_bundle.private_equity_value_multipliers[rollout_index, month_position]
                ),
                home_value_multiplier=float(home_multiplier[rollout_index, month_position]),
                rent_multiplier=float(rent_multiplier[rollout_index, month_position]),
                mortgage_30y_rate_pct=float(market_bundle.mortgage_30y_rate_pct[rollout_index, month_position]),
                private_equity_sale_opportunity_event=bool(
                    market_bundle.private_equity_sale_opportunity_mask[rollout_index, month_position]
                ),
            )
        )
    return tuple(observations)


def _record_private_equity_sale_opportunity_observations(
    records: list[SimulationMarketObservation],
    *,
    month_index: int,
    source_asset_id: str,
    opportunity: PrivateEquitySaleOpportunityBatch,
) -> None:
    active_rollouts = np.nonzero(opportunity.sale_opportunity_mask)[0].tolist()
    records.extend(
        (
            PrivateEquitySaleOpportunityObservation(
                rollout_index=rollout_index,
                month_index=month_index,
                source_asset_id=source_asset_id,
                opportunity_id=str(opportunity.opportunity_id[rollout_index]),
                opportunity_cause_id=str(opportunity.opportunity_cause_id[rollout_index]),
                sale_opportunity_value_usd=float(opportunity.sale_opportunity_value_usd[rollout_index]),
                private_equity_value_before_sale_usd=float(
                    opportunity.private_equity_value_before_sale_usd[rollout_index]
                ),
            )
        )
        for rollout_index in active_rollouts
    )


def _record_monthly_spend_decisions(
    records: list[SimulationPolicyDecision],
    *,
    month_index: int,
    policy_step: ActorPolicyStep[Policy],
    amount_usd: np.ndarray,
    inflation_multiplier: np.ndarray,
) -> None:
    policy = policy_step.policy
    if not isinstance(policy, MonthlySpendPolicy):
        raise TypeError(f"monthly spend decision recorder received {type(policy).__name__}")
    active_rollouts = np.nonzero(amount_usd > 0)[0].tolist()
    records.extend(
        (
            MonthlySpendDecision(
                rollout_index=rollout_index,
                month_index=month_index,
                actor_id=policy.actor_id,
                policy_id=policy.policy_id,
                policy_sequence_index=policy_step.sequence_index,
                amount_usd=float(amount_usd[rollout_index]),
                inflation_multiplier=float(inflation_multiplier[rollout_index]),
            )
        )
        for rollout_index in active_rollouts
    )


def _record_sell_public_stock_decisions(
    records: list[SimulationPolicyDecision],
    *,
    month_index: int,
    policy_step: ActorPolicyStep[Policy],
    current_cash_usd: np.ndarray,
    requested_amount_usd: np.ndarray,
) -> None:
    policy = policy_step.policy
    if not isinstance(policy, CheckingFloorSellPublicStockPolicy):
        raise TypeError(f"public stock decision recorder received {type(policy).__name__}")
    active_rollouts = np.nonzero(requested_amount_usd > 0)[0].tolist()
    records.extend(
        (
            SellPublicStockDecision(
                rollout_index=rollout_index,
                month_index=month_index,
                actor_id=policy.actor_id,
                policy_id=policy.policy_id,
                policy_sequence_index=policy_step.sequence_index,
                requested_amount_usd=float(requested_amount_usd[rollout_index]),
                current_cash_usd=float(current_cash_usd[rollout_index]),
                target_cash_floor_usd=float(policy.floor_usd),
            )
        )
        for rollout_index in active_rollouts
    )


def _record_private_equity_sale_decisions(
    records: list[SimulationPolicyDecision],
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
    sale_rule_type = policy.sale_rule.sale_rule_type
    configured_sale_amount_usd = _private_equity_configured_sale_amount_usd(policy.sale_rule)
    records.extend(
        (
            PrivateEquitySaleDecision(
                rollout_index=rollout_index,
                month_index=month_index,
                actor_id=instruction.actor_id,
                policy_id=instruction.policy_id,
                policy_sequence_index=policy_step.sequence_index,
                decision_reason=_private_equity_sale_decision_reason(
                    requested_amount_usd=instruction.requested_amount_usd[rollout_index],
                    sale_opportunity_value_usd=opportunity.sale_opportunity_value_usd[rollout_index],
                ),
                source_asset_id=source_asset_id,
                sale_rule_type=sale_rule_type,
                configured_sale_amount_usd=configured_sale_amount_usd,
                opportunity_id=instruction.opportunity_id[rollout_index],
                opportunity_cause_id=str(instruction.opportunity_cause_id[rollout_index]),
                requested_amount_usd=float(instruction.requested_amount_usd[rollout_index]),
                sale_opportunity_value_usd=float(opportunity.sale_opportunity_value_usd[rollout_index]),
                private_equity_value_before_sale_usd=float(
                    opportunity.private_equity_value_before_sale_usd[rollout_index]
                ),
                liquid_net_worth_usd=float(liquid_net_worth_usd[rollout_index]),
                target_liquid_net_worth_floor_usd=target_liquid_net_worth_floor_usd,
                proceeds_destination=instruction.proceeds_destination,
            )
        )
        for rollout_index in range(instruction.requested_amount_usd.shape[0])
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
    records: list[SimulationPolicyDecision], *, month_index: np.ndarray, partner_equity: PartnerEquityArrays
) -> None:
    for agreement in partner_equity.agreements:
        policy = agreement.policy
        rollout_indexes, month_positions = np.nonzero(agreement.contribution_usd > 0)
        for rollout_index, month_position in zip(rollout_indexes.tolist(), month_positions.tolist(), strict=True):
            records.append(
                PartnerContributionDecision(
                    rollout_index=rollout_index,
                    month_index=int(month_index[month_position]),
                    actor_id=policy.actor_id,
                    policy_id=policy.policy_id,
                    policy_sequence_index=agreement.policy_sequence_index,
                    recipient_actor_id=agreement.recipient_actor_id,
                    requested_amount_usd=float(agreement.contribution_usd[rollout_index, month_position]),
                    property_id=agreement.property_id,
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
    accounting: AccountingTraceBuilder,
    *,
    rollout_count: int,
    month_index: np.ndarray,
    role: ChartAccountRole,
    side: PostingSide | None = None,
    journal_entry_type: JournalEntryType | None = None,
) -> np.ndarray:
    matrix = np.zeros((rollout_count, len(month_index)), dtype="float64")
    month_position_by_index = {int(month): position for position, month in enumerate(month_index.tolist())}
    journal_type_by_id = {entry.journal_entry_id: entry.journal_entry_type for entry in accounting.journal_entries}
    for posting in accounting.postings:
        account = accounting.chart_accounts_by_id[posting.chart_account_id]
        if account.role is not role:
            continue
        if side is not None and posting.side is not side:
            continue
        if journal_entry_type is not None and journal_type_by_id[posting.journal_entry_id] is not journal_entry_type:
            continue
        try:
            month_position = month_position_by_index[posting.month_index]
        except KeyError as exc:
            raise ValueError(f"posting has month outside result horizon: {posting.month_index}") from exc
        matrix[posting.rollout_index, month_position] += posting.amount_usd
    return matrix


def _balance_snapshot_amount_matrix(
    accounting: AccountingTraceBuilder, *, rollout_count: int, month_index: np.ndarray, role: ChartAccountRole
) -> np.ndarray:
    matrix = np.zeros((rollout_count, len(month_index)), dtype="float64")
    month_position_by_index = {int(month): position for position, month in enumerate(month_index.tolist())}
    for snapshot in accounting.balance_snapshots:
        account = accounting.chart_accounts_by_id[snapshot.chart_account_id]
        if account.role is not role:
            continue
        try:
            month_position = month_position_by_index[snapshot.month_index]
        except KeyError as exc:
            raise ValueError(f"balance snapshot has month outside result horizon: {snapshot.month_index}") from exc
        matrix[snapshot.rollout_index, month_position] += snapshot.balance_usd
    return matrix


def _lot_disposition_amount_matrix(
    records: list[LotDisposition],
    *,
    rollout_count: int,
    month_index: np.ndarray,
    asset_class: LotAssetClass,
    amount_field: str,
) -> np.ndarray:
    matrix = np.zeros((rollout_count, len(month_index)), dtype="float64")
    month_position_by_index = {int(month): position for position, month in enumerate(month_index.tolist())}
    for disposition in records:
        if disposition.asset_class is not asset_class:
            continue
        try:
            month_position = month_position_by_index[disposition.month_index]
        except KeyError as exc:
            raise ValueError(f"lot disposition has month outside result horizon: {disposition.month_index}") from exc
        amount = getattr(disposition, amount_field)
        if not isinstance(amount, int | float):
            raise TypeError(f"lot disposition field {amount_field!r} is not numeric")
        matrix[disposition.rollout_index, month_position] += float(amount)
    return matrix


def _tax_lot_id(asset_class: LotAssetClass, source_id: str) -> str:
    return f"lot:{asset_class.value}:{source_id}"


def _mortgage_liability_id(property_id: str) -> str:
    return f"mortgage:{property_id}"


def _accounting_detail_amount_matrix(
    records: list[SimulationAccountingDetail],
    *,
    rollout_count: int,
    month_index: np.ndarray,
    detail_type: AccountingDetailType,
    amount_field: str,
) -> np.ndarray:
    matrix = np.zeros((rollout_count, len(month_index)), dtype="float64")
    month_position_by_index = {int(month): position for position, month in enumerate(month_index.tolist())}
    for detail in records:
        if detail.detail_type != detail_type:
            continue
        try:
            month_position = month_position_by_index[detail.month_index]
        except KeyError as exc:
            raise ValueError(f"accounting detail has month outside result horizon: {detail.month_index}") from exc
        amount = getattr(detail, amount_field)
        if not isinstance(amount, int | float):
            raise TypeError(f"accounting detail field {amount_field!r} is not numeric")
        matrix[detail.rollout_index, month_position] += float(amount)
    return matrix


def _record_property_sale_journal_entries(
    accounting: AccountingTraceBuilder,
    lot_dispositions: list[LotDisposition],
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
    settlement = disposition.sale_settlement
    gross = settlement.gross_usd[:, month_index]
    selling_cost = settlement.selling_cost_usd[:, month_index]
    debt_payoff = settlement.debt_payoff_usd[:, month_index]
    tax = tax_usd[:, month_index]
    net_proceeds = net_proceeds_usd[:, month_index]
    entry_prefix = f"event:{sale_event.event_id}:property_sale"
    accounting.record_entry(
        month_index=month_index,
        entry=JournalEntryBatch(
            journal_entry_type=JournalEntryType.PROPERTY_SALE,
            cause_type=AccountingCauseType.SCHEDULED_EVENT,
            cause_id_prefix=entry_prefix,
            actor_id=actor_id,
            policy_id=PROPERTY_SALE_SETTLEMENT_POLICY_ID,
            event_id=sale_event.event_id,
            description="property sale settlement",
            postings=(
                PostingBatch(
                    role=ChartAccountRole.CHECKING_CASH,
                    side=PostingSide.DEBIT,
                    amount_usd=np.maximum(0.0, net_proceeds),
                    actor_id=actor_id,
                ),
                PostingBatch(
                    role=ChartAccountRole.PROPERTY_SALE_CLOSING_EXPENSE,
                    side=PostingSide.DEBIT,
                    amount_usd=selling_cost,
                    actor_id=actor_id,
                    property_id=property_id,
                ),
                PostingBatch(
                    role=ChartAccountRole.MORTGAGE_PAYABLE,
                    side=PostingSide.DEBIT,
                    amount_usd=debt_payoff,
                    actor_id=actor_id,
                    liability_id=_mortgage_liability_id(property_id),
                    property_id=property_id,
                ),
                PostingBatch(
                    role=ChartAccountRole.PROPERTY,
                    side=PostingSide.CREDIT,
                    amount_usd=gross,
                    actor_id=actor_id,
                    property_id=property_id,
                ),
                PostingBatch(
                    role=ChartAccountRole.CHECKING_CASH,
                    side=PostingSide.CREDIT,
                    amount_usd=np.maximum(0.0, -net_proceeds),
                    actor_id=actor_id,
                ),
            ),
        ),
    )
    lot_id = _tax_lot_id(LotAssetClass.PROPERTY, property_id)
    for rollout_index in np.nonzero(gross > 0)[0].tolist():
        journal_entry_id = _trace_row_id(entry_prefix, rollout_index=rollout_index, month_index=month_index)
        lot_dispositions.append(
            LotDisposition(
                lot_disposition_id=f"{journal_entry_id}:lot:{lot_id}",
                journal_entry_id=journal_entry_id,
                rollout_index=rollout_index,
                month_index=month_index,
                lot_id=lot_id,
                asset_class=LotAssetClass.PROPERTY,
                proceeds_usd=float(gross[rollout_index]),
                cost_basis_usd=float(settlement.adjusted_basis_usd[rollout_index, month_index]),
                realized_gain_usd=float(settlement.realized_property_gain_usd[rollout_index, month_index]),
                taxable_gain_usd=float(settlement.taxable_property_gain_usd[rollout_index, month_index]),
                tax_expense_usd=float(tax[rollout_index]),
            )
        )


def _record_property_sale_accounting_details(
    records: list[SimulationAccountingDetail], *, scenario: Scenario, disposition: PropertyDispositionArrays
) -> None:
    if disposition.sale_event is None or disposition.sale_month is None:
        return
    sale_event = disposition.sale_event
    property_id = sale_event.property_id or scenario.property_selection.property_id
    if property_id is None:
        return
    actor_id = sale_event.actor_id or _primary_owner_actor_id(scenario)
    month_position = disposition.sale_month
    settlement = disposition.sale_settlement
    active_rollouts = np.nonzero(
        (settlement.gross_usd[:, month_position] != 0)
        | (settlement.realized_property_gain_usd[:, month_position] != 0)
        | (settlement.taxable_property_gain_usd[:, month_position] != 0)
    )[0]
    records.extend(
        PropertySaleBasisGainDetail(
            rollout_index=int(rollout_index),
            month_index=int(disposition.sale_month),
            actor_id=actor_id,
            policy_id=PROPERTY_SALE_SETTLEMENT_POLICY_ID,
            event_id=sale_event.event_id,
            property_id=property_id,
            gross_sale_usd=float(settlement.gross_usd[rollout_index, month_position]),
            selling_cost_usd=float(settlement.selling_cost_usd[rollout_index, month_position]),
            debt_payoff_usd=float(settlement.debt_payoff_usd[rollout_index, month_position]),
            adjusted_basis_usd=float(settlement.adjusted_basis_usd[rollout_index, month_position]),
            realized_gain_usd=float(settlement.realized_property_gain_usd[rollout_index, month_position]),
            depreciation_recapture_usd=float(settlement.depreciation_recapture_usd[rollout_index, month_position]),
            capital_gain_usd=float(settlement.property_sale_capital_gain_usd[rollout_index, month_position]),
            capital_gain_exclusion_usd=float(
                settlement.property_sale_capital_gain_exclusion_usd[rollout_index, month_position]
            ),
            taxable_capital_gain_usd=float(settlement.taxable_property_capital_gain_usd[rollout_index, month_position]),
            taxable_gain_usd=float(settlement.taxable_property_gain_usd[rollout_index, month_position]),
        )
        for rollout_index in active_rollouts.tolist()
    )


def _record_tax_payment_allocation_details(
    records: list[SimulationAccountingDetail],
    *,
    scenario: Scenario,
    month_index: np.ndarray,
    annual_tax: AnnualSaleTaxAllocation,
    property_depreciation_recapture_usd: np.ndarray,
    taxable_property_capital_gain_usd: np.ndarray,
    generic_sp500_sale_gain_usd: np.ndarray,
    private_equity_sale_taxable_gain_usd: np.ndarray,
    net_rental_taxable_income_usd: np.ndarray,
) -> None:
    property_recapture = np.maximum(0.0, property_depreciation_recapture_usd)
    property_capital_gain = np.maximum(0.0, taxable_property_capital_gain_usd)
    sp500_capital_gain = np.maximum(0.0, generic_sp500_sale_gain_usd)
    private_equity_capital_gain = np.maximum(0.0, private_equity_sale_taxable_gain_usd)
    rental_taxable = np.maximum(0.0, net_rental_taxable_income_usd)
    total_taxable_income = (
        property_recapture + property_capital_gain + sp500_capital_gain + private_equity_capital_gain + rental_taxable
    )
    active_rollouts, active_month_positions = np.nonzero(
        (annual_tax.total_income_tax_usd != 0) | (total_taxable_income != 0)
    )
    actor_id = _primary_owner_actor_id(scenario)
    for rollout_index, month_position in zip(active_rollouts.tolist(), active_month_positions.tolist(), strict=True):
        records.append(
            TaxPaymentAllocationDetail(
                rollout_index=rollout_index,
                month_index=int(month_index[month_position]),
                actor_id=actor_id,
                policy_id=ANNUAL_TAX_ACCOUNTING_POLICY_ID,
                tax_year_index=int(month_index[month_position] // MONTHS_PER_YEAR),
                federal_income_tax_usd=float(annual_tax.federal_income_tax_usd[rollout_index, month_position]),
                california_income_tax_usd=float(annual_tax.california_income_tax_usd[rollout_index, month_position]),
                total_income_tax_usd=float(annual_tax.total_income_tax_usd[rollout_index, month_position]),
                property_sale_tax_usd=float(annual_tax.property_sale_tax_usd[rollout_index, month_position]),
                generic_sp500_sale_tax_usd=float(annual_tax.generic_sp500_sale_tax_usd[rollout_index, month_position]),
                private_equity_sale_tax_usd=float(
                    annual_tax.private_equity_sale_tax_usd[rollout_index, month_position]
                ),
                rental_income_tax_usd=float(annual_tax.rental_income_tax_usd[rollout_index, month_position]),
                property_depreciation_recapture_usd=float(property_recapture[rollout_index, month_position]),
                taxable_property_capital_gain_usd=float(property_capital_gain[rollout_index, month_position]),
                generic_sp500_taxable_gain_usd=float(sp500_capital_gain[rollout_index, month_position]),
                private_equity_taxable_gain_usd=float(private_equity_capital_gain[rollout_index, month_position]),
                net_rental_taxable_income_usd=float(rental_taxable[rollout_index, month_position]),
                total_taxable_income_usd=float(total_taxable_income[rollout_index, month_position]),
            )
        )


def _record_sp500_sale_journal_entries(
    accounting: AccountingTraceBuilder,
    lot_dispositions: list[LotDisposition],
    *,
    month_index: int,
    policy: Policy,
    cause_id_prefix: str,
    amount_usd: np.ndarray,
    basis_usd: np.ndarray,
    tax_usd: np.ndarray,
) -> None:
    entry_prefix = cause_id_prefix
    accounting.record_entry(
        month_index=month_index,
        entry=JournalEntryBatch(
            journal_entry_type=JournalEntryType.ASSET_SALE,
            cause_type=AccountingCauseType.POLICY_DECISION,
            cause_id_prefix=entry_prefix,
            actor_id=policy.actor_id,
            policy_id=policy.policy_id,
            description="public security sale",
            postings=(
                PostingBatch(
                    role=ChartAccountRole.CHECKING_CASH,
                    side=PostingSide.DEBIT,
                    amount_usd=amount_usd,
                    actor_id=policy.actor_id,
                ),
                PostingBatch(
                    role=ChartAccountRole.PUBLIC_SECURITY,
                    side=PostingSide.CREDIT,
                    amount_usd=amount_usd,
                    actor_id=policy.actor_id,
                ),
            ),
        ),
    )
    lot_id = _tax_lot_id(LotAssetClass.PUBLIC_SECURITY, "portfolio")
    for rollout_index in np.nonzero(amount_usd > 0)[0].tolist():
        amount = float(amount_usd[rollout_index])
        basis = float(basis_usd[rollout_index])
        journal_entry_id = _trace_row_id(entry_prefix, rollout_index=rollout_index, month_index=month_index)
        lot_dispositions.append(
            LotDisposition(
                lot_disposition_id=f"{journal_entry_id}:lot:{lot_id}",
                journal_entry_id=journal_entry_id,
                rollout_index=rollout_index,
                month_index=month_index,
                lot_id=lot_id,
                asset_class=LotAssetClass.PUBLIC_SECURITY,
                proceeds_usd=amount,
                cost_basis_usd=max(0.0, basis),
                realized_gain_usd=amount - basis,
                taxable_gain_usd=max(0.0, amount - basis),
                tax_expense_usd=float(tax_usd[rollout_index]),
            )
        )


def _record_private_equity_sale_journal_entries(
    accounting: AccountingTraceBuilder,
    lot_dispositions: list[LotDisposition],
    *,
    month_index: int,
    instruction: PrivateEquitySaleInstructionBatch,
    sale_application: PrivateEquitySaleApplication,
    tax_usd: np.ndarray,
    source_holding_id: str,
) -> None:
    destination_role = (
        ChartAccountRole.PUBLIC_SECURITY
        if instruction.proceeds_destination is AssetType.GENERIC_SP500_STOCK
        else ChartAccountRole.CHECKING_CASH
    )
    entry_prefix = f"policy:{instruction.policy_id}:private_equity_sale"
    accounting.record_entry(
        month_index=month_index,
        entry=JournalEntryBatch(
            journal_entry_type=JournalEntryType.ASSET_SALE,
            cause_type=AccountingCauseType.POLICY_DECISION,
            cause_id_prefix=entry_prefix,
            actor_id=instruction.actor_id,
            policy_id=instruction.policy_id,
            description="private equity sale",
            postings=(
                PostingBatch(
                    role=destination_role,
                    side=PostingSide.DEBIT,
                    amount_usd=sale_application.sale_usd,
                    actor_id=instruction.actor_id,
                ),
                PostingBatch(
                    role=ChartAccountRole.PRIVATE_EQUITY,
                    side=PostingSide.CREDIT,
                    amount_usd=sale_application.sale_usd,
                    actor_id=instruction.actor_id,
                    source_asset_id=source_holding_id,
                ),
            ),
        ),
    )
    lot_id = _tax_lot_id(LotAssetClass.PRIVATE_EQUITY, source_holding_id)
    for rollout_index in np.nonzero(sale_application.sale_usd > 0)[0].tolist():
        amount = float(sale_application.sale_usd[rollout_index])
        basis = float(sale_application.basis_usd[rollout_index])
        journal_entry_id = _trace_row_id(entry_prefix, rollout_index=rollout_index, month_index=month_index)
        lot_dispositions.append(
            LotDisposition(
                lot_disposition_id=f"{journal_entry_id}:lot:{lot_id}",
                journal_entry_id=journal_entry_id,
                rollout_index=rollout_index,
                month_index=month_index,
                lot_id=lot_id,
                asset_class=LotAssetClass.PRIVATE_EQUITY,
                proceeds_usd=amount,
                cost_basis_usd=max(0.0, basis),
                realized_gain_usd=amount - basis,
                taxable_gain_usd=float(sale_application.taxable_gain_usd[rollout_index]),
                quantity_sold=float(sale_application.sold_units[rollout_index]),
                tax_expense_usd=float(tax_usd[rollout_index]),
            )
        )


def _record_partner_agreement_accounting_detail(
    accounting: AccountingTraceBuilder, *, month_index: np.ndarray, partner_equity: PartnerEquityArrays
) -> None:
    if not partner_equity.journal_entries and not partner_equity.balance_snapshots:
        return
    _record_journal_entry_batches(accounting, month_index=month_index, entries=partner_equity.journal_entries)
    _record_balance_snapshot_batches(accounting, month_index=month_index, entries=partner_equity.balance_snapshots)


def _sorted_policy_decisions(records: list[SimulationPolicyDecision]) -> tuple[SimulationPolicyDecision, ...]:
    return tuple(
        sorted(
            records,
            key=lambda decision: (
                decision.month_index,
                decision.rollout_index,
                decision.actor_id,
                decision.policy_sequence_index,
                decision.decision_type,
                decision.policy_id,
            ),
        )
    )


def _sorted_market_observations(records: list[SimulationMarketObservation]) -> tuple[SimulationMarketObservation, ...]:
    return tuple(
        sorted(
            records,
            key=lambda observation: (observation.month_index, observation.rollout_index, observation.observation_type),
        )
    )


def _sorted_chart_accounts(records: list[ChartAccount]) -> tuple[ChartAccount, ...]:
    return tuple(sorted(records, key=lambda account: account.chart_account_id))


def _sorted_journal_entries(records: list[SimulationJournalEntry]) -> tuple[SimulationJournalEntry, ...]:
    return tuple(
        sorted(
            records,
            key=lambda entry: (
                entry.month_index,
                entry.rollout_index,
                entry.journal_entry_type,
                entry.journal_entry_id,
                entry.actor_id,
                entry.policy_id or "",
            ),
        )
    )


def _sorted_postings(records: list[SimulationPosting]) -> tuple[SimulationPosting, ...]:
    return tuple(
        sorted(
            records,
            key=lambda posting: (
                posting.month_index,
                posting.rollout_index,
                posting.journal_entry_id,
                posting.posting_id,
            ),
        )
    )


def _sorted_balance_snapshots(records: list[SimulationBalanceSnapshot]) -> tuple[SimulationBalanceSnapshot, ...]:
    return tuple(sorted(records, key=lambda entry: (entry.month_index, entry.rollout_index, entry.chart_account_id)))


def _sorted_tax_lots(records: list[TaxLot]) -> tuple[TaxLot, ...]:
    return tuple(sorted(records, key=lambda lot: lot.lot_id))


def _sorted_lot_dispositions(records: list[LotDisposition]) -> tuple[LotDisposition, ...]:
    return tuple(
        sorted(
            records,
            key=lambda disposition: (
                disposition.month_index,
                disposition.rollout_index,
                disposition.asset_class,
                disposition.lot_disposition_id,
            ),
        )
    )


def _sorted_liabilities(records: list[LiabilityState]) -> tuple[LiabilityState, ...]:
    return tuple(sorted(records, key=lambda liability: liability.liability_id))


def _sorted_accounting_details(records: list[SimulationAccountingDetail]) -> tuple[SimulationAccountingDetail, ...]:
    return tuple(
        sorted(
            records,
            key=lambda detail: (
                detail.month_index,
                detail.rollout_index,
                detail.detail_type,
                detail.actor_id,
                detail.policy_id or "",
                detail.event_id or "",
                detail.property_id or "",
            ),
        )
    )


def _sorted_obligations(records: list[SimulationObligation]) -> tuple[SimulationObligation, ...]:
    return tuple(
        sorted(
            records,
            key=lambda obligation: (
                obligation.month_index,
                obligation.rollout_index,
                obligation.obligation_type,
                obligation.obligation_id,
            ),
        )
    )


def _sorted_funding_decisions(records: list[SimulationFundingDecision]) -> tuple[SimulationFundingDecision, ...]:
    return tuple(
        sorted(
            records,
            key=lambda decision: (
                decision.month_index,
                decision.rollout_index,
                -1 if decision.policy_sequence_index is None else decision.policy_sequence_index,
                decision.decision_type,
                decision.policy_id or "",
                decision.obligation_id,
            ),
        )
    )


def _sorted_settlement_results(records: list[SimulationSettlementResult]) -> tuple[SimulationSettlementResult, ...]:
    return tuple(
        sorted(
            records,
            key=lambda settlement: (
                settlement.month_index,
                settlement.rollout_index,
                settlement.obligation_type,
                settlement.obligation_id,
            ),
        )
    )


def _sorted_failure_events(records: list[SimulationFailureEvent]) -> tuple[SimulationFailureEvent, ...]:
    return tuple(
        sorted(
            records,
            key=lambda event: (
                event.month_index,
                event.rollout_index,
                event.failure_event_type,
                event.failure_event_id,
            ),
        )
    )


def _record_property_sale_actions(
    actions: list[SimulationAction],
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
    settlement = disposition.sale_settlement
    sale_tax = tax_usd if tax_usd is not None else settlement.tax_usd
    net_proceeds = net_proceeds_usd if net_proceeds_usd is not None else settlement.net_proceeds_usd
    active = (
        (settlement.gross_usd[:, month] != 0)
        | (settlement.selling_cost_usd[:, month] != 0)
        | (settlement.debt_payoff_usd[:, month] != 0)
        | (sale_tax[:, month] != 0)
        | (net_proceeds[:, month] != 0)
    )
    actor_id = sale_event.actor_id or _primary_owner_actor_id(scenario)
    actions.extend(
        SettlePropertySaleAction(
            rollout_index=rollout_index,
            month_index=month,
            actor_id=actor_id,
            policy_id=PROPERTY_SALE_SETTLEMENT_POLICY_ID,
            event_id=sale_event.event_id,
            property_id=property_id,
            gross_sale_usd=float(settlement.gross_usd[rollout_index, month]),
            selling_cost_usd=float(settlement.selling_cost_usd[rollout_index, month]),
            debt_payoff_usd=float(settlement.debt_payoff_usd[rollout_index, month]),
            adjusted_basis_usd=float(settlement.adjusted_basis_usd[rollout_index, month]),
            realized_gain_usd=float(settlement.realized_property_gain_usd[rollout_index, month]),
            depreciation_recapture_usd=float(settlement.depreciation_recapture_usd[rollout_index, month]),
            capital_gain_usd=float(settlement.property_sale_capital_gain_usd[rollout_index, month]),
            capital_gain_exclusion_usd=float(settlement.property_sale_capital_gain_exclusion_usd[rollout_index, month]),
            taxable_capital_gain_usd=float(settlement.taxable_property_capital_gain_usd[rollout_index, month]),
            taxable_gain_usd=float(settlement.taxable_property_gain_usd[rollout_index, month]),
            tax_usd=float(sale_tax[rollout_index, month]),
            net_proceeds_usd=float(net_proceeds[rollout_index, month]),
        )
        for rollout_index in np.nonzero(active)[0].tolist()
    )


def _record_sp500_sale_actions(
    actions: list[SimulationAction],
    *,
    month_index: int,
    policy: Policy,
    amount_usd: np.ndarray,
    basis_usd: np.ndarray,
    tax_usd: np.ndarray,
    shortfall_usd: np.ndarray,
) -> None:
    for rollout_index in np.nonzero((amount_usd > 0) | (shortfall_usd > 0))[0].tolist():
        amount = float(amount_usd[rollout_index])
        basis = float(basis_usd[rollout_index])
        tax = float(tax_usd[rollout_index])
        actions.append(
            SellSp500Action(
                rollout_index=rollout_index,
                month_index=month_index,
                actor_id=policy.actor_id,
                policy_id=policy.policy_id,
                amount_usd=amount,
                after_tax_proceeds_usd=max(0.0, amount - tax),
                basis_usd=basis,
                gain_usd=amount - basis,
                tax_usd=tax,
                shortfall_usd=float(shortfall_usd[rollout_index]),
            )
        )


def _record_private_equity_sale_actions(
    actions: list[SimulationAction],
    *,
    month_index: int,
    instruction: PrivateEquitySaleInstructionBatch,
    sale_application: PrivateEquitySaleApplication,
    estimated_tax_usd: np.ndarray,
) -> None:
    sale_tax = estimated_tax_usd
    actions.extend(
        SellPrivateEquityAction(
            rollout_index=rollout_index,
            month_index=month_index,
            actor_id=instruction.actor_id,
            policy_id=instruction.policy_id,
            opportunity_id=instruction.opportunity_id[rollout_index],
            opportunity_cause_id=str(instruction.opportunity_cause_id[rollout_index]),
            amount_usd=float(sale_application.sale_usd[rollout_index]),
            after_tax_proceeds_usd=float(np.maximum(0.0, sale_application.sale_usd - sale_tax)[rollout_index]),
            basis_usd=float(sale_application.basis_usd[rollout_index]),
            taxable_gain_usd=float(sale_application.taxable_gain_usd[rollout_index]),
            estimated_tax_usd=float(sale_tax[rollout_index]),
            units_sold=float(sale_application.sold_units[rollout_index]),
            sold_fraction=float(sale_application.sold_fraction[rollout_index]),
            proceeds_destination=instruction.proceeds_destination,
        )
        for rollout_index in np.nonzero(sale_application.sale_usd > 0)[0].tolist()
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


@dataclass(frozen=True)
class _MortgageObligationKind:
    interest_usd: np.ndarray
    principal_usd: np.ndarray
    property_id: str
    obligation_type: ObligationType = ObligationType.MORTGAGE_PAYMENT


_ObligationKind = _AnnualTaxObligationKind | _MortgageObligationKind


def _year_end_tax_obligation_due_usd(*, month_index: np.ndarray, source_month_tax_due_usd: np.ndarray) -> np.ndarray:
    """Aggregate per-source-month tax allocations into a year-end-due matrix.

    Tax accrued in months that share a tax year (`month_index // 12`) collects
    into a single obligation due at the year-end month (`year * 12 + 11`).
    Years whose year-end falls past the simulation horizon settle at the last
    in-horizon month belonging to that year — this keeps the horizon a clean
    cutoff for outstanding tax. Quarterly estimated payments are a follow-on
    (see TODO Tax Follow-Ups).
    """
    obligation = np.zeros_like(source_month_tax_due_usd, dtype="float64")
    if obligation.size == 0:
        return obligation
    tax_year_by_position = month_index // MONTHS_PER_YEAR
    horizon_last_position = obligation.shape[1] - 1
    for tax_year in np.unique(tax_year_by_position):
        year_positions = np.nonzero(tax_year_by_position == tax_year)[0]
        if year_positions.size == 0:
            continue
        year_total = np.sum(source_month_tax_due_usd[:, year_positions], axis=1)
        year_end_month_index = int(tax_year) * MONTHS_PER_YEAR + (MONTHS_PER_YEAR - 1)
        year_end_position_matches = np.nonzero(month_index == year_end_month_index)[0]
        if year_end_position_matches.size > 0:
            due_position = int(year_end_position_matches[0])
        else:
            # Year-end is outside the simulated month_index. Fall back to the last
            # in-horizon month that still belongs to this tax year so the cash
            # impact lands within the simulation.
            due_position = int(year_positions[-1])
            # Belt-and-suspenders: never schedule outside the simulation.
            due_position = min(due_position, horizon_last_position)
        obligation[:, due_position] = obligation[:, due_position] + year_total
    return obligation


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
    checking_floor_shortfall_usd: np.ndarray,
    obligations: list[SimulationObligation],
    funding_decisions: list[SimulationFundingDecision],
    settlement_results: list[SimulationSettlementResult],
    failure_events: list[SimulationFailureEvent],
    accounting: AccountingTraceBuilder,
    sp500_sale_action_records: list[Sp500SaleActionRecord],
) -> None:
    obligation_type = obligation_kind.obligation_type
    actor_id = _primary_owner_actor_id(scenario)
    cash_source_account = _single_checking_account_source(scenario, actor_id=actor_id)
    sp500_source_asset = _single_sp500_asset_source(scenario, actor_id=actor_id)
    for month_position, due_month_index in enumerate(month_index.tolist()):
        due = obligation_amount_usd[:, month_position]
        if not np.any(due > 0):
            continue

        paid_from_cash = np.minimum(np.maximum(0.0, cash_usd[:, month_position]), due)
        remaining_due = np.maximum(0.0, due - paid_from_cash)
        _record_obligation_cash_funding_decisions(
            funding_decisions,
            obligation_type=obligation_type,
            actor_id=actor_id,
            month_index=int(due_month_index),
            obligation_amount_usd=due,
            available_cash_usd=cash_usd[:, month_position],
            funded_cash_usd=paid_from_cash,
            source_account=cash_source_account,
        )

        for policy_step in policy_steps:
            if not np.any(remaining_due > 0):
                break
            application = _apply_obligation_funding_policy_step(
                policy_step,
                due_usd=due,
                remaining_due_usd=remaining_due,
                cash_usd=cash_usd[:, month_position],
                remaining_units=remaining_sp500_units_by_month[:, month_position],
                remaining_basis_usd=remaining_sp500_basis_by_month[:, month_position],
                sp500_unit_price_usd=market_bundle.generic_sp500_multipliers[:, month_position],
                source_asset=sp500_source_asset,
            )
            if application is None:
                continue
            old_units = remaining_sp500_units_by_month[:, month_position].copy()
            old_basis = remaining_sp500_basis_by_month[:, month_position].copy()
            units_sold = np.maximum(0.0, old_units - application.remaining_units)
            basis_sold = np.maximum(0.0, old_basis - application.remaining_basis_usd)
            remaining_sp500_units_by_month[:, month_position:] = np.maximum(
                0.0, remaining_sp500_units_by_month[:, month_position:] - units_sold[:, None]
            )
            remaining_sp500_basis_by_month[:, month_position:] = np.maximum(
                0.0, remaining_sp500_basis_by_month[:, month_position:] - basis_sold[:, None]
            )
            generic_sp500_value_usd[:, month_position:] = (
                remaining_sp500_units_by_month[:, month_position:]
                * market_bundle.generic_sp500_multipliers[:, month_position:]
            )
            cash_usd[:, month_position:] = cash_usd[:, month_position:] + application.sale_usd[:, None]
            remaining_due = application.remaining_due_usd
            checking_floor_shortfall_usd[:, month_position] = np.maximum(
                checking_floor_shortfall_usd[:, month_position], remaining_due
            )
            _record_obligation_sale_funding_decisions(
                funding_decisions,
                obligation_type=obligation_type,
                actor_id=actor_id,
                month_index=int(due_month_index),
                policy_step=application.policy_step,
                obligation_amount_usd=due,
                requested_sale_usd=application.instruction.requested_amount_usd,
                funded_cash_usd=application.funded_cash_usd,
                shortfall_usd=application.shortfall_usd,
                source_asset=sp500_source_asset,
            )
            sp500_sale_action_records.append(
                Sp500SaleActionRecord(
                    month_position=month_position,
                    month_index=int(due_month_index),
                    policy=application.policy_step.policy,
                    cause_id_prefix=(
                        f"policy:{application.policy_step.policy.policy_id}:{obligation_type.value}:funding_sale"
                    ),
                    amount_usd=application.sale_usd,
                    basis_usd=basis_sold,
                    shortfall_usd=remaining_due,
                )
            )

        amount_paid = np.minimum(due, np.maximum(0.0, cash_usd[:, month_position]))
        unpaid = np.maximum(0.0, due - amount_paid)
        cash_usd[:, month_position:] = cash_usd[:, month_position:] - amount_paid[:, None]
        _record_obligation_accrual_and_settlement_entries(
            accounting,
            obligation_kind=obligation_kind,
            month_position=month_position,
            month_index=int(due_month_index),
            actor_id=actor_id,
            source_policy_id=source_policy_id,
            due_usd=due,
            amount_paid_usd=amount_paid,
        )
        _record_unfunded_obligation_decisions(
            funding_decisions,
            obligation_type=obligation_type,
            actor_id=actor_id,
            month_index=int(due_month_index),
            obligation_amount_usd=due,
            unpaid_amount_usd=unpaid,
        )
        _record_obligation_settlement_rows(
            obligations,
            settlement_results,
            failure_events,
            obligation_type=obligation_type,
            actor_id=actor_id,
            creditor_id=creditor_id,
            source_policy_id=source_policy_id,
            month_index=int(due_month_index),
            amount_due_usd=due,
            amount_paid_usd=amount_paid,
            unpaid_amount_usd=unpaid,
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
        accounting.record_entry(
            month_index=month_index,
            entry=JournalEntryBatch(
                journal_entry_type=JournalEntryType.TAX_ACCRUAL,
                cause_type=AccountingCauseType.ACCOUNTING_PROCESS,
                cause_id_prefix=f"policy:{source_policy_id}:{obligation_type.value}:accrual",
                obligation_id_prefix=obligation_type.value,
                actor_id=actor_id,
                policy_id=source_policy_id,
                description=obligation_type.value,
                postings=(
                    PostingBatch(
                        role=ChartAccountRole.TAX_EXPENSE, side=PostingSide.DEBIT, amount_usd=due_usd, actor_id=actor_id
                    ),
                    PostingBatch(
                        role=ChartAccountRole.TAX_PAYABLE,
                        side=PostingSide.CREDIT,
                        amount_usd=due_usd,
                        actor_id=actor_id,
                        liability_id=f"tax:{obligation_type.value}",
                    ),
                ),
            ),
        )
        accounting.record_entry(
            month_index=month_index,
            entry=JournalEntryBatch(
                journal_entry_type=JournalEntryType.OBLIGATION_SETTLEMENT,
                cause_type=AccountingCauseType.OBLIGATION_SETTLEMENT,
                cause_id_prefix=f"policy:{source_policy_id}:{obligation_type.value}:settlement",
                obligation_id_prefix=obligation_type.value,
                actor_id=actor_id,
                policy_id=source_policy_id,
                description=obligation_type.value,
                postings=(
                    PostingBatch(
                        role=ChartAccountRole.TAX_PAYABLE,
                        side=PostingSide.DEBIT,
                        amount_usd=amount_paid_usd,
                        actor_id=actor_id,
                        liability_id=f"tax:{obligation_type.value}",
                    ),
                    PostingBatch(
                        role=ChartAccountRole.CHECKING_CASH,
                        side=PostingSide.CREDIT,
                        amount_usd=amount_paid_usd,
                        actor_id=actor_id,
                    ),
                ),
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
    accounting.record_entry(
        month_index=month_index,
        entry=JournalEntryBatch(
            journal_entry_type=JournalEntryType.MORTGAGE_PAYMENT,
            cause_type=AccountingCauseType.OBLIGATION_SETTLEMENT,
            cause_id_prefix=f"policy:{source_policy_id}:{obligation_type.value}:settlement",
            obligation_id_prefix=obligation_type.value,
            actor_id=actor_id,
            policy_id=source_policy_id,
            description=obligation_type.value,
            postings=(
                PostingBatch(
                    role=ChartAccountRole.MORTGAGE_INTEREST_EXPENSE,
                    side=PostingSide.DEBIT,
                    amount_usd=interest_paid,
                    actor_id=actor_id,
                ),
                PostingBatch(
                    role=ChartAccountRole.MORTGAGE_PAYABLE,
                    side=PostingSide.DEBIT,
                    amount_usd=principal_paid,
                    actor_id=actor_id,
                    liability_id=liability_id,
                ),
                PostingBatch(
                    role=ChartAccountRole.CHECKING_CASH,
                    side=PostingSide.CREDIT,
                    amount_usd=amount_paid_usd,
                    actor_id=actor_id,
                ),
            ),
        ),
    )


def _apply_obligation_funding_policy_step(
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
    if isinstance(policy, CheckingFloorSellPublicStockPolicy):
        return _apply_checking_floor_obligation_funding_policy(
            policy_step,
            due_usd=due_usd,
            remaining_due_usd=remaining_due_usd,
            cash_usd=cash_usd,
            remaining_units=remaining_units,
            remaining_basis_usd=remaining_basis_usd,
            sp500_unit_price_usd=sp500_unit_price_usd,
            source_asset=source_asset,
        )
    return None


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


def _record_obligation_cash_funding_decisions(
    records: list[SimulationFundingDecision],
    *,
    obligation_type: ObligationType,
    actor_id: str,
    month_index: int,
    obligation_amount_usd: np.ndarray,
    available_cash_usd: np.ndarray,
    funded_cash_usd: np.ndarray,
    source_account: AccountBalance | None,
) -> None:
    records.extend(
        (
            SimulationFundingDecision(
                rollout_index=rollout_index,
                month_index=month_index,
                obligation_id=_obligation_id(obligation_type, rollout_index=rollout_index, month_index=month_index),
                decision_type=FundingDecisionType.USE_CASH,
                actor_id=actor_id,
                source_type=FundingSourceType.CASH_ACCOUNT,
                source_account_id=source_account.account_id if source_account is not None else None,
                source_account_type=AccountType.CHECKING,
                available_cash_usd=float(available_cash_usd[rollout_index]),
                requested_cash_usd=float(obligation_amount_usd[rollout_index]),
                funded_cash_usd=float(funded_cash_usd[rollout_index]),
                shortfall_usd=float(
                    np.maximum(0.0, obligation_amount_usd[rollout_index] - funded_cash_usd[rollout_index])
                ),
            )
        )
        for rollout_index in np.nonzero(obligation_amount_usd > 0)[0].tolist()
    )


def _record_obligation_sale_funding_decisions(
    records: list[SimulationFundingDecision],
    *,
    obligation_type: ObligationType,
    actor_id: str,
    month_index: int,
    policy_step: ActorPolicyStep[Policy],
    obligation_amount_usd: np.ndarray,
    requested_sale_usd: np.ndarray,
    funded_cash_usd: np.ndarray,
    shortfall_usd: np.ndarray,
    source_asset: GenericSp500StockPosition | None,
) -> None:
    policy = policy_step.policy
    records.extend(
        (
            SimulationFundingDecision(
                rollout_index=rollout_index,
                month_index=month_index,
                obligation_id=_obligation_id(obligation_type, rollout_index=rollout_index, month_index=month_index),
                decision_type=FundingDecisionType.SELL_PUBLIC_STOCK,
                actor_id=actor_id,
                policy_id=policy.policy_id,
                policy_sequence_index=policy_step.sequence_index,
                source_type=FundingSourceType.PUBLIC_MARKET_ASSET,
                source_asset_id=source_asset.asset_id if source_asset is not None else None,
                source_asset_type=AssetType.GENERIC_SP500_STOCK,
                available_cash_usd=0.0,
                requested_cash_usd=float(obligation_amount_usd[rollout_index]),
                requested_sale_usd=float(requested_sale_usd[rollout_index]),
                funded_cash_usd=float(funded_cash_usd[rollout_index]),
                shortfall_usd=float(shortfall_usd[rollout_index]),
            )
        )
        for rollout_index in np.nonzero((requested_sale_usd > 0) | (shortfall_usd > 0))[0].tolist()
    )


def _record_unfunded_obligation_decisions(
    records: list[SimulationFundingDecision],
    *,
    obligation_type: ObligationType,
    actor_id: str,
    month_index: int,
    obligation_amount_usd: np.ndarray,
    unpaid_amount_usd: np.ndarray,
) -> None:
    records.extend(
        (
            SimulationFundingDecision(
                rollout_index=rollout_index,
                month_index=month_index,
                obligation_id=_obligation_id(obligation_type, rollout_index=rollout_index, month_index=month_index),
                decision_type=FundingDecisionType.UNFUNDED,
                actor_id=actor_id,
                source_type=FundingSourceType.UNFUNDED,
                available_cash_usd=0.0,
                requested_cash_usd=float(obligation_amount_usd[rollout_index]),
                funded_cash_usd=0.0,
                shortfall_usd=float(unpaid_amount_usd[rollout_index]),
            )
        )
        for rollout_index in np.nonzero(unpaid_amount_usd > 0)[0].tolist()
    )


def _record_obligation_settlement_rows(
    obligations: list[SimulationObligation],
    settlement_results: list[SimulationSettlementResult],
    failure_events: list[SimulationFailureEvent],
    *,
    obligation_type: ObligationType,
    actor_id: str,
    creditor_id: str,
    source_policy_id: str,
    month_index: int,
    amount_due_usd: np.ndarray,
    amount_paid_usd: np.ndarray,
    unpaid_amount_usd: np.ndarray,
) -> None:
    for rollout_index in np.nonzero(amount_due_usd > 0)[0].tolist():
        due = float(amount_due_usd[rollout_index])
        paid = float(amount_paid_usd[rollout_index])
        unpaid = float(unpaid_amount_usd[rollout_index])
        obligation_id = _obligation_id(obligation_type, rollout_index=rollout_index, month_index=month_index)
        obligation_status = _obligation_status(amount_due_usd=due, unpaid_amount_usd=unpaid)
        settlement_status = _settlement_status(amount_due_usd=due, unpaid_amount_usd=unpaid)
        obligations.append(
            SimulationObligation(
                rollout_index=rollout_index,
                month_index=month_index,
                obligation_id=obligation_id,
                obligation_type=obligation_type,
                actor_id=actor_id,
                creditor_id=creditor_id,
                due_month_index=month_index,
                amount_due_usd=due,
                amount_paid_usd=paid,
                unpaid_amount_usd=unpaid,
                status=obligation_status,
                source_policy_id=source_policy_id,
            )
        )
        settlement_results.append(
            SimulationSettlementResult(
                rollout_index=rollout_index,
                month_index=month_index,
                obligation_id=obligation_id,
                obligation_type=obligation_type,
                actor_id=actor_id,
                status=settlement_status,
                amount_due_usd=due,
                amount_paid_usd=paid,
                unpaid_amount_usd=unpaid,
            )
        )
        if unpaid > 0:
            failure_events.append(
                SimulationFailureEvent(
                    rollout_index=rollout_index,
                    month_index=month_index,
                    failure_event_id=f"{obligation_id}:failure",
                    failure_event_type=FailureEventType.UNSETTLED_OBLIGATION,
                    obligation_id=obligation_id,
                    actor_id=actor_id,
                    unpaid_amount_usd=unpaid,
                )
            )


def _obligation_id(obligation_type: ObligationType, *, rollout_index: int, month_index: int) -> str:
    return f"{obligation_type.value}:rollout:{rollout_index}:month:{month_index}"


def _obligation_status(*, amount_due_usd: float, unpaid_amount_usd: float) -> ObligationStatus:
    if unpaid_amount_usd <= 0:
        return ObligationStatus.PAID
    if unpaid_amount_usd >= amount_due_usd:
        return ObligationStatus.UNPAID
    return ObligationStatus.PARTIALLY_PAID


def _settlement_status(*, amount_due_usd: float, unpaid_amount_usd: float) -> SettlementStatus:
    if unpaid_amount_usd <= 0:
        return SettlementStatus.PAID
    if unpaid_amount_usd >= amount_due_usd:
        return SettlementStatus.UNPAID
    return SettlementStatus.PARTIALLY_PAID


def _sorted_actions(actions: list[SimulationAction]) -> tuple[SimulationAction, ...]:
    return tuple(sorted(actions, key=lambda action: (action.month_index, action.rollout_index, action.action_type)))


def _property_cash_flow_arrays(
    scenario: Scenario,
    market_bundle: MarketBundle,
    *,
    location_id: str | None,
    property_value_usd: np.ndarray,
    mortgage_interest_usd: np.ndarray,
    mortgage_principal_usd: np.ndarray,
) -> PropertyCashFlowArrays:
    zeros = np.zeros_like(property_value_usd, dtype="float64")
    mortgage_payment = mortgage_interest_usd + mortgage_principal_usd
    # Mortgage payments are settled through the obligation pipeline in
    # _settle_required_cash_obligations, not directly through net_property_cash_flow_usd.
    # The mortgage_payment_usd is retained on this array for reporting parity, but the
    # operating cash flow stops at carrying cost minus rental income.
    if scenario.property_selection.property_id is None:
        return PropertyCashFlowArrays(
            mortgage_payment_usd=mortgage_payment,
            property_tax_usd=zeros,
            hoa_usd=zeros,
            insurance_usd=zeros,
            maintenance_usd=zeros,
            rental_income_usd=zeros,
            rental_management_fee_usd=zeros,
            rental_leasing_fee_usd=zeros,
            property_carrying_cost_usd=zeros,
            net_property_cash_flow_usd=zeros,
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
        mortgage_payment_usd=mortgage_payment,
        property_tax_usd=operating_cash_flow.property_tax_usd,
        hoa_usd=operating_cash_flow.hoa_usd,
        insurance_usd=operating_cash_flow.insurance_usd,
        maintenance_usd=operating_cash_flow.maintenance_usd,
        rental_income_usd=operating_cash_flow.rental_income_usd,
        rental_management_fee_usd=operating_cash_flow.rental_management_fee_usd,
        rental_leasing_fee_usd=operating_cash_flow.rental_leasing_fee_usd,
        property_carrying_cost_usd=operating_cash_flow.property_carrying_cost_usd,
        net_property_cash_flow_usd=operating_cash_flow.net_operating_cash_flow_usd,
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
    zeros = np.zeros_like(home_equity_usd, dtype="float64")
    owner_equity_without_partners = float(owner_initial_equity_usd) + np.cumsum(mortgage_principal_usd, axis=1)
    empty = PartnerEquityArrays(
        contribution_usd=zeros,
        contribution_used_usd=zeros,
        unallocated_excess_usd=zeros,
        house_costs_usd=zeros,
        mortgage_payment_usd=zeros,
        mortgage_interest_usd=zeros,
        mortgage_principal_usd=zeros,
        principal_credit_usd=zeros,
        owner_principal_usd=mortgage_principal_usd,
        house_cost_share=zeros,
        partner_equity_ledger_usd=zeros,
        owner_equity_ledger_usd=owner_equity_without_partners,
        ownership_pct=zeros,
        home_equity_claim_usd=zeros,
        owner_home_equity_claim_usd=home_equity_usd,
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
                contribution_usd=contribution_instruction.amount_usd,
                contribution_used_usd=contribution_application.contribution_used_usd,
                unallocated_excess_usd=contribution_application.unallocated_excess_usd,
                house_costs_usd=contribution_application.house_costs_usd,
                mortgage_payment_usd=mortgage_payment,
                mortgage_interest_usd=mortgage_interest_usd,
                mortgage_principal_usd=principal_available,
                principal_credit_usd=contribution_application.principal_credit_usd,
                owner_principal_usd=owner_principal,
                house_cost_share=contribution_application.house_cost_share,
                partner_equity_ledger_usd=ownership_application.partner_equity_ledger_usd,
                owner_equity_ledger_usd=owner_equity_ledger,
                ownership_pct=ownership_application.ownership_pct,
                home_equity_claim_usd=ownership_application.home_equity_claim_usd,
                owner_home_equity_claim_usd=ownership_application.owner_home_equity_claim_usd,
                journal_entries=contribution_application.journal_entries + ownership_application.journal_entries,
                balance_snapshots=ownership_application.balance_snapshots,
            )
        )

    contribution_usd = sum((agreement.contribution_usd for agreement in agreements), start=zeros.copy())
    contribution_used = sum((agreement.contribution_used_usd for agreement in agreements), start=zeros.copy())
    unallocated_excess = sum((agreement.unallocated_excess_usd for agreement in agreements), start=zeros.copy())
    home_equity_claim = sum((agreement.home_equity_claim_usd for agreement in agreements), start=zeros.copy())
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
        contribution_usd=contribution_usd,
        contribution_used_usd=contribution_used,
        unallocated_excess_usd=unallocated_excess,
        house_costs_usd=house_uses,
        mortgage_payment_usd=mortgage_payment,
        mortgage_interest_usd=mortgage_interest_usd,
        mortgage_principal_usd=mortgage_principal_usd,
        principal_credit_usd=principal_credit,
        owner_principal_usd=owner_principal,
        house_cost_share=np.divide(
            contribution_used, house_uses, out=np.zeros_like(contribution_used), where=house_uses > 0
        ),
        partner_equity_ledger_usd=total_partner_equity_ledger,
        owner_equity_ledger_usd=owner_equity_ledger,
        ownership_pct=ownership_pct,
        home_equity_claim_usd=home_equity_claim,
        owner_home_equity_claim_usd=owner_home_equity_claim,
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
        (agreement.home_equity_claim_usd for agreement in agreements),
        start=np.zeros_like(partner_equity.home_equity_claim_usd),
    )
    owner_home_equity_claim_usd = partner_equity.owner_home_equity_claim_usd.copy()
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
        partner_equity,
        home_equity_claim_usd=partner_home_equity_claim_usd,
        owner_home_equity_claim_usd=owner_home_equity_claim_usd,
        agreements=agreements,
        balance_snapshots=owner_balance_snapshots
        + tuple(snapshot for agreement in agreements for snapshot in agreement.balance_snapshots),
    )


def _settle_partner_equity_agreement_on_property_sale(
    agreement: PartnerEquityAgreementArrays, *, sale_month: int, property_sale_net_proceeds_usd: np.ndarray
) -> PartnerEquityAgreementArrays:
    home_equity_claim_usd = agreement.home_equity_claim_usd.copy()
    owner_home_equity_claim_usd = agreement.owner_home_equity_claim_usd.copy()
    sale_net_proceeds = property_sale_net_proceeds_usd[:, sale_month]
    partner_sale_claim = np.maximum(0.0, sale_net_proceeds) * agreement.ownership_pct[:, sale_month]
    home_equity_claim_usd[:, sale_month:] = partner_sale_claim[:, None]
    owner_home_equity_claim_usd[:, sale_month:] = sale_net_proceeds[:, None] - partner_sale_claim[:, None]
    balance_snapshots = tuple(
        replace(snapshot, amount_usd=home_equity_claim_usd)
        if snapshot.role is ChartAccountRole.PARTNER_HOME_EQUITY_CLAIM
        else snapshot
        for snapshot in agreement.balance_snapshots
    )
    return replace(
        agreement,
        home_equity_claim_usd=home_equity_claim_usd,
        owner_home_equity_claim_usd=owner_home_equity_claim_usd,
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
    return sum(
        asset.cost_basis_usd if asset.cost_basis_usd is not None else asset.value_usd
        for asset in scenario.initial_balance_sheet.assets
        if isinstance(asset, GenericSp500StockPosition)
    )


def _single_sp500_asset_source(scenario: Scenario, *, actor_id: str) -> GenericSp500StockPosition | None:
    positions = tuple(
        asset
        for asset in scenario.initial_balance_sheet.assets
        if isinstance(asset, GenericSp500StockPosition) and asset.owner_actor_id == actor_id
    )
    if len(positions) == 1:
        return positions[0]
    return None


def _initial_private_equity_value_usd(scenario: Scenario) -> float:
    return sum(
        asset.value_usd for asset in scenario.initial_balance_sheet.assets if isinstance(asset, PrivateEquityPosition)
    )


def _initial_private_equity_cost_basis_usd(scenario: Scenario) -> float:
    return sum(
        asset.cost_basis_usd if asset.cost_basis_usd is not None else asset.value_usd
        for asset in scenario.initial_balance_sheet.assets
        if isinstance(asset, PrivateEquityPosition)
    )


def _initial_private_equity_units(scenario: Scenario) -> float:
    return sum(
        asset.units or 0.0
        for asset in scenario.initial_balance_sheet.assets
        if isinstance(asset, PrivateEquityPosition)
    )


def _private_equity_source_holding_id(scenario: Scenario) -> str:
    positions = tuple(
        asset for asset in scenario.initial_balance_sheet.assets if isinstance(asset, PrivateEquityPosition)
    )
    if len(positions) == 1:
        return positions[0].asset_id
    return "private_equity_portfolio"


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


def _required_local_regulation(scenario: Scenario) -> LocalRegulation:
    if scenario.property_selection.local_regulation is not None:
        return scenario.property_selection.local_regulation
    location_id = scenario.location_id
    if location_id is None:
        raise ValueError(f"scenario {scenario.scenario_id!r} has real estate but no location_id")
    return local_regulation_for_location(location_id)


def _pct_fraction(value: float, name: str) -> float:
    if value < 0 or value > 100:
        raise ValueError(f"{name} must be in [0, 100]")
    return value / 100


def _flat(values: np.ndarray) -> list[float]:
    return values.reshape(-1).tolist()


def _flat_bool(values: np.ndarray) -> list[bool]:
    return values.reshape(-1).astype(bool).tolist()


def _fan_columns(values: np.ndarray) -> ColumnarTable:
    matrix = np.asarray(values, dtype="float64")
    if matrix.ndim != 2:
        raise ValueError("fan values must be shaped (rollout, month)")
    month_index = np.arange(matrix.shape[1], dtype="int64")
    percentiles = (1, 2, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 98, 99)
    percentile_values = np.nanpercentile(matrix, percentiles, axis=0)
    columns: dict[str, list[Any]] = {
        "month_index": month_index.tolist(),
        "year": (month_index / MONTHS_PER_YEAR).tolist(),
    }
    for index, percentile in enumerate(percentiles):
        columns[f"p{percentile:02d}"] = percentile_values[index].tolist()
    return ColumnarTable(row_count=int(matrix.shape[1]), columns=columns)
