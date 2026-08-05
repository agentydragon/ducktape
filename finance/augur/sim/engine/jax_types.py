"""Static host-side data types shared by the JAX simulation engine modules."""

from __future__ import annotations

from dataclasses import dataclass


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
class _LiquidityPool:
    """One (asset, source-account) FIFO pool a liquidity policy can sell from."""

    asset_idx: int
    ordered_lots: tuple[int, ...]


@dataclass(frozen=True)
class _FoldedLiquidity:
    """One liquidity policy, static data resolved host-side."""

    policy_index: int
    agent: int
    cash_slot: int
    trigger: tuple[int, int, int, int, int]
    sale: tuple[int, int, int, int, int]
    pools: tuple[_LiquidityPool, ...]


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
    amount_cents: int
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
    liq_policy_count: int
    liq_max_assets: int
    pe_issuer_count: int
    n_pe_kinds: int
    folded_lifecycle: tuple[_FoldedLifecycleEvent, ...]
    folded_pr: tuple[tuple[int, int], ...]
    folded_liquidity: tuple[_FoldedLiquidity, ...]
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


@dataclass(frozen=True)
class _ScanMeta:
    """Structural data the post-scan host scatter needs."""

    folded_sales: list[_FoldedSale]
    folded_purchases: list[_FoldedPurchase]
    folded_lifecycle: list[_FoldedLifecycleEvent]
    folded_pr: list[tuple[int, int]]
    folded_sale_events: list[tuple[int, int]]
    folded_liquidity: list[_FoldedLiquidity]
    folded_pe: list[_FoldedPE]
    link_count: int
    liability_count: int
    horizon: int
