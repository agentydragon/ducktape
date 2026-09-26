"""A world's month-scoped outcomes copied as the caller steps it, and their columnar event frames."""

from dataclasses import dataclass
from typing import Literal

import polars as pl

from finance.augur.sim import private_equity
from finance.augur.sim.accounting import MortgagePaymentOutcome, TransferOutcome
from finance.augur.sim.books import (
    BondCashflowOutcome,
    Book,
    DistributionOutcome,
    JournalEntry,
    TaxAccrual,
    TaxPaymentOutcome,
    TaxSettlementOutcome,
)
from finance.augur.sim.events import EVENT_FRAMES, EventLog
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.holdings import Disposition
from finance.augur.sim.ids import AgentId
from finance.augur.sim.managed import FinancialEffect
from finance.augur.sim.payments import ObligationOutcome
from finance.augur.sim.property import CapitalImprovement, Origination, Purchase, RentedFraction, Residence, Sale
from finance.augur.sim.world import World


@dataclass(frozen=True)
class Failure:
    month: int
    cause_id: str
    agent_id: AgentId
    deficit: int
    obligation_id: str
    obligation_type: str
    amount_due: int
    amount_paid: int
    shortfall: int


@dataclass(frozen=True)
class PropertyOutcomes:
    """The housing domain's outcomes, present exactly when the world declares housing."""

    purchases: list[Purchase]
    primary_residence_events: list[Residence]
    rented_fraction_events: list[RentedFraction]
    capital_improvements: list[CapitalImprovement]
    sales: list[Sale]
    mortgage_originations: list[Origination]


@dataclass(frozen=True)
class PrivateEquityOutcomes:
    events: list[private_equity.ProtocolEvent]
    opportunities: list[private_equity.Opportunity]


@dataclass(frozen=True)
class FinancialOutput:
    """A world's recorded outcomes; a domain the world does not have is `None`, not empty.

    `tlh_financial_effects` needs a managed portfolio, `bond_cashflows` a held bond and
    `distributions` a declared distribution or managed portfolio.
    """

    rollout_id: int
    months: list[Book]
    journal: list[JournalEntry]
    transfers: list[TransferOutcome]
    dispositions: list[Disposition]
    obligations: list[ObligationOutcome]
    tax_accruals: list[TaxAccrual]
    tax_payments: list[TaxPaymentOutcome]
    tax_settlements: list[TaxSettlementOutcome]
    mortgage_payments: list[MortgagePaymentOutcome]
    failed_month: int | None
    tlh_financial_effects: list[FinancialEffect] | None = None
    private_equity: PrivateEquityOutcomes | None = None
    bond_cashflows: list[BondCashflowOutcome] | None = None
    distributions: list[DistributionOutcome] | None = None
    properties: PropertyOutcomes | None = None

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


class FinancialCapture:
    """Copy one world's month-scoped outcomes into a `FinancialOutput` as the caller steps it.

    The world keeps nothing past the current month; this is the caller's record. `dense` keeps
    everything but the journal, `forensic` keeps the journal too.
    """

    def __init__(self, world: World, *, capture: Literal["dense", "forensic"]) -> None:
        self.world = world
        self.capture = capture
        self.months: list[Book] = [world.book()]
        self.journal: list[JournalEntry] = []
        self.transfers: list[TransferOutcome] = []
        self.dispositions: list[Disposition] = []
        self.obligations: list[ObligationOutcome] = []
        self.tax_accruals: list[TaxAccrual] = []
        self.tax_payments: list[TaxPaymentOutcome] = []
        self.tax_settlements: list[TaxSettlementOutcome] = []
        self.mortgage_payments: list[MortgagePaymentOutcome] = []
        self.tlh_financial_effects: list[FinancialEffect] = []
        self.private_equity_events: list[private_equity.ProtocolEvent] = []
        self.private_equity_opportunities: list[private_equity.Opportunity] = []
        self.bond_cashflows: list[BondCashflowOutcome] = []
        self.distributions: list[DistributionOutcome] = []
        self.property_purchases: list[Purchase] = []
        self.primary_residence_events: list[Residence] = []
        self.property_rented_fraction_events: list[RentedFraction] = []
        self.capital_improvements: list[CapitalImprovement] = []
        self.property_sales: list[Sale] = []
        self.mortgage_originations: list[Origination] = []

    def record(self) -> None:
        """Call after the world closes a month and before the next one opens."""
        world = self.world
        accounting = world.accounting
        self.months.append(world.book())
        if self.capture == "forensic":
            self.journal.extend(accounting.journal)
        self.transfers.extend(accounting.transfers)
        self.dispositions.extend(world.holdings.dispositions)
        self.obligations.extend(world.obligations)
        self.tax_accruals.extend(accounting.tax_accruals)
        self.tax_payments.extend(accounting.tax_payments)
        self.tax_settlements.extend(accounting.tax_settlements)
        self.mortgage_payments.extend(accounting.mortgage_payments)
        if world.managed is not None:
            self.tlh_financial_effects.extend(world.managed.effects)
        if world.private_equity is not None:
            self.private_equity_events.extend(world.private_equity.events)
            self.private_equity_opportunities.extend(world.private_equity.opportunities)
        if world.bonds is not None:
            self.bond_cashflows.extend(world.bonds.cashflows)
        if world.distributions is not None:
            self.distributions.extend(world.distributions.outcomes)
        if world.managed is not None:
            self.distributions.extend(world.managed.distributions)
        properties = world.properties
        if properties is not None:
            self.property_purchases.extend(properties.purchases)
            self.primary_residence_events.extend(properties.residences)
            self.property_rented_fraction_events.extend(properties.rented_fractions)
            self.capital_improvements.extend(properties.improvements)
            self.property_sales.extend(properties.sales)
            self.mortgage_originations.extend(properties.originations)

    def financial(self) -> FinancialOutput:
        world = self.world
        return FinancialOutput(
            rollout_id=world.rollout_id,
            months=self.months,
            journal=self.journal,
            transfers=self.transfers,
            dispositions=self.dispositions,
            obligations=self.obligations,
            tax_accruals=self.tax_accruals,
            tax_payments=self.tax_payments,
            tax_settlements=self.tax_settlements,
            mortgage_payments=self.mortgage_payments,
            failed_month=world.failed_month,
            tlh_financial_effects=None if world.managed is None else self.tlh_financial_effects,
            private_equity=None
            if world.private_equity is None
            else PrivateEquityOutcomes(self.private_equity_events, self.private_equity_opportunities),
            bond_cashflows=None if world.bonds is None else self.bond_cashflows,
            distributions=None if world.distributions is None and world.managed is None else self.distributions,
            properties=None
            if world.properties is None
            else PropertyOutcomes(
                purchases=self.property_purchases,
                primary_residence_events=self.primary_residence_events,
                rented_fraction_events=self.property_rented_fraction_events,
                capital_improvements=self.capital_improvements,
                sales=self.property_sales,
                mortgage_originations=self.mortgage_originations,
            ),
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
                    "income_quanta": row.income,
                }
                for row in output.tlh_financial_effects
            ],
            schema=EVENT_FRAMES.tlh_financial_effects.schema,
        )
    frames["private_equity_events"] = EVENT_FRAMES.private_equity_events.schema.to_frame()
    private_equity_events = [] if output.private_equity is None else output.private_equity.events
    if private_equity_events:
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
                for row in private_equity_events
            ],
            schema=EVENT_FRAMES.private_equity_events.schema,
        )
    frames["private_equity_opportunities"] = EVENT_FRAMES.private_equity_opportunities.schema.to_frame()
    private_equity_opportunities = [] if output.private_equity is None else output.private_equity.opportunities
    if private_equity_opportunities:
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
                for row in private_equity_opportunities
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
    property_purchases = [] if output.properties is None else output.properties.purchases
    if property_purchases:
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
                for row in property_purchases
            ],
            schema=EVENT_FRAMES.property_purchases.schema,
        )
    frames["mortgage_originations"] = EVENT_FRAMES.mortgage_originations.schema.to_frame()
    mortgage_originations = [] if output.properties is None else output.properties.mortgage_originations
    if mortgage_originations:
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
                for row in mortgage_originations
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
    primary_residence_events = [] if output.properties is None else output.properties.primary_residence_events
    if primary_residence_events:
        frames["set_primary_residence_events"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "agent_id": row.agent_id,
                    "property_id": row.property_id,
                    "is_primary_residence": row.is_primary_residence,
                }
                for row in primary_residence_events
            ],
            schema=EVENT_FRAMES.set_primary_residence_events.schema,
        )
    frames["set_rented_fraction_events"] = EVENT_FRAMES.set_rented_fraction_events.schema.to_frame()
    property_rented_fraction_events = [] if output.properties is None else output.properties.rented_fraction_events
    if property_rented_fraction_events:
        frames["set_rented_fraction_events"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "property_id": row.property_id,
                    "rented_fraction": row.rented_fraction_ppb / MONEY_FACTOR_SCALE,
                }
                for row in property_rented_fraction_events
            ],
            schema=EVENT_FRAMES.set_rented_fraction_events.schema,
        )
    frames["capital_improvement_events"] = EVENT_FRAMES.capital_improvement_events.schema.to_frame()
    capital_improvements = [] if output.properties is None else output.properties.capital_improvements
    if capital_improvements:
        frames["capital_improvement_events"] = pl.DataFrame(
            [
                {
                    "rollout_id": output.rollout_id,
                    "month_index": row.month,
                    "property_id": row.property_id,
                    "amount_quanta": row.amount,
                    "description": row.description,
                }
                for row in capital_improvements
            ],
            schema=EVENT_FRAMES.capital_improvement_events.schema,
        )
    frames["property_sale_events"] = EVENT_FRAMES.property_sale_events.schema.to_frame()
    property_sales = [] if output.properties is None else output.properties.sales
    if property_sales:
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
                for row in property_sales
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
