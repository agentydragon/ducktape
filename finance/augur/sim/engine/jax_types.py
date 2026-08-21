"""Static host-side data types shared by the JAX simulation engine modules."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import NamedTuple

import jax

from finance.augur.sim.compiler.plan import SlotPlan


class _StateOutput(NamedTuple):
    cash: jax.Array
    ordinary: jax.Array
    lots: jax.Array
    capital_gain_active: jax.Array
    capital_gain_ytd: jax.Array
    property_active: jax.Array
    property_basis: jax.Array
    property_contribution: jax.Array
    property_equity: jax.Array
    property_cumulative_depreciation: jax.Array
    property_owner_occupied_months: jax.Array
    liability_active: jax.Array
    liability_principal: jax.Array
    liability_monthly_payment: jax.Array
    liability_interest_ytd: jax.Array
    liability_principal_ytd: jax.Array
    failed: jax.Array
    failed_month: jax.Array
    spending_tier: jax.Array


class _TransferOutput(NamedTuple):
    active: jax.Array
    amount: jax.Array


class _TransferInputs(NamedTuple):
    cause: jax.Array
    amount_kind: jax.Array
    amount_fixed: jax.Array
    amount_base: jax.Array
    amount_series: jax.Array
    amount_base_month: jax.Array
    amount_period: jax.Array
    from_slot: jax.Array
    to_slot: jax.Array
    income_profile: jax.Array
    deduction_profile: jax.Array


class _PropertyCashflowInputs(NamedTuple):
    cause: jax.Array
    amount_kind: jax.Array
    amount_fixed: jax.Array
    amount_base: jax.Array
    amount_series: jax.Array
    amount_base_month: jax.Array
    amount_period: jax.Array
    from_slot: jax.Array
    to_slot: jax.Array
    property_slot: jax.Array
    income_profile: jax.Array
    deduction_profile: jax.Array


@partial(
    jax.tree_util.register_dataclass,
    data_fields=("month", "amount_quanta", "price_fixed", "price_series", "quantity_scale"),
    meta_fields=("lot_slot", "cash_slot"),
)
@dataclass(frozen=True)
class _AssetPurchaseProgram:
    """Scheduled asset-purchase values and the static slots that interpret them."""

    month: jax.Array
    amount_quanta: jax.Array
    price_fixed: jax.Array
    price_series: jax.Array
    quantity_scale: jax.Array
    lot_slot: tuple[int, ...]
    cash_slot: tuple[int, ...]


@partial(
    jax.tree_util.register_dataclass,
    data_fields=(
        "month",
        "quantity",
        "same_pool_prior",
        "capital_gain_map",
        "tlh_policy_lot_mask",
        "price_fixed",
        "price_series",
    ),
    meta_fields=("proceeds_slot", "buffer_index", "ordered_lots"),
)
@dataclass(frozen=True)
class _AssetSaleProgram:
    """Scheduled asset-sale values and the static FIFO topology that interprets them."""

    month: jax.Array
    quantity: jax.Array
    same_pool_prior: jax.Array
    capital_gain_map: jax.Array
    tlh_policy_lot_mask: jax.Array
    price_fixed: jax.Array
    price_series: jax.Array
    proceeds_slot: tuple[int, ...]
    buffer_index: tuple[int, ...]
    ordered_lots: tuple[tuple[int, ...], ...]


class _BondInputs(NamedTuple):
    coupon: jax.Array
    redemption: jax.Array
    to_slot: jax.Array
    income_row: jax.Array
    indexed: jax.Array
    cpi_series: jax.Array
    index_base_month: jax.Array
    period_rate: jax.Array
    face: jax.Array
    pays: jax.Array
    matures: jax.Array
    on_books: jax.Array


class _DistributionInputs(NamedTuple):
    lot_mask: jax.Array
    series: jax.Array
    quantity_scale: jax.Array
    fraction: jax.Array
    to_slot: jax.Array
    income_row: jax.Array


class _PEChannelInputs(NamedTuple):
    mark_quanta: jax.Array
    regime: jax.Array
    sale_opportunity_active: jax.Array
    capacity_fraction: jax.Array
    eligible_fraction: jax.Array
    forced_sale_fraction: jax.Array
    liquidity_blocked: jax.Array
    forced_recovery_cashout: jax.Array


class _ObligationOutput(NamedTuple):
    active: jax.Array
    due: jax.Array
    paid: jax.Array
    shortfall: jax.Array
    failure_active: jax.Array


class _ObligationMetadataInputs(NamedTuple):
    agent: jax.Array
    from_slot: jax.Array
    to_slot: jax.Array
    deduction_profile: jax.Array
    deductible_fraction: jax.Array
    property_tax_profile: jax.Array
    property_slot: jax.Array


class _PaymentBatch(NamedTuple):
    """Common source output consumed by the shared funding/settlement phase."""

    active: jax.Array
    due: jax.Array
    metadata: _ObligationMetadataInputs


class _ConfiguredObligationInputs(NamedTuple):
    active: jax.Array
    amount_kind: jax.Array
    amount_fixed: jax.Array
    amount_base: jax.Array
    amount_series: jax.Array
    amount_base_month: jax.Array
    amount_period: jax.Array
    tier_policy: jax.Array


class _TierAmountScheduleInputs(NamedTuple):
    value: jax.Array
    kind: jax.Array
    series: jax.Array
    base_month: jax.Array
    period: jax.Array


class _TieredSpendingObligationInputs(NamedTuple):
    initial_tier: jax.Array
    tier_count: jax.Array
    spend: _TierAmountScheduleInputs
    drop: _TierAmountScheduleInputs
    recover: _TierAmountScheduleInputs
    cash_mask: jax.Array
    lot_mask: jax.Array
    liability_mask: jax.Array


class _PropertyTaxObligationInputs(NamedTuple):
    active: jax.Array
    property_slot: jax.Array
    amount: jax.Array
    property_purchase_month: jax.Array


class _MortgageObligationInputs(NamedTuple):
    active: jax.Array
    liability_slot: jax.Array
    property_slot: jax.Array
    annual_rate: jax.Array
    property_purchase_month: jax.Array


class _EstimatedTaxObligationInputs(NamedTuple):
    active: jax.Array
    profile_index: jax.Array
    quarterly_amount: jax.Array


class _PriorYearTaxObligationInputs(NamedTuple):
    active: jax.Array
    profile_index: jax.Array
    prior_year_tax: jax.Array
    tax_liability_selector: jax.Array
    tax_year_end_month: jax.Array


class _ObligationInputs(NamedTuple):
    metadata: _ObligationMetadataInputs
    configured: _ConfiguredObligationInputs
    tiered_spending: _TieredSpendingObligationInputs
    property_tax: _PropertyTaxObligationInputs
    mortgage: _MortgageObligationInputs
    estimated_tax: _EstimatedTaxObligationInputs
    q4_estimated_tax: _PriorYearTaxObligationInputs
    tax_true_up: _PriorYearTaxObligationInputs


class _PropertyPurchaseOutput(NamedTuple):
    active: jax.Array
    transfer_active: jax.Array


class _MortgageOutput(NamedTuple):
    origination_active: jax.Array
    payment_active: jax.Array
    payment_interest: jax.Array
    payment_principal: jax.Array
    payment_total: jax.Array


class _TaxOutput(NamedTuple):
    accrual_active: jax.Array
    accrual_amount: jax.Array
    ordinary_income: jax.Array
    long_term_capital_gain: jax.Array
    short_term_capital_gain: jax.Array
    standard_deduction: jax.Array
    mortgage_interest_deduction: jax.Array
    salt_deduction: jax.Array
    itemized_deduction: jax.Array
    ordinary_taxable: jax.Array
    capital_gain_taxable: jax.Array
    ordinary_tax: jax.Array
    capital_gain_tax: jax.Array
    liability_amount: jax.Array
    liability_active: jax.Array
    settlement_active: jax.Array
    settlement_amount: jax.Array
    settlement_year_end: jax.Array


class _DispositionOutput(NamedTuple):
    active: jax.Array
    units: jax.Array
    basis: jax.Array
    proceeds: jax.Array


class _TargetAllocationOutput(NamedTuple):
    dispositions: _DispositionOutput
    obligation_attempt_policy: jax.Array


class _PrivateEquityOpportunityOutput(NamedTuple):
    active: jax.Array
    outcome: jax.Array
    floor: jax.Array
    liquid_net_worth: jax.Array
    shortfall: jax.Array
    units_held: jax.Array
    sellable_units: jax.Array
    target_units: jax.Array
    proceeds: jax.Array


class _PrivateEquityOutput(NamedTuple):
    dispositions: _DispositionOutput
    opportunities: _PrivateEquityOpportunityOutput


class _PropertySaleTraceOutput(NamedTuple):
    gross_proceeds: jax.Array
    mortgage_payoff: jax.Array
    net_cash: jax.Array
    realized_gain: jax.Array
    depreciation_recapture: jax.Array
    section_121_exclusion: jax.Array
    long_term_capital_gain: jax.Array


class _LifecycleOutput(NamedTuple):
    fired: jax.Array
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
    primary_residence_fired: jax.Array


class _DenseFinalOutput(NamedTuple):
    lot_cost_basis: jax.Array
    lot_purchase_month: jax.Array
    scheduled_dispositions: _DispositionOutput
    sale_oversell: jax.Array
    target_allocation_buy_count: jax.Array


class _ProductTailOutput(NamedTuple):
    sale_oversell: jax.Array
    failed_month: jax.Array
    target_allocation_buy_count: jax.Array


class _DenseProductTailOutput(NamedTuple):
    dense: _DenseFinalOutput
    failed_month: jax.Array


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


@dataclass(frozen=True)
class _Static:
    """One hashable structural contract shared by the scan and host scatter."""

    slot_plan: SlotPlan
    lot_axis: int
    ta_policy_count: int
    ta_max_sleeves: int
    pe_issuer_count: int
    n_pe_kinds: int
    folded_purchases: tuple[_FoldedPurchase, ...]
    folded_lifecycle: tuple[_FoldedLifecycleEvent, ...]
    folded_pr: tuple[tuple[int, int], ...]
    folded_sale_events: tuple[tuple[int, int], ...]
    folded_target_allocation: tuple[_FoldedTargetAllocation, ...]
    folded_pe: tuple[_FoldedPE, ...]
    folded_harvest: tuple[_FoldedHarvest, ...]
    salt_link_active: tuple[bool, ...]
    pur_buf: tuple[int, ...]
    pur_month: tuple[int, ...]
    pur_stake: tuple[int, ...]
    pur_buyer: tuple[int, ...]
    pur_seller: tuple[int, ...]
    pur_mort_rows: tuple[int, ...]
    pur_mort_idx: tuple[int, ...]
    folded_purchases_present: bool
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
