"""JAX simulation engine — in-progress parity port of the NumPy engine.

The dense engine is being ported to JAX phase-by-phase (see <augur/plans/jax_migration.md>). Each
phase is a functional `jnp.at[]` translation of its NumPy counterpart in `phases.py`. Parity is
verified by running the existing simulator test suite under both backends (the autouse `backend`
fixture in `augur/sim/conftest.py` parameterizes every test over NumPy and JAX); the JAX variants
for scenarios touching not-yet-ported phases fail until the port lands them. Selection is via
`sim_backend.current_backend()`.

`run_jax(plan, buffers)` fills the (already NumPy-allocated, zeroed) `buffers` from a JAX run.
Un-ported phases / branches are no-ops, so the JAX backend is correct only for scenarios that
exercise only the ported paths — which is exactly what the passing parity tests use.

Ported so far (in `_run_month_step` order):
- scheduled / recurring transfers;
- property purchases (cash + mortgage origination);
- scheduled asset sales (FIFO lot matching + capital-gain classification + lot-disposition log);
- liquidity-policy sales;
- obligation accruals + settlement with failure tracking and `_zero_failed_state`, for every
  source kind (CONFIGURED_OBLIGATION, PROPERTY_TAX with the SALT/Schedule-E split, MORTGAGE_PAYMENT
  with the interest/principal split, and ESTIMATED_TAX / ESTIMATED_TAX_Q4 / TAX_TRUE_UP);
- PE tenders (LNW-floor tender / public-market / forced-sale / forced-recovery sales + opportunity
  trace);
- the December year-end tax machinery: Schedule-E rental-interest/depreciation deductions,
  §1211/§1212 capital-loss netting, the two-pass SALT walk over MID + LTCG brackets + the §1250
  worksheet, tax-liability accrual, and the true-up settlement (the latter in float64, since a
  ~$50k liability must settle to exactly zero — float32 leaves a ~$0.004 residual).

Not yet ported (no-op): property sale, §168 depreciation accrual, owner-occupied-month tracking,
lifecycle events, and primary-residence assignment — so depreciation_ytd / recapture stay zero,
which is correct for non-rental, non-sale scenarios.

Float32 note: tax amounts, cash flows, and settlements match the float64 reference to within a few
parts in 1e8, but a handful of existing tests assert breakdown fields (income, deductions) to
`rel=1e-9` / `abs=1e-6`, which float32 cannot meet on $10k-$200k values; those JAX variants fail on
precision alone, not logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import partial
from typing import NamedTuple

import jax
import jax.numpy as jnp
import numpy as np

from augur.model.series import PrivateEquityRegimeCode
from augur.sim.buffers import SimulationBuffers
from augur.sim.codec.plan import CompiledSimulation
from augur.sim.compiler.helpers import AMOUNT_FIXED, NO_CODE
from augur.sim.compiler.plan import SlotPlan
from augur.sim.enums import (
    CapitalGainClassification,
    LifecycleKind,
    ObligationSource,
    PrivateEquityDispositionKind,
    PrivateEquityOpportunityOutcome,
)
from augur.sim.tensor_fifo import lot_order_for_pool

# Opt-in JAX persistent on-disk compilation cache: when the env var is set, compiled executables
# survive across processes so the ~6400-instruction scan program need not recompile each run. A no-op
# otherwise (the in-process native cache still reuses across `run_jax_scan` calls of one structure).
_JAX_CACHE_DIR = os.environ.get("AUGUR_JAX_COMPILATION_CACHE_DIR")
if _JAX_CACHE_DIR:
    jax.config.update("jax_compilation_cache_dir", _JAX_CACHE_DIR)
    jax.config.update("jax_persistent_cache_min_compile_time_secs", 0.0)
    jax.config.update("jax_persistent_cache_min_entry_size_bytes", -1)

SECTION_121_LOOKBACK_MONTHS = 60
SECTION_121_MIN_QUALIFYING_MONTHS = 24


class _ScanState(NamedTuple):
    """`run_jax_scan`'s carry pytree (NamedTuple → native JAX pytree). Grown field-by-field as the
    fold covers more phases; per-rollout state is `(entity, rollouts)` except the failure vectors."""

    cash: jnp.ndarray
    ordinary_ytd: jnp.ndarray
    property_tax_ytd: jnp.ndarray
    lot_remaining: jnp.ndarray
    capital_gain_active: jnp.ndarray
    capital_gain_ytd: jnp.ndarray
    tlh: jnp.ndarray
    property_active: jnp.ndarray
    property_basis: jnp.ndarray
    property_ownership: jnp.ndarray
    property_contribution: jnp.ndarray
    property_equity: jnp.ndarray
    property_cumulative_depreciation: jnp.ndarray
    property_owner_occupied_months: jnp.ndarray
    property_depreciation_ytd: jnp.ndarray
    property_rented_fraction: jnp.ndarray  # mutable: lifecycle FRACTION/SALE events change it
    property_building_basis: jnp.ndarray  # mutable: lifecycle CAPITAL_IMPROVEMENT/SALE events change it
    owner_occupied_window: jnp.ndarray  # (60, property, R) ring of monthly owner-occupancy flags (§121)
    liability_active: jnp.ndarray
    liability_principal: jnp.ndarray
    liability_monthly_payment: jnp.ndarray
    liability_interest_ytd: jnp.ndarray
    liability_principal_ytd: jnp.ndarray
    liability_rental_interest_ytd: jnp.ndarray
    capital_loss_carryforward: jnp.ndarray
    recapture_section_1250_ytd: jnp.ndarray
    tax_liability_active: jnp.ndarray
    tax_liability_amount: jnp.ndarray
    failed: jnp.ndarray
    failed_month: jnp.ndarray
    # Scheduled-sale dispositions accumulated in-carry (`(scheduled_sale, lot, R)`): each sale fires
    # once, so accumulating at the firing month collapses the per-month horizon axis the old ys emitted.
    sale_disp_units: jnp.ndarray
    sale_disp_basis: jnp.ndarray
    sale_disp_proceeds: jnp.ndarray
    sale_oversell: jnp.ndarray  # () bool: any scheduled sale oversold its pool (post-scan hard error)


@dataclass(frozen=True)
class LiabilityState:
    """Per-(liability, rollout) mortgage state threaded through the month loop (all R-last `[L, R]`).

    `rental_interest_ytd` is the rented-share slice of `interest_ytd` (Schedule E vs MID split); it
    stays at the NumPy reference's behavior of not being zeroed on rollout failure.
    """

    active: jnp.ndarray
    principal: jnp.ndarray
    monthly_payment: jnp.ndarray
    interest_ytd: jnp.ndarray
    principal_ytd: jnp.ndarray
    rental_interest_ytd: jnp.ndarray


@dataclass(frozen=True)
class _FoldedSale:
    """One real scheduled sale, with its static FIFO data resolved host-side for the scan fold.

    `ordered_lots` is the FIFO lot order for the sale's (agent, account, asset) pool (a static index
    tuple — `tuple` so the enclosing `_Structure` is hashable; convert with `np.asarray` at the use
    site); `buffer_index` is the sale's column in the `lot_dispositions.scheduled` buffers; `month`
    is the (static) month it fires, compared against the traced scan index inside the step."""

    buffer_index: int
    month: int
    ordered_lots: tuple[int, ...]
    quantity: float
    proceeds_slot: int
    agent_code: int


@dataclass(frozen=True)
class _FoldedPurchase:
    """One real cash property purchase, static data resolved host-side for the scan fold. `month` is
    the (static) purchase month, compared against the traced scan index; `buffer_index` is the
    property's column in the property-state and property-event buffers.

    The pure-value purchase/mortgage amounts (basis, ownership, equity, mortgage principal/payment) are
    NOT carried here: the step reads them as traced inputs from the (hybrid) plan by `buffer_index` /
    `mortgage_slot`, so a sweep over those values reuses the compiled program. `stake_contribution`
    stays (it gates a Python `if > 0`, a baked feature)."""

    buffer_index: int
    month: int
    stake_contribution: float
    buyer_slot: int
    seller_slot: int
    mortgage_slot: int  # NO_CODE for cash purchases; else the liability slot to originate


@dataclass(frozen=True)
class _LiquidityPool:
    """One (asset, source-account) FIFO pool a liquidity policy can sell from."""

    asset_idx: int  # column in the policy's asset list (the disposition buffer's asset axis)
    series_index: int
    ordered_lots: tuple[int, ...]  # `tuple` (not np array) so the enclosing `_Structure` is hashable


@dataclass(frozen=True)
class _FoldedLiquidity:
    """One liquidity policy, static data resolved host-side. `pools` enumerates its (asset, account)
    FIFO pools in eager order; `trigger`/`sale` are the amount-spec tuples for the buffer rule."""

    policy_index: int
    agent: int
    cash_slot: int
    trigger: tuple[int, float, float, int, int, int]
    sale: tuple[int, float, float, int, int, int]
    pools: tuple[_LiquidityPool, ...]


@dataclass(frozen=True)
class _ScanMeta:
    """Structural (rollout-value-independent) data the post-scan host code needs to scatter the
    stacked `ys` back into the NumPy buffers. Carried alongside the compiled program in the cache so
    a cache hit needs no recompute — these are pure functions of the plan's *structure*."""

    folded_sales: list
    folded_purchases: list
    folded_lifecycle: list
    folded_pr: list
    folded_sale_events: list
    folded_liquidity: list
    folded_pe: list
    link_count: int
    liability_count: int
    horizon: int


class _TracedConfig(NamedTuple):
    """JAX-native typed bundle of the swept numeric config the compiled program takes as TRACED inputs
    (a NamedTuple → native JAX pytree, so it passes through `jax.jit` typed). The cores read VALUES from
    here (`jax.Array`s) while reading structure / feature flags / counts / slot indices from the
    concrete `plan` — so nothing puns a traced array into the compiler's NumPy-typed plan fields. Each
    field is a swept numeric value (not baked structure), so sweeping it reuses the compiled program."""

    link_standard_deduction: jnp.ndarray
    link_ordinary_upper: jnp.ndarray
    link_ordinary_rate: jnp.ndarray
    link_ltcg_upper: jnp.ndarray
    link_ltcg_rate: jnp.ndarray
    mid_principal_ratio: jnp.ndarray
    transfer_amount_fixed: jnp.ndarray
    transfer_amount_base: jnp.ndarray
    cost_basis_per_unit: jnp.ndarray
    cash_initial_balance: jnp.ndarray
    lot_initial_quantity: jnp.ndarray
    property_adjusted_basis: jnp.ndarray
    property_ownership: jnp.ndarray
    property_equity_ledger: jnp.ndarray
    liability_principal: jnp.ndarray
    liability_monthly_payment: jnp.ndarray


def _traced_config(plan: CompiledSimulation) -> _TracedConfig:
    """Build the traced-config bundle of swept numeric values from the (concrete) plan."""
    return _TracedConfig(
        link_standard_deduction=jnp.asarray(plan.tax.link_standard_deduction),
        link_ordinary_upper=jnp.asarray(plan.tax.link_ordinary_upper),
        link_ordinary_rate=jnp.asarray(plan.tax.link_ordinary_rate),
        link_ltcg_upper=jnp.asarray(plan.tax.link_ltcg_upper),
        link_ltcg_rate=jnp.asarray(plan.tax.link_ltcg_rate),
        mid_principal_ratio=jnp.asarray(plan.mid.principal_ratio),
        transfer_amount_fixed=jnp.asarray(plan.transfers.amount_fixed),
        transfer_amount_base=jnp.asarray(plan.transfers.amount_base),
        cost_basis_per_unit=jnp.asarray(plan.lot_cost_basis_per_unit),
        cash_initial_balance=jnp.asarray(plan.cash_initial_balance),
        lot_initial_quantity=jnp.asarray(plan.lot_initial_quantity),
        property_adjusted_basis=jnp.asarray(plan.properties.adjusted_basis),
        property_ownership=jnp.asarray(plan.properties.ownership),
        property_equity_ledger=jnp.asarray(plan.properties.equity_ledger),
        liability_principal=jnp.asarray(plan.liabilities.principal),
        liability_monthly_payment=jnp.asarray(plan.liabilities.monthly_payment),
    )


class _Baked(NamedTuple):
    """Every device array the scan program closes over, packed into a single pytree (a `NamedTuple` is
    an auto-registered JAX pytree node; nested `dict[str, jnp.ndarray]` values are valid pytree nodes
    too). Passed to `_program_impl` as a TRACED argument: JAX keys the native compile cache on its
    avals, so an identical-structure plan (identical shapes/dtypes) is a cache hit and differing VALUES
    reuse the same executable — no hand-rolled hashing of array contents."""

    # Carry-init device constants.
    cash0: jnp.ndarray
    ordinary0: jnp.ndarray
    property_tax_ytd0: jnp.ndarray
    lot0: jnp.ndarray
    cg_active0: jnp.ndarray
    cg_ytd0: jnp.ndarray
    tlh0: jnp.ndarray
    property_rented_fraction_0: jnp.ndarray
    property_building_basis_0: jnp.ndarray
    prop0: jnp.ndarray
    liab0: jnp.ndarray
    # Whole-horizon static tables sliced by the traced month.
    tr: dict[str, jnp.ndarray]
    og: dict[str, jnp.ndarray]
    acc: dict[str, jnp.ndarray]
    # Scheduled-sale stacked static data.
    sale_months_t: jnp.ndarray
    sale_qty_t: jnp.ndarray
    sale_prior_t: jnp.ndarray
    sale_cg_map_t: jnp.ndarray
    sale_policy_mask_t: jnp.ndarray
    sale_price_fixed_t: jnp.ndarray
    # Year-end / property tables.
    property_is_primary_table: jnp.ndarray
    tax_slot_table: jnp.ndarray
    salt_cap_table: jnp.ndarray
    # Device arrays the bodies + de-`plan`-ed cores read directly.
    lot_purchase_month: jnp.ndarray
    capital_gain_agent_codes: jnp.ndarray
    cg_rep_profile: jnp.ndarray
    property_owner_profile_index: jnp.ndarray
    liability_owner_profile_index: jnp.ndarray
    salt_contributing_mask: jnp.ndarray
    lot_asset_series_index: jnp.ndarray
    pe_owner_cash_mask: jnp.ndarray  # (pe_policy, cash)


@dataclass(frozen=True)
class _FoldedHarvest:
    """One reduced-form TLH harvest policy, static data resolved host-side (hashable for `_Structure`)."""

    policy_idx: int
    gain_profile: int
    lot_indices: tuple[int, ...]
    series_index: int
    peak_annual_yield: float
    floor_annual_yield: float
    maturity_decay_exponent: float
    drawdown_sensitivity: float
    short_term_fraction: float


@dataclass(frozen=True)
class _FoldedPE:
    """One private-equity issuer's tender static data, plus its policy's per-issuer scalars (hashable)."""

    issuer_idx: int
    policy_idx: int
    ordered: tuple[int, ...]
    proceeds_cash_slot: int
    owner_agent: int
    floor_kind: int
    floor_fixed: float
    floor_base: float
    floor_series: int
    floor_base_month: int
    floor_period: int
    owner_non_pe_lot_indices: tuple[int, ...]  # for `_compute_liquid_net_worth`


@dataclass(frozen=True)
class _FoldedLifecycleEvent:
    """One lifecycle event with the per-event scalars the step reads as Python (hashable)."""

    event_index: int
    month: int
    kind: int
    property_slot: int
    rented_fraction: float  # for FRACTION events
    amount: float  # for CAPITAL_IMPROVEMENT (cash) / SALE (closing-cost pct) events
    owner_cash_slot: int  # `props.buyer_slot[property_slot]`
    # SALE static data (resolved host-side; defaults for non-SALE events).
    home_value_series_index: int
    purchase_price: float
    building_basis_initial: float
    owner_profile: int
    gain_profile: int
    exclusion_cap: float
    mortgage_liabilities: tuple[int, ...]  # liability slots whose property_slot == this property


@dataclass(frozen=True)
class _CapitalGainTarget:
    """One (agent_code) -> matching capital-gain profile rows, resolved host-side (hashable)."""

    agent_code: int
    profiles: tuple[int, ...]


@dataclass(frozen=True)
class _LinkTaxStatic:
    """One tax link's per-link Python scalars (read at trace time by `_compute_tax_for_link`)."""

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
class _Structure:
    """Every natively-hashable Python value the scan bodies read at TRACE TIME (counts, slot indices,
    feature flags, and the folded event lists as tuples-of-frozen-dataclasses with int/float fields).
    A frozen dataclass of `int`/`bool`/`float`/`tuple` (and tuples of small frozen dataclasses) is
    hashable by Python's default frozen-dataclass `__hash__` — NO custom `__hash__`/`__eq__`. Passed
    as a `static_argnames` arg to `_program_impl`, so identical structure is one cache key (a cache
    hit) and a structural change is a fresh key (one extra compile)."""

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
    # Folded event tuples (iterated in the step body / december pass).
    folded_lifecycle: tuple[_FoldedLifecycleEvent, ...]
    folded_pr: tuple[tuple[int, int], ...]
    folded_liquidity: tuple[_FoldedLiquidity, ...]
    folded_pe: tuple[_FoldedPE, ...]
    folded_harvest: tuple[_FoldedHarvest, ...]
    # Small index/selection arrays converted to (tuples of) tuples so they are hashable; converted
    # back with `np.asarray(...)` at the use site inside the jitted region.
    salt_link_active: tuple[bool, ...]
    sale_pslot: tuple[int, ...]
    sale_bufidx: tuple[int, ...]
    sale_olots: tuple[tuple[int, ...], ...]
    sale_price_series: tuple[int, ...]
    pur_buf: tuple[int, ...]
    pur_month: tuple[int, ...]
    pur_stake: tuple[float, ...]
    pur_buyer: tuple[int, ...]
    pur_seller: tuple[int, ...]
    pur_mort_rows: tuple[int, ...]
    pur_mort_idx: tuple[int, ...]
    folded_purchases_present: bool
    folded_sales_present: bool
    # Capital-gain accrual targets (agent_code -> matching profile rows) for the de-`plan`-ed
    # `_record_capital_gains` (keyed by agent code, looked up at the call site).
    cg_targets: tuple[_CapitalGainTarget, ...]
    # Per-link tax static scalars for `_compute_tax_for_link`.
    link_tax_static: tuple[_LinkTaxStatic, ...]
    # Per-link tax profile / gain profile for the december breakdown column reads.
    link_profile: tuple[int, ...]
    profile_gain_index: tuple[int, ...]


def run_jax_scan(plan: CompiledSimulation, buffers: SimulationBuffers) -> None:
    """Single-program `lax.scan` engine: the whole month loop compiles into one XLA program (one
    dispatch for all months) whose only traced inputs are the seed-varying series and swept numeric
    config. `_build_program` builds + `jax.jit`-wraps the device program for this plan structure; JAX
    compiles it on first invocation."""
    # PE-mark validation is seed-dependent (the marks are a sampled series), so it runs every call on
    # the concrete plan — the in-scan path can't raise.
    pe_channels = plan.pe_channels
    if pe_channels.marks.size and (not np.isfinite(pe_channels.marks).all() or (pe_channels.marks < 0.0).any()):
        raise ValueError("private-equity mark series produced a negative or non-finite value")
    if pe_channels.forced_recovery_cashout_usd.size and (pe_channels.forced_recovery_cashout_usd < 0.0).any():
        raise ValueError("private-equity forced-recovery cashout series produced a negative value")

    baked, structure, p, meta = _build_program(plan)
    external, pe, cfg = _program_inputs(plan)
    ys, sale_disp = _program_impl(external, pe, cfg, baked, p, structure)
    _scatter_ys_to_buffers(plan, buffers, meta, ys, sale_disp)


def _program_inputs(plan: CompiledSimulation) -> tuple[jnp.ndarray, dict[str, jnp.ndarray], _TracedConfig]:
    """The three traced arguments the compiled program takes: the external-series cube, the
    seed-varying PE channel dict, and the swept-numeric `_TracedConfig`."""
    pe_channels = plan.pe_channels
    pe_ch_dyn = {
        "marks": jnp.asarray(pe_channels.marks),
        "regime": jnp.asarray(pe_channels.regime_codes),
        "sale_opp": jnp.asarray(pe_channels.sale_opportunity_active),
        "capacity": jnp.asarray(pe_channels.sale_capacity_fractions),
        "eligible": jnp.asarray(pe_channels.eligible_fractions),
        "forced_sale": jnp.asarray(pe_channels.forced_sale_fractions),
        "liq_blocked": jnp.asarray(pe_channels.liquidity_blocked),
        "forced_recovery": jnp.asarray(pe_channels.forced_recovery_cashout_usd),
    }
    return jnp.asarray(plan.external_values), pe_ch_dyn, _traced_config(plan)


def compiled_hlo_text(plan: CompiledSimulation) -> str:
    """Optimized-HLO text of the compiled program for `plan` (introspection / op-count profiling)."""
    baked, structure, p, _ = _build_program(plan)
    external, pe, cfg = _program_inputs(plan)
    text = _program_impl.lower(external, pe, cfg, baked, p, structure).compile().as_text()
    if text is None:
        raise RuntimeError("compiled program exposes no HLO text")
    return text


def _build_program(plan: CompiledSimulation) -> tuple[_Baked, _Structure, SlotPlan, _ScanMeta]:
    """Host-only build of the device program inputs for one plan *structure*. Does ALL numpy/Python
    precompute and packs the results into a `_Baked` pytree (every device array the scan closes over,
    a TRACED arg) and a `_Structure` frozen dataclass (every natively-hashable Python value the bodies
    read at trace time, a STATIC arg) — plus the plan's `SlotPlan` (`p`, already natively hashable) and
    the host-side `_ScanMeta` for the post-scan scatter. The compiled program is `_program_impl`, whose
    native JAX cache reuses the executable across calls of the same structure (and across traced
    value/seed sweeps) — no hand-rolled hashing."""
    p = plan.slot_plan
    r = p.rollout_count
    horizon = plan.horizon_months
    cash0 = jnp.asarray(np.broadcast_to(plan.cash_initial_balance[:, None], (p.cash_count, r)))
    ordinary0 = jnp.zeros((p.tax_profile_count, r))
    property_tax_ytd0 = jnp.zeros((p.tax_profile_count, r))
    lot0 = jnp.asarray(np.broadcast_to(plan.lot_initial_quantity[:, None], (p.lot_count, r)))
    cg_active0 = jnp.zeros((p.capital_gain_agent_count, 2, r), dtype=bool)
    cg_ytd0 = jnp.zeros((p.capital_gain_agent_count, 2, r))
    # TLH give-back ledger stays zero here (harvest policies are barred by `scan_supported`), but the
    # capital-gains core threads it, so carry a zeroed copy.
    tlh0 = jnp.zeros((p.harvest_policy_count, r))
    props = plan.properties
    # `rented_fraction`/`building_basis` are mutable (lifecycle FRACTION/CAPITAL_IMPROVEMENT/SALE
    # events), so they're carry state initialized from the compile-time broadcast.
    property_rented_fraction_0 = jnp.asarray(
        np.broadcast_to(plan.property_rented_fraction[:, None], (p.property_count, r))
    )
    property_building_basis_0 = jnp.asarray(
        np.broadcast_to(plan.property_building_basis[:, None], (p.property_count, r))
    )
    # Per-month `is_primary[prop]` (rollout-independent): walk primary-residence events and SALE-driven
    # resets month by month so the §121 owner-occupied counter uses the right assignment each month.
    owner_agent = plan.property_owner_agent_index
    n_agents = owner_agent.shape[0]
    apr = plan.initial_primary_residence_property_index.copy()
    pr_starts = plan.primary_residence_events.month_starts
    pr_events = plan.primary_residence_events
    le_all = plan.lifecycle_events
    le_starts = le_all.month_starts
    is_primary_by_month = np.zeros((horizon, p.property_count), dtype=bool)
    for m in range(horizon):
        if m + 1 < pr_starts.shape[0]:
            for ei in range(int(pr_starts[m]), int(pr_starts[m + 1])):
                apr[int(pr_events.agent_slot[ei])] = int(pr_events.property_slot[ei])
        if m + 1 < le_starts.shape[0]:  # SALE events globally clear the seller's primary residence
            for ei in range(int(le_starts[m]), int(le_starts[m + 1])):
                if int(le_all.kind[ei]) == LifecycleKind.SALE:
                    sold_prop = int(le_all.property_slot[ei])
                    owner_slot = int(plan.property_owner_agent_index[sold_prop])
                    if owner_slot >= 0 and int(apr[owner_slot]) == sold_prop:
                        apr[owner_slot] = NO_CODE
        primary_of_owner = _np_gather(apr, np.where(owner_agent < 0, 0, owner_agent), NO_CODE)
        is_primary_by_month[m] = (owner_agent >= 0) & (primary_of_owner == np.arange(n_agents))
    property_is_primary_table = jnp.asarray(is_primary_by_month)

    # Lifecycle + primary-residence events: map each event to the month it fires (event index ->
    # month, via the CSR month_starts). The step applies all events firing at the traced month.
    def _event_months(starts: np.ndarray, count: int) -> np.ndarray:
        em = np.full(count, -1, dtype=np.int64)
        for m in range(horizon):
            if m + 1 < starts.shape[0]:
                em[int(starts[m]) : int(starts[m + 1])] = m
        return em

    le_event_month = _event_months(le_starts, le_all.kind.shape[0])

    def _fold_lifecycle_event(i: int) -> _FoldedLifecycleEvent:
        kind = int(le_all.kind[i])
        prop = int(le_all.property_slot[i])
        owner_profile = int(plan.property_owner_profile_index[prop]) if prop >= 0 else NO_CODE
        gain_profile = int(plan.tax_profile_capital_gain_index[owner_profile]) if owner_profile >= 0 else NO_CODE
        exclusion_cap = float(plan.tax.profile_section_121_exclusion[owner_profile]) if owner_profile >= 0 else 0.0
        mortgage_liabilities = tuple(
            int(lia)
            for lia in range(plan.liabilities.property_slot.shape[0])
            if int(plan.liabilities.property_slot[lia]) == prop
        )
        return _FoldedLifecycleEvent(
            event_index=i,
            month=int(le_event_month[i]),
            kind=kind,
            property_slot=prop,
            rented_fraction=float(le_all.rented_fraction[i]),
            amount=float(le_all.amount[i]),
            owner_cash_slot=int(props.buyer_slot[prop]) if prop >= 0 else NO_CODE,
            home_value_series_index=int(plan.property_home_value_series_index[prop]) if prop >= 0 else 0,
            purchase_price=float(plan.properties.purchase_price[prop]) if prop >= 0 else 0.0,
            building_basis_initial=float(plan.property_building_basis[prop]) if prop >= 0 else 0.0,
            owner_profile=owner_profile,
            gain_profile=gain_profile,
            exclusion_cap=exclusion_cap,
            mortgage_liabilities=mortgage_liabilities,
        )

    folded_lifecycle = [_fold_lifecycle_event(i) for i in range(le_all.kind.shape[0]) if le_event_month[i] >= 0]
    pr_event_month = _event_months(pr_starts, pr_events.agent_slot.shape[0])
    folded_pr = [(i, int(pr_event_month[i])) for i in range(pr_events.agent_slot.shape[0]) if pr_event_month[i] >= 0]
    folded_sale_events = [(ev.event_index, ev.month) for ev in folded_lifecycle if ev.kind == LifecycleKind.SALE]

    # Scheduled asset sales: resolve each real sale's static FIFO data once (host-side); the step
    # applies all firing sales (masked by the traced month). No sales -> the whole block is skipped.
    sales = plan.sales
    folded_sales = [
        _FoldedSale(
            buffer_index=s,
            month=int(sales.month[s]),
            ordered_lots=tuple(
                int(lot)
                for lot in lot_order_for_pool(
                    lot_agent_codes=plan.lot_agent_codes,
                    lot_account_codes=plan.lot_account_codes,
                    lot_asset_codes=plan.lot_asset_codes,
                    lot_purchase_month=plan.lot_purchase_month,
                    lot_id_codes=plan.lot_id_codes,
                    agent_code=int(sales.agent[s]),
                    account_code=int(sales.source_account[s]),
                    asset_code=int(sales.asset[s]),
                )
            ),
            quantity=float(sales.quantity[s]),
            proceeds_slot=int(sales.proceeds_slot[s]),
            agent_code=int(sales.agent[s]),
        )
        for s in range(sales.month.shape[0])
        if int(sales.month[s]) >= 0
    ]
    # Stacked per-sale static data for the loop-free scheduled-sale FIFO. The across-sales FIFO is one
    # cumulative-supply x cumulative-demand interval overlap (generalizing `_fifo_sell_units` from one
    # sale to all): supply prefix over each pool's lots, demand prefix over each pool's sales (via the
    # `same_pool_prior` lower-triangular mask), so shared pools need no sequential loop.
    n_sales = len(folded_sales)
    sale_max_pool = max((len(fs.ordered_lots) for fs in folded_sales), default=1)
    sale_months_t = jnp.asarray([fs.month for fs in folded_sales], dtype=jnp.int32)
    sale_qty_t = jnp.asarray([fs.quantity for fs in folded_sales], dtype=jnp.float32)
    sale_pslot = np.array([fs.proceeds_slot for fs in folded_sales], dtype=np.int64).reshape(n_sales)
    sale_bufidx = np.array([fs.buffer_index for fs in folded_sales], dtype=np.int64).reshape(n_sales)
    sale_olots = np.full((n_sales, sale_max_pool), p.lot_count, dtype=np.int64)  # pad with the dummy lot
    for _i, _fs in enumerate(folded_sales):
        sale_olots[_i, : len(_fs.ordered_lots)] = np.asarray(_fs.ordered_lots, dtype=np.int64)
    sale_price_fixed_t = jnp.asarray(
        [float(sales.price_fixed[fs.buffer_index]) for fs in folded_sales], dtype=jnp.float32
    )
    sale_price_series = np.array(
        [int(sales.price_series[fs.buffer_index]) for fs in folded_sales], dtype=np.int64
    ).reshape(n_sales)
    # Same-pool ((agent, account, asset)) earlier-sale mask -> cumulative prior demand on each pool.
    _pool_key = [
        (
            int(sales.agent[fs.buffer_index]),
            int(sales.source_account[fs.buffer_index]),
            int(sales.asset[fs.buffer_index]),
        )
        for fs in folded_sales
    ]
    _prior = np.zeros((n_sales, n_sales), dtype=np.float32)
    for _j in range(n_sales):
        for _k in range(_j):
            if _pool_key[_k] == _pool_key[_j]:
                _prior[_j, _k] = 1.0
    sale_prior_t = jnp.asarray(_prior)
    # Per-sale -> capital-gain-agent accrual map (the sale's agent's cg buckets).
    sale_cg_map_t = jnp.asarray(
        np.array([(plan.capital_gain_agent_codes == fs.agent_code) for fs in folded_sales], dtype=np.float32).reshape(
            n_sales, p.capital_gain_agent_count
        )
    )
    # TLH give-back: active harvest policies' lot masks (others zeroed). Used to drain each policy's
    # cumulative harvested loss proportionally to units sold (the per-sale telescoping reduces to a
    # per-policy rate `tlh0 / pre_sale_units`).
    _hp = plan.harvest_policies
    _hp_active = (_hp.gain_profile_index >= 0)[:, None]
    sale_policy_mask_t = jnp.asarray((_hp.lot_mask & _hp_active).astype(np.float32))  # (policy, L)
    folded_purchases = [
        _FoldedPurchase(
            buffer_index=prop,
            month=int(props.month[prop]),
            stake_contribution=float(props.stake_contribution[prop]),
            buyer_slot=int(props.buyer_slot[prop]),
            seller_slot=int(props.seller_slot[prop]),
            mortgage_slot=int(props.mortgage_slot[prop]),
        )
        for prop in range(props.month.shape[0])
        if int(props.month[prop]) >= 0
    ]
    # Static per-purchase plan columns (folded order). Purchases are independent — each fires on its
    # own month into its own property buffer — so the month-step applies them as batched scatters
    # (over these arrays) instead of a Python loop over entities. `pur_mort_rows`/`pur_mort_idx`
    # select the financed subset (distinct liability slots) for mortgage origination.
    pur_buf = np.array([fp.buffer_index for fp in folded_purchases], dtype=np.int64)
    pur_month = np.array([fp.month for fp in folded_purchases], dtype=np.int64)
    pur_stake = np.array([fp.stake_contribution for fp in folded_purchases], dtype=np.float32)
    pur_buyer = np.array([fp.buyer_slot for fp in folded_purchases], dtype=np.int64)
    pur_seller = np.array([fp.seller_slot for fp in folded_purchases], dtype=np.int64)
    pur_mort = np.array([fp.mortgage_slot for fp in folded_purchases], dtype=np.int64)
    pur_mort_rows = np.flatnonzero(pur_mort >= 0)
    pur_mort_idx = pur_mort[pur_mort_rows]
    # Whole-horizon `(months, slots)` plan tables live as device arrays in the closure; each step
    # indexes them by the traced `month`. Built explicitly (no getattr) so a field rename is caught.
    t = plan.transfers
    tr = {
        "cause": jnp.asarray(t.cause),
        "kind": jnp.asarray(t.amount_kind),
        "fixed": jnp.asarray(t.amount_fixed),
        "base": jnp.asarray(t.amount_base),
        "series": jnp.asarray(t.amount_series),
        "base_month": jnp.asarray(t.amount_base_month),
        "period": jnp.asarray(t.amount_period),
        "from_slot": jnp.asarray(t.from_slot),
        "to_slot": jnp.asarray(t.to_slot),
        "income_profile": jnp.asarray(t.income_profile),
        "deduction_profile": jnp.asarray(t.deduction_profile),
    }
    ob = plan.obligations
    og = {
        "cause": jnp.asarray(ob.cause),
        "source_kind": jnp.asarray(ob.source_kind),
        "amount_kind": jnp.asarray(ob.amount_kind),
        "amount_fixed": jnp.asarray(ob.amount_fixed),
        "amount_base": jnp.asarray(ob.amount_base),
        "amount_series": jnp.asarray(ob.amount_series),
        "amount_base_month": jnp.asarray(ob.amount_base_month),
        "amount_period": jnp.asarray(ob.amount_period),
        "agent": jnp.asarray(ob.agent),
        "from_slot": jnp.asarray(ob.from_slot),
        "to_slot": jnp.asarray(ob.to_slot),
        "deduction_profile": jnp.asarray(ob.deduction_profile),
        "deductible_fraction": jnp.asarray(ob.deductible_fraction),
        "property_tax_profile": jnp.asarray(ob.property_tax_profile),
        "property_slot": jnp.asarray(ob.property_slot),
    }
    # Per-(month, slot) static obligation-accrual tables — the host-side precompute of
    # `_apply_obligation_accruals`, vectorized over the whole horizon at once (everything here is a
    # function of the static plan only). The scan indexes these by the traced month and runs the
    # branch-free `_obligation_accruals_jit` core against the live property/liability/tax-liability
    # state. Covers every source kind; kinds with no backing entity are masked off inside the core.
    liabs = plan.liabilities
    acc_kind = ob.source_kind
    acc = {
        "kind": jnp.asarray(acc_kind),
        "valid": jnp.asarray((ob.cause >= 0) & (acc_kind >= 0)),
        "prop_idx": jnp.asarray(np.where(acc_kind == ObligationSource.PROPERTY_TAX, ob.source_index, 0)),
        "liab_idx": jnp.asarray(np.where(acc_kind == ObligationSource.MORTGAGE_PAYMENT, ob.source_index, 0)),
    }
    acc_prop_idx_np = np.where(acc_kind == ObligationSource.PROPERTY_TAX, ob.source_index, 0)
    acc_pt_rate = np.where(
        np.isnan(ob.amount_fixed), _np_gather(props.location_tax_rate, acc_prop_idx_np, 0.0), ob.amount_fixed
    )
    acc["pt_amount"] = jnp.asarray(
        _np_gather(props.initial_assessed_value, acc_prop_idx_np, 0.0) * acc_pt_rate / 12.0
        + _np_gather(props.special_assessment_annual_usd, acc_prop_idx_np, 0.0) / 12.0
    )
    acc["pt_prop_month"] = jnp.asarray(_np_gather(props.month, acc_prop_idx_np, 0))
    acc_liab_idx_np = np.where(acc_kind == ObligationSource.MORTGAGE_PAYMENT, ob.source_index, 0)
    acc_liab_prop_slot = _np_gather(liabs.property_slot, acc_liab_idx_np, -1)
    acc["mort_prop_month"] = jnp.asarray(
        _np_gather(props.month, np.where(acc_liab_prop_slot >= 0, acc_liab_prop_slot, 0), 0)
    )
    acc["mort_rate"] = jnp.asarray(_np_gather(liabs.annual_rate, acc_liab_idx_np, 0.0))
    # Property slot of each mortgage obligation's liability (for the Schedule-E rented-share of interest).
    acc["mort_prop_idx"] = jnp.asarray(np.where(acc_liab_prop_slot >= 0, acc_liab_prop_slot, 0))
    acc_prof_idx = np.where(acc_kind >= ObligationSource.ESTIMATED_TAX, ob.source_index, 0)
    acc_est_prior = _np_gather(plan.tax.profile_prior_year_tax, acc_prof_idx, 0.0)
    acc["est_prior"] = jnp.asarray(acc_est_prior)
    acc["est_quarterly"] = jnp.asarray(acc_est_prior / 4.0)
    tax_year_end = (np.arange(horizon) // 12 - 1) * 12 + 11
    acc["trueup_sel"] = jnp.asarray(
        (
            (plan.tax_liabilities.profile_index[None, None, :] == acc_prof_idx[:, :, None])
            & (plan.tax_liabilities.year_end_month[None, None, :] == tax_year_end[:, None, None])
        ).astype(np.float64)
    )
    # Tax-profile index per estimated/true-up obligation slot (for settlement scatter to profile rows);
    # and the (static, per-month) prior year-end being settled.
    acc["prof_idx"] = jnp.asarray(np.where(acc_kind >= ObligationSource.ESTIMATED_TAX, ob.source_index, -1))
    acc["tax_year_end"] = jnp.asarray(tax_year_end)

    # Liquidity policies: resolve each policy's (asset, source-account) FIFO pools host-side. Sells
    # raise cash to cover the month's obligation demand (+ an optional buffer) before the funding check.
    liq_policies = plan.liquidity_policies
    liq_policy_count = int(liq_policies.cash_slot.shape[0])
    liq_max_assets = int(liq_policies.assets.shape[1]) if liq_policies.assets.ndim == 2 else 1
    folded_liquidity: list[_FoldedLiquidity] = []
    for policy in range(liq_policy_count):
        if int(liq_policies.cash_slot[policy]) < 0:
            continue  # padded sentinel policy
        agent_code = int(liq_policies.agent[policy])
        pools: list[_LiquidityPool] = []
        for asset_idx in range(liq_max_assets):
            asset_code = int(liq_policies.assets[policy, asset_idx])
            series_index = int(liq_policies.asset_series[policy, asset_idx])
            if asset_code < 0 or series_index < 0:
                continue
            for account in liq_policies.source_accounts[policy]:
                account_code = int(account)
                if account_code < 0:
                    continue
                ordered = lot_order_for_pool(
                    lot_agent_codes=plan.lot_agent_codes,
                    lot_account_codes=plan.lot_account_codes,
                    lot_asset_codes=plan.lot_asset_codes,
                    lot_purchase_month=plan.lot_purchase_month,
                    lot_id_codes=plan.lot_id_codes,
                    agent_code=agent_code,
                    account_code=account_code,
                    asset_code=asset_code,
                )
                if ordered.size:
                    pools.append(
                        _LiquidityPool(
                            asset_idx=asset_idx,
                            series_index=series_index,
                            ordered_lots=tuple(int(lot) for lot in ordered),
                        )
                    )
        folded_liquidity.append(
            _FoldedLiquidity(
                policy_index=policy,
                agent=agent_code,
                cash_slot=int(liq_policies.cash_slot[policy]),
                trigger=(
                    int(liq_policies.trigger_kind[policy]),
                    float(liq_policies.trigger_fixed[policy]),
                    float(liq_policies.trigger_base[policy]),
                    int(liq_policies.trigger_series[policy]),
                    int(liq_policies.trigger_base_month[policy]),
                    int(liq_policies.trigger_period[policy]),
                ),
                sale=(
                    int(liq_policies.sale_kind[policy]),
                    float(liq_policies.sale_fixed[policy]),
                    float(liq_policies.sale_base[policy]),
                    int(liq_policies.sale_series[policy]),
                    int(liq_policies.sale_base_month[policy]),
                    int(liq_policies.sale_period[policy]),
                ),
                pools=tuple(pools),
            )
        )
    lot_axis = max(1, p.lot_count)

    # Private-equity tenders: per-issuer static FIFO data. The channel device tables (marks, regimes,
    # capacities, ...) are seed-varying, so they arrive as the traced `pe_ch` dict (see `_program_impl`)
    # rather than baked here; mark validation lives in `run_jax_scan` (it runs on every draw).
    pe_issuers = plan.pe_issuers
    pe_policies = plan.pe_policies
    pe_issuer_count = int(pe_issuers.codes.shape[0])
    n_pe_kinds = len(PrivateEquityDispositionKind)
    folded_pe: list[_FoldedPE] = []
    for issuer_idx in range(pe_issuer_count):
        if int(pe_issuers.codes[issuer_idx]) < 0:
            continue
        lot_indices = np.flatnonzero(pe_issuers.lot_mask[issuer_idx])
        if lot_indices.size == 0:
            continue
        ordered = lot_indices[np.argsort(plan.lot_purchase_month[lot_indices], kind="stable")]
        policy_idx = int(pe_issuers.policy_index[issuer_idx])
        if policy_idx >= 0:
            owner_non_pe = tuple(int(lot) for lot in np.flatnonzero(pe_policies.owner_non_pe_lot_mask[policy_idx]))
            folded_pe.append(
                _FoldedPE(
                    issuer_idx=issuer_idx,
                    policy_idx=policy_idx,
                    ordered=tuple(int(lot) for lot in ordered),
                    proceeds_cash_slot=int(pe_policies.proceeds_cash_slot[policy_idx]),
                    owner_agent=int(pe_policies.owner_agent[policy_idx]),
                    floor_kind=int(pe_policies.floor_kind[policy_idx]),
                    floor_fixed=float(pe_policies.floor_fixed[policy_idx]),
                    floor_base=float(pe_policies.floor_base[policy_idx]),
                    floor_series=int(pe_policies.floor_series[policy_idx]),
                    floor_base_month=int(pe_policies.floor_base_month[policy_idx]),
                    floor_period=int(pe_policies.floor_period[policy_idx]),
                    owner_non_pe_lot_indices=owner_non_pe,
                )
            )
        else:
            folded_pe.append(
                _FoldedPE(
                    issuer_idx=issuer_idx,
                    policy_idx=policy_idx,
                    ordered=tuple(int(lot) for lot in ordered),
                    proceeds_cash_slot=NO_CODE,
                    owner_agent=NO_CODE,
                    floor_kind=AMOUNT_FIXED,
                    floor_fixed=0.0,
                    floor_base=0.0,
                    floor_series=NO_CODE,
                    floor_base_month=0,
                    floor_period=1,
                    owner_non_pe_lot_indices=(),
                )
            )

    # TLH harvest policies: per-policy static data (the jitted core books a calibrated capital loss).
    harvest = plan.harvest_policies
    folded_harvest: list[_FoldedHarvest] = []
    for policy_idx in range(harvest.gain_profile_index.shape[0]):
        gain_profile = int(harvest.gain_profile_index[policy_idx])
        lot_indices = np.flatnonzero(harvest.lot_mask[policy_idx])
        if gain_profile < 0 or lot_indices.size == 0:
            continue
        params = harvest.params[policy_idx]
        folded_harvest.append(
            _FoldedHarvest(
                policy_idx=policy_idx,
                gain_profile=gain_profile,
                lot_indices=tuple(int(lot) for lot in lot_indices),
                series_index=int(harvest.series_index[policy_idx]),
                peak_annual_yield=float(params.peak_annual_yield),
                floor_annual_yield=float(params.floor_annual_yield),
                maturity_decay_exponent=float(params.maturity_decay_exponent),
                drawdown_sensitivity=float(params.drawdown_sensitivity),
                short_term_fraction=float(harvest.short_term_fraction[policy_idx]),
            )
        )

    # December year-end tax pass: static tables. `link_count`/`taxliab_count`/`profile_count` are
    # compile-time. `tax_slot_table[m, link]` is the tax-liability slot a link accrues at year-end
    # month m (or -1). `cg_rep_profile[gp]` is the representative tax profile a cg-agent's ordinary
    # offset lands on (the netting is per cg-agent; the offset goes to its first tax profile).
    taxc = plan.tax
    link_count = int(taxc.link_profile.shape[0])
    profile_count = int(p.tax_profile_count)
    taxliab_count = int(p.tax_liability_count)
    tlq = plan.tax_liabilities
    tax_slot_table_np = np.full((horizon, max(1, link_count)), NO_CODE, dtype=np.int64)
    for m in range(11, horizon, 12):
        for link in range(link_count):
            sel = np.flatnonzero(
                (tlq.profile_index == int(taxc.link_profile[link]))
                & (tlq.link_index == link)
                & (tlq.year_end_month == m)
            )
            if sel.size:
                tax_slot_table_np[m, link] = int(sel[0])
    tax_slot_table = jnp.asarray(tax_slot_table_np)
    cg_rep_profile = np.full(max(1, p.capital_gain_agent_count), NO_CODE, dtype=np.int64)
    for profile in range(profile_count):
        gp = int(plan.tax_profile_capital_gain_index[profile])
        if gp >= 0 and cg_rep_profile[gp] < 0:
            cg_rep_profile[gp] = profile
    salt_link_active = plan.salt.link_active  # bool array per link
    cap_year_index_by_month = np.minimum(np.arange(horizon) // 12, plan.salt.cap_by_year.shape[1] - 1)
    # Per-(link, month) SALT cap (cap_by_year indexed by the month's tax year), so the traced month
    # can index it directly inside the pass.
    salt_cap_table = (
        jnp.asarray(plan.salt.cap_by_year[:, cap_year_index_by_month]) if link_count else jnp.zeros((0, horizon))
    )

    salt_contributing_mask = (
        jnp.asarray(plan.salt.contributing_mask.astype(np.float64))
        if link_count
        else jnp.zeros((0, max(1, link_count)))
    )
    pe_owner_cash_mask = (
        jnp.asarray(pe_policies.owner_cash_mask)
        if pe_policies.owner_cash_mask.size
        else jnp.zeros((max(1, pe_issuer_count), p.cash_count))
    )

    baked = _Baked(
        cash0=cash0,
        ordinary0=ordinary0,
        property_tax_ytd0=property_tax_ytd0,
        lot0=lot0,
        cg_active0=cg_active0,
        cg_ytd0=cg_ytd0,
        tlh0=tlh0,
        property_rented_fraction_0=property_rented_fraction_0,
        property_building_basis_0=property_building_basis_0,
        prop0=jnp.zeros((p.property_count, r)),
        liab0=jnp.zeros((p.liability_count, r)),
        tr=tr,
        og=og,
        acc=acc,
        sale_months_t=sale_months_t,
        sale_qty_t=sale_qty_t,
        sale_prior_t=sale_prior_t,
        sale_cg_map_t=sale_cg_map_t,
        sale_policy_mask_t=sale_policy_mask_t,
        sale_price_fixed_t=sale_price_fixed_t,
        property_is_primary_table=property_is_primary_table,
        tax_slot_table=tax_slot_table,
        salt_cap_table=salt_cap_table,
        lot_purchase_month=jnp.asarray(plan.lot_purchase_month),
        capital_gain_agent_codes=jnp.asarray(plan.capital_gain_agent_codes),
        cg_rep_profile=jnp.asarray(cg_rep_profile),
        property_owner_profile_index=jnp.asarray(plan.property_owner_profile_index),
        liability_owner_profile_index=jnp.asarray(plan.liability_owner_profile_index),
        salt_contributing_mask=salt_contributing_mask,
        lot_asset_series_index=jnp.asarray(plan.lot_asset_series_index),
        pe_owner_cash_mask=pe_owner_cash_mask,
    )

    # Capital-gain accrual targets: each agent code that sells (liquidity / PE owners) maps to the
    # capital-gain profile rows whose agent code matches (the de-`plan`-ed `_record_capital_gains`).
    cg_agent_codes = {fl.agent for fl in folded_liquidity} | {
        fpe.owner_agent for fpe in folded_pe if fpe.owner_agent >= 0
    }
    cg_targets = tuple(
        _CapitalGainTarget(
            agent_code=agent_code,
            profiles=tuple(int(profile) for profile in np.flatnonzero(plan.capital_gain_agent_codes == agent_code)),
        )
        for agent_code in sorted(cg_agent_codes)
    )
    link_tax_static = tuple(
        _LinkTaxStatic(
            link=link,
            profile=int(taxc.link_profile[link]),
            gain_profile=int(plan.tax_profile_capital_gain_index[int(taxc.link_profile[link])]),
            section_1250_rate=float(taxc.link_section_1250_rate[link]),
            mid_active=bool(plan.mid.link_active[link]),
            ordinary_count=int(taxc.link_ordinary_count[link]),
            has_ltcg=int(taxc.link_has_ltcg[link]),
            ltcg_count=int(taxc.link_ltcg_count[link]),
            salt_active=bool(salt_link_active[link]),
        )
        for link in range(link_count)
    )
    structure = _Structure(
        rollout_count=r,
        horizon=horizon,
        cash_count=p.cash_count,
        lot_count=p.lot_count,
        property_count=p.property_count,
        liability_count=p.liability_count,
        tax_profile_count=p.tax_profile_count,
        capital_gain_agent_count=p.capital_gain_agent_count,
        tax_liability_count=p.tax_liability_count,
        harvest_policy_count=p.harvest_policy_count,
        scheduled_sale_count=p.scheduled_sale_count,
        link_count=link_count,
        profile_count=profile_count,
        taxliab_count=taxliab_count,
        n_sales=n_sales,
        sale_max_pool=sale_max_pool,
        lot_axis=lot_axis,
        liq_policy_count=liq_policy_count,
        liq_max_assets=liq_max_assets,
        pe_issuer_count=pe_issuer_count,
        n_pe_kinds=n_pe_kinds,
        folded_lifecycle=tuple(folded_lifecycle),
        folded_pr=tuple(folded_pr),
        folded_liquidity=tuple(folded_liquidity),
        folded_pe=tuple(folded_pe),
        folded_harvest=tuple(folded_harvest),
        salt_link_active=tuple(bool(salt_link_active[link]) for link in range(link_count)),
        sale_pslot=tuple(int(x) for x in sale_pslot),
        sale_bufidx=tuple(int(x) for x in sale_bufidx),
        sale_olots=tuple(tuple(int(x) for x in row) for row in sale_olots),
        sale_price_series=tuple(int(x) for x in sale_price_series),
        pur_buf=tuple(int(x) for x in pur_buf),
        pur_month=tuple(int(x) for x in pur_month),
        pur_stake=tuple(float(x) for x in pur_stake),
        pur_buyer=tuple(int(x) for x in pur_buyer),
        pur_seller=tuple(int(x) for x in pur_seller),
        pur_mort_rows=tuple(int(x) for x in pur_mort_rows),
        pur_mort_idx=tuple(int(x) for x in pur_mort_idx),
        folded_purchases_present=bool(folded_purchases),
        folded_sales_present=bool(folded_sales),
        cg_targets=cg_targets,
        link_tax_static=link_tax_static,
        link_profile=tuple(int(taxc.link_profile[link]) for link in range(link_count)),
        profile_gain_index=tuple(int(x) for x in plan.tax_profile_capital_gain_index),
    )

    meta = _ScanMeta(
        folded_sales=folded_sales,
        folded_purchases=folded_purchases,
        folded_lifecycle=folded_lifecycle,
        folded_pr=folded_pr,
        folded_sale_events=folded_sale_events,
        folded_liquidity=folded_liquidity,
        folded_pe=folded_pe,
        link_count=link_count,
        liability_count=p.liability_count,
        horizon=horizon,
    )
    return baked, structure, p, meta


@partial(jax.jit, static_argnames=("p", "structure"))
def _program_impl(
    external_values: jnp.ndarray,
    pe_ch: dict[str, jnp.ndarray],
    cfg: _TracedConfig,
    baked: _Baked,
    p: SlotPlan,
    structure: _Structure,
) -> tuple:
    """Module-level, natively-cached scan program. `external_values` / `pe_ch` / `cfg` are TRACED
    (seed-varying series + swept numeric config); `baked` is a TRACED pytree of every device array the
    bodies close over; `p` (`SlotPlan`) and `structure` are STATIC (`static_argnames`), so JAX keys the
    compile cache on them and reuses the executable across identical-structure calls and across traced
    value/seed sweeps. A structural change is a fresh static key (exactly one extra compile)."""
    r = structure.rollout_count
    horizon = structure.horizon
    lot_count = structure.lot_count
    link_count = structure.link_count
    profile_count = structure.profile_count
    taxliab_count = structure.taxliab_count
    n_sales = structure.n_sales
    sale_max_pool = structure.sale_max_pool
    lot_axis = structure.lot_axis
    liq_policy_count = structure.liq_policy_count
    liq_max_assets = structure.liq_max_assets
    pe_issuer_count = structure.pe_issuer_count
    n_pe_kinds = structure.n_pe_kinds
    folded_lifecycle = structure.folded_lifecycle
    folded_pr = structure.folded_pr
    folded_liquidity = structure.folded_liquidity
    folded_pe = structure.folded_pe
    folded_harvest = structure.folded_harvest
    folded_sale_events = [ev for ev in folded_lifecycle if ev.kind == LifecycleKind.SALE]
    salt_link_active = structure.salt_link_active
    link_tax_static = structure.link_tax_static
    link_profile = structure.link_profile
    profile_gain_index = structure.profile_gain_index
    cg_profiles_by_agent = {ct.agent_code: ct.profiles for ct in structure.cg_targets}
    # Static index/selection arrays (rebuilt from the hashable tuples carried in `structure`).
    sale_pslot = np.asarray(structure.sale_pslot, dtype=np.int64).reshape(n_sales)
    sale_bufidx = np.asarray(structure.sale_bufidx, dtype=np.int64).reshape(n_sales)
    sale_olots = np.asarray(structure.sale_olots, dtype=np.int64).reshape(n_sales, sale_max_pool)
    sale_price_series = np.asarray(structure.sale_price_series, dtype=np.int64).reshape(n_sales)
    pur_buf = np.asarray(structure.pur_buf, dtype=np.int64)
    pur_month = np.asarray(structure.pur_month, dtype=np.int64)
    pur_stake = np.asarray(structure.pur_stake, dtype=np.float32)
    pur_buyer = np.asarray(structure.pur_buyer, dtype=np.int64)
    pur_seller = np.asarray(structure.pur_seller, dtype=np.int64)
    pur_mort_rows = np.asarray(structure.pur_mort_rows, dtype=np.int64)
    pur_mort_idx = np.asarray(structure.pur_mort_idx, dtype=np.int64)
    folded_purchases = structure.folded_purchases_present
    folded_sales = structure.folded_sales_present
    # Device arrays unpacked from the baked pytree (SAME names the bodies use).
    cash0 = baked.cash0
    ordinary0 = baked.ordinary0
    property_tax_ytd0 = baked.property_tax_ytd0
    lot0 = baked.lot0
    cg_active0 = baked.cg_active0
    cg_ytd0 = baked.cg_ytd0
    tlh0 = baked.tlh0
    property_rented_fraction_0 = baked.property_rented_fraction_0
    property_building_basis_0 = baked.property_building_basis_0
    prop0 = baked.prop0
    liab0 = baked.liab0
    tr = dict(baked.tr)  # copy: `fixed`/`base` are overwritten with the traced cfg values below
    og = baked.og
    acc = baked.acc
    sale_months_t = baked.sale_months_t
    sale_qty_t = baked.sale_qty_t
    sale_prior_t = baked.sale_prior_t
    sale_cg_map_t = baked.sale_cg_map_t
    sale_policy_mask_t = baked.sale_policy_mask_t
    sale_price_fixed_t = baked.sale_price_fixed_t
    property_is_primary_table = baked.property_is_primary_table
    tax_slot_table = baked.tax_slot_table
    salt_cap_table = baked.salt_cap_table
    lot_purchase_month = baked.lot_purchase_month
    cg_rep_profile = baked.cg_rep_profile
    property_owner_profile_index = baked.property_owner_profile_index
    liability_owner_profile_index = baked.liability_owner_profile_index
    salt_contributing_mask = baked.salt_contributing_mask
    lot_asset_series_index = baked.lot_asset_series_index
    pe_owner_cash_mask = baked.pe_owner_cash_mask
    # Swept numeric config (traced): cost basis + the transfer-amount entries of the `tr` table.
    tcfg = cfg
    cost_basis_per_unit = cfg.cost_basis_per_unit
    tr["fixed"] = cfg.transfer_amount_fixed
    tr["base"] = cfg.transfer_amount_base

    def december_tax(
        ordinary: jnp.ndarray,
        cg_ytd: jnp.ndarray,
        carryforward: jnp.ndarray,
        recapture: jnp.ndarray,
        property_tax_ytd: jnp.ndarray,
        liab_interest_ytd: jnp.ndarray,
        liab_rental_ytd: jnp.ndarray,
        property_dep_ytd: jnp.ndarray,
        taxliab_active: jnp.ndarray,
        taxliab_amount: jnp.ndarray,
        active: jnp.ndarray,
        month: jnp.ndarray,
    ):
        """Branch-free December (`month % 12 == 11`) year-end tax pass, gated per-rollout by `dec`.

        Returns the post-pass YTD/carryforward/tax-liability state plus the 13 per-link tax buffer
        slabs `(link_count, R)`. For non-December months every output reduces to the inputs / zeros.
        """
        dec = (month % 12 == 11) & active  # (R,)
        liabs_view = LiabilityState(
            active=taxliab_active,  # unused by _compute_tax_for_link
            principal=liab_interest_ytd,  # unused
            monthly_payment=liab_interest_ytd,  # unused
            interest_ytd=liab_interest_ytd,
            principal_ytd=liab_interest_ytd,  # unused
            rental_interest_ytd=liab_rental_ytd,
        )

        # Schedule E: §168 depreciation + rented-share mortgage interest, deducted from each entity's
        # owner tax profile. Vectorized over the property / liability axis: scatter-add the (December-
        # masked) amounts to their owner-profile rows; entities with no owner profile (index < 0) route
        # to `_scatter_rows`'s dump row and contribute nothing.
        dec_col = dec[None, :]
        ordinary = ordinary + _scatter_rows(
            jnp.zeros_like(ordinary), property_owner_profile_index, -jnp.where(dec_col, property_dep_ytd, 0.0)
        )
        ordinary = ordinary + _scatter_rows(
            jnp.zeros_like(ordinary), liability_owner_profile_index, -jnp.where(dec_col, liab_rental_ytd, 0.0)
        )

        # §1211/§1212 netting, vectorized over the capital-gain-agent axis (each agent netted once).
        # Only agents reachable from a tax profile (`cg_rep_profile >= 0`) are netted — matching the
        # per-profile loop — and the ordinary offset scatters to each agent's representative profile.
        net_st, net_lt, ord_offset, carry_out = _net_capital_gains_jnp(
            cg_ytd[:, CapitalGainClassification.SHORT_TERM, :],
            cg_ytd[:, CapitalGainClassification.LONG_TERM, :],
            carryforward,
        )
        # `cg_rep_profile` is padded to `max(1, count)`; align it to the actual cap-gain-agent axis
        # (which may be 0 when the scenario has no tax profiles).
        cg_rep = cg_rep_profile[: cg_ytd.shape[0]]
        do_net = dec_col & (cg_rep >= 0)[:, None]
        cg_ytd = cg_ytd.at[:, CapitalGainClassification.SHORT_TERM, :].set(
            jnp.where(do_net, net_st, cg_ytd[:, CapitalGainClassification.SHORT_TERM, :])
        )
        cg_ytd = cg_ytd.at[:, CapitalGainClassification.LONG_TERM, :].set(
            jnp.where(do_net, net_lt, cg_ytd[:, CapitalGainClassification.LONG_TERM, :])
        )
        carryforward = jnp.where(do_net, carry_out, carryforward)
        ordinary = ordinary + _scatter_rows(jnp.zeros_like(ordinary), cg_rep, -jnp.where(do_net, ord_offset, 0.0))

        # Two-pass SALT bracket math; collect per-link tax + breakdown slabs.
        annual_tax_by_link = jnp.zeros((r, max(1, link_count)))
        zero_salt = jnp.zeros(r)
        breakdown = [jnp.zeros((max(1, link_count), r)) for _ in range(13)]

        def run_link(link: int, salt_deduction: jnp.ndarray, ann: jnp.ndarray) -> jnp.ndarray:
            mid, itemized, ord_taxable, cap_taxable, ord_tax, cap_tax = _compute_tax_for_link(
                link_tax_static[link],
                tcfg,
                ordinary,
                cg_ytd,
                recapture,
                liabs_view,
                salt_deduction=salt_deduction,
                rollout_count=r,
            )
            profile = link_profile[link]
            gp = profile_gain_index[profile]
            tax = ord_tax + cap_tax
            cols = [
                dec.astype(jnp.float32),  # accrual_active flag (->bool post-scan)
                jnp.where(dec, tax, 0.0),
                jnp.where(dec, ordinary[profile], 0.0),
                jnp.where(dec, cg_ytd[gp, CapitalGainClassification.LONG_TERM], 0.0),
                jnp.where(dec, cg_ytd[gp, CapitalGainClassification.SHORT_TERM], 0.0),
                jnp.where(dec, tcfg.link_standard_deduction[link], 0.0),  # traced value
                jnp.where(dec, mid, 0.0),
                jnp.where(dec, salt_deduction, 0.0),
                jnp.where(dec, itemized, 0.0),
                jnp.where(dec, ord_taxable, 0.0),
                jnp.where(dec, cap_taxable, 0.0),
                jnp.where(dec, ord_tax, 0.0),
                jnp.where(dec, cap_tax, 0.0),
            ]
            for b, col in enumerate(cols):
                breakdown[b] = breakdown[b].at[link].set(col)
            return ann.at[:, link].set(tax)

        for link in range(link_count):
            if not bool(salt_link_active[link]):
                annual_tax_by_link = run_link(link, zero_salt, annual_tax_by_link)
        for link in range(link_count):
            if not bool(salt_link_active[link]):
                continue
            profile = link_profile[link]
            state_tax_total = annual_tax_by_link @ salt_contributing_mask[link]
            salt_total = property_tax_ytd[profile] + state_tax_total
            salt_deduction = jnp.minimum(salt_total, salt_cap_table[link][month])
            annual_tax_by_link = run_link(link, salt_deduction, annual_tax_by_link)

        # Accrue this year's tax liabilities (scatter each link's tax to its year-end slot).
        slot_for_link = tax_slot_table[month]  # (link_count,)
        link_tax = annual_tax_by_link.T  # (link_count, R)
        written = _scatter_rows(jnp.zeros((taxliab_count, r)), slot_for_link, jnp.where(dec, link_tax, 0.0))
        written_mask = _scatter_rows(jnp.zeros((taxliab_count, r)), slot_for_link, dec.astype(jnp.float32)) > 0.0
        taxliab_amount = jnp.where(written_mask, written, taxliab_amount)
        taxliab_active = taxliab_active | written_mask

        # Year-end YTD resets for active rollouts (dec).
        notdec = ~dec
        ordinary = ordinary * notdec
        cg_ytd = cg_ytd * notdec[None, None, :]
        property_tax_ytd = property_tax_ytd * notdec
        recapture = recapture * notdec
        liab_interest_ytd = liab_interest_ytd * notdec
        liab_rental_ytd = liab_rental_ytd * notdec
        property_dep_ytd = property_dep_ytd * notdec
        return (
            ordinary,
            cg_ytd,
            carryforward,
            recapture,
            property_tax_ytd,
            liab_interest_ytd,
            liab_rental_ytd,
            property_dep_ytd,
            taxliab_active,
            taxliab_amount,
            tuple(breakdown),
        )

    def step(s: _ScanState, month: jnp.ndarray) -> tuple[_ScanState, tuple[jnp.ndarray, ...]]:
        cash, ordinary, property_tax_ytd, lot_remaining = s.cash, s.ordinary_ytd, s.property_tax_ytd, s.lot_remaining
        cg_active, cg_ytd, tlh = s.capital_gain_active, s.capital_gain_ytd, s.tlh
        property_active, property_basis = s.property_active, s.property_basis
        property_ownership, property_contribution, property_equity = (
            s.property_ownership,
            s.property_contribution,
            s.property_equity,
        )
        property_cum_dep, property_owner_occupied = s.property_cumulative_depreciation, s.property_owner_occupied_months
        property_dep_ytd = s.property_depreciation_ytd
        property_rented_fraction, property_building_basis = s.property_rented_fraction, s.property_building_basis
        oo_window = s.owner_occupied_window
        liab_active, liab_principal, liab_monthly = (
            s.liability_active,
            s.liability_principal,
            s.liability_monthly_payment,
        )
        liab_interest_ytd, liab_principal_ytd = s.liability_interest_ytd, s.liability_principal_ytd
        liab_rental_ytd = s.liability_rental_interest_ytd
        capital_loss_carryforward, recapture_ytd = s.capital_loss_carryforward, s.recapture_section_1250_ytd
        taxliab_active, taxliab_amount = s.tax_liability_active, s.tax_liability_amount
        failed, failed_month = s.failed, s.failed_month
        sale_disp_units, sale_disp_basis = s.sale_disp_units, s.sale_disp_basis
        sale_disp_proceeds, sale_oversell = s.sale_disp_proceeds, s.sale_oversell
        active = ~failed

        # Primary-residence + lifecycle events (first in the month, eager order). Each event fires when
        # its static month equals the traced month, masked per-rollout. is_primary is precomputed
        # per-month host-side; the SALE path uses the §121 owner-occupancy window for the exclusion.
        pr_fired = [jnp.where(month == pr_m, active, jnp.zeros_like(active)) for _, pr_m in folded_pr]
        le_fired: list[jnp.ndarray] = []
        sale_traces: list[tuple] = []
        for ev in folded_lifecycle:
            ev_month, ev_kind, ev_prop = ev.month, ev.kind, ev.property_slot
            fires = month == ev_month
            active_property = fires & active & property_active[ev_prop]
            if ev_kind == LifecycleKind.FRACTION:
                property_rented_fraction = property_rented_fraction.at[ev_prop].set(
                    jnp.where(active_property, ev.rented_fraction, property_rented_fraction[ev_prop])
                )
            elif ev_kind == LifecycleKind.CAPITAL_IMPROVEMENT:
                amount = ev.amount
                owner_cash_slot = ev.owner_cash_slot
                if owner_cash_slot >= 0:
                    cash = cash.at[owner_cash_slot].add(jnp.where(active_property, -amount, 0.0))
                property_building_basis = property_building_basis.at[ev_prop].add(
                    jnp.where(active_property, amount, 0.0)
                )
            elif ev_kind == LifecycleKind.SALE:
                (
                    cash,
                    property_active,
                    property_rented_fraction,
                    property_building_basis,
                    liab_active,
                    liab_principal,
                    recapture_ytd,
                    cg_active,
                    cg_ytd,
                    sale_trace,
                ) = _scan_property_sale(
                    ev,
                    external_values,
                    cash=cash,
                    property_active=property_active,
                    property_rented_fraction=property_rented_fraction,
                    property_building_basis=property_building_basis,
                    property_cum_dep=property_cum_dep,
                    oo_window=oo_window,
                    liab_active=liab_active,
                    liab_principal=liab_principal,
                    recapture_ytd=recapture_ytd,
                    cg_active=cg_active,
                    cg_ytd=cg_ytd,
                    month=month,
                    active_property=active_property,
                    rollout_count=r,
                )
                sale_traces.append(sale_trace)
            le_fired.append(active_property)

        cash, ordinary, transfer_active, transfer_amount = _transfers_jit(
            tr["cause"][month],
            tr["kind"][month],
            tr["fixed"][month],
            tr["base"][month],
            tr["series"][month],
            tr["base_month"][month],
            tr["period"][month],
            tr["from_slot"][month],
            tr["to_slot"][month],
            tr["income_profile"][month],
            tr["deduction_profile"][month],
            cash,
            ordinary,
            active,
            external_values,
            month,
        )

        # Property purchases (after transfers, before sales — eager order). Vectorized over all real
        # purchases at once (no Python loop): each fires when its static month equals the traced month
        # for the rollouts still active then, into its own property buffer (distinct indices, no
        # cross-purchase dependency). Pure-value purchase amounts are gathered from `tcfg` by index.
        # The down payment (stake_contribution) moves buyer->seller via sentinel-aware scatter-add
        # (shared/absent cash slots fall out, duplicates accumulate); financed purchases originate the
        # mortgage liability (principal + monthly payment set, YTD interest/principal reset).
        mort_orig_rows = jnp.zeros((liab_active.shape[0], r), dtype=bool)
        purchase_active_rows = transfer_active_rows = None
        if folded_purchases:
            fires = (month == pur_month)[:, None] & active[None, :]  # (P, R)
            stake_pos = (pur_stake > 0.0)[:, None]  # (P, 1) static
            # Gathered `tcfg` columns are 1-D per-entity (P,)/(M,) -> `[:, None]` to broadcast over R.
            property_active = property_active.at[pur_buf].set(jnp.where(fires, True, property_active[pur_buf]))
            property_basis = property_basis.at[pur_buf].set(
                jnp.where(fires, tcfg.property_adjusted_basis[pur_buf][:, None], property_basis[pur_buf])
            )
            property_ownership = property_ownership.at[pur_buf].set(
                jnp.where(fires, tcfg.property_ownership[pur_buf][:, None], property_ownership[pur_buf])
            )
            property_contribution = property_contribution.at[pur_buf].set(
                jnp.where(fires, pur_stake[:, None], property_contribution[pur_buf])
            )
            property_equity = property_equity.at[pur_buf].set(
                jnp.where(fires, tcfg.property_equity_ledger[pur_buf][:, None], property_equity[pur_buf])
            )
            stake_flow = jnp.where(fires & stake_pos, pur_stake[:, None], 0.0)  # (P, R)
            cash = _scatter_rows(cash, jnp.asarray(pur_buyer), -stake_flow)
            cash = _scatter_rows(cash, jnp.asarray(pur_seller), stake_flow)
            # Mortgage origination: financed subset only (distinct liability slots -> plain scatter-set).
            mfires = fires[pur_mort_rows]  # (M, R)
            liab_active = liab_active.at[pur_mort_idx].set(jnp.where(mfires, True, liab_active[pur_mort_idx]))
            liab_principal = liab_principal.at[pur_mort_idx].set(
                jnp.where(mfires, tcfg.liability_principal[pur_mort_idx][:, None], liab_principal[pur_mort_idx])
            )
            liab_monthly = liab_monthly.at[pur_mort_idx].set(
                jnp.where(mfires, tcfg.liability_monthly_payment[pur_mort_idx][:, None], liab_monthly[pur_mort_idx])
            )
            liab_interest_ytd = liab_interest_ytd.at[pur_mort_idx].set(
                jnp.where(mfires, 0.0, liab_interest_ytd[pur_mort_idx])
            )
            liab_principal_ytd = liab_principal_ytd.at[pur_mort_idx].set(
                jnp.where(mfires, 0.0, liab_principal_ytd[pur_mort_idx])
            )
            mort_orig_rows = mort_orig_rows.at[pur_mort_idx].set(mfires)
            # Per-purchase event rows for `ys` (folded order): purchase fired; transfer fired (stake>0).
            purchase_active_rows = fires
            transfer_active_rows = fires & stake_pos

        # Scheduled asset sales (before obligations: proceeds can fund the month's obligations).
        # Vectorized over ALL sales at once — no Python loop. The across-sales FIFO is one
        # cumulative-supply (over each pool's lots) x cumulative-demand (over each pool's sales,
        # `sale_prior_t`) interval overlap, so shared pools fall out without sequencing. Each sale's
        # disposition `(sale, lot, R)` accumulates into the carry at its slot (fires once -> horizon
        # collapsed). `L` is padded with a zero dummy lot so the ragged pools share one shape.
        if folded_sales:
            ld = lot_count
            lot_rem_pad = jnp.concatenate([lot_remaining, jnp.zeros((1, r))], axis=0)  # (L+1, R)
            cost_pad = jnp.concatenate([cost_basis_per_unit, jnp.zeros(1)])  # (L+1,)
            lpm_pad = jnp.concatenate([lot_purchase_month.astype(jnp.int32), jnp.zeros(1, jnp.int32)])
            pool_qty = lot_rem_pad[sale_olots]  # (N, P, R) supply per pool lot
            target = jnp.where(
                (active[None, :]) & (month == sale_months_t)[:, None], sale_qty_t[:, None], 0.0
            )  # (N, R)
            prior = sale_prior_t @ target  # (N, R) demand already claimed by earlier same-pool sales
            oversell = target > (pool_qty.sum(axis=1) - prior) + 1e-9  # (N, R)
            d_lo = prior  # demand interval (D_{j-1}, D_j], with oversold sales selling nothing
            d_hi = prior + jnp.where(oversell, 0.0, target)
            s_before = jnp.cumsum(pool_qty, axis=1) - pool_qty  # supply prefix S_{k-1} (N, P, R)
            sold = jnp.maximum(
                0.0, jnp.minimum(d_hi[:, None, :], s_before + pool_qty) - jnp.maximum(d_lo[:, None, :], s_before)
            )

            # TLH give-back (telescoped): each policy drains tlh proportional to units sold of its lots,
            # at rate tlh0 / pre_sale_units; the per-sale realization is `sold * rate` on the sold lots.
            t_policy = sale_policy_mask_t @ lot_remaining  # (policy, R) pre-sale units
            gb_rate = jnp.where(t_policy > 0.0, tlh / jnp.where(t_policy > 0.0, t_policy, 1.0), 0.0)  # (policy, R)
            lot_gb_rate_pad = jnp.concatenate([sale_policy_mask_t.T @ gb_rate, jnp.zeros((1, r))], axis=0)  # (L+1, R)

            # Per-sale price: fixed if set, else the sampled series at this month. Guarded on the static
            # series count (and the series index clamped) so fixed-only sales never gather an empty cube.
            if external_values.shape[0] > 0:
                safe_series = np.where(sale_price_series >= 0, sale_price_series, 0)
                unit_price = jnp.where(
                    jnp.isnan(sale_price_fixed_t)[:, None],
                    external_values[safe_series, :, month],
                    sale_price_fixed_t[:, None],
                )  # (N, R)
            else:
                unit_price = jnp.broadcast_to(sale_price_fixed_t[:, None], (n_sales, r))
            proceeds = sold * unit_price[:, None, :]  # (N, P, R)
            basis = sold * cost_pad[sale_olots][:, :, None]
            gains = proceeds - basis + sold * lot_gb_rate_pad[sale_olots]  # (N, P, R) incl. give-back

            total_sold = jnp.zeros((ld + 1, r)).at[sale_olots].add(sold)  # (L+1, R)
            lot_remaining = lot_remaining - total_sold[:ld]
            tlh = tlh - (sale_policy_mask_t @ total_sold[:ld]) * gb_rate
            cash = cash.at[np.maximum(sale_pslot, 0)].add(
                jnp.where(jnp.asarray(sale_pslot >= 0)[:, None], proceeds.sum(axis=1), 0.0)
            )

            # Capital gains: classify each pool lot long/short, accrue per sale's cg agents via cg_map.
            long_m = (month - lpm_pad[sale_olots]) >= 12  # (N, P)
            gains_long = (gains * long_m[:, :, None]).sum(axis=1)  # (N, R)
            gains_short = (gains * (~long_m)[:, :, None]).sum(axis=1)
            sold_pos = sold > 0.0
            act_long = (sold_pos & long_m[:, :, None]).any(axis=1)  # (N, R)
            act_short = (sold_pos & (~long_m)[:, :, None]).any(axis=1)
            cg_ytd = cg_ytd.at[:, CapitalGainClassification.LONG_TERM, :].add(sale_cg_map_t.T @ gains_long)
            cg_ytd = cg_ytd.at[:, CapitalGainClassification.SHORT_TERM, :].add(sale_cg_map_t.T @ gains_short)
            cg_active = cg_active.at[:, CapitalGainClassification.LONG_TERM, :].set(
                cg_active[:, CapitalGainClassification.LONG_TERM, :]
                | ((sale_cg_map_t.T @ act_long.astype(jnp.float32)) > 0.0)
            )
            cg_active = cg_active.at[:, CapitalGainClassification.SHORT_TERM, :].set(
                cg_active[:, CapitalGainClassification.SHORT_TERM, :]
                | ((sale_cg_map_t.T @ act_short.astype(jnp.float32)) > 0.0)
            )

            # Dispositions: scatter sold/basis/proceeds into each sale's slot (dummy lot clamped; sold 0).
            disp_sale = np.broadcast_to(sale_bufidx[:, None], sale_olots.shape)
            disp_lot = np.minimum(sale_olots, ld - 1)
            sale_disp_units = sale_disp_units.at[disp_sale, disp_lot].add(sold)
            sale_disp_basis = sale_disp_basis.at[disp_sale, disp_lot].add(basis)
            sale_disp_proceeds = sale_disp_proceeds.at[disp_sale, disp_lot].add(proceeds)
            sale_oversell = sale_oversell | oversell.any()

        # Obligation accrual — every source kind, branch-free. Static per-(month,slot) data is sliced
        # from the precomputed `acc` tables; the live property/liability/tax-liability state comes from
        # the carry. Kinds with no backing entity (e.g. mortgages with no liability) mask to inactive.
        slot_active, accrual_due = _obligation_accruals_jit(
            acc["kind"][month],
            acc["valid"][month],
            og["amount_kind"][month],
            og["amount_fixed"][month],
            og["amount_base"][month],
            og["amount_series"][month],
            og["amount_base_month"][month],
            og["amount_period"][month],
            acc["prop_idx"][month],
            acc["pt_amount"][month],
            acc["pt_prop_month"][month],
            acc["liab_idx"][month],
            acc["mort_rate"][month],
            acc["mort_prop_month"][month],
            acc["est_quarterly"][month],
            acc["est_prior"][month],
            acc["trueup_sel"][month],
            property_active,
            liab_principal,
            liab_monthly,
            liab_active,
            taxliab_active,
            taxliab_amount,
            active,
            external_values,
            month,
        )

        # Liquidity-policy sales (before the funding check, eager order): raise cash to cover this
        # month's obligation demand for the policy's account (+ an optional buffer top-up), selling
        # FIFO across the policy's (asset, account) pools sequentially. Branch-free: a pool whose
        # target is 0 sells nothing (the dollar target is capped at the pool's available value).
        liq_disp_active = jnp.zeros((liq_policy_count, liq_max_assets, lot_axis, r), dtype=bool)
        liq_disp_units = jnp.zeros((liq_policy_count, liq_max_assets, lot_axis, r))
        liq_disp_basis = jnp.zeros((liq_policy_count, liq_max_assets, lot_axis, r))
        liq_disp_proceeds = jnp.zeros((liq_policy_count, liq_max_assets, lot_axis, r))
        attempt_policy = jnp.full((slot_active.shape[0], r), NO_CODE, dtype=jnp.int64)
        for lp in folded_liquidity:
            matching = (og["agent"][month] == lp.agent) & (og["from_slot"][month] == lp.cash_slot)  # (slots,)
            hard_demand = jnp.where(matching[:, None] & slot_active, accrual_due, 0.0).sum(axis=0)  # (R,)
            attempt_policy = jnp.where(matching[:, None] & slot_active, lp.policy_index, attempt_policy)
            cash_balance = cash[lp.cash_slot]
            required_sale = jnp.maximum(hard_demand - cash_balance, 0.0)
            post_required_cash = cash_balance + required_sale - hard_demand
            trigger_val = _amount_values_tuple(lp.trigger, external_values, month, r)
            sale_val = _amount_values_tuple(lp.sale, external_values, month, r)
            buffer_sale = jnp.where((sale_val > 0.0) & (post_required_cash < trigger_val), sale_val, 0.0)
            remaining = jnp.where(active, required_sale + buffer_sale, 0.0)
            for pool in lp.pools:
                raw_price = external_values[pool.series_index, :, month]
                valid_price = jnp.isfinite(raw_price) & (raw_price > 0.0)
                unit_price = jnp.where(valid_price, raw_price, 0.0)
                pool_lots = np.asarray(pool.ordered_lots, dtype=np.int64)
                available = lot_remaining[pool_lots].sum(axis=0) * unit_price
                target = jnp.where(valid_price & active, jnp.minimum(jnp.maximum(remaining, 0.0), available), 0.0)
                sold_units, proceeds, basis, _ovr = _fifo_sell_dollars(
                    lot_remaining.T, pool_lots, target, unit_price, cost_basis_per_unit
                )
                lot_remaining = lot_remaining - sold_units.T
                total_proceeds = proceeds.sum(axis=1)
                cash = cash.at[lp.cash_slot].add(total_proceeds)
                cg_active, cg_ytd, tlh = _record_capital_gains(
                    folded_harvest,
                    lot_purchase_month,
                    cg_profiles_by_agent[lp.agent],
                    cg_active,
                    cg_ytd,
                    tlh,
                    lot_remaining,
                    month,
                    sold_units,
                    proceeds - basis,
                )
                liq_disp_active = liq_disp_active.at[lp.policy_index, pool.asset_idx].set(
                    liq_disp_active[lp.policy_index, pool.asset_idx] | (sold_units > 0.0).T
                )
                liq_disp_units = liq_disp_units.at[lp.policy_index, pool.asset_idx].add(sold_units.T)
                liq_disp_basis = liq_disp_basis.at[lp.policy_index, pool.asset_idx].add(basis.T)
                liq_disp_proceeds = liq_disp_proceeds.at[lp.policy_index, pool.asset_idx].add(proceeds.T)
                remaining = jnp.maximum(remaining - total_proceeds, 0.0)

        agent_row, from_row = og["agent"][month], og["from_slot"][month]
        group_matrix = (agent_row[:, None] == agent_row[None, :]) & (from_row[:, None] == from_row[None, :])
        funded = _obligation_group_funded_jit(group_matrix, from_row, cash, slot_active, accrual_due)

        property_slot = og["property_slot"][month]
        paid, paid_buffer, cash, ordinary, property_tax_ytd, shortfall, failure_active, failed, failed_month = (
            _settlement_core_jit(
                from_row,
                og["to_slot"][month],
                og["deduction_profile"][month],
                og["deductible_fraction"][month],
                og["property_tax_profile"][month],
                jnp.where(property_slot < 0, 0, property_slot),
                property_slot >= 0,
                og["property_tax_profile"][month] >= 0,
                og["deduction_profile"][month] >= 0,
                slot_active,
                accrual_due,
                funded,
                cash,
                ordinary,
                property_tax_ytd,
                property_rented_fraction,
                failed,
                failed_month,
                month,
            )
        )

        # Mortgage payments: split each paid mortgage bill into interest (rate/12 on the outstanding
        # principal, capped at the payment) and principal (the remainder, capped at the balance), then
        # pay down the liability and accrue the YTD interest/principal (+ the rented share for Sch E).
        # Non-mortgage slots route to the sentinel index -1, so `_scatter_rows` ignores them.
        is_mortgage = (og["source_kind"][month] == ObligationSource.MORTGAGE_PAYMENT) & (og["cause"][month] >= 0)
        mort_liab_idx = jnp.where(is_mortgage, acc["liab_idx"][month], -1)
        principal_before = _gather_rows(liab_principal, jnp.where(is_mortgage, acc["liab_idx"][month], 0))
        interest = jnp.minimum(principal_before * acc["mort_rate"][month][:, None] / 12.0, paid_buffer)
        principal_paid = jnp.minimum(jnp.maximum(paid_buffer - interest, 0.0), principal_before)
        mort_paid = is_mortgage[:, None] & paid
        interest_m = jnp.where(mort_paid, interest, 0.0)
        principal_m = jnp.where(mort_paid, principal_paid, 0.0)
        rented_per_slot = _gather_rows(property_rented_fraction, acc["mort_prop_idx"][month])
        liab_principal = _scatter_rows(liab_principal, mort_liab_idx, -principal_m)
        liab_interest_ytd = _scatter_rows(liab_interest_ytd, mort_liab_idx, interest_m)
        liab_principal_ytd = _scatter_rows(liab_principal_ytd, mort_liab_idx, principal_m)
        liab_rental_ytd = _scatter_rows(liab_rental_ytd, mort_liab_idx, interest_m * rented_per_slot)
        # Mortgage-payment event slabs, scattered from obligation slots to their liability rows.
        liab_count = liab_principal.shape[0]
        mort_pay_active = _scatter_rows(jnp.zeros((liab_count, r)), mort_liab_idx, mort_paid.astype(jnp.float32)) > 0.0
        mort_pay_interest = _scatter_rows(jnp.zeros((liab_count, r)), mort_liab_idx, interest_m)
        mort_pay_principal = _scatter_rows(jnp.zeros((liab_count, r)), mort_liab_idx, principal_m)
        mort_pay_total = _scatter_rows(
            jnp.zeros((liab_count, r)), mort_liab_idx, jnp.where(mort_paid, paid_buffer, 0.0)
        )
        mort_orig = mort_orig_rows

        # Tax-liability settlement: a paid TAX_TRUE_UP fully clears its profile-year's liability (the
        # estimated prepayments covered the rest). `trueup_sel` maps each true-up obligation slot to
        # the tax-liability slots of the year it settles; `paid` (active & funded) gates it.
        trueup_sel_m = acc["trueup_sel"][month]  # (slots, taxliab)
        is_trueup = (og["source_kind"][month] == ObligationSource.TAX_TRUE_UP) & (og["cause"][month] >= 0)
        trueup_paid = is_trueup[:, None] & paid  # (slots, R)
        eligible = jnp.where(taxliab_active, taxliab_amount, 0.0)  # (taxliab, R)
        actual_per_trueup = trueup_sel_m @ eligible  # (slots, R): full year tax owed
        settle_k = (trueup_sel_m.astype(bool)[:, :, None] & trueup_paid[:, None, :]).any(axis=0)  # (taxliab, R)
        taxliab_amount = jnp.where(settle_k, 0.0, taxliab_amount)
        # Settlement event buffers, scattered to tax-profile rows (one true-up per profile per month).
        settle_prof_idx = jnp.where(is_trueup, acc["prof_idx"][month], -1)
        settle_amount = _scatter_rows(
            jnp.zeros((profile_count, r)), settle_prof_idx, jnp.where(trueup_paid, actual_per_trueup, 0.0)
        )
        settle_active = (
            _scatter_rows(jnp.zeros((profile_count, r)), settle_prof_idx, trueup_paid.astype(jnp.float32)) > 0.0
        )
        settle_year_end = jnp.where(settle_active, acc["tax_year_end"][month], NO_CODE)

        # TLH harvest (after settlement, before PE): book a calibrated capital loss per policy. The
        # prior price clamps to month 0 (max(0, month-1)), giving a flat period return there — so the
        # eager engine's month-0 `has_prior=False` special case is unnecessary inside the scan.
        for fh in folded_harvest:
            hp_policy = fh.policy_idx
            hp_lots = np.asarray(fh.lot_indices, dtype=np.int64)
            hp_price = external_values[fh.series_index, :, month]
            hp_prior = external_values[fh.series_index, :, jnp.maximum(0, month - 1)]
            cg_ytd, cg_active, hp_cumulative = _tlh_harvest_policy_jit(
                lot_remaining[hp_lots, :],
                cost_basis_per_unit[hp_lots],
                hp_price,
                hp_prior,
                tlh[hp_policy],
                cg_ytd,
                cg_active,
                active,
                gain_profile=fh.gain_profile,
                has_prior=True,
                peak=fh.peak_annual_yield,
                floor=fh.floor_annual_yield,
                gamma=fh.maturity_decay_exponent,
                drawdown_sensitivity=fh.drawdown_sensitivity,
                short_term_fraction=fh.short_term_fraction,
            )
            tlh = tlh.at[hp_policy].set(hp_cumulative)

        # Private-equity tenders (after settlement, eager order): per issuer, forced-recovery and
        # forced-sale dispositions, then the LNW-floor tender (+ public-market sale). Branch-free —
        # each FIFO unit-sale's target is capped at units held, so non-firing rollouts sell nothing.
        pe_disp_active = jnp.zeros((pe_issuer_count, n_pe_kinds, lot_axis, r), dtype=bool)
        pe_disp_units = jnp.zeros((pe_issuer_count, n_pe_kinds, lot_axis, r))
        pe_disp_basis = jnp.zeros((pe_issuer_count, n_pe_kinds, lot_axis, r))
        pe_disp_proceeds = jnp.zeros((pe_issuer_count, n_pe_kinds, lot_axis, r))
        pe_opp = {  # 9 opportunity-trace fields per issuer
            k: jnp.zeros((pe_issuer_count, r), dtype=(jnp.int64 if k in ("active", "outcome") else jnp.float32))
            for k in ("active", "outcome", "floor", "lnw", "shortfall", "units", "sellable", "target", "proceeds")
        }
        for fpe in folded_pe:
            issuer_idx, policy_idx = fpe.issuer_idx, fpe.policy_idx
            ordered = np.asarray(fpe.ordered, dtype=np.int64)
            mark = pe_ch["marks"][issuer_idx, :, month]
            positive_mark = mark > 0.0
            tender_active = pe_ch["sale_opp"][issuer_idx, :, month] & active
            public_active = pe_ch["regime"][issuer_idx, :, month] == int(PrivateEquityRegimeCode.PUBLIC_MARKET)
            liq_blocked = pe_ch["liq_blocked"][issuer_idx, :, month]
            forced_sale_fraction = pe_ch["forced_sale"][issuer_idx, :, month]
            forced_recovery = pe_ch["forced_recovery"][issuer_idx, :, month]
            capacity = pe_ch["capacity"][issuer_idx, :, month]
            eligible = pe_ch["eligible"][issuer_idx, :, month]
            units_held = lot_remaining[ordered].sum(axis=0)
            if policy_idx < 0:
                pe_opp["active"] = pe_opp["active"].at[issuer_idx].set(tender_active.astype(jnp.int64))
                pe_opp["outcome"] = (
                    pe_opp["outcome"]
                    .at[issuer_idx]
                    .set(jnp.where(tender_active, int(PrivateEquityOpportunityOutcome.NO_POLICY), 0))
                )
                pe_opp["units"] = pe_opp["units"].at[issuer_idx].set(units_held)
                pe_opp["sellable"] = pe_opp["sellable"].at[issuer_idx].set(units_held * capacity * eligible)
                continue
            proceeds_slot = fpe.proceeds_cash_slot
            owner = fpe.owner_agent

            # Default args bind this issuer's loop vars per iteration (the closure is called inline).
            def book(
                target,
                price,
                kind,
                state,
                *,
                ordered=ordered,
                proceeds_slot=proceeds_slot,
                owner=owner,
                issuer_idx=issuer_idx,
            ):
                cash, lot_remaining, cg_active, cg_ytd, tlh, da, du, db, dp = state
                sold, proceeds, basis, _ovr = _fifo_sell_units(
                    lot_remaining.T, ordered, target, price, cost_basis_per_unit
                )
                lot_remaining = lot_remaining - sold.T
                if proceeds_slot >= 0:
                    cash = cash.at[proceeds_slot].add(proceeds.sum(axis=1))
                cg_active, cg_ytd, tlh = _record_capital_gains(
                    folded_harvest,
                    lot_purchase_month,
                    cg_profiles_by_agent[owner],
                    cg_active,
                    cg_ytd,
                    tlh,
                    lot_remaining,
                    month,
                    sold,
                    proceeds - basis,
                )
                ki = int(kind)
                da = da.at[issuer_idx, ki].set(da[issuer_idx, ki] | (sold > 0.0).T)
                du = du.at[issuer_idx, ki].add(sold.T)
                db = db.at[issuer_idx, ki].add(basis.T)
                dp = dp.at[issuer_idx, ki].add(proceeds.T)
                return cash, lot_remaining, cg_active, cg_ytd, tlh, da, du, db, dp

            state = (
                cash,
                lot_remaining,
                cg_active,
                cg_ytd,
                tlh,
                pe_disp_active,
                pe_disp_units,
                pe_disp_basis,
                pe_disp_proceeds,
            )
            # Forced recovery: cash out the whole position at the recovery-implied price.
            recovery_active = (forced_recovery > 0.0) & active & (units_held > 0.0)
            safe_units = jnp.where(units_held > 0.0, units_held, 1.0)
            recovery_price = jnp.where(units_held > 0.0, forced_recovery / safe_units, 1.0)
            state = book(
                jnp.where(recovery_active, units_held, 0.0),
                recovery_price,
                PrivateEquityDispositionKind.FORCED_RECOVERY,
                state,
            )
            units_held = state[1][ordered].sum(axis=0)
            # Forced sale: a fraction of the remaining position at the mark.
            forced_active = (forced_sale_fraction > 0.0) & active & positive_mark & (units_held > 0.0)
            state = book(
                jnp.where(forced_active, units_held * forced_sale_fraction, 0.0),
                mark,
                PrivateEquityDispositionKind.FORCED_SALE,
                state,
            )
            cash, lot_remaining = state[0], state[1]
            # LNW-floor tender: sell to lift liquid net worth to the floor, capped at sellable units.
            floor = _amount_values(
                amount_kind=fpe.floor_kind,
                amount_fixed=fpe.floor_fixed,
                amount_base=fpe.floor_base,
                amount_series=fpe.floor_series,
                amount_base_month=fpe.floor_base_month,
                amount_period=fpe.floor_period,
                external_values=external_values,
                month=month,
                rollout_count=r,
            )
            lnw = _compute_liquid_net_worth(
                pe_owner_cash_mask[policy_idx],
                lot_asset_series_index,
                fpe.owner_non_pe_lot_indices,
                cash,
                lot_remaining,
                external_values,
                month,
            )
            pe_shortfall = jnp.maximum(0.0, floor - lnw)  # distinct from the obligation `shortfall` in base_ys
            units_held = lot_remaining[ordered].sum(axis=0)
            sellable = units_held * capacity * eligible
            shortfall_units = jnp.where(positive_mark, pe_shortfall / jnp.where(positive_mark, mark, 1.0), 0.0)
            opp_active = (tender_active | public_active) & active & ~liq_blocked & positive_mark
            target = jnp.where(opp_active, jnp.minimum(shortfall_units, sellable), 0.0)
            outcome = jnp.full(r, int(PrivateEquityOpportunityOutcome.SOLD))
            outcome = jnp.where(pe_shortfall <= 0.0, int(PrivateEquityOpportunityOutcome.FLOOR_SATISFIED), outcome)
            outcome = jnp.where(
                (capacity * eligible) <= 0.0, int(PrivateEquityOpportunityOutcome.CAPACITY_ZERO), outcome
            )
            outcome = jnp.where(~positive_mark, int(PrivateEquityOpportunityOutcome.NONPOSITIVE_MARK), outcome)
            outcome = jnp.where(liq_blocked, int(PrivateEquityOpportunityOutcome.LIQUIDITY_BLOCKED), outcome)
            outcome = jnp.where(units_held <= 0.0, int(PrivateEquityOpportunityOutcome.NO_UNITS), outcome)
            for key, val in (
                ("active", tender_active.astype(jnp.int64)),
                ("outcome", jnp.where(tender_active, outcome, 0)),
                ("floor", jnp.where(tender_active, floor, 0.0)),
                ("lnw", jnp.where(tender_active, lnw, 0.0)),
                ("shortfall", jnp.where(tender_active, pe_shortfall, 0.0)),
                ("units", jnp.where(tender_active, units_held, 0.0)),
                ("sellable", jnp.where(tender_active, sellable, 0.0)),
                ("target", jnp.where(tender_active, target, 0.0)),
                ("proceeds", jnp.where(tender_active, target * mark, 0.0)),
            ):
                pe_opp[key] = pe_opp[key].at[issuer_idx].set(val)
            state = (cash, lot_remaining, cg_active, cg_ytd, tlh, state[5], state[6], state[7], state[8])
            state = book(
                jnp.where(tender_active & ~public_active, target, 0.0), mark, PrivateEquityDispositionKind.TENDER, state
            )
            state = book(jnp.where(public_active, target, 0.0), mark, PrivateEquityDispositionKind.PUBLIC_MARKET, state)
            cash, lot_remaining, cg_active, cg_ytd, tlh = state[0], state[1], state[2], state[3], state[4]
            pe_disp_active, pe_disp_units, pe_disp_basis, pe_disp_proceeds = state[5], state[6], state[7], state[8]

        # §121 owner-occupied-month counter then §168 depreciation accrual (eager order: after
        # settlement, before the year-end tax pass that reads depreciation_ytd). Inlined (vs the jit
        # core) to also push this month's occupancy flag into the trailing-60-month §121 ring.
        is_primary_m = property_is_primary_table[month]
        occupied_flag = active[None, :] & property_active & (property_rented_fraction < 1.0) & is_primary_m[:, None]
        property_owner_occupied = property_owner_occupied + occupied_flag.astype(property_owner_occupied.dtype)
        oo_window = oo_window.at[month % SECTION_121_LOOKBACK_MONTHS].set(occupied_flag)
        property_cum_dep, property_dep_ytd = _apply_depreciation_accrual(
            property_active,
            property_rented_fraction,
            property_building_basis,
            property_cum_dep,
            property_dep_ytd,
            failed,
        )

        # December year-end tax pass (creates this year's tax liabilities; resets the YTD buckets).
        (
            ordinary,
            cg_ytd,
            capital_loss_carryforward,
            recapture_ytd,
            property_tax_ytd,
            liab_interest_ytd,
            liab_rental_ytd,
            property_dep_ytd,
            taxliab_active,
            taxliab_amount,
            tax_breakdown,
        ) = december_tax(
            ordinary,
            cg_ytd,
            capital_loss_carryforward,
            recapture_ytd,
            property_tax_ytd,
            liab_interest_ytd,
            liab_rental_ytd,
            property_dep_ytd,
            taxliab_active,
            taxliab_amount,
            active,
            month,
        )

        keep = ~failed
        # `_zero_failed_state`: drain dollar-valued state for newly-failed rollouts. `cg_active`, the
        # property activity flag, depreciation accumulators, and owner-occupied months are left intact
        # (matches the eager engine, which zeros only dollar fields).
        cash, ordinary, lot_remaining = cash * keep, ordinary * keep, lot_remaining * keep
        cg_ytd, tlh = cg_ytd * keep[None, None, :], tlh * keep
        capital_loss_carryforward, taxliab_amount = capital_loss_carryforward * keep, taxliab_amount * keep
        property_basis, property_ownership, property_contribution, property_equity = (
            property_basis * keep,
            property_ownership * keep,
            property_contribution * keep,
            property_equity * keep,
        )
        # Liability dollar fields are drained on failure; `active` and `rental_interest_ytd` are not.
        liab_principal, liab_monthly = liab_principal * keep, liab_monthly * keep
        liab_interest_ytd, liab_principal_ytd = liab_interest_ytd * keep, liab_principal_ytd * keep
        carry = _ScanState(
            cash=cash,
            ordinary_ytd=ordinary,
            property_tax_ytd=property_tax_ytd,
            lot_remaining=lot_remaining,
            capital_gain_active=cg_active,
            capital_gain_ytd=cg_ytd,
            tlh=tlh,
            property_active=property_active,
            property_basis=property_basis,
            property_ownership=property_ownership,
            property_contribution=property_contribution,
            property_equity=property_equity,
            property_cumulative_depreciation=property_cum_dep,
            property_owner_occupied_months=property_owner_occupied,
            property_depreciation_ytd=property_dep_ytd,
            property_rented_fraction=property_rented_fraction,
            property_building_basis=property_building_basis,
            owner_occupied_window=oo_window,
            liability_active=liab_active,
            liability_principal=liab_principal,
            liability_monthly_payment=liab_monthly,
            liability_interest_ytd=liab_interest_ytd,
            liability_principal_ytd=liab_principal_ytd,
            liability_rental_interest_ytd=liab_rental_ytd,
            capital_loss_carryforward=capital_loss_carryforward,
            recapture_section_1250_ytd=recapture_ytd,
            tax_liability_active=taxliab_active,
            tax_liability_amount=taxliab_amount,
            failed=failed,
            failed_month=failed_month,
            sale_disp_units=sale_disp_units,
            sale_disp_basis=sale_disp_basis,
            sale_disp_proceeds=sale_disp_proceeds,
            sale_oversell=sale_oversell,
        )
        base_ys = (
            cash,
            ordinary,
            lot_remaining,
            cg_active,
            cg_ytd,
            property_active,
            property_basis,
            property_ownership,
            property_contribution,
            property_equity,
            property_cum_dep,
            property_owner_occupied,
            liab_active,
            liab_principal,
            liab_monthly,
            liab_interest_ytd,
            liab_principal_ytd,
            failed,
            failed_month,
            transfer_active,
            transfer_amount,
            slot_active,
            accrual_due,
            paid_buffer,
            shortfall,
            failure_active,
        )
        # Per-month property-event slabs (stacked over real purchases); empty when no purchases.
        purchase_ys = (purchase_active_rows, transfer_active_rows) if folded_purchases else ()
        # Mortgage event slabs (per-liability), only when the plan has liabilities (event buffers are
        # padded to max(1, liability_count), so a 0-row emit can't be scattered into them).
        mortgage_ys = (
            (mort_orig, mort_pay_active, mort_pay_interest, mort_pay_principal, mort_pay_total)
            if liab_count > 0
            else ()
        )
        # Per-(sale, lot, rollout) disposition slabs (stacked over real sales) + a per-month oversell
        # Scheduled-sale dispositions are carried (accumulated at each sale's firing month), not emitted
        # per-month — see `_ScanState.sale_disp_*`; the post-scan scatter reads them from the final carry.
        sale_ys: tuple = ()
        # Tax slabs: 13 per-(link, rollout) breakdown buffers + the post-month tax-liability snapshot
        # (amount, active) for change-log reconstruction + the 3 per-(profile, rollout) settlement
        # event buffers. Only when the plan has tax links.
        tax_ys = (
            (*tax_breakdown, taxliab_amount, taxliab_active, settle_active, settle_amount, settle_year_end)
            if link_count > 0
            else ()
        )
        # Liquidity slabs: per-(policy, asset) disposition (active/units/basis/proceeds) + the
        # per-obligation attempt-policy assignment. Only when the plan has liquidity policies.
        liquidity_ys = (
            (liq_disp_active, liq_disp_units, liq_disp_basis, liq_disp_proceeds, attempt_policy)
            if folded_liquidity
            else ()
        )
        # PE slabs: 4 per-(issuer, kind) disposition arrays + 9 per-issuer opportunity-trace fields.
        pe_ys = (
            (
                pe_disp_active,
                pe_disp_units,
                pe_disp_basis,
                pe_disp_proceeds,
                pe_opp["active"],
                pe_opp["outcome"],
                pe_opp["floor"],
                pe_opp["lnw"],
                pe_opp["shortfall"],
                pe_opp["units"],
                pe_opp["sellable"],
                pe_opp["target"],
                pe_opp["proceeds"],
            )
            if folded_pe
            else ()
        )
        # Lifecycle/PR event slabs: per-event `fired` flags (lifecycle, then PR) + the 7 sale-trace
        # fields stacked over SALE events. Each group present iff that event class exists in the plan.
        lifecycle_ys = (
            *([jnp.stack(le_fired)] if folded_lifecycle else []),
            *([jnp.stack(pr_fired)] if folded_pr else []),
            *([jnp.stack([st[f] for st in sale_traces]) for f in range(7)] if folded_sale_events else []),
        )
        return carry, (*base_ys, *sale_ys, *purchase_ys, *mortgage_ys, *tax_ys, *liquidity_ys, *pe_ys, *lifecycle_ys)

    init = _ScanState(
        cash=cash0,
        ordinary_ytd=ordinary0,
        property_tax_ytd=property_tax_ytd0,
        lot_remaining=lot0,
        capital_gain_active=cg_active0,
        capital_gain_ytd=cg_ytd0,
        tlh=tlh0,
        property_active=jnp.zeros((p.property_count, r), dtype=bool),
        property_basis=prop0,
        property_ownership=prop0,
        property_contribution=prop0,
        property_equity=prop0,
        property_cumulative_depreciation=prop0,
        property_owner_occupied_months=jnp.zeros((p.property_count, r), dtype=jnp.int32),
        property_depreciation_ytd=prop0,
        property_rented_fraction=property_rented_fraction_0,
        property_building_basis=property_building_basis_0,
        owner_occupied_window=jnp.zeros((SECTION_121_LOOKBACK_MONTHS, p.property_count, r), dtype=bool),
        liability_active=jnp.zeros((p.liability_count, r), dtype=bool),
        liability_principal=liab0,
        liability_monthly_payment=liab0,
        liability_interest_ytd=liab0,
        liability_principal_ytd=liab0,
        liability_rental_interest_ytd=liab0,
        capital_loss_carryforward=jnp.zeros((p.capital_gain_agent_count, r)),
        recapture_section_1250_ytd=jnp.zeros((p.tax_profile_count, r)),
        tax_liability_active=jnp.zeros((p.tax_liability_count, r), dtype=bool),
        tax_liability_amount=jnp.zeros((p.tax_liability_count, r)),
        failed=jnp.zeros(r, dtype=bool),
        failed_month=jnp.full(r, -1, dtype=jnp.int32),
        sale_disp_units=jnp.zeros((p.scheduled_sale_count, lot_axis, r)),
        sale_disp_basis=jnp.zeros((p.scheduled_sale_count, lot_axis, r)),
        sale_disp_proceeds=jnp.zeros((p.scheduled_sale_count, lot_axis, r)),
        sale_oversell=jnp.zeros((), dtype=bool),
    )
    months = jnp.arange(horizon, dtype=jnp.int32)
    # Initial cash / lot carry: broadcast the traced per-entity opening balances across rollouts.
    init = init._replace(
        cash=jnp.broadcast_to(cfg.cash_initial_balance[:, None], (p.cash_count, r)),
        lot_remaining=jnp.broadcast_to(cfg.lot_initial_quantity[:, None], (p.lot_count, r)),
    )
    final_carry, ys = jax.lax.scan(step, init, months)
    # The scheduled-sale dispositions live in the final carry (accumulated, horizon collapsed).
    return ys, (
        final_carry.sale_disp_units,
        final_carry.sale_disp_basis,
        final_carry.sale_disp_proceeds,
        final_carry.sale_oversell,
    )


def _scatter_ys_to_buffers(
    plan: CompiledSimulation, buffers: SimulationBuffers, meta: _ScanMeta, ys: tuple, sale_disp: tuple
) -> None:
    """Scatter the stacked per-month `ys` from the compiled program back into the NumPy buffers (one
    device->host transfer). Pure host code; uses `meta` for the structural scatter targets. `sale_disp`
    is the horizon-collapsed scheduled-sale disposition `(units, basis, proceeds, oversell)` carried out
    of the scan (each `(scheduled_sale, lot, R)`)."""
    p = plan.slot_plan
    r = p.rollout_count
    horizon = meta.horizon
    link_count = meta.link_count
    folded_purchases = meta.folded_purchases
    folded_liquidity = meta.folded_liquidity
    folded_pe = meta.folded_pe
    folded_lifecycle = meta.folded_lifecycle
    folded_pr = meta.folded_pr
    folded_sale_events = meta.folded_sale_events
    cash0 = np.broadcast_to(plan.cash_initial_balance[:, None], (p.cash_count, r))
    lot0 = np.broadcast_to(plan.lot_initial_quantity[:, None], (p.lot_count, r))
    (
        cash_h,
        ordinary_h,
        lot_h,
        cg_active_h,
        cg_ytd_h,
        prop_active_h,
        prop_basis_h,
        prop_ownership_h,
        prop_contribution_h,
        prop_equity_h,
        prop_cum_dep_h,
        prop_occupied_h,
        liab_active_h,
        liab_principal_h,
        liab_monthly_h,
        liab_interest_ytd_h,
        liab_principal_ytd_h,
        failed_h,
        failed_month_h,
        t_active,
        t_amount,
        ob_active,
        ob_due,
        ob_paid,
        ob_short,
        ob_fail,
        *rest,
    ) = ys
    # Five variable-length tail groups, sliced by compile-time presence: sale slabs (5 if any sales),
    # property-event slabs (2 if any purchases), mortgage-event slabs (5 if any liabilities), tax slabs
    # (18 = 13 breakdowns + 2 tax-liability snapshots + 3 settlement events, if any tax links), and
    # liquidity slabs (5 if any liquidity policies).
    n_sale = 0  # scheduled-sale dispositions are carried out-of-band (`sale_disp`), not in `ys`
    n_purchase = 2 if folded_purchases else 0
    n_mortgage = 5 if p.liability_count > 0 else 0
    n_tax = 18 if link_count > 0 else 0
    n_liquidity = 5 if folded_liquidity else 0
    n_pe = 13 if folded_pe else 0
    n_le_fired = 1 if folded_lifecycle else 0
    n_pr_fired = 1 if folded_pr else 0
    o1 = n_sale
    o2 = o1 + n_purchase
    o3 = o2 + n_mortgage
    o4 = o3 + n_tax
    o5 = o4 + n_liquidity
    o6 = o5 + n_pe
    o7 = o6 + n_le_fired
    o8 = o7 + n_pr_fired
    purchase_h = rest[o1:o2]  # o1 == 0 (scheduled-sale dispositions are carried, not in `ys`)
    mortgage_h = rest[o2:o3]
    tax_h = rest[o3:o4]
    liquidity_h = rest[o4:o5]
    pe_h = rest[o5:o6]
    le_fired_h = rest[o6:o7]
    pr_fired_h = rest[o7:o8]
    sale_trace_h = rest[o8:]

    # Single device->host transfer of the stacked results into the (zeroed) NumPy buffers.
    buffers.state.cash_state[0] = np.asarray(cash0)
    buffers.state.cash_state[1:] = np.asarray(cash_h)
    buffers.state.ordinary_state[1:] = np.asarray(ordinary_h)
    buffers.state.lot_state[0] = np.asarray(lot0)
    buffers.state.lot_state[1:] = np.asarray(lot_h)
    buffers.state.capital_gain_active_state[1:] = np.asarray(cg_active_h)
    buffers.state.capital_gain_state[1:] = np.asarray(cg_ytd_h)
    buffers.state.property_active_state[1:] = np.asarray(prop_active_h)
    buffers.state.property_basis_state[1:] = np.asarray(prop_basis_h)
    buffers.state.property_ownership_state[1:] = np.asarray(prop_ownership_h)
    buffers.state.property_contribution_state[1:] = np.asarray(prop_contribution_h)
    buffers.state.property_equity_state[1:] = np.asarray(prop_equity_h)
    buffers.state.property_cumulative_depreciation_state[1:] = np.asarray(prop_cum_dep_h)
    buffers.state.property_owner_occupied_months_state[1:] = np.asarray(prop_occupied_h)
    buffers.state.liability_active_state[1:] = np.asarray(liab_active_h)
    buffers.state.liability_principal_state[1:] = np.asarray(liab_principal_h)
    buffers.state.liability_monthly_payment_state[1:] = np.asarray(liab_monthly_h)
    buffers.state.liability_interest_ytd_state[1:] = np.asarray(liab_interest_ytd_h)
    buffers.state.liability_principal_ytd_state[1:] = np.asarray(liab_principal_ytd_h)
    buffers.state.rollout_failed_state[1:] = np.asarray(failed_h)
    buffers.state.rollout_failed_month_state[1:] = np.asarray(failed_month_h)
    buffers.transfers.active[:] = np.asarray(t_active)
    buffers.transfers.amount[:] = np.asarray(t_amount)
    buffers.obligations.active[:] = np.asarray(ob_active)
    buffers.obligations.due[:] = np.asarray(ob_due)
    buffers.obligations.paid[:] = np.asarray(ob_paid)
    buffers.obligations.shortfall[:] = np.asarray(ob_short)
    buffers.obligations.failure_active[:] = np.asarray(ob_fail)
    # Scheduled-sale dispositions: the carry holds `(scheduled_sale, lot, R)` already indexed by each
    # sale's slot (the firing month is static — `plan.sales.month` — so the decoder re-derives it).
    disp_units_h, disp_basis_h, disp_proceeds_h, oversell_h = sale_disp
    if bool(np.asarray(oversell_h)):  # match the eager engine's hard error on the first oversell
        raise ValueError("scheduled asset sale exceeds available lots")
    disp = buffers.lot_dispositions.scheduled
    disp.units[:] = np.asarray(disp_units_h)
    disp.basis[:] = np.asarray(disp_basis_h)
    disp.proceeds[:] = np.asarray(disp_proceeds_h)
    disp.active[:] = disp.units > 0.0
    if folded_purchases:
        # Stacks are `(horizon, num_real_purchases, R)`; scatter each to its property column.
        purchase_active_np, transfer_active_np = (np.asarray(a) for a in purchase_h)
        for i, fp in enumerate(folded_purchases):
            buffers.properties.purchase_active[:, fp.buffer_index] = purchase_active_np[:, i]
            buffers.properties.transfer_active[:, fp.buffer_index] = transfer_active_np[:, i]
    if mortgage_h:
        # Per-liability mortgage event stacks `(horizon, liability_count, R)`.
        orig_h, pay_active_h, pay_interest_h, pay_principal_h, pay_total_h = (np.asarray(a) for a in mortgage_h)
        props_buf = buffers.properties
        props_buf.mortgage_origination_active[:] = orig_h
        props_buf.mortgage_payment_active[:] = pay_active_h
        props_buf.mortgage_payment_interest[:] = pay_interest_h
        props_buf.mortgage_payment_principal[:] = pay_principal_h
        props_buf.mortgage_payment_total[:] = pay_total_h
    if tax_h:
        # 13 per-(month, link) breakdown stacks + tax-liability snapshots + 3 settlement event stacks.
        *breakdown_h, taxliab_amount_h, taxliab_active_h, settle_active_h, settle_amount_h, settle_year_end_h = (
            np.asarray(a) for a in tax_h
        )
        taxes = buffers.taxes
        taxes.accrual_active[:] = breakdown_h[0] > 0.0
        for buf, slab in zip(
            (
                taxes.accrual_amount,
                taxes.breakdown_ordinary,
                taxes.breakdown_ltcg,
                taxes.breakdown_stcg,
                taxes.breakdown_standard_deduction,
                taxes.breakdown_mortgage_interest_deduction,
                taxes.breakdown_salt_deduction,
                taxes.breakdown_itemized_deduction,
                taxes.breakdown_ordinary_taxable,
                taxes.breakdown_capital_taxable,
                taxes.breakdown_ordinary_tax,
                taxes.breakdown_capital_tax,
            ),
            breakdown_h[1:],
            strict=True,
        ):
            buf[:] = slab
        n_prof = settle_active_h.shape[1]
        taxes.settlement_active[:, :n_prof] = settle_active_h
        taxes.settlement_amount[:, :n_prof] = settle_amount_h
        taxes.settlement_year_end_month[:, :n_prof] = settle_year_end_h
        # Reconstruct the sparse tax-liability change log by diffing per-month snapshots: a year-end
        # accrual (0 -> tax) and a true-up settlement (tax -> 0) each change a slot's balance; record
        # the post-change balance at month m+1 for every slot that changed that month.
        prev_amount = np.zeros_like(taxliab_amount_h[0])
        prev_active = np.zeros_like(taxliab_active_h[0])
        for m in range(horizon):
            changed = np.flatnonzero(
                (taxliab_amount_h[m] != prev_amount).any(axis=1) | (taxliab_active_h[m] != prev_active).any(axis=1)
            )
            if changed.size:
                buffers.tax_liability_changes.record(
                    snapshot_month=m + 1, slots=changed, amount=taxliab_amount_h[m], active=taxliab_active_h[m]
                )
            prev_amount, prev_active = taxliab_amount_h[m], taxliab_active_h[m]
    if liquidity_h:
        # Per-(month, policy, asset) liquidity disposition stacks + the per-obligation attempt-policy.
        liq_active_h, liq_units_h, liq_basis_h, liq_proceeds_h, attempt_h = (np.asarray(a) for a in liquidity_h)
        liq = buffers.lot_dispositions.liquidity
        liq.active[:] = liq_active_h
        liq.units[:] = liq_units_h
        liq.basis[:] = liq_basis_h
        liq.proceeds[:] = liq_proceeds_h
        buffers.obligations.attempt_policy[:] = attempt_h
    if pe_h:
        # 4 per-(month, issuer, kind) disposition stacks + 9 per-(month, issuer) opportunity stacks.
        pe_active_h, pe_units_h, pe_basis_h, pe_proceeds_h = (np.asarray(a) for a in pe_h[:4])
        pe = buffers.lot_dispositions.pe
        pe.active[:] = pe_active_h
        pe.units[:] = pe_units_h
        pe.basis[:] = pe_basis_h
        pe.proceeds[:] = pe_proceeds_h
        opp = buffers.private_equity_opportunities
        (opp_active, opp_outcome, opp_floor, opp_lnw, opp_short, opp_units, opp_sellable, opp_target, opp_proceeds) = (
            np.asarray(a) for a in pe_h[4:]
        )
        opp.active[:] = opp_active.astype(bool)
        opp.outcome[:] = opp_outcome
        opp.floor[:] = opp_floor
        opp.liquid_net_worth[:] = opp_lnw
        opp.shortfall[:] = opp_short
        opp.units_held[:] = opp_units
        opp.sellable_units[:] = opp_sellable
        opp.target_units[:] = opp_target
        opp.proceeds[:] = opp_proceeds
    if le_fired_h:
        # `le_fired_h[0]` is `(horizon, n_lifecycle_events, R)`; each event fires once at its month.
        fired_np = np.asarray(le_fired_h[0])
        for pos, ev in enumerate(folded_lifecycle):
            buffers.lifecycle.fired[ev.event_index] = fired_np[ev.month, pos]
    if pr_fired_h:
        pr_fired_np = np.asarray(pr_fired_h[0])
        for pos, (ei, ev_month) in enumerate(folded_pr):
            buffers.primary_residence.fired[ei] = pr_fired_np[ev_month, pos]
    if sale_trace_h:
        # 7 stacks `(horizon, n_sale_events, R)` in the lifecycle.sale_* field order.
        trace_np = [np.asarray(a) for a in sale_trace_h]
        sale_fields = (
            buffers.lifecycle.sale_gross_proceeds,
            buffers.lifecycle.sale_mortgage_payoff,
            buffers.lifecycle.sale_net_cash,
            buffers.lifecycle.sale_realized_gain,
            buffers.lifecycle.sale_recapture,
            buffers.lifecycle.sale_section_121_exclusion,
            buffers.lifecycle.sale_long_term_gain,
        )
        for pos, (i, ev_month) in enumerate(folded_sale_events):
            for field, stack in zip(sale_fields, trace_np, strict=True):
                field[i] = stack[ev_month, pos]


def _amount_values(
    *,
    amount_kind: int,
    amount_fixed: float,
    amount_base: float,
    amount_series: int,
    amount_base_month: int,
    amount_period: int,
    external_values: jnp.ndarray,
    month: int | jnp.ndarray,
    rollout_count: int,
) -> jnp.ndarray:
    """Port of `phases._amount_values`: a fixed or series-indexed per-rollout amount."""
    if amount_kind == AMOUNT_FIXED:
        return jnp.full(rollout_count, amount_fixed)
    reset_month = amount_base_month + ((month - amount_base_month) // amount_period) * amount_period
    base_level = external_values[amount_series, :, amount_base_month]
    reset_level = external_values[amount_series, :, reset_month]
    return amount_base * reset_level / base_level


def _amount_values_tuple(
    spec: tuple[int, float, float, int, int, int], external_values: jnp.ndarray, month: int | jnp.ndarray, r: int
) -> jnp.ndarray:
    """`_amount_values` from a `(kind, fixed, base, series, base_month, period)` tuple."""
    kind, fixed, base, series, base_month, period = spec
    return _amount_values(
        amount_kind=kind,
        amount_fixed=fixed,
        amount_base=base,
        amount_series=series,
        amount_base_month=base_month,
        amount_period=period,
        external_values=external_values,
        month=month,
        rollout_count=r,
    )


def _scatter_rows(target: jnp.ndarray, indices: jnp.ndarray, values: jnp.ndarray) -> jnp.ndarray:
    """Sentinel-aware segment scatter-add: add `values[s]` into `target[indices[s]]`, ignoring
    rows where `indices[s] < 0`. Duplicate indices accumulate. Branch-free (no per-row Python
    loop / `if idx >= 0`): a `-1` index is redirected to a padding row that is then sliced off."""
    dump = target.shape[0]
    padded = jnp.concatenate([target, jnp.zeros((1, *target.shape[1:]), target.dtype)], axis=0)
    idx = jnp.where(indices < 0, dump, indices)
    return padded.at[idx].add(values)[:dump]


def _np_gather(arr: np.ndarray, idx: np.ndarray, fill: float) -> np.ndarray:
    """Host-side gather tolerating an empty source array (returns `fill` for every slot when the
    plan array has no rows, e.g. a scenario with no properties / liabilities / tax profiles)."""
    if arr.shape[0] == 0:
        return np.full(idx.shape, fill, dtype=arr.dtype)
    return np.asarray(arr[idx])


def _gather_rows(source: jnp.ndarray, idx: jnp.ndarray) -> jnp.ndarray:
    """Gather `source[idx[s]]` into `(slots, rollouts)`, tolerating an empty source (`idx` is
    expected pre-clamped to valid rows; rows for inapplicable slots are masked off by the caller).
    A 0-row source (e.g. a scenario with no properties/liabilities) yields zeros."""
    if source.shape[0] == 0:
        return jnp.zeros((idx.shape[0], *source.shape[1:]), source.dtype)
    return source[idx]


def _amount_values_vec(
    amount_kind: jnp.ndarray,
    amount_fixed: jnp.ndarray,
    amount_base: jnp.ndarray,
    amount_series: jnp.ndarray,
    amount_base_month: jnp.ndarray,
    amount_period: jnp.ndarray,
    external_values: jnp.ndarray,
    month: jnp.ndarray,
    rollout_count: int,
) -> jnp.ndarray:
    """`_amount_values` vectorized over slots (branch-free): returns `(slots, rollouts)`.

    The series path is computed for every slot and selected against the fixed amount by the
    `AMOUNT_FIXED` mask; `-1` series / non-positive periods are sanitized to safe indices so the
    (unused) series math never indexes out of range or divides by zero on fixed slots.
    """
    if external_values.shape[0] == 0:
        # No exogenous series in this scenario, so every amount is necessarily fixed; skip the
        # series gather (it would index a size-0 axis). `shape[0]` is static under jit.
        return jnp.broadcast_to(amount_fixed[:, None], (amount_kind.shape[0], rollout_count))
    safe_period = jnp.where(amount_period > 0, amount_period, 1)
    reset_month = amount_base_month + ((month - amount_base_month) // safe_period) * safe_period
    safe_series = jnp.where(amount_series >= 0, amount_series, 0)
    rows = jnp.arange(rollout_count)
    base_level = external_values[safe_series[:, None], rows[None, :], amount_base_month[:, None]]
    reset_level = external_values[safe_series[:, None], rows[None, :], reset_month[:, None]]
    series_amount = amount_base[:, None] * reset_level / base_level
    return jnp.where((amount_kind == AMOUNT_FIXED)[:, None], amount_fixed[:, None], series_amount)


@jax.jit
def _transfers_jit(
    cause: jnp.ndarray,
    amount_kind: jnp.ndarray,
    amount_fixed: jnp.ndarray,
    amount_base: jnp.ndarray,
    amount_series: jnp.ndarray,
    amount_base_month: jnp.ndarray,
    amount_period: jnp.ndarray,
    from_slot: jnp.ndarray,
    to_slot: jnp.ndarray,
    income_profile: jnp.ndarray,
    deduction_profile: jnp.ndarray,
    cash: jnp.ndarray,
    ordinary_ytd: jnp.ndarray,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Branch-free, jit-compiled scheduled-transfer step (all slots vectorized; `month` traced)."""
    rollout_count = cash.shape[1]
    fire = (cause >= 0)[:, None] & active[None, :]  # (slots, rollouts)
    raw = _amount_values_vec(
        amount_kind,
        amount_fixed,
        amount_base,
        amount_series,
        amount_base_month,
        amount_period,
        external_values,
        month,
        rollout_count,
    )
    amounts = jnp.where(fire, raw, 0.0)
    cash = _scatter_rows(cash, from_slot, -amounts)
    cash = _scatter_rows(cash, to_slot, amounts)
    ordinary_ytd = _scatter_rows(ordinary_ytd, income_profile, amounts)
    ordinary_ytd = _scatter_rows(ordinary_ytd, deduction_profile, -amounts)
    return cash, ordinary_ytd, fire, amounts


def _fifo_sell_units(
    lot_remaining: jnp.ndarray,
    ordered_lots: np.ndarray,
    target_units: jnp.ndarray,
    unit_price: jnp.ndarray,
    cost_basis_per_unit: jnp.ndarray,
    epsilon: float = 1e-9,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Port of `tensor_fifo.fifo_sell_units`: vectorized cumulative-sum FIFO over `[R, L]` lots."""
    ordered_quantity = lot_remaining[:, ordered_lots]
    available_units = ordered_quantity.sum(axis=1)
    oversell = target_units > available_units + epsilon
    effective_target = jnp.where(oversell, 0.0, target_units)
    before_units = jnp.cumsum(ordered_quantity, axis=1) - ordered_quantity
    sold_ordered = jnp.clip(effective_target[:, None] - before_units, 0.0, ordered_quantity)
    proceeds_ordered = sold_ordered * unit_price[:, None]
    basis_ordered = sold_ordered * cost_basis_per_unit[ordered_lots][None, :]
    zeros = jnp.zeros_like(lot_remaining)
    sold_units = zeros.at[:, ordered_lots].set(sold_ordered)
    proceeds = zeros.at[:, ordered_lots].set(proceeds_ordered)
    basis = zeros.at[:, ordered_lots].set(basis_ordered)
    return sold_units, proceeds, basis, oversell


def _apply_tlh_give_back(
    folded_harvest: tuple[_FoldedHarvest, ...],
    tlh_cumulative_harvest: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    sold_units: jnp.ndarray,
    gains: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Port of `phases._apply_tlh_give_back`: repay deferred harvested loss as extra gain on sold
    harvest-policy lots. The fraction of the policy's pre-sale units sold here realizes that share
    of `tlh_cumulative_harvest`, distributed across the sold policy-lots by sold units (preserving
    each lot's ST/LT character) and drained from the ledger. Branch-free over rollouts; the per-policy
    Python loop is over static folded data. `lot_remaining` is post-sale (caller already subtracted)."""
    for fh in folded_harvest:
        policy_idx = fh.policy_idx
        lot_indices = np.asarray(fh.lot_indices, dtype=np.int64)
        sold_policy = sold_units[:, lot_indices]  # (R, policy_lots)
        units_sold = sold_policy.sum(axis=1)  # (R,)
        pre_sale_units = lot_remaining[lot_indices, :].T.sum(axis=1) + units_sold  # (R,)
        cumulative = tlh_cumulative_harvest[policy_idx]  # (R,)
        fraction_sold = jnp.where(
            pre_sale_units > 0.0, units_sold / jnp.where(pre_sale_units > 0.0, pre_sale_units, 1.0), 0.0
        )
        give_back = fraction_sold * cumulative  # (R,)
        per_lot_weight = jnp.where(
            units_sold[:, None] > 0.0, sold_policy / jnp.where(units_sold[:, None] > 0.0, units_sold[:, None], 1.0), 0.0
        )
        gains = gains.at[:, lot_indices].add(per_lot_weight * give_back[:, None])
        tlh_cumulative_harvest = tlh_cumulative_harvest.at[policy_idx].set(cumulative - give_back)
    return gains, tlh_cumulative_harvest


def _record_capital_gains(
    folded_harvest: tuple[_FoldedHarvest, ...],
    lot_purchase_month: jnp.ndarray,
    cg_profiles: tuple[int, ...],
    capital_gain_active: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    tlh_cumulative_harvest: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    month: int | jnp.ndarray,
    sold_units: jnp.ndarray,
    gains: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Port of `phases._record_capital_gains`: TLH give-back, then classify each lot's gain
    long/short and accrue.

    Branch-free: the per-lot long/short split is a static `(L,)` boolean mask (holding period vs
    the lot's purchase month), so the whole `[2, R]` classification block is one masked sum/any —
    no per-lot scatter loop, no data-dependent branching. The only Python loop is over the
    statically-resolved capital-gain profiles (`cg_profiles`) of the selling agent.
    """
    gains, tlh_cumulative_harvest = _apply_tlh_give_back(
        folded_harvest, tlh_cumulative_harvest, lot_remaining, sold_units, gains
    )
    long_mask = (month - lot_purchase_month) >= 12  # (L,)
    masks = jnp.stack([long_mask, ~long_mask])  # (2, L), rows ordered LONG_TERM=0, SHORT_TERM=1
    sold = sold_units > 0.0  # (R, L)
    # einsum over lots: (2, L) x (R, L) -> (2, R) per-classification gain sums and activity flags.
    gains_by_class = jnp.einsum("cl,rl->cr", masks.astype(gains.dtype), gains)
    active_by_class = (masks[:, None, :] & sold[None, :, :]).any(axis=2)  # (2, R)
    for profile in cg_profiles:
        capital_gain_active = capital_gain_active.at[profile].set(capital_gain_active[profile] | active_by_class)
        capital_gain_ytd = capital_gain_ytd.at[profile].add(gains_by_class)
    return capital_gain_active, capital_gain_ytd, tlh_cumulative_harvest


@jax.jit
def _obligation_accruals_jit(
    kind: jnp.ndarray,
    valid_slot: jnp.ndarray,
    amount_kind: jnp.ndarray,
    amount_fixed: jnp.ndarray,
    amount_base: jnp.ndarray,
    amount_series: jnp.ndarray,
    amount_base_month: jnp.ndarray,
    amount_period: jnp.ndarray,
    prop_idx: jnp.ndarray,
    pt_amount: jnp.ndarray,
    pt_prop_month: jnp.ndarray,
    liab_idx: jnp.ndarray,
    mort_rate: jnp.ndarray,
    mort_prop_month: jnp.ndarray,
    est_quarterly: jnp.ndarray,
    est_prior: jnp.ndarray,
    trueup_sel: jnp.ndarray,
    property_active: jnp.ndarray,
    liab_principal: jnp.ndarray,
    liab_monthly: jnp.ndarray,
    liab_active: jnp.ndarray,
    taxliab_active: jnp.ndarray,
    taxliab_amount: jnp.ndarray,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Branch-free obligation accrual: every source kind's `(slots, rollouts)` due amount is computed,
    then selected by `kind`. Per-slot static data (rates, indices, the true-up selection matrix) is
    precomputed host-side; only the runtime state gathers (`property_active`, liability principal,
    tax-liability balances) are traced."""
    rollout_count = active.shape[0]
    k = kind[:, None]
    configured = _amount_values_vec(
        amount_kind,
        amount_fixed,
        amount_base,
        amount_series,
        amount_base_month,
        amount_period,
        external_values,
        month,
        rollout_count,
    )
    property_tax = jnp.broadcast_to(pt_amount[:, None], configured.shape)
    property_mask = _gather_rows(property_active, prop_idx) & (pt_prop_month[:, None] < month)
    principal = _gather_rows(liab_principal, liab_idx)
    mortgage = jnp.minimum(_gather_rows(liab_monthly, liab_idx), principal + principal * mort_rate[:, None] / 12.0)
    mortgage_mask = _gather_rows(liab_active, liab_idx) & (principal > 0.0) & (mort_prop_month[:, None] < month)
    estimated = jnp.broadcast_to(est_quarterly[:, None], configured.shape)
    actual = trueup_sel @ jnp.where(taxliab_active, taxliab_amount, 0.0)  # (slots, rollouts)
    safe_harbor = jnp.minimum(est_prior[:, None], actual)
    q4 = jnp.maximum(safe_harbor - est_prior[:, None] * 0.75, 0.0)
    true_up = jnp.maximum(actual - safe_harbor, 0.0)

    amount = jnp.select(
        [
            k == ObligationSource.CONFIGURED_OBLIGATION,
            k == ObligationSource.PROPERTY_TAX,
            k == ObligationSource.MORTGAGE_PAYMENT,
            k == ObligationSource.ESTIMATED_TAX,
            k == ObligationSource.ESTIMATED_TAX_Q4,
            k == ObligationSource.TAX_TRUE_UP,
        ],
        [configured, property_tax, mortgage, estimated, q4, true_up],
        default=0.0,
    )
    kind_mask = jnp.select(
        [k == ObligationSource.PROPERTY_TAX, k == ObligationSource.MORTGAGE_PAYMENT],
        [property_mask, mortgage_mask],
        default=True,
    )
    slot_active = valid_slot[:, None] & active[None, :] & kind_mask & (amount > 0.0)
    return slot_active, jnp.where(slot_active, amount, 0.0)


@jax.jit
def _obligation_group_funded_jit(
    group_matrix: jnp.ndarray,
    from_slot: jnp.ndarray,
    cash: jnp.ndarray,
    accrual_active: jnp.ndarray,
    accrual_due: jnp.ndarray,
) -> jnp.ndarray:
    """Branch-free funding check: each obligation group (same agent + from-account) is funded for a
    rollout iff that account's cash covers the group's total due. The per-slot group is encoded as
    a static `(slots, slots)` membership matrix, so the group sums are one matmul."""
    due_masked = jnp.where(accrual_active, accrual_due, 0.0)  # (slots, rollouts)
    group_due = group_matrix.astype(due_masked.dtype) @ due_masked  # (slots, rollouts)
    cash_padded = jnp.concatenate([cash, jnp.zeros((1, cash.shape[1]), cash.dtype)], axis=0)
    available = cash_padded[jnp.where(from_slot < 0, cash.shape[0], from_slot)]  # (slots, rollouts), -1 -> 0
    return accrual_active & (available >= group_due - 1e-9)


_ESTIMATED_TAX_KINDS = (ObligationSource.ESTIMATED_TAX, ObligationSource.ESTIMATED_TAX_Q4, ObligationSource.TAX_TRUE_UP)


@jax.jit
def _settlement_core_jit(
    from_slot: jnp.ndarray,
    to_slot: jnp.ndarray,
    deduction_profile: jnp.ndarray,
    deductible_fraction: jnp.ndarray,
    property_tax_profile: jnp.ndarray,
    property_slot_idx: jnp.ndarray,
    has_property_slot: jnp.ndarray,
    has_property_tax_profile: jnp.ndarray,
    has_deduction: jnp.ndarray,
    accrual_active: jnp.ndarray,
    accrual_due: jnp.ndarray,
    funded: jnp.ndarray,
    cash: jnp.ndarray,
    ordinary_ytd: jnp.ndarray,
    property_tax_ytd: jnp.ndarray,
    property_rented_fraction: jnp.ndarray,
    failed: jnp.ndarray,
    failed_month: jnp.ndarray,
    month: jnp.ndarray,
) -> tuple[
    jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray
]:
    """Branch-free core of obligation settlement: per-slot pay/fail, the funded cash move, the
    property-tax owner-share YTD accumulation, and the Schedule-E/itemized deduction — all
    vectorized over slots (duplicate from/to/profile indices accumulate via `_scatter_rows`).

    Failure ordering is month-stable: every slot that fails this month would stamp the same
    `month`, so the per-rollout first-failure month is `month` iff any slot fails and it had not
    failed before. Mortgage liability updates and tax settlement are handled by the caller.
    """
    paid = accrual_active & funded
    slot_failed = accrual_active & ~funded
    paid_amount = jnp.where(paid, accrual_due, 0.0)
    cash = _scatter_rows(cash, from_slot, -paid_amount)
    cash = _scatter_rows(cash, to_slot, paid_amount)
    rented = _gather_rows(property_rented_fraction, property_slot_idx)  # (slots, rollouts)
    property_tax_ytd = _scatter_rows(
        property_tax_ytd,
        property_tax_profile,
        jnp.where(has_property_tax_profile[:, None], paid_amount * (1.0 - rented), 0.0),
    )
    deductible = jnp.where(has_property_slot[:, None], rented, deductible_fraction[:, None])
    ordinary_ytd = _scatter_rows(
        ordinary_ytd, deduction_profile, jnp.where(has_deduction[:, None], -paid_amount * deductible, 0.0)
    )
    shortfall = jnp.where(slot_failed, accrual_due, 0.0)
    failed_this = slot_failed.any(axis=0)
    failed_month = jnp.where(failed_this & (failed_month < 0), month, failed_month)
    failed = failed | failed_this
    return paid, paid_amount, cash, ordinary_ytd, property_tax_ytd, shortfall, slot_failed, failed, failed_month


def _fifo_sell_dollars(
    lot_remaining: jnp.ndarray,
    ordered_lots: np.ndarray,
    target_dollars: jnp.ndarray,
    unit_price: jnp.ndarray,
    cost_basis_per_unit: jnp.ndarray,
    epsilon: float = 1e-9,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Port of `tensor_fifo.fifo_sell_dollars`: FIFO sell a dollar target, ceiling-rounding units."""
    ordered_quantity = lot_remaining[:, ordered_lots]
    available_value = ordered_quantity * unit_price[:, None]
    oversell = target_dollars > available_value.sum(axis=1) + epsilon
    effective_target = jnp.where(oversell, 0.0, target_dollars)
    before_value = jnp.cumsum(available_value, axis=1) - available_value
    sold_value_ordered = jnp.clip(effective_target[:, None] - before_value, 0.0, available_value)
    price_col = unit_price[:, None]
    sold_units_ordered = jnp.clip(
        jnp.ceil(jnp.where(price_col > 0.0, sold_value_ordered / jnp.where(price_col > 0.0, price_col, 1.0), 0.0)),
        0.0,
        ordered_quantity,
    )
    proceeds_ordered = sold_units_ordered * price_col
    basis_ordered = sold_units_ordered * cost_basis_per_unit[ordered_lots][None, :]
    zeros = jnp.zeros_like(lot_remaining)
    sold_units = zeros.at[:, ordered_lots].set(sold_units_ordered)
    proceeds = zeros.at[:, ordered_lots].set(proceeds_ordered)
    basis = zeros.at[:, ordered_lots].set(basis_ordered)
    return sold_units, proceeds, basis, oversell


def _compute_liquid_net_worth(
    owner_cash_mask: jnp.ndarray,
    lot_asset_series_index: jnp.ndarray,
    owner_non_pe_lot_indices: tuple[int, ...],
    cash: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    external_values: jnp.ndarray,
    month: int | jnp.ndarray,
) -> jnp.ndarray:
    """Port of `phases._compute_liquid_net_worth`: owner cash + non-PE lot value at current marks.
    `owner_cash_mask` (this policy's row, device) and `lot_asset_series_index` (device) come from
    `_Baked`; `owner_non_pe_lot_indices` is the resolved (host) non-PE lot list (no `plan` reference)."""
    cash_total = (cash * owner_cash_mask[:, None]).sum(axis=0)
    if not owner_non_pe_lot_indices:
        return cash_total
    lot_indices = np.asarray(owner_non_pe_lot_indices, dtype=np.int64)
    series_indices = lot_asset_series_index[lot_indices]
    valid = series_indices >= 0
    prices = external_values[jnp.where(valid, series_indices, 0), :, month]
    prices = jnp.nan_to_num(jnp.where(valid[:, None], prices, 0.0), nan=0.0)
    lot_value = (lot_remaining[lot_indices, :] * prices).sum(axis=0)
    return cash_total + lot_value


@partial(
    jax.jit,
    static_argnames=(
        "gain_profile",
        "has_prior",
        "peak",
        "floor",
        "gamma",
        "drawdown_sensitivity",
        "short_term_fraction",
    ),
)
def _tlh_harvest_policy_jit(
    remaining_lots: jnp.ndarray,
    cost_basis_lots: jnp.ndarray,
    price: jnp.ndarray,
    prior_price: jnp.ndarray,
    cumulative: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    capital_gain_active: jnp.ndarray,
    active: jnp.ndarray,
    *,
    gain_profile: int,
    has_prior: bool,
    peak: float,
    floor: float,
    gamma: float,
    drawdown_sensitivity: float,
    short_term_fraction: float,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Port of one `HarvestPolicy`'s reduced-form monthly harvest (`tlh_harvest.monthly_harvest_fraction`
    + `split_short_long`), vectorized over rollouts: book a calibrated capital loss as a NEGATIVE in
    `capital_gain_ytd` and accumulate it into the give-back ledger `cumulative`. Per-policy params
    are static (the jitted core compiles once per policy)."""
    market_value = (remaining_lots * price[None, :]).sum(axis=0)  # (R,)
    original_basis = (remaining_lots * cost_basis_lots[:, None]).sum(axis=0)  # (R,)
    adjusted_basis = jnp.maximum(0.0, original_basis - cumulative)
    safe_mv = jnp.where(market_value > 0.0, market_value, 1.0)
    embedded_gain = jnp.clip(jnp.where(market_value > 0.0, (market_value - adjusted_basis) / safe_mv, 0.0), 0.0, 1.0)
    if has_prior:
        safe_prior = jnp.where(prior_price > 0.0, prior_price, 1.0)
        period_return = jnp.where(prior_price > 0.0, (price - prior_price) / safe_prior, 0.0)
    else:
        period_return = jnp.zeros_like(price)  # month 0: no prior price, treat as flat
    base_monthly = (floor + (peak - floor) * (1.0 - embedded_gain) ** gamma) / 12.0
    fraction = base_monthly * (1.0 + drawdown_sensitivity * jnp.maximum(0.0, -period_return))
    ceiling = jnp.maximum(0.0, original_basis - cumulative)  # never harvest past available below-basis room
    gross = jnp.where(active, jnp.minimum(jnp.maximum(market_value * fraction, 0.0), ceiling), 0.0)
    stf = min(max(short_term_fraction, 0.0), 1.0)
    short_term = int(CapitalGainClassification.SHORT_TERM)
    long_term = int(CapitalGainClassification.LONG_TERM)
    capital_gain_ytd = capital_gain_ytd.at[gain_profile, short_term].add(-gross * stf)
    capital_gain_ytd = capital_gain_ytd.at[gain_profile, long_term].add(-gross * (1.0 - stf))
    capital_gain_active = capital_gain_active.at[gain_profile, short_term].set(
        capital_gain_active[gain_profile, short_term] | (gross * stf > 0.0)
    )
    capital_gain_active = capital_gain_active.at[gain_profile, long_term].set(
        capital_gain_active[gain_profile, long_term] | (gross * (1.0 - stf) > 0.0)
    )
    return capital_gain_ytd, capital_gain_active, cumulative + gross


def _apply_brackets(amount: jnp.ndarray, *, upper: jnp.ndarray, rate: jnp.ndarray, count: int) -> jnp.ndarray:
    """Port of `phases._apply_brackets`: progressive bracket tax on `amount`."""
    if count <= 0:
        return jnp.zeros_like(amount)
    upper_edges = jnp.asarray(upper[:count])
    bracket_rates = jnp.asarray(rate[:count])
    previous_upper = jnp.concatenate([jnp.zeros(1), upper_edges[:-1]])
    slice_top = jnp.minimum(amount[:, None], upper_edges[None, :])
    in_bracket = jnp.maximum(slice_top - previous_upper[None, :], 0.0)
    return (in_bracket * bracket_rates[None, :]).sum(axis=1)


def _apply_ltcg_brackets(
    ltcg_amount: jnp.ndarray, ordinary_taxable: jnp.ndarray, *, upper: jnp.ndarray, rate: jnp.ndarray, count: int
) -> jnp.ndarray:
    """Port of `phases._apply_ltcg_brackets`: LTCG stacked on top of ordinary taxable income."""
    if count <= 0:
        return jnp.zeros_like(ltcg_amount)
    upper_edges = jnp.asarray(upper[:count])
    bracket_rates = jnp.asarray(rate[:count])
    previous_upper = jnp.concatenate([jnp.zeros(1), upper_edges[:-1]])
    total_taxable = ordinary_taxable + ltcg_amount
    slice_top = jnp.minimum(total_taxable[:, None], upper_edges[None, :])
    slice_bottom = jnp.maximum(ordinary_taxable[:, None], previous_upper[None, :])
    in_bracket = jnp.maximum(slice_top - slice_bottom, 0.0)
    return (in_bracket * bracket_rates[None, :]).sum(axis=1)


def _net_capital_gains_jnp(
    short_term: jnp.ndarray,
    long_term: jnp.ndarray,
    carryforward_in: jnp.ndarray,
    *,
    max_ordinary_offset_usd: float = 3000.0,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Branch-free `jnp` port of `tax.net_capital_gains_with_carryforward` (§1211/§1212 netting)."""
    st, lt = short_term, long_term
    st_loss_vs_lt_gain = jnp.minimum(jnp.maximum(-st, 0.0), jnp.maximum(lt, 0.0))
    st, lt = st + st_loss_vs_lt_gain, lt - st_loss_vs_lt_gain
    lt_loss_vs_st_gain = jnp.minimum(jnp.maximum(-lt, 0.0), jnp.maximum(st, 0.0))
    lt, st = lt + lt_loss_vs_st_gain, st - lt_loss_vs_st_gain
    carry = carryforward_in
    used_short_term = jnp.minimum(jnp.maximum(st, 0.0), carry)
    st, carry = st - used_short_term, carry - used_short_term
    used_long_term = jnp.minimum(jnp.maximum(lt, 0.0), carry)
    lt, carry = lt - used_long_term, carry - used_long_term
    net_short_term, net_long_term = jnp.maximum(st, 0.0), jnp.maximum(lt, 0.0)
    residual_loss = jnp.maximum(-(st + lt), 0.0) + carry
    ordinary_offset = jnp.minimum(residual_loss, max_ordinary_offset_usd)
    return net_short_term, net_long_term, ordinary_offset, residual_loss - ordinary_offset


def _compute_tax_for_link(
    static: _LinkTaxStatic,
    tcfg: _TracedConfig,
    ordinary_ytd: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    recapture_section_1250_ytd: jnp.ndarray,
    liabilities: LiabilityState,
    *,
    salt_deduction: jnp.ndarray,
    rollout_count: int,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Port of `phases._compute_tax_for_link`: one link's bracket math (MID + SALT + §1250 + LTCG).
    Bracket values / rates / deduction / MID ratio come from the traced `tcfg`; feature flags, counts
    and the §1250 style rate are read from the hashable `_LinkTaxStatic` (no `plan` reference)."""
    link = static.link
    profile = static.profile
    gain_profile = static.gain_profile
    ordinary = ordinary_ytd[profile]
    ltcg = capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM]
    stcg = capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM]
    recapture = recapture_section_1250_ytd[profile]
    section_1250_rate = static.section_1250_rate
    standard_deduction = tcfg.link_standard_deduction[link]
    if static.mid_active:
        owner_interest_ytd = liabilities.interest_ytd - liabilities.rental_interest_ytd
        mortgage_interest_deduction = tcfg.mid_principal_ratio[link] @ owner_interest_ytd
    else:
        mortgage_interest_deduction = jnp.zeros(rollout_count)
    itemized_deduction = mortgage_interest_deduction + salt_deduction
    deduction_used = jnp.maximum(itemized_deduction, standard_deduction)

    federal_style_section_1250 = section_1250_rate > 0.0
    ordinary_for_brackets = ordinary if federal_style_section_1250 else ordinary + recapture

    ordinary_upper = tcfg.link_ordinary_upper[link]
    ordinary_rate = tcfg.link_ordinary_rate[link]
    ordinary_count = static.ordinary_count
    if static.has_ltcg == 1:
        ordinary_taxable = jnp.maximum(ordinary_for_brackets + stcg - deduction_used, 0.0)
        capital_taxable = ltcg
        ordinary_tax = _apply_brackets(ordinary_taxable, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count)
        ltcg_tax = _apply_ltcg_brackets(
            ltcg,
            ordinary_taxable,
            upper=tcfg.link_ltcg_upper[link],
            rate=tcfg.link_ltcg_rate[link],
            count=static.ltcg_count,
        )
    else:
        ordinary_taxable = jnp.maximum(ordinary_for_brackets + ltcg + stcg - deduction_used, 0.0)
        capital_taxable = jnp.zeros(rollout_count)
        ordinary_tax = _apply_brackets(ordinary_taxable, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count)
        ltcg_tax = jnp.zeros(rollout_count)

    if federal_style_section_1250:
        ordinary_tax_with_recapture = _apply_brackets(
            ordinary_taxable + recapture, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count
        )
        implied_recapture_tax = jnp.maximum(ordinary_tax_with_recapture - ordinary_tax, 0.0)
        section_1250_tax = jnp.minimum(implied_recapture_tax, recapture * section_1250_rate)
    else:
        section_1250_tax = jnp.zeros(rollout_count)

    capital_tax = ltcg_tax + section_1250_tax
    return mortgage_interest_deduction, itemized_deduction, ordinary_taxable, capital_taxable, ordinary_tax, capital_tax


def _scan_property_sale(
    ev: _FoldedLifecycleEvent,
    external_values: jnp.ndarray,
    *,
    cash: jnp.ndarray,
    property_active: jnp.ndarray,
    property_rented_fraction: jnp.ndarray,
    property_building_basis: jnp.ndarray,
    property_cum_dep: jnp.ndarray,
    oo_window: jnp.ndarray,
    liab_active: jnp.ndarray,
    liab_principal: jnp.ndarray,
    recapture_ytd: jnp.ndarray,
    cg_active: jnp.ndarray,
    cg_ytd: jnp.ndarray,
    month: jnp.ndarray,
    active_property: jnp.ndarray,
    rollout_count: int,
) -> tuple[
    jnp.ndarray,
    jnp.ndarray,
    jnp.ndarray,
    jnp.ndarray,
    jnp.ndarray,
    jnp.ndarray,
    jnp.ndarray,
    jnp.ndarray,
    jnp.ndarray,
    tuple[jnp.ndarray, ...],
]:
    """Branch-free `lax.scan` port of `_apply_property_sale`: §1250 recapture + §121 exclusion (via the
    owner-occupancy window) + mortgage payoff, returning the updated state and the 7-field sale trace.
    All per-property statics come from the hashable `_FoldedLifecycleEvent` (no `plan` reference)."""
    prop = ev.property_slot
    closing_cost_pct = ev.amount
    series_idx = ev.home_value_series_index
    market_value = ev.purchase_price * external_values[series_idx, :, month] / external_values[series_idx, :, 0]
    gross_proceeds = market_value * (1.0 - closing_cost_pct / 100.0)
    capex = property_building_basis[prop] - ev.building_basis_initial
    cum_dep = property_cum_dep[prop]
    realized_gain = gross_proceeds - (ev.purchase_price + capex - cum_dep)
    recapture = jnp.minimum(jnp.maximum(realized_gain, 0.0), cum_dep)
    post_recapture_gain = jnp.maximum(realized_gain - recapture, 0.0)
    # §121: months owner-occupied within the trailing 60-month window (the carried ring's column sum).
    qualifies = oo_window[:, prop, :].sum(axis=0) >= SECTION_121_MIN_QUALIFYING_MONTHS
    owner_profile = ev.owner_profile
    exclusion_cap = ev.exclusion_cap
    section_121_exclusion = jnp.where(qualifies, jnp.minimum(post_recapture_gain, exclusion_cap), 0.0)
    ltcg = post_recapture_gain - section_121_exclusion
    mortgage_payoff = jnp.zeros(rollout_count)
    for lia in ev.mortgage_liabilities:
        mortgage_payoff = mortgage_payoff + liab_principal[lia]
        liab_principal = liab_principal.at[lia].set(jnp.where(active_property, 0.0, liab_principal[lia]))
        liab_active = liab_active.at[lia].set(jnp.where(active_property, False, liab_active[lia]))
    net_cash = gross_proceeds - mortgage_payoff
    owner_cash_slot = ev.owner_cash_slot
    if owner_cash_slot >= 0:
        cash = cash.at[owner_cash_slot].add(jnp.where(active_property, net_cash, 0.0))
    if owner_profile >= 0:
        recapture_ytd = recapture_ytd.at[owner_profile].add(jnp.where(active_property, recapture, 0.0))
        gain_profile = ev.gain_profile
        if gain_profile >= 0:
            lt = int(CapitalGainClassification.LONG_TERM)
            cg_ytd = cg_ytd.at[gain_profile, lt].add(jnp.where(active_property, ltcg, 0.0))
            cg_active = cg_active.at[gain_profile, lt].set(cg_active[gain_profile, lt] | active_property)
    property_active = property_active.at[prop].set(property_active[prop] & ~active_property)
    property_rented_fraction = property_rented_fraction.at[prop].set(
        jnp.where(active_property, 0.0, property_rented_fraction[prop])
    )
    property_building_basis = property_building_basis.at[prop].set(
        jnp.where(active_property, 0.0, property_building_basis[prop])
    )
    sale_trace = (
        jnp.where(active_property, gross_proceeds, 0.0),
        jnp.where(active_property, mortgage_payoff, 0.0),
        jnp.where(active_property, net_cash, 0.0),
        jnp.where(active_property, realized_gain, 0.0),
        jnp.where(active_property, recapture, 0.0),
        jnp.where(active_property, section_121_exclusion, 0.0),
        jnp.where(active_property, ltcg, 0.0),
    )
    return (
        cash,
        property_active,
        property_rented_fraction,
        property_building_basis,
        liab_active,
        liab_principal,
        recapture_ytd,
        cg_active,
        cg_ytd,
        sale_trace,
    )


@jax.jit
def _apply_depreciation_accrual(
    property_active: jnp.ndarray,
    property_rented_fraction: jnp.ndarray,
    property_building_basis: jnp.ndarray,
    property_cumulative_depreciation: jnp.ndarray,
    property_depreciation_ytd: jnp.ndarray,
    failed: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Port of `phases._apply_depreciation_accrual`: §168 straight-line monthly depreciation,
    branch-free over all properties (one masked elementwise accrual)."""
    monthly_dep = jnp.where(
        (~failed)[None, :] & property_active, property_building_basis * property_rented_fraction / (27.5 * 12.0), 0.0
    )
    return property_cumulative_depreciation + monthly_dep, property_depreciation_ytd + monthly_dep
