"""Typed financial capture and external artifact projection, separate from live books."""

from dataclasses import dataclass
from typing import Any

import polars as pl
from pydantic import TypeAdapter

from finance.augur.sim import private_equity, results
from finance.augur.sim.accounting import MortgagePaymentOutcome, TransferOutcome
from finance.augur.sim.books import (
    AccountBalance,
    BondCashflowOutcome,
    BondState,
    Book,
    DistributionOutcome,
    JournalEntry,
    MortgageState,
    PropertyState,
    TaxAccrual,
    TaxLiabilityState,
    TaxPaymentOutcome,
    TaxSettlementOutcome,
)
from finance.augur.sim.events import EVENT_FRAMES, EventLog
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.holdings import Disposition
from finance.augur.sim.managed import FinancialEffect
from finance.augur.sim.observations import TlhPortfolioObservation
from finance.augur.sim.payments import ObligationOutcome
from finance.augur.sim.property import CapitalImprovement, Origination, Purchase, RentedFraction, Residence, Sale


@dataclass(frozen=True)
class Failure:
    month: int
    cause_id: str
    agent_id: str
    deficit: int
    obligation_id: str
    obligation_type: str
    amount_due: int
    amount_paid: int
    shortfall: int


@dataclass(frozen=True)
class FinancialOutput:
    rollout_id: int
    months: list[Book]
    journal: list[JournalEntry]
    transfers: list[TransferOutcome]
    dispositions: list[Disposition]
    tlh_financial_effects: list[FinancialEffect]
    private_equity_events: list[private_equity.ProtocolEvent]
    private_equity_opportunities: list[private_equity.Opportunity]
    obligations: list[ObligationOutcome]
    tax_accruals: list[TaxAccrual]
    tax_payments: list[TaxPaymentOutcome]
    tax_settlements: list[TaxSettlementOutcome]
    bond_cashflows: list[BondCashflowOutcome]
    distributions: list[DistributionOutcome]
    property_purchases: list[Purchase]
    primary_residence_events: list[Residence]
    property_rented_fraction_events: list[RentedFraction]
    capital_improvements: list[CapitalImprovement]
    property_sales: list[Sale]
    mortgage_originations: list[Origination]
    mortgage_payments: list[MortgagePaymentOutcome]
    failed_month: int | None

    @property
    def rollout_failures(self) -> list[Failure]:
        return [
            Failure(
                row.month,
                f"{row.cause_id}_failure",
                row.from_account.agent_id,
                row.shortfall,
                row.obligation_id,
                row.obligation_type,
                row.amount_due,
                row.amount_paid,
                row.shortfall,
            )
            for row in self.obligations
            if row.failure_active
        ]

    def export(self) -> dict[str, Any]:
        payload: dict[str, Any] = _FINANCIAL_OUTPUT.dump_python(self, mode="json", by_alias=True)
        for channel in ("transfers", "obligations"):
            for row in payload[channel]:
                row["from"] = row.pop("from_account")
                row["to"] = row.pop("to_account")
        payload["rollout_failures"] = _FAILURES.dump_python(self.rollout_failures, mode="json")
        return payload


_FINANCIAL_OUTPUT = TypeAdapter(FinancialOutput)
_FAILURES = TypeAdapter(list[Failure])


@dataclass(frozen=True)
class ConfiguredSummary:
    rollout_id: int
    ending_balances: list[AccountBalance]
    ending_bonds: list[BondState]
    ending_properties: list[PropertyState]
    ending_mortgages: list[MortgageState]
    ending_tax_liabilities: list[TaxLiabilityState]
    ending_tlh_portfolios: list[TlhPortfolioObservation]
    journal_entry_count: int
    disposition_count: int
    private_equity_event_count: int
    private_equity_opportunity_count: int
    tax_accrual_count: int
    tax_payment_count: int
    tax_settlement_count: int
    bond_cashflow_count: int
    distribution_count: int
    property_purchase_count: int
    primary_residence_event_count: int
    property_rented_fraction_event_count: int
    capital_improvement_count: int
    property_sale_count: int
    mortgage_payment_count: int
    failed_month: int | None

    def export(self) -> dict[str, Any]:
        payload: dict[str, Any] = _CONFIGURED_SUMMARY.dump_python(self, mode="json")
        return payload


_CONFIGURED_SUMMARY = TypeAdapter(ConfiguredSummary)


@dataclass(frozen=True)
class WorldResult:
    rollout_id: int
    summary: results.Summary | None
    financial: FinancialOutput | None
    events: EventLog | None
    configured_summary: ConfiguredSummary | None
    product_metrics: list[tuple[int, int, int, int, int, int, int]]

    def rollout(
        self, receipts: list[results.Receipt], last_receipts: list[results.Receipt], stop: results.Stop | None
    ) -> results.Rollout:
        if self.summary is None:
            raise RuntimeError("actor capture requires a financial summary")
        trace = None
        if self.financial is not None:
            if self.events is None:
                raise RuntimeError("detailed capture requires financial event frames")
            trace = results.Trace(
                events=self.events,
                books=self.financial.months,
                journal=self.financial.journal,
                bond_cashflows=self.financial.bond_cashflows,
                distributions=self.financial.distributions,
                receipts=receipts,
            )
        return results.Rollout(
            rollout_id=self.rollout_id,
            summary=self.summary.model_copy(update={"last_receipts": last_receipts}),
            trace=trace,
            stop=stop,
        )


def event_log(output: FinancialOutput) -> EventLog:
    frames = {}
    frames["transfers"] = EVENT_FRAMES.transfers.schema.to_frame()
    if output.transfers:
        frames["transfers"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "from_agent_id": row.from_account.agent_id,
                    "from_account_id": row.from_account.account_id,
                    "to_agent_id": row.to_account.agent_id,
                    "to_account_id": row.to_account.account_id,
                    "amount_quanta": row.amount,
                    "income_category": row.income_category,
                }
                for row in output.transfers
            ],
            schema=EVENT_FRAMES.transfers.schema,
        )
    frames["lot_dispositions"] = EVENT_FRAMES.lot_dispositions.schema.to_frame()
    if output.dispositions:
        frames["lot_dispositions"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "agent_id": row.agent_id,
                    "source_account_id": row.source_account_id,
                    "asset_id": row.asset_id,
                    "lot_id": row.lot_id,
                    "purchase_month_index": row.purchase_month,
                    "units_sold": row.units / row.quantity_scale,
                    "cost_basis_consumed_quanta": row.basis,
                    "proceeds_quanta": row.proceeds,
                    "realized_gain_quanta": row.realized_gain,
                    "proceeds_account_id": row.proceeds_account_id,
                }
                for row in output.dispositions
            ],
            schema=EVENT_FRAMES.lot_dispositions.schema,
        )
    frames["tlh_financial_effects"] = EVENT_FRAMES.tlh_financial_effects.schema.to_frame()
    if output.tlh_financial_effects:
        frames["tlh_financial_effects"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "portfolio_id": row.portfolio_id,
                    "agent_id": row.agent_id,
                    "account_id": row.account_id,
                    "cash_account_id": row.cash_account_id,
                    "operation": row.operation,
                    "cash_amount_quanta": row.cash_amount,
                    "short_term_gain_quanta": row.short_term_gain,
                    "long_term_gain_quanta": row.long_term_gain,
                    "basis_change_quanta": row.basis_change,
                    "interest_income_quanta": row.interest_income,
                }
                for row in output.tlh_financial_effects
            ],
            schema=EVENT_FRAMES.tlh_financial_effects.schema,
        )
    frames["private_equity_events"] = EVENT_FRAMES.private_equity_events.schema.to_frame()
    if output.private_equity_events:
        frames["private_equity_events"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "issuer_id": row.issuer_id,
                    "asset_id": row.asset_id,
                    "event_kind": row.event_kind,
                    "regime": row.regime,
                    "mark_quanta": row.mark,
                    "sale_capacity_fraction": row.sale_capacity_fraction_ppb / MONEY_FACTOR_SCALE,
                    "eligible_fraction": row.eligible_fraction_ppb / MONEY_FACTOR_SCALE,
                    "forced_sale_fraction": row.forced_sale_fraction_ppb / MONEY_FACTOR_SCALE,
                    "liquidity_blocked": row.liquidity_blocked,
                    "forced_recovery_cashout_quanta": row.forced_recovery_cashout,
                }
                for row in output.private_equity_events
            ],
            schema=EVENT_FRAMES.private_equity_events.schema,
        )
    frames["private_equity_opportunities"] = EVENT_FRAMES.private_equity_opportunities.schema.to_frame()
    if output.private_equity_opportunities:
        frames["private_equity_opportunities"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "issuer_id": row.issuer_id,
                    "asset_id": row.asset_id,
                    "event_kind": row.event_kind,
                    "regime": row.regime,
                    "outcome": row.outcome,
                    "mark_quanta": row.mark,
                    "sale_capacity_fraction": row.sale_capacity_fraction_ppb / MONEY_FACTOR_SCALE,
                    "eligible_fraction": row.eligible_fraction_ppb / MONEY_FACTOR_SCALE,
                    "liquidity_blocked": row.liquidity_blocked,
                    "floor_quanta": row.floor,
                    "liquid_net_worth_quanta": row.liquid_net_worth,
                    "shortfall_quanta": row.shortfall,
                    "units_held": row.units_held / row.quantity_scale,
                    "sellable_units": row.sellable_units / row.quantity_scale,
                    "target_units": row.target_units / row.quantity_scale,
                    "proceeds_quanta": row.proceeds,
                }
                for row in output.private_equity_opportunities
            ],
            schema=EVENT_FRAMES.private_equity_opportunities.schema,
        )
    frames["obligation_accruals"] = EVENT_FRAMES.obligation_accruals.schema.to_frame()
    if output.obligations:
        frames["obligation_accruals"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "obligation_id": row.obligation_id,
                    "obligation_type": row.obligation_type,
                    "agent_id": row.from_account.agent_id,
                    "from_account_id": row.from_account.account_id,
                    "to_agent_id": row.to_account.agent_id,
                    "to_account_id": row.to_account.account_id,
                    "amount_due_quanta": row.amount_due,
                }
                for row in output.obligations
            ],
            schema=EVENT_FRAMES.obligation_accruals.schema,
        )
    frames["obligation_settlements"] = EVENT_FRAMES.obligation_settlements.schema.to_frame()
    if output.obligations:
        frames["obligation_settlements"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "obligation_id": row.obligation_id,
                    "obligation_type": row.obligation_type,
                    "agent_id": row.from_account.agent_id,
                    "from_account_id": row.from_account.account_id,
                    "amount_due_quanta": row.amount_due,
                    "amount_paid_quanta": row.amount_paid,
                    "shortfall_quanta": row.shortfall,
                }
                for row in output.obligations
            ],
            schema=EVENT_FRAMES.obligation_settlements.schema,
        )
    frames["rollout_failures"] = EVENT_FRAMES.rollout_failures.schema.to_frame()
    if output.rollout_failures:
        frames["rollout_failures"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "agent_id": row.agent_id,
                    "deficit_quanta": row.deficit,
                    "obligation_id": row.obligation_id,
                    "obligation_type": row.obligation_type,
                    "amount_due_quanta": row.amount_due,
                    "amount_paid_quanta": row.amount_paid,
                    "shortfall_quanta": row.shortfall,
                }
                for row in output.rollout_failures
            ],
            schema=EVENT_FRAMES.rollout_failures.schema,
        )
    frames["property_purchases"] = EVENT_FRAMES.property_purchases.schema.to_frame()
    if output.property_purchases:
        frames["property_purchases"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "property_id": row.property_id,
                    "location_id": row.location_id,
                    "buyer_agent_id": row.buyer_agent_id,
                    "purchase_price_quanta": row.purchase_price,
                    "closing_cost_quanta": row.closing_cost,
                    "adjusted_basis_quanta": row.adjusted_basis,
                    "stake_contribution_quanta": row.stake_contribution,
                    "equity_ledger_quanta": row.equity_ledger,
                }
                for row in output.property_purchases
            ],
            schema=EVENT_FRAMES.property_purchases.schema,
        )
    frames["mortgage_originations"] = EVENT_FRAMES.mortgage_originations.schema.to_frame()
    if output.mortgage_originations:
        frames["mortgage_originations"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "liability_id": row.liability_id,
                    "agent_id": row.agent_id,
                    "payment_account_id": row.payment_account_id,
                    "counterparty_agent_id": row.counterparty_agent_id,
                    "counterparty_account_id": row.counterparty_account_id,
                    "property_id": row.property_id,
                    "principal_quanta": row.principal,
                    "annual_interest_rate": row.annual_interest_rate_ppb / MONEY_FACTOR_SCALE,
                    "term_months": row.term_months,
                    "monthly_payment_quanta": row.monthly_payment,
                }
                for row in output.mortgage_originations
            ],
            schema=EVENT_FRAMES.mortgage_originations.schema,
        )
    frames["mortgage_payments"] = EVENT_FRAMES.mortgage_payments.schema.to_frame()
    if output.mortgage_payments:
        frames["mortgage_payments"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "liability_id": row.liability_id,
                    "agent_id": row.agent_id,
                    "counterparty_agent_id": row.counterparty_agent_id,
                    "property_id": row.property_id,
                    "from_account_id": row.from_account_id,
                    "to_account_id": row.to_account_id,
                    "interest_quanta": row.interest,
                    "principal_quanta": row.principal,
                    "total_payment_quanta": row.total_payment,
                }
                for row in output.mortgage_payments
            ],
            schema=EVENT_FRAMES.mortgage_payments.schema,
        )
    frames["set_primary_residence_events"] = EVENT_FRAMES.set_primary_residence_events.schema.to_frame()
    if output.primary_residence_events:
        frames["set_primary_residence_events"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "agent_id": row.agent_id,
                    "property_id": row.property_id,
                    "is_primary_residence": row.is_primary_residence,
                }
                for row in output.primary_residence_events
            ],
            schema=EVENT_FRAMES.set_primary_residence_events.schema,
        )
    frames["set_rented_fraction_events"] = EVENT_FRAMES.set_rented_fraction_events.schema.to_frame()
    if output.property_rented_fraction_events:
        frames["set_rented_fraction_events"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "property_id": row.property_id,
                    "rented_fraction": row.rented_fraction_ppb / MONEY_FACTOR_SCALE,
                }
                for row in output.property_rented_fraction_events
            ],
            schema=EVENT_FRAMES.set_rented_fraction_events.schema,
        )
    frames["capital_improvement_events"] = EVENT_FRAMES.capital_improvement_events.schema.to_frame()
    if output.capital_improvements:
        frames["capital_improvement_events"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "property_id": row.property_id,
                    "amount_quanta": row.amount,
                    "description": row.description,
                }
                for row in output.capital_improvements
            ],
            schema=EVENT_FRAMES.capital_improvement_events.schema,
        )
    frames["property_sale_events"] = EVENT_FRAMES.property_sale_events.schema.to_frame()
    if output.property_sales:
        frames["property_sale_events"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "property_id": row.property_id,
                    "gross_proceeds_quanta": row.gross_proceeds,
                    "mortgage_payoff_quanta": row.mortgage_payoff,
                    "net_cash_to_owner_quanta": row.net_cash_to_owner,
                    "realized_gain_quanta": row.realized_gain,
                    "depreciation_recapture_quanta": row.depreciation_recapture,
                    "section_121_exclusion_quanta": row.section_121_exclusion,
                    "long_term_capital_gain_quanta": row.long_term_capital_gain,
                }
                for row in output.property_sales
            ],
            schema=EVENT_FRAMES.property_sale_events.schema,
        )
    frames["tax_accruals"] = EVENT_FRAMES.tax_accruals.schema.to_frame()
    if output.tax_accruals:
        frames["tax_accruals"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "agent_id": row.agent_id,
                    "jurisdiction_id": row.jurisdiction_id,
                    "tax_year_end_month": row.tax_year_end_month,
                    "amount_quanta": row.total_tax,
                }
                for row in output.tax_accruals
            ],
            schema=EVENT_FRAMES.tax_accruals.schema,
        )
    frames["tax_breakdowns"] = EVENT_FRAMES.tax_breakdowns.schema.to_frame()
    if output.tax_accruals:
        frames["tax_breakdowns"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "agent_id": row.agent_id,
                    "jurisdiction_id": row.jurisdiction_id,
                    "tax_year_end_month": row.tax_year_end_month,
                    "ordinary_income_quanta": row.ordinary_income,
                    "ltcg_quanta": row.long_term_gain,
                    "stcg_quanta": row.short_term_gain,
                    "standard_deduction_quanta": row.standard_deduction,
                    "mortgage_interest_deduction_quanta": row.mortgage_interest_deduction,
                    "salt_deduction_quanta": row.salt_deduction,
                    "itemized_deduction_quanta": row.itemized_deduction,
                    "ordinary_taxable_quanta": row.ordinary_taxable,
                    "capital_gain_taxable_quanta": row.long_term_capital_gain_taxable,
                    "ordinary_tax_quanta": row.ordinary_tax,
                    "capital_gain_tax_quanta": row.capital_gain_tax,
                    "total_tax_quanta": row.total_tax,
                }
                for row in output.tax_accruals
            ],
            schema=EVENT_FRAMES.tax_breakdowns.schema,
        )
    frames["tax_settlements"] = EVENT_FRAMES.tax_settlements.schema.to_frame()
    if output.tax_settlements:
        frames["tax_settlements"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "cause_id": row.cause_id,
                    "agent_id": row.agent_id,
                    "tax_year_end_month": row.tax_year_end_month,
                    "amount_quanta": row.amount,
                }
                for row in output.tax_settlements
            ],
            schema=EVENT_FRAMES.tax_settlements.schema,
        )
    return EventLog.from_frames(frames, rollout_ids=[output.rollout_id])
