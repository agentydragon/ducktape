"""Event log for the simulation.

Every state-changing happening is a row on an event-kind frame.
`EventLog` bundles all the kind frames together so the simulate loop
can hand one object to `apply_events`. Each kind frame's schema is
keyed by `(rollout_index, month_index, cause_id)` plus the kind-
specific columns.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

import polars as pl

from finance.augur.frames import FrameSpec

TRANSFER_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "from_agent_id": pl.Utf8(),
        "from_account_id": pl.Utf8(),
        "to_agent_id": pl.Utf8(),
        "to_account_id": pl.Utf8(),
        "amount_quanta": pl.Int64(),
        # Tax classification: when set (e.g. "ordinary" for W-2 wages),
        # apply_events increments the recipient's ordinary_income_ytd.
        # Null for non-income transfers (e.g. expense payments).
        "income_category": pl.Utf8(),
    }
)

# `TaxAccrual` records a year-end tax computation: a single
# year's ordinary income for one agent under one jurisdiction has
# been bracket-walked, and the resulting tax `amount_quanta` is now
# owed. apply_events appends a row to `state.tax_liabilities` and
# zeroes the agent's `ordinary_income_ytd` for the next year.
TAX_ACCRUAL_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "jurisdiction_id": pl.Utf8(),
        "tax_year_end_month": pl.Int64(),
        "amount_quanta": pl.Int64(),
    }
)

# `TaxBreakdown` records the inputs and component tax amounts behind
# each year-end accrual. It is audit/output only; `apply_events` does
# not mutate state from this frame.
TAX_BREAKDOWN_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "jurisdiction_id": pl.Utf8(),
        "tax_year_end_month": pl.Int64(),
        "ordinary_income_quanta": pl.Int64(),
        "ltcg_quanta": pl.Int64(),
        "stcg_quanta": pl.Int64(),
        "standard_deduction_quanta": pl.Int64(),
        # MID under this jurisdiction's principal cap, summed across the profile's qualifying
        # mortgages. Zero when no MortgageInterestDeductionPolicy applies (e.g. cash buy, owner
        # doesn't live in the property, or jurisdiction excluded from the policy's cap map).
        "mortgage_interest_deduction_quanta": pl.Int64(),
        # Federal SALT deduction allowed this year: property tax paid this calendar year + state
        # income tax accrued this year for the profile's non-federal jurisdictions, capped per
        # the federal SALT schedule. Zero on state-jurisdiction links (SALT is a federal-only
        # Schedule A concept) and on federal links without a FederalSaltDeductionPolicy.
        "salt_deduction_quanta": pl.Int64(),
        # Total itemized deductions used after comparing against the standard: MID + SALT today,
        # plus other Schedule A lines once we model them. Equals MID + SALT when itemized >
        # standard; equals standard otherwise.
        "itemized_deduction_quanta": pl.Int64(),
        "ordinary_taxable_quanta": pl.Int64(),
        "capital_gain_taxable_quanta": pl.Int64(),
        "ordinary_tax_quanta": pl.Int64(),
        "capital_gain_tax_quanta": pl.Int64(),
        "total_tax_quanta": pl.Int64(),
    }
)

# `TaxSettlement` applies paid tax dollars against already-accrued
# liabilities for an agent and tax year. Cash still moves through
# Transfer events; this frame is the liability-side settlement.
TAX_SETTLEMENT_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "tax_year_end_month": pl.Int64(),
        "amount_quanta": pl.Int64(),
    }
)

OBLIGATION_ACCRUAL_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "obligation_id": pl.Utf8(),
        "obligation_type": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "from_account_id": pl.Utf8(),
        "to_agent_id": pl.Utf8(),
        "to_account_id": pl.Utf8(),
        "amount_due_quanta": pl.Int64(),
    }
)

OBLIGATION_SETTLEMENT_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "obligation_id": pl.Utf8(),
        "obligation_type": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "from_account_id": pl.Utf8(),
        "amount_due_quanta": pl.Int64(),
        "amount_paid_quanta": pl.Int64(),
        "shortfall_quanta": pl.Int64(),
        "attempted_funding_sources": pl.Utf8(),
    }
)

PROPERTY_PURCHASE_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "property_id": pl.Utf8(),
        "location_id": pl.Utf8(),
        "buyer_agent_id": pl.Utf8(),
        "purchase_price_quanta": pl.Int64(),
        "closing_cost_quanta": pl.Int64(),
        "adjusted_basis_quanta": pl.Int64(),
        "stake_contribution_quanta": pl.Int64(),
        "equity_ledger_quanta": pl.Int64(),
    }
)

MORTGAGE_ORIGINATION_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "liability_id": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "payment_account_id": pl.Utf8(),
        "counterparty_agent_id": pl.Utf8(),
        "counterparty_account_id": pl.Utf8(),
        "property_id": pl.Utf8(),
        "principal_quanta": pl.Int64(),
        "annual_interest_rate": pl.Float64(),
        "term_months": pl.Int64(),
        "monthly_payment_quanta": pl.Int64(),
    }
)

MORTGAGE_PAYMENT_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "liability_id": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "counterparty_agent_id": pl.Utf8(),
        "property_id": pl.Utf8(),
        "from_account_id": pl.Utf8(),
        "to_account_id": pl.Utf8(),
        "interest_quanta": pl.Int64(),
        "principal_quanta": pl.Int64(),
        "total_payment_quanta": pl.Int64(),
    }
)

# `RolloutFailure` flags a rollout where a required due-now
# obligation could not be paid in full after liquidity policy sale
# decisions. Once flagged, the rollout stays failed for the rest of the
# sim and value-bearing state snapshots freeze at zero (L11.2).
ROLLOUT_FAILURE_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "deficit_quanta": pl.Int64(),
        "obligation_id": pl.Utf8(),
        "obligation_type": pl.Utf8(),
        "amount_due_quanta": pl.Int64(),
        "amount_paid_quanta": pl.Int64(),
        "shortfall_quanta": pl.Int64(),
        "attempted_funding_sources": pl.Utf8(),
    }
)

# `LotDisposition` records the consumption of part (or all) of one
# lot by one logical sale. A single AssetSale "sell N units of vti"
# decomposes into one disposition row per lot the sale ate into;
# `cause_id` groups all dispositions of the same sale for downstream
# tax classification. Holding period for LTCG/STCG is
# `month_index - purchase_month_index`.
LOT_DISPOSITION_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "agent_id": pl.Utf8(),
        "source_account_id": pl.Utf8(),
        "asset_id": pl.Utf8(),
        "lot_id": pl.Utf8(),
        "purchase_month_index": pl.Int64(),
        "units_sold": pl.Float64(),
        "cost_basis_consumed_quanta": pl.Int64(),
        "proceeds_quanta": pl.Int64(),
        "proceeds_account_id": pl.Utf8(),
    }
)


SET_RENTED_FRACTION_EVENT_SCHEMA = pl.Schema(
    {"rollout_index": pl.Int64(), "month_index": pl.Int64(), "property_id": pl.Utf8(), "rented_fraction": pl.Float64()}
)

SET_PRIMARY_RESIDENCE_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "agent_id": pl.Utf8(),
        "property_id": pl.Utf8(),
        "is_primary_residence": pl.Boolean(),
    }
)

CAPITAL_IMPROVEMENT_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "property_id": pl.Utf8(),
        "amount_quanta": pl.Int64(),
        "description": pl.Utf8(),
    }
)

PROPERTY_SALE_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "property_id": pl.Utf8(),
        "gross_proceeds_quanta": pl.Int64(),
        "mortgage_payoff_quanta": pl.Int64(),
        "net_cash_to_owner_quanta": pl.Int64(),
        "realized_gain_quanta": pl.Int64(),
        "depreciation_recapture_quanta": pl.Int64(),
        "section_121_exclusion_quanta": pl.Int64(),
        "long_term_capital_gain_quanta": pl.Int64(),
    }
)

PRIVATE_EQUITY_EVENT_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "issuer_id": pl.Utf8(),
        "asset_id": pl.Utf8(),
        "event_kind": pl.Utf8(),
        "regime": pl.Utf8(),
        "mark_quanta": pl.Int64(),
        "sale_capacity_fraction": pl.Float64(),
        "eligible_fraction": pl.Float64(),
        "forced_sale_fraction": pl.Float64(),
        "liquidity_blocked": pl.Boolean(),
        "forced_recovery_cashout_quanta": pl.Int64(),
    }
)

PRIVATE_EQUITY_OPPORTUNITY_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "cause_id": pl.Utf8(),
        "issuer_id": pl.Utf8(),
        "asset_id": pl.Utf8(),
        "event_kind": pl.Utf8(),
        "regime": pl.Utf8(),
        "outcome": pl.Utf8(),
        "mark_quanta": pl.Int64(),
        "sale_capacity_fraction": pl.Float64(),
        "eligible_fraction": pl.Float64(),
        "liquidity_blocked": pl.Boolean(),
        "floor_quanta": pl.Int64(),
        "liquid_net_worth_quanta": pl.Int64(),
        "shortfall_quanta": pl.Int64(),
        "units_held": pl.Float64(),
        "sellable_units": pl.Float64(),
        "target_units": pl.Float64(),
        "proceeds_quanta": pl.Int64(),
    }
)


@dataclass(frozen=True)
class EventFrameCatalog:
    """Schemas for every frame carried by `EventLog`."""

    transfers: FrameSpec
    lot_dispositions: FrameSpec
    tax_accruals: FrameSpec
    tax_breakdowns: FrameSpec
    tax_settlements: FrameSpec
    obligation_accruals: FrameSpec
    obligation_settlements: FrameSpec
    property_purchases: FrameSpec
    mortgage_originations: FrameSpec
    mortgage_payments: FrameSpec
    rollout_failures: FrameSpec
    set_rented_fraction_events: FrameSpec
    set_primary_residence_events: FrameSpec
    capital_improvement_events: FrameSpec
    property_sale_events: FrameSpec
    private_equity_events: FrameSpec
    private_equity_opportunities: FrameSpec

    def ordered(self) -> tuple[FrameSpec, ...]:
        return (
            self.transfers,
            self.lot_dispositions,
            self.tax_accruals,
            self.tax_breakdowns,
            self.tax_settlements,
            self.obligation_accruals,
            self.obligation_settlements,
            self.property_purchases,
            self.mortgage_originations,
            self.mortgage_payments,
            self.rollout_failures,
            self.set_rented_fraction_events,
            self.set_primary_residence_events,
            self.capital_improvement_events,
            self.property_sale_events,
            self.private_equity_events,
            self.private_equity_opportunities,
        )


EVENT_FRAMES = EventFrameCatalog(
    transfers=FrameSpec("transfers", TRANSFER_EVENT_SCHEMA),
    lot_dispositions=FrameSpec("lot_dispositions", LOT_DISPOSITION_EVENT_SCHEMA),
    tax_accruals=FrameSpec("tax_accruals", TAX_ACCRUAL_EVENT_SCHEMA),
    tax_breakdowns=FrameSpec("tax_breakdowns", TAX_BREAKDOWN_EVENT_SCHEMA),
    tax_settlements=FrameSpec("tax_settlements", TAX_SETTLEMENT_EVENT_SCHEMA),
    obligation_accruals=FrameSpec("obligation_accruals", OBLIGATION_ACCRUAL_EVENT_SCHEMA),
    obligation_settlements=FrameSpec("obligation_settlements", OBLIGATION_SETTLEMENT_EVENT_SCHEMA),
    property_purchases=FrameSpec("property_purchases", PROPERTY_PURCHASE_EVENT_SCHEMA),
    mortgage_originations=FrameSpec("mortgage_originations", MORTGAGE_ORIGINATION_EVENT_SCHEMA),
    mortgage_payments=FrameSpec("mortgage_payments", MORTGAGE_PAYMENT_EVENT_SCHEMA),
    rollout_failures=FrameSpec("rollout_failures", ROLLOUT_FAILURE_EVENT_SCHEMA),
    set_rented_fraction_events=FrameSpec("set_rented_fraction_events", SET_RENTED_FRACTION_EVENT_SCHEMA),
    set_primary_residence_events=FrameSpec("set_primary_residence_events", SET_PRIMARY_RESIDENCE_EVENT_SCHEMA),
    capital_improvement_events=FrameSpec("capital_improvement_events", CAPITAL_IMPROVEMENT_EVENT_SCHEMA),
    property_sale_events=FrameSpec("property_sale_events", PROPERTY_SALE_EVENT_SCHEMA),
    private_equity_events=FrameSpec("private_equity_events", PRIVATE_EQUITY_EVENT_SCHEMA),
    private_equity_opportunities=FrameSpec("private_equity_opportunities", PRIVATE_EQUITY_OPPORTUNITY_SCHEMA),
)

EVENT_FRAME_SPECS = EVENT_FRAMES.ordered()


@dataclass(frozen=True)
class EventLog:
    """Per-step or per-simulation collection of events, one frame
    per event kind."""

    _frames: Mapping[str, pl.DataFrame]

    @classmethod
    def empty(cls) -> EventLog:
        return cls.from_frames({})

    @classmethod
    def from_frames(cls, frames: Mapping[str, pl.DataFrame]) -> EventLog:
        unknown = set(frames) - {spec.name for spec in EVENT_FRAME_SPECS}
        if unknown:
            unknown_list = ", ".join(sorted(unknown))
            msg = f"Unknown event frame(s): {unknown_list}"
            raise ValueError(msg)
        by_name = {
            spec.name: spec.normalize(frames[spec.name]) if spec.name in frames else spec.empty()
            for spec in EVENT_FRAME_SPECS
        }
        return cls(MappingProxyType(by_name))

    @classmethod
    def concat(cls, logs: Iterable[EventLog]) -> EventLog:
        logs_tuple = tuple(logs)
        return cls.from_frames(
            {spec.name: spec.concat(log.frame(spec) for log in logs_tuple) for spec in EVENT_FRAME_SPECS}
        )

    def at_month(self, month: int) -> EventLog:
        return self.from_frames(
            {spec.name: self.frame(spec).filter(pl.col("month_index") == month) for spec in EVENT_FRAME_SPECS}
        )

    def frame(self, spec: FrameSpec) -> pl.DataFrame:
        return self._frames[spec.name]

    @property
    def transfers(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.transfers)

    @property
    def lot_dispositions(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.lot_dispositions)

    @property
    def tax_accruals(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.tax_accruals)

    @property
    def tax_breakdowns(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.tax_breakdowns)

    @property
    def tax_settlements(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.tax_settlements)

    @property
    def obligation_accruals(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.obligation_accruals)

    @property
    def obligation_settlements(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.obligation_settlements)

    @property
    def property_purchases(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.property_purchases)

    @property
    def mortgage_originations(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.mortgage_originations)

    @property
    def mortgage_payments(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.mortgage_payments)

    @property
    def rollout_failures(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.rollout_failures)

    @property
    def set_rented_fraction_events(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.set_rented_fraction_events)

    @property
    def set_primary_residence_events(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.set_primary_residence_events)

    @property
    def capital_improvement_events(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.capital_improvement_events)

    @property
    def property_sale_events(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.property_sale_events)

    @property
    def private_equity_events(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.private_equity_events)

    @property
    def private_equity_opportunities(self) -> pl.DataFrame:
        return self.frame(EVENT_FRAMES.private_equity_opportunities)
