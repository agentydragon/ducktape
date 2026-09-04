"""Static host-side data types shared by the JAX simulation engine modules."""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
from dataclasses import dataclass
from functools import partial
from typing import NamedTuple

import jax
from jaxtyping import Array, Bool, Int32, Int64

from finance.augur.sim.compiler.plan import SlotPlan
from finance.augur.sim.output import DenseFinalOutput


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

    month: Int32[Array, " scheduled_sale"]
    quantity: Int64[Array, " scheduled_sale"]
    same_pool_prior: Int64[Array, " scheduled_sale prior_sale"]
    capital_gain_map: Int64[Array, " scheduled_sale capital_gain_profile"]
    tlh_policy_lot_mask: Int64[Array, " harvest_policy lot"]
    price_fixed: Int64[Array, " scheduled_sale"]
    price_series: Int64[Array, " scheduled_sale"]
    proceeds_slot: tuple[int, ...]
    buffer_index: tuple[int, ...]
    ordered_lots: tuple[tuple[int, ...], ...]


class _PurchaseInputs(NamedTuple):
    """Scheduled purchase columns, kept on the full property axis."""

    month: Int64[Array, " property"]
    stake_contribution: Int64[Array, " property"]
    buyer_slot: Int64[Array, " property"]
    seller_slot: Int64[Array, " property"]
    mortgage_slot: Int64[Array, " property"]
    mortgage_principal: Int64[Array, " property"]
    mortgage_monthly_payment: Int64[Array, " property"]


class _ProductTailOutput(NamedTuple):
    sale_oversell: Bool[Array, ""]
    failed_month: Int64[Array, " rollout"]
    target_allocation_buy_count: Int64[Array, " policy sleeve rollout"]


class _DenseProductTailOutput(NamedTuple):
    dense: DenseFinalOutput[jax.Array]
    failed_month: Int64[Array, " rollout"]


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
    # One tuple of plan lot indices per (asset, source-account) FIFO pool.
    pools: tuple[tuple[int, ...], ...]
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
    peak_annual_yield_ppb: int
    floor_annual_yield_ppb: int
    maturity_decay_half_exponent: int
    drawdown_sensitivity_ppb: int
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
    folded_lifecycle: tuple[_FoldedLifecycleEvent, ...]
    folded_pr: tuple[tuple[int, int], ...]
    folded_sale_events: tuple[tuple[int, int], ...]
    folded_target_allocation: tuple[_FoldedTargetAllocation, ...]
    folded_pe: tuple[_FoldedPE, ...]
    folded_harvest: tuple[_FoldedHarvest, ...]
    salt_link_active: tuple[bool, ...]
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
