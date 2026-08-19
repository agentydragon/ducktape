"""Static host-side data types shared by the JAX simulation engine modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import jax

type _Array = jax.Array


class _StateOutput(NamedTuple):
    cash: _Array
    ordinary: _Array
    lots: _Array
    capital_gain_active: _Array
    capital_gain_ytd: _Array
    property_active: _Array
    property_basis: _Array
    property_contribution: _Array
    property_equity: _Array
    property_cumulative_depreciation: _Array
    property_owner_occupied_months: _Array
    liability_active: _Array
    liability_principal: _Array
    liability_monthly_payment: _Array
    liability_interest_ytd: _Array
    liability_principal_ytd: _Array
    failed: _Array
    failed_month: _Array


class _TransferOutput(NamedTuple):
    active: _Array
    amount: _Array


class _ObligationOutput(NamedTuple):
    active: _Array
    due: _Array
    paid: _Array
    shortfall: _Array
    failure_active: _Array


class _PropertyPurchaseOutput(NamedTuple):
    active: _Array
    transfer_active: _Array


class _MortgageOutput(NamedTuple):
    origination_active: _Array
    payment_active: _Array
    payment_interest: _Array
    payment_principal: _Array
    payment_total: _Array


class _TaxOutput(NamedTuple):
    accrual_active: _Array
    accrual_amount: _Array
    ordinary_income: _Array
    long_term_capital_gain: _Array
    short_term_capital_gain: _Array
    standard_deduction: _Array
    mortgage_interest_deduction: _Array
    salt_deduction: _Array
    itemized_deduction: _Array
    ordinary_taxable: _Array
    capital_gain_taxable: _Array
    ordinary_tax: _Array
    capital_gain_tax: _Array
    liability_amount: _Array
    liability_active: _Array
    settlement_active: _Array
    settlement_amount: _Array
    settlement_year_end: _Array


class _DispositionOutput(NamedTuple):
    active: _Array
    units: _Array
    basis: _Array
    proceeds: _Array


class _TargetAllocationOutput(NamedTuple):
    dispositions: _DispositionOutput
    obligation_attempt_policy: _Array


class _PrivateEquityOpportunityOutput(NamedTuple):
    active: _Array
    outcome: _Array
    floor: _Array
    liquid_net_worth: _Array
    shortfall: _Array
    units_held: _Array
    sellable_units: _Array
    target_units: _Array
    proceeds: _Array


class _PrivateEquityOutput(NamedTuple):
    dispositions: _DispositionOutput
    opportunities: _PrivateEquityOpportunityOutput


class _PropertySaleTraceOutput(NamedTuple):
    gross_proceeds: _Array
    mortgage_payoff: _Array
    net_cash: _Array
    realized_gain: _Array
    depreciation_recapture: _Array
    section_121_exclusion: _Array
    long_term_capital_gain: _Array


class _LifecycleOutput(NamedTuple):
    fired: _Array
    property_sales: _PropertySaleTraceOutput


class _DenseScanOutput(NamedTuple):
    state: _StateOutput
    transfers: _TransferOutput
    property_cashflows: _TransferOutput
    obligations: _ObligationOutput
    property_purchases: _PropertyPurchaseOutput
    mortgages: _MortgageOutput
    taxes: _TaxOutput
    target_allocation: _TargetAllocationOutput
    private_equity: _PrivateEquityOutput
    lifecycle: _LifecycleOutput
    primary_residence_fired: _Array


class _DenseFinalOutput(NamedTuple):
    lot_cost_basis: _Array
    lot_purchase_month: _Array
    scheduled_dispositions: _DispositionOutput
    sale_oversell: _Array
    target_allocation_buy_count: _Array


class _ProductTailOutput(NamedTuple):
    sale_oversell: _Array
    failed_month: _Array
    target_allocation_buy_count: _Array


class _DenseProductTailOutput(NamedTuple):
    dense: _DenseFinalOutput
    failed_month: _Array


@dataclass(frozen=True)
class _FoldedSale:
    """One real scheduled sale, with its static FIFO data resolved host-side for the scan fold."""

    buffer_index: int
    month: int
    ordered_lots: tuple[int, ...]
    quantity: int
    proceeds_slot: int
    agent_code: int


@dataclass(frozen=True)
class _FoldedPurchase:
    """One real cash property purchase, static data resolved host-side for the scan fold."""

    buffer_index: int
    month: int
    stake_contribution: int
    buyer_slot: int
    seller_slot: int
    mortgage_slot: int


@dataclass(frozen=True)
class _SalePool:
    """One (asset, source-account) FIFO pool a sleeve can sell from."""

    asset_idx: int
    ordered_lots: tuple[int, ...]


@dataclass(frozen=True)
class _FoldedSleeve:
    """One sleeve of a target allocation, resolved host-side.

    `view_lot_rows` indexes the POLICY'S view lot axis rather than the plan's, because that is
    what `SleeveUniverse` is defined against — the view has already narrowed to this agent, so
    plan indices would read the wrong lots. `pools` keeps the plan indices, since execution
    sells against the engine's own lot tensor.

    No `weight` here on purpose: this record is folded into `_Static` and so forms part of the XLA
    compile key, and a weight is swept numeric config rather than structure. Baking it in made every
    distinct weight vector its own compiled program, so an allocation sweep paid a full compile per
    arm. Weights ride the traced `_Operands.ta_sleeve_weights` instead.
    """

    sleeve_idx: int
    view_lot_rows: tuple[int, ...]
    pools: tuple[_SalePool, ...]
    # Quanta per unit, from the compiler rather than from this sleeve's lots: a sleeve holding
    # nothing still has to be priceable, which is what the buy side needs.
    quantity_scale: int
    # Plan lot indices this sleeve may buy into, in fill order. Empty when the policy configured
    # no purchase slots, which is what makes the buy side opt-in: a policy with none accumulates
    # surplus cash instead of investing it.
    purchase_slots: tuple[int, ...]


@dataclass(frozen=True)
class _FoldedTargetAllocation:
    """One target-allocation policy, static data resolved host-side.

    `lot_slots` is every plan lot this policy can see, in view-axis order; a sleeve's
    `view_lot_rows` are positions within it. Two structures rather than one because the
    observation is built once per policy and then sliced per sleeve.
    """

    policy_index: int
    agent: int
    cash_slot: int
    # Static, so `None` means the drift rebalance is never traced rather than merely inactive.
    rebalance_tolerance: float | None
    floor: tuple[int, int, int, int, int]
    ceiling: tuple[int, int, int, int, int]
    lot_slots: tuple[int, ...]
    sleeves: tuple[_FoldedSleeve, ...]


@dataclass(frozen=True)
class _FoldedHarvest:
    """One reduced-form TLH harvest policy, static data resolved host-side."""

    policy_idx: int
    gain_profile: int
    lot_indices: tuple[int, ...]
    peak_annual_yield: float
    floor_annual_yield: float
    maturity_decay_exponent: float
    drawdown_sensitivity: float
    short_term_fraction: float


@dataclass(frozen=True)
class _FoldedPE:
    """One private-equity issuer's tender static data."""

    issuer_idx: int
    policy_idx: int
    ordered: tuple[int, ...]
    proceeds_cash_slot: int
    owner_agent: int
    floor_kind: int
    floor_fixed: int
    floor_base: int
    floor_base_month: int
    floor_period: int
    owner_non_pe_lot_indices: tuple[int, ...]


@dataclass(frozen=True)
class _FoldedLifecycleEvent:
    """One lifecycle event with the per-event scalars the step reads as Python."""

    event_index: int
    month: int
    kind: int
    property_slot: int
    rented_fraction: float
    amount: float
    amount_quanta: int
    owner_cash_slot: int
    purchase_price: int
    building_basis_initial: int
    owner_profile: int
    gain_profile: int
    exclusion_cap: int
    mortgage_liabilities: tuple[int, ...]


@dataclass(frozen=True)
class _CapitalGainTarget:
    """One agent-code to capital-gain-profile-row mapping."""

    agent_code: int
    profiles: tuple[int, ...]


@dataclass(frozen=True)
class _LinkTaxStatic:
    """One tax link's per-link Python scalars."""

    link: int
    profile: int
    gain_profile: int
    section_1250_rate: float
    mid_active: bool
    ordinary_count: int
    has_ltcg: int
    ltcg_count: int
    salt_active: bool


@dataclass(frozen=True)
class _Static:
    """Every hashable Python value the scan bodies read at trace time."""

    rollout_count: int
    horizon: int
    cash_count: int
    lot_count: int
    property_count: int
    liability_count: int
    tax_profile_count: int
    capital_gain_agent_count: int
    tax_liability_count: int
    harvest_policy_count: int
    scheduled_sale_count: int
    link_count: int
    profile_count: int
    taxliab_count: int
    n_sales: int
    sale_max_pool: int
    lot_axis: int
    ta_policy_count: int
    ta_max_sleeves: int
    pe_issuer_count: int
    n_pe_kinds: int
    folded_lifecycle: tuple[_FoldedLifecycleEvent, ...]
    folded_pr: tuple[tuple[int, int], ...]
    folded_target_allocation: tuple[_FoldedTargetAllocation, ...]
    folded_pe: tuple[_FoldedPE, ...]
    folded_harvest: tuple[_FoldedHarvest, ...]
    salt_link_active: tuple[bool, ...]
    sale_pslot: tuple[int, ...]
    sale_bufidx: tuple[int, ...]
    sale_olots: tuple[tuple[int, ...], ...]
    pur_buf: tuple[int, ...]
    pur_month: tuple[int, ...]
    pur_stake: tuple[int, ...]
    pur_buyer: tuple[int, ...]
    pur_seller: tuple[int, ...]
    pur_mort_rows: tuple[int, ...]
    pur_mort_idx: tuple[int, ...]
    folded_purchases_present: bool
    folded_sales_present: bool
    # Scheduled ASSET purchases (`pur_*` above is real property). One dedicated lot slot each,
    # so unlike sales there is no shared-pool ordering to fold.
    buy_lot_slot: tuple[int, ...]
    buy_cash_slot: tuple[int, ...]
    asset_buys_present: bool
    # Contra row every asset purchase pays into — the market is outside the modeled world.
    external_cash_slot: int
    cg_targets: tuple[_CapitalGainTarget, ...]
    link_tax_static: tuple[_LinkTaxStatic, ...]
    link_profile: tuple[int, ...]
    profile_gain_index: tuple[int, ...]
    # Row of the YTD income tensor holding each profile's ORDINARY income. Not the profile
    # index: tensor rows are (profile, income source) pairs.
    profile_ordinary_bucket: tuple[int, ...]
    # Static: a scenario with no TIPS has a possibly zero-row series cube, so the indexed
    # branch must be skipped at trace time rather than masked at runtime.
    has_indexed_bonds: bool
    # Same reason: a scenario with no distributing holding has nothing to gather a
    # dollars-per-unit row from, and the payout phase is skipped at trace time.
    has_distributions: bool


@dataclass(frozen=True)
class _ScanMeta:
    """Structural data the post-scan host scatter needs."""

    folded_sales: list[_FoldedSale]
    folded_purchases: list[_FoldedPurchase]
    folded_lifecycle: list[_FoldedLifecycleEvent]
    folded_pr: list[tuple[int, int]]
    folded_sale_events: list[tuple[int, int]]
    folded_target_allocation: list[_FoldedTargetAllocation]
    folded_pe: list[_FoldedPE]
    link_count: int
    liability_count: int
    horizon: int
