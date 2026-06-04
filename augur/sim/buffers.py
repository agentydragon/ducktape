"""Engine state buffers + their plan-time shape validation.

This module owns every dataclass that holds simulation arrays:

- `CurrentStateBuffers`: per-step mutable state read+written by the run-loop phases.
- `StateHistoryBuffers`: snapshots of the above at month boundaries (read by the codec
  decoders to produce state-history frames).
- The per-domain `*EventBuffers` (Transfer, Property, LotDisposition, Tax, Obligation,
  Lifecycle): per-event-month sparse arrays of what actually fired.
- `SimulationBuffers`: bundles all of the above so the run-loop can carry one object
  through `_run_month_step`.

Lives at the top level of `augur.sim` (not under `engine/` or `codec/`) because both
sides of the encoder/decoder pair need to share it as a stable data interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from augur.sim.compiler import CompiledSimulation, SlotPlan
from augur.sim.enums import PrivateEquityDispositionKind


def _expect_array(name: str, array: np.ndarray, *, shape: tuple[int, ...], dtype: Any) -> None:
    if array.shape != shape:
        raise ValueError(f"{name} shape {array.shape} != expected {shape}")
    if array.dtype != np.dtype(dtype):
        raise ValueError(f"{name} dtype {array.dtype} != expected {np.dtype(dtype)}")


@dataclass
class StateHistoryBuffers:
    cash_state: NDArray[np.float64]
    lot_state: NDArray[np.float64]
    ordinary_state: NDArray[np.float64]
    capital_gain_active_state: NDArray[np.bool_]
    capital_gain_state: NDArray[np.float64]
    property_active_state: NDArray[np.bool_]
    property_basis_state: NDArray[np.float64]
    property_ownership_state: NDArray[np.float64]
    property_contribution_state: NDArray[np.float64]
    property_equity_state: NDArray[np.float64]
    liability_active_state: NDArray[np.bool_]
    liability_principal_state: NDArray[np.float64]
    liability_monthly_payment_state: NDArray[np.float64]
    liability_interest_ytd_state: NDArray[np.float64]
    liability_principal_ytd_state: NDArray[np.float64]
    # Cumulative §168 depreciation USD per (snapshot_month, rollout, property). Monotone
    # non-decreasing; accrues monthly while a property has rented_fraction > 0. Used at sale
    # time for §1250 unrecaptured-depreciation recapture (phase 4) and at year-end for the
    # Schedule E depreciation deduction (the YTD slice is computed from the delta between
    # consecutive snapshots).
    property_cumulative_depreciation_state: NDArray[np.float64]
    # Cumulative count of owner-occupied months per (snapshot_month, rollout, property). Used
    # at sale time to compute the §121 24-of-last-60-months test by subtracting the 60-mo-ago
    # snapshot from the current cumulative count.
    property_owner_occupied_months_state: NDArray[np.int64]
    rollout_failed_state: NDArray[np.bool_]
    rollout_failed_month_state: NDArray[np.int64]

    def validate(self, plan: SlotPlan) -> None:
        s = plan.snapshot_months
        r = plan.rollout_count
        _expect_array("cash_state", self.cash_state, shape=(s, plan.cash_count, r), dtype=np.float64)
        _expect_array("lot_state", self.lot_state, shape=(s, plan.lot_count, r), dtype=np.float64)
        _expect_array("ordinary_state", self.ordinary_state, shape=(s, plan.tax_profile_count, r), dtype=np.float64)
        _expect_array(
            "capital_gain_active_state",
            self.capital_gain_active_state,
            shape=(s, plan.capital_gain_agent_count, 2, r),
            dtype=np.bool_,
        )
        _expect_array(
            "capital_gain_state",
            self.capital_gain_state,
            shape=(s, plan.capital_gain_agent_count, 2, r),
            dtype=np.float64,
        )
        _expect_array(
            "property_active_state", self.property_active_state, shape=(s, plan.property_count, r), dtype=np.bool_
        )
        _expect_array(
            "property_basis_state", self.property_basis_state, shape=(s, plan.property_count, r), dtype=np.float64
        )
        _expect_array(
            "property_ownership_state",
            self.property_ownership_state,
            shape=(s, plan.property_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "property_contribution_state",
            self.property_contribution_state,
            shape=(s, plan.property_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "property_equity_state", self.property_equity_state, shape=(s, plan.property_count, r), dtype=np.float64
        )
        _expect_array(
            "liability_active_state", self.liability_active_state, shape=(s, plan.liability_count, r), dtype=np.bool_
        )
        _expect_array(
            "liability_principal_state",
            self.liability_principal_state,
            shape=(s, plan.liability_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "liability_monthly_payment_state",
            self.liability_monthly_payment_state,
            shape=(s, plan.liability_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "liability_interest_ytd_state",
            self.liability_interest_ytd_state,
            shape=(s, plan.liability_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "liability_principal_ytd_state",
            self.liability_principal_ytd_state,
            shape=(s, plan.liability_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "property_cumulative_depreciation_state",
            self.property_cumulative_depreciation_state,
            shape=(s, plan.property_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "property_owner_occupied_months_state",
            self.property_owner_occupied_months_state,
            shape=(s, plan.property_count, r),
            dtype=np.int64,
        )
        _expect_array("rollout_failed_state", self.rollout_failed_state, shape=(s, r), dtype=np.bool_)
        _expect_array("rollout_failed_month_state", self.rollout_failed_month_state, shape=(s, r), dtype=np.int64)


@dataclass
class CurrentStateBuffers:
    cash: NDArray[np.float64]
    lot_remaining: NDArray[np.float64]
    ordinary_ytd: NDArray[np.float64]
    capital_gain_active: NDArray[np.bool_]
    capital_gain_ytd: NDArray[np.float64]
    # Pooled unused capital-loss carryforward per (capital-gain agent, rollout), >= 0. Updated at
    # year-end by the §1211/§1212 netting: this year's net capital loss, less the portion applied
    # against ordinary income ($3k cap), carries here into future years. Unlike the YTD gain
    # buffers it is NOT zeroed at year-end — it persists across tax years by design.
    capital_loss_carryforward: NDArray[np.float64]
    # Cumulative reduced-form TLH harvested loss per (harvest policy, rollout), >= 0 (Piece 2b).
    # Each month `_apply_tlh_harvest` adds the loss it booked into `capital_gain_ytd` here; the
    # total lowers the policy holding's adjusted basis (so `e` rises and yield ossifies) and is the
    # exact amount GIVEN BACK as extra realized gain when the policy's lots are sold. Like the
    # carryforward it persists across years (it is NOT zeroed at year-end) — it is reset only on
    # rollout failure, mirroring `capital_loss_carryforward`. Clamped so adjusted_basis stays >= 0.
    tlh_cumulative_harvest: NDArray[np.float64]
    tax_liability_active: NDArray[np.bool_]
    tax_liability_amount: NDArray[np.float64]
    property_active: NDArray[np.bool_]
    property_basis: NDArray[np.float64]
    property_ownership: NDArray[np.float64]
    property_contribution: NDArray[np.float64]
    property_equity: NDArray[np.float64]
    liability_active: NDArray[np.bool_]
    liability_principal: NDArray[np.float64]
    liability_monthly_payment: NDArray[np.float64]
    liability_interest_ytd: NDArray[np.float64]
    liability_principal_ytd: NDArray[np.float64]
    # Property-tax USD paid this calendar year, per (rollout, profile). Property-tax obligation
    # settlements add to this so the federal SALT pass at year-end can read accumulated SALT.
    # Zeroed in the year-end accrual after federal SALT has been consumed.
    property_tax_ytd: NDArray[np.float64]
    # Cumulative §168 depreciation per (rollout, property). Monotone non-decreasing; accrues
    # monthly while rented_fraction > 0. Used for Schedule E deduction (delta-vs-prior-year-end)
    # and §1250 recapture at sale (phase 4).
    property_cumulative_depreciation: NDArray[np.float64]
    # YTD depreciation accrued this calendar year per (rollout, property). Used at year-end to
    # deduct Schedule E depreciation from the owner's ordinary_ytd; zeroed after.
    property_depreciation_ytd: NDArray[np.float64]
    # Runtime rented_fraction per (rollout, property) (0..1). Initialized at scenario start from
    # `plan.property_rented_fraction[prop]` and mutated by `_apply_lifecycle_events` when
    # PropertyLifecycleEvent rows fire mid-horizon. Depreciation accrual, MID computation, and
    # Schedule E rental interest all read this each month.
    property_rented_fraction: NDArray[np.float64]
    # Runtime depreciable building basis per (rollout, property). Initialized from
    # `plan.property_building_basis[prop]` and bumped by `CapitalImprovementEvent`. Depreciation
    # accrual multiplies this by `current.property_rented_fraction[p, r] / (27.5 × 12)` monthly.
    property_building_basis: NDArray[np.float64]
    # Cumulative count of §121 qualifying-use months per (rollout, property). Increments by 1
    # each month while the property is active, assigned as the owning agent's primary residence,
    # and not fully rented. At sale time the engine looks back 60 months by subtracting the
    # 60-mo-ago snapshot — qualifies for §121 if the difference is ≥ 24.
    property_owner_occupied_months: NDArray[np.int64]
    # Current primary-residence assignment per agent. Value is a property slot or NO_CODE when
    # the agent has no assigned primary residence. This is agent-scoped so the runtime cannot
    # represent two simultaneous primary residences for one agent.
    agent_primary_residence_property: NDArray[np.int64]
    # YTD §1250 unrecaptured-depreciation gain per (rollout, tax_profile). Populated by
    # PropertySaleEvent. At year-end, federal taxes this at min(25%, marginal); CA taxes as
    # ordinary (added back to bracket input). Zeroed at year-end.
    recapture_section_1250_ytd: NDArray[np.float64]
    # Rented-share of YTD mortgage interest per (rollout, liability). Each mortgage payment
    # accrues `interest × current.property_rented_fraction[prop_of_lia, r]` into this buffer.
    # At year-end:
    #   MID owner-share interest = liability_interest_ytd - liability_rental_interest_ytd
    #   Schedule E rental interest = liability_rental_interest_ytd (deducted from ordinary_ytd).
    # Reset annually.
    liability_rental_interest_ytd: NDArray[np.float64]
    failed: NDArray[np.bool_]
    failed_month: NDArray[np.int64]

    def validate(self, plan: SlotPlan) -> None:
        r = plan.rollout_count
        _expect_array("current cash", self.cash, shape=(plan.cash_count, r), dtype=np.float64)
        _expect_array("current lot_remaining", self.lot_remaining, shape=(plan.lot_count, r), dtype=np.float64)
        _expect_array("current ordinary_ytd", self.ordinary_ytd, shape=(plan.tax_profile_count, r), dtype=np.float64)
        _expect_array(
            "current capital_gain_active",
            self.capital_gain_active,
            shape=(plan.capital_gain_agent_count, 2, r),
            dtype=np.bool_,
        )
        _expect_array(
            "current capital_gain_ytd",
            self.capital_gain_ytd,
            shape=(plan.capital_gain_agent_count, 2, r),
            dtype=np.float64,
        )
        _expect_array(
            "current capital_loss_carryforward",
            self.capital_loss_carryforward,
            shape=(plan.capital_gain_agent_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "current tlh_cumulative_harvest",
            self.tlh_cumulative_harvest,
            shape=(plan.harvest_policy_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "current tax_liability_active",
            self.tax_liability_active,
            shape=(plan.tax_liability_count, r),
            dtype=np.bool_,
        )
        _expect_array(
            "current tax_liability_amount",
            self.tax_liability_amount,
            shape=(plan.tax_liability_count, r),
            dtype=np.float64,
        )
        _expect_array("current property_active", self.property_active, shape=(plan.property_count, r), dtype=np.bool_)
        _expect_array("current property_basis", self.property_basis, shape=(plan.property_count, r), dtype=np.float64)
        _expect_array(
            "current property_ownership", self.property_ownership, shape=(plan.property_count, r), dtype=np.float64
        )
        _expect_array(
            "current property_contribution",
            self.property_contribution,
            shape=(plan.property_count, r),
            dtype=np.float64,
        )
        _expect_array("current property_equity", self.property_equity, shape=(plan.property_count, r), dtype=np.float64)
        _expect_array(
            "current liability_active", self.liability_active, shape=(plan.liability_count, r), dtype=np.bool_
        )
        _expect_array(
            "current liability_principal", self.liability_principal, shape=(plan.liability_count, r), dtype=np.float64
        )
        _expect_array(
            "current liability_monthly_payment",
            self.liability_monthly_payment,
            shape=(plan.liability_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "current liability_interest_ytd",
            self.liability_interest_ytd,
            shape=(plan.liability_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "current liability_principal_ytd",
            self.liability_principal_ytd,
            shape=(plan.liability_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "current property_tax_ytd", self.property_tax_ytd, shape=(plan.tax_profile_count, r), dtype=np.float64
        )
        _expect_array(
            "current property_cumulative_depreciation",
            self.property_cumulative_depreciation,
            shape=(plan.property_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "current property_depreciation_ytd",
            self.property_depreciation_ytd,
            shape=(plan.property_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "current property_rented_fraction",
            self.property_rented_fraction,
            shape=(plan.property_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "current property_building_basis",
            self.property_building_basis,
            shape=(plan.property_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "current property_owner_occupied_months",
            self.property_owner_occupied_months,
            shape=(plan.property_count, r),
            dtype=np.int64,
        )
        _expect_array(
            "current agent_primary_residence_property",
            self.agent_primary_residence_property,
            shape=(plan.agent_count,),
            dtype=np.int64,
        )
        _expect_array(
            "current recapture_section_1250_ytd",
            self.recapture_section_1250_ytd,
            shape=(plan.tax_profile_count, r),
            dtype=np.float64,
        )
        _expect_array(
            "current liability_rental_interest_ytd",
            self.liability_rental_interest_ytd,
            shape=(plan.liability_count, r),
            dtype=np.float64,
        )
        _expect_array("current failed", self.failed, shape=(r,), dtype=np.bool_)
        _expect_array("current failed_month", self.failed_month, shape=(r,), dtype=np.int64)


@dataclass
class LifecycleEventBuffers:
    """Per-(lifecycle_event_index, rollout) tracking for deterministic lifecycle events.

    `fired[e, r]` = True iff event `e` fired on rollout `r` (i.e., the rollout was not failed
    when the event month arrived). Sale events also populate the per-amount arrays at the
    moment of the sale; for non-sale kinds those arrays stay zero.
    """

    fired: NDArray[np.bool_]
    sale_gross_proceeds: NDArray[np.float64]
    sale_mortgage_payoff: NDArray[np.float64]
    sale_net_cash: NDArray[np.float64]
    sale_realized_gain: NDArray[np.float64]
    sale_recapture: NDArray[np.float64]
    sale_section_121_exclusion: NDArray[np.float64]
    sale_long_term_gain: NDArray[np.float64]

    def validate(self, plan: SlotPlan, event_count: int) -> None:
        shape = (max(1, event_count), plan.rollout_count)
        _expect_array("lifecycle_fired", self.fired, shape=shape, dtype=np.bool_)
        for name, arr in [
            ("sale_gross_proceeds", self.sale_gross_proceeds),
            ("sale_mortgage_payoff", self.sale_mortgage_payoff),
            ("sale_net_cash", self.sale_net_cash),
            ("sale_realized_gain", self.sale_realized_gain),
            ("sale_recapture", self.sale_recapture),
            ("sale_section_121_exclusion", self.sale_section_121_exclusion),
            ("sale_long_term_gain", self.sale_long_term_gain),
        ]:
            _expect_array(name, arr, shape=shape, dtype=np.float64)


@dataclass
class PrimaryResidenceEventBuffers:
    """Per-(primary_residence_event_index, rollout) fired markers."""

    fired: NDArray[np.bool_]

    def validate(self, plan: SlotPlan, event_count: int) -> None:
        _expect_array(
            "primary_residence_fired", self.fired, shape=(max(1, event_count), plan.rollout_count), dtype=np.bool_
        )


@dataclass
class TransferEventBuffers:
    active: NDArray[np.bool_]
    amount: NDArray[np.float64]

    def validate(self, plan: SlotPlan) -> None:
        shape = (plan.event_months, plan.max_transfer_slots, plan.rollout_count)
        _expect_array("active", self.active, shape=shape, dtype=np.bool_)
        _expect_array("amount", self.amount, shape=shape, dtype=np.float64)


@dataclass
class PropertyEventBuffers:
    transfer_active: NDArray[np.bool_]
    purchase_active: NDArray[np.bool_]
    mortgage_origination_active: NDArray[np.bool_]
    mortgage_payment_active: NDArray[np.bool_]
    mortgage_payment_interest: NDArray[np.float64]
    mortgage_payment_principal: NDArray[np.float64]
    mortgage_payment_total: NDArray[np.float64]

    def validate(self, plan: SlotPlan) -> None:
        h = plan.event_months
        r = plan.rollout_count
        property_shape = (h, plan.property_count, r)
        liability_event_shape = (h, max(1, plan.liability_count), r)
        _expect_array("transfer_active", self.transfer_active, shape=property_shape, dtype=np.bool_)
        _expect_array("purchase_active", self.purchase_active, shape=property_shape, dtype=np.bool_)
        _expect_array(
            "mortgage_origination_active", self.mortgage_origination_active, shape=liability_event_shape, dtype=np.bool_
        )
        _expect_array(
            "mortgage_payment_active", self.mortgage_payment_active, shape=liability_event_shape, dtype=np.bool_
        )
        _expect_array(
            "mortgage_payment_interest", self.mortgage_payment_interest, shape=liability_event_shape, dtype=np.float64
        )
        _expect_array(
            "mortgage_payment_principal", self.mortgage_payment_principal, shape=liability_event_shape, dtype=np.float64
        )
        _expect_array(
            "mortgage_payment_total", self.mortgage_payment_total, shape=liability_event_shape, dtype=np.float64
        )


@dataclass(frozen=True)
class DispositionGroup:
    active: NDArray[np.bool_]
    units: NDArray[np.float64]
    basis: NDArray[np.float64]
    proceeds: NDArray[np.float64]

    def validate(self, name: str, shape: tuple[int, ...]) -> None:
        _expect_array(f"{name}.active", self.active, shape=shape, dtype=np.bool_)
        _expect_array(f"{name}.units", self.units, shape=shape, dtype=np.float64)
        _expect_array(f"{name}.basis", self.basis, shape=shape, dtype=np.float64)
        _expect_array(f"{name}.proceeds", self.proceeds, shape=shape, dtype=np.float64)


@dataclass
class LotDispositionEventBuffers:
    scheduled: DispositionGroup
    liquidity: DispositionGroup
    pe: DispositionGroup

    def validate(self, plan: SlotPlan) -> None:
        h = plan.event_months
        r = plan.rollout_count
        lot_axis = max(1, plan.lot_count)
        self.scheduled.validate("scheduled", (plan.scheduled_sale_count, lot_axis, r))
        self.liquidity.validate(
            "liquidity", (h, plan.liquidity_policy_count, plan.max_liquidity_policy_assets, lot_axis, r)
        )
        self.pe.validate("pe", (h, plan.pe_issuer_count, len(PrivateEquityDispositionKind), lot_axis, r))


@dataclass
class PrivateEquityOpportunityEventBuffers:
    active: NDArray[np.bool_]
    outcome: NDArray[np.int64]
    floor: NDArray[np.float64]
    liquid_net_worth: NDArray[np.float64]
    shortfall: NDArray[np.float64]
    units_held: NDArray[np.float64]
    sellable_units: NDArray[np.float64]
    target_units: NDArray[np.float64]
    proceeds: NDArray[np.float64]

    def validate(self, plan: SlotPlan) -> None:
        shape = (plan.event_months, plan.pe_issuer_count, plan.rollout_count)
        _expect_array("pe_opportunity.active", self.active, shape=shape, dtype=np.bool_)
        _expect_array("pe_opportunity.outcome", self.outcome, shape=shape, dtype=np.int64)
        _expect_array("pe_opportunity.floor", self.floor, shape=shape, dtype=np.float64)
        _expect_array("pe_opportunity.liquid_net_worth", self.liquid_net_worth, shape=shape, dtype=np.float64)
        _expect_array("pe_opportunity.shortfall", self.shortfall, shape=shape, dtype=np.float64)
        _expect_array("pe_opportunity.units_held", self.units_held, shape=shape, dtype=np.float64)
        _expect_array("pe_opportunity.sellable_units", self.sellable_units, shape=shape, dtype=np.float64)
        _expect_array("pe_opportunity.target_units", self.target_units, shape=shape, dtype=np.float64)
        _expect_array("pe_opportunity.proceeds", self.proceeds, shape=shape, dtype=np.float64)


@dataclass
class TaxEventBuffers:
    accrual_active: NDArray[np.bool_]
    accrual_amount: NDArray[np.float64]
    breakdown_ordinary: NDArray[np.float64]
    breakdown_ltcg: NDArray[np.float64]
    breakdown_stcg: NDArray[np.float64]
    breakdown_standard_deduction: NDArray[np.float64]
    breakdown_mortgage_interest_deduction: NDArray[np.float64]
    breakdown_salt_deduction: NDArray[np.float64]
    breakdown_itemized_deduction: NDArray[np.float64]
    breakdown_ordinary_taxable: NDArray[np.float64]
    breakdown_capital_taxable: NDArray[np.float64]
    breakdown_ordinary_tax: NDArray[np.float64]
    breakdown_capital_tax: NDArray[np.float64]
    settlement_active: NDArray[np.bool_]
    settlement_amount: NDArray[np.float64]
    settlement_year_end_month: NDArray[np.int64]

    def validate(self, plan: SlotPlan) -> None:
        h = plan.event_months
        r = plan.rollout_count
        link_shape = (h, plan.tax_link_count, r)
        settlement_shape = (h, plan.max_tax_settlement_slots, r)
        _expect_array("accrual_active", self.accrual_active, shape=link_shape, dtype=np.bool_)
        _expect_array("accrual_amount", self.accrual_amount, shape=link_shape, dtype=np.float64)
        _expect_array("breakdown_ordinary", self.breakdown_ordinary, shape=link_shape, dtype=np.float64)
        _expect_array("breakdown_ltcg", self.breakdown_ltcg, shape=link_shape, dtype=np.float64)
        _expect_array("breakdown_stcg", self.breakdown_stcg, shape=link_shape, dtype=np.float64)
        _expect_array(
            "breakdown_standard_deduction", self.breakdown_standard_deduction, shape=link_shape, dtype=np.float64
        )
        _expect_array(
            "breakdown_mortgage_interest_deduction",
            self.breakdown_mortgage_interest_deduction,
            shape=link_shape,
            dtype=np.float64,
        )
        _expect_array("breakdown_salt_deduction", self.breakdown_salt_deduction, shape=link_shape, dtype=np.float64)
        _expect_array(
            "breakdown_itemized_deduction", self.breakdown_itemized_deduction, shape=link_shape, dtype=np.float64
        )
        _expect_array("breakdown_ordinary_taxable", self.breakdown_ordinary_taxable, shape=link_shape, dtype=np.float64)
        _expect_array("breakdown_capital_taxable", self.breakdown_capital_taxable, shape=link_shape, dtype=np.float64)
        _expect_array("breakdown_ordinary_tax", self.breakdown_ordinary_tax, shape=link_shape, dtype=np.float64)
        _expect_array("breakdown_capital_tax", self.breakdown_capital_tax, shape=link_shape, dtype=np.float64)
        _expect_array("settlement_active", self.settlement_active, shape=settlement_shape, dtype=np.bool_)
        _expect_array("settlement_amount", self.settlement_amount, shape=settlement_shape, dtype=np.float64)
        _expect_array(
            "settlement_year_end_month", self.settlement_year_end_month, shape=settlement_shape, dtype=np.int64
        )


@dataclass
class ObligationEventBuffers:
    active: NDArray[np.bool_]
    due: NDArray[np.float64]
    paid: NDArray[np.float64]
    shortfall: NDArray[np.float64]
    attempt_policy: NDArray[np.int64]
    failure_active: NDArray[np.bool_]

    def validate(self, plan: SlotPlan) -> None:
        shape = (plan.event_months, plan.max_obligation_slots, plan.rollout_count)
        _expect_array("active", self.active, shape=shape, dtype=np.bool_)
        _expect_array("due", self.due, shape=shape, dtype=np.float64)
        _expect_array("paid", self.paid, shape=shape, dtype=np.float64)
        _expect_array("shortfall", self.shortfall, shape=shape, dtype=np.float64)
        _expect_array("attempt_policy", self.attempt_policy, shape=shape, dtype=np.int64)
        _expect_array("failure_active", self.failure_active, shape=shape, dtype=np.bool_)


@dataclass
class TaxLiabilityChange:
    """One year-tax-liability balance-change event, captured at the month it occurred.

    `slots` indexes `plan.tax_liabilities`; `amount[k, r]` is the post-change balance and
    `active[k, r]` whether the liability exists for rollout `r` (failed rollouts never accrue
    one). Decode emits one output row per active `(slot, rollout)`.
    """

    snapshot_month: int
    slots: NDArray[np.int64]
    amount: NDArray[np.float64]
    active: NDArray[np.bool_]


@dataclass
class TaxLiabilityChangeLog:
    """Sparse replacement for the old dense `(snapshot, year_slots, R)` tax-liability state
    history (which was O(horizon² × R) since year-slots grow with the horizon). Year-end
    accrual and each settlement append one change; the per-month outstanding balance is
    piecewise-constant between changes, so the change list reconstructs the output frame."""

    changes: list[TaxLiabilityChange] = field(default_factory=list)

    def record(self, *, snapshot_month: int, slots: np.ndarray, amount: np.ndarray, active: np.ndarray) -> None:
        """Record the post-change balance of `slots` (read from the current-state arrays)."""
        if slots.size == 0:
            return
        self.changes.append(
            TaxLiabilityChange(
                snapshot_month=snapshot_month,
                slots=slots.astype(np.int64, copy=True),
                amount=amount[slots, :].copy(),
                active=active[slots, :].copy(),
            )
        )


@dataclass
class SimulationBuffers:
    state: StateHistoryBuffers
    transfers: TransferEventBuffers
    properties: PropertyEventBuffers
    lot_dispositions: LotDispositionEventBuffers
    private_equity_opportunities: PrivateEquityOpportunityEventBuffers
    taxes: TaxEventBuffers
    obligations: ObligationEventBuffers
    primary_residence: PrimaryResidenceEventBuffers
    lifecycle: LifecycleEventBuffers
    tax_liability_changes: TaxLiabilityChangeLog

    def validate(self, plan: CompiledSimulation) -> None:
        slot_plan = plan.slot_plan
        if slot_plan.event_months != plan.horizon_months:
            raise ValueError("slot plan event months do not match compiled horizon")
        if slot_plan.rollout_count != plan.rollout_count:
            raise ValueError("slot plan rollout count does not match compiled rollout count")
        self.state.validate(slot_plan)
        self.transfers.validate(slot_plan)
        self.properties.validate(slot_plan)
        self.lot_dispositions.validate(slot_plan)
        self.private_equity_opportunities.validate(slot_plan)
        self.taxes.validate(slot_plan)
        self.obligations.validate(slot_plan)
        self.primary_residence.validate(slot_plan, event_count=int(plan.primary_residence_events.month.shape[0]))
        self.lifecycle.validate(slot_plan, event_count=int(plan.lifecycle_events.month.shape[0]))
