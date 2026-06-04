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

import hashlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from functools import partial
from typing import Any, NamedTuple

import jax
import jax.numpy as jnp
import numpy as np

from augur.model.series import PrivateEquityRegimeCode
from augur.sim.buffers import SimulationBuffers
from augur.sim.codec.plan import CompiledSimulation
from augur.sim.compiler.helpers import AMOUNT_FIXED, NO_CODE
from augur.sim.enums import (
    CapitalGainClassification,
    LifecycleKind,
    ObligationSource,
    PrivateEquityDispositionKind,
    PrivateEquityOpportunityOutcome,
)
from augur.sim.tensor_fifo import lot_order_for_pool

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
    array); `buffer_index` is the sale's column in the `lot_dispositions.scheduled` buffers; `month`
    is the (static) month it fires, compared against the traced scan index inside the step."""

    buffer_index: int
    month: int
    ordered_lots: np.ndarray
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
    ordered_lots: np.ndarray


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


# Fields that flow into the compiled program as TRACED inputs (not baked constants) are EXCLUDED from
# the fingerprint: the same compiled program is reused across plans that differ only in these values.
# Everything else the program bakes IS fingerprinted, so a cache hit guarantees byte-identical baked
# config (correct reuse). Two classes of traced input:
#   - seed-varying series (`external_values`, the PE channel arrays) → rollout-draw reuse;
#   - swept numeric config (the income/LTCG tax brackets, rates, standard deduction, MID principal
#     ratio, transfer amounts, and per-lot cost basis) → config-value-sweep reuse, with the structural
#     feature/count `if`s left baked so they still ride in the fingerprint. See `_traced_config`.
_FINGERPRINT_EXCLUDE: frozenset[tuple[str, str]] = frozenset(
    {
        ("CompiledSimulation", "external_values"),
        ("CompiledSimulation", "pe_channels"),
        ("CompiledSimulation", "lot_cost_basis_per_unit"),
        ("CompiledSimulation", "cash_initial_balance"),
        ("CompiledSimulation", "lot_initial_quantity"),
        ("TaxCompileOutput", "link_standard_deduction"),
        ("TaxCompileOutput", "link_ordinary_upper"),
        ("TaxCompileOutput", "link_ordinary_rate"),
        ("TaxCompileOutput", "link_ltcg_upper"),
        ("TaxCompileOutput", "link_ltcg_rate"),
        ("MIDCompileOutput", "principal_ratio"),
        ("TransferCompileOutput", "amount_fixed"),
        ("TransferCompileOutput", "amount_base"),
        ("PropertyCompileOutput", "adjusted_basis"),
        ("PropertyCompileOutput", "ownership"),
        ("PropertyCompileOutput", "equity_ledger"),
        ("LiabilityCompileOutput", "principal"),
        ("LiabilityCompileOutput", "monthly_payment"),
    }
)


class _TracedConfig(NamedTuple):
    """JAX-native typed bundle of the swept numeric config the compiled program takes as TRACED inputs
    (a NamedTuple → native JAX pytree, so it passes through `jax.jit` typed). The cores read VALUES from
    here (`jax.Array`s) while reading structure / feature flags / counts / slot indices from the
    concrete `plan` — so nothing puns a traced array into the compiler's NumPy-typed plan fields. Each
    field mirrors a `_FINGERPRINT_EXCLUDE` entry, so a sweep over these values reuses the program."""

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
    """Build the traced-config bundle from the (concrete) plan. Mirrors the fingerprint exclusions."""
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


def _fingerprint_into(h: Any, obj: Any) -> None:
    if is_dataclass(obj) and not isinstance(obj, type):
        h.update(b"D")
        h.update(type(obj).__name__.encode())
        for f in fields(obj):
            if (type(obj).__name__, f.name) in _FINGERPRINT_EXCLUDE:
                continue
            h.update(f.name.encode())
            _fingerprint_into(h, getattr(obj, f.name))
    elif isinstance(obj, np.ndarray):
        h.update(b"A")
        h.update(str(obj.dtype).encode())
        h.update(repr(obj.shape).encode())
        h.update(np.ascontiguousarray(obj).tobytes())
    elif isinstance(obj, (list, tuple)):
        h.update(b"L")
        h.update(str(len(obj)).encode())
        for x in obj:
            _fingerprint_into(h, x)
    elif isinstance(obj, dict):
        h.update(b"M")
        for k in sorted(obj, key=repr):
            h.update(repr(k).encode())
            _fingerprint_into(h, obj[k])
    else:
        # Scalars, enums, AssetKey/LevelSeriesKey (frozen value objects): stable repr captures content.
        h.update(b"S")
        h.update(repr(obj).encode())


def _plan_fingerprint(plan: CompiledSimulation) -> bytes:
    """Content hash of everything the compiled scan program bakes as a constant (all of `plan` except
    the seed-varying traced inputs). Two plans with the same fingerprint produce byte-identical
    programs, so the compiled executable can be safely reused."""
    h = hashlib.blake2b(digest_size=16)
    _fingerprint_into(h, plan)
    return h.digest()


# Compiled scan programs, keyed by plan fingerprint (LRU-bounded). Each entry is the jitted device
# program plus the structural `_ScanMeta` for post-scan scatter. Bounded so a long-lived process that
# simulates many distinct structures doesn't retain every executable.
_PROGRAM_CACHE: OrderedDict[bytes, tuple[Callable, _ScanMeta]] = OrderedDict()
_PROGRAM_CACHE_MAX = 32


def _get_program(plan: CompiledSimulation) -> tuple[Callable, _ScanMeta]:
    key = _plan_fingerprint(plan)
    cached = _PROGRAM_CACHE.get(key)
    if cached is not None:
        _PROGRAM_CACHE.move_to_end(key)
        return cached
    result = _build_program(plan)
    _PROGRAM_CACHE[key] = result
    if len(_PROGRAM_CACHE) > _PROGRAM_CACHE_MAX:
        _PROGRAM_CACHE.popitem(last=False)
    return result


def run_jax_scan(plan: CompiledSimulation, buffers: SimulationBuffers) -> None:
    """Cached single-program `lax.scan` engine: the whole month loop compiles into one XLA program
    (one dispatch for all months) whose only traced inputs are the seed-varying series. The traced
    program is cached by plan fingerprint, so repeated simulations of the same scenario structure
    (e.g. a Monte-Carlo sweep over rollout draws) pay tracing + XLA compilation once, not per call.

    The first call for a structure builds + compiles the program (`_build_program`); subsequent calls
    reuse the cached executable, passing the new draw's `external_values` / PE channel arrays."""
    # PE-mark validation is seed-dependent (the marks are a sampled series), so it runs every call on
    # the concrete plan — the in-scan path can't raise.
    pe_channels = plan.pe_channels
    if pe_channels.marks.size and (not np.isfinite(pe_channels.marks).all() or (pe_channels.marks < 0.0).any()):
        raise ValueError("private-equity mark series produced a negative or non-finite value")
    if pe_channels.forced_recovery_cashout_usd.size and (pe_channels.forced_recovery_cashout_usd < 0.0).any():
        raise ValueError("private-equity forced-recovery cashout series produced a negative value")

    program, meta = _get_program(plan)
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
    ys = program(jnp.asarray(plan.external_values), pe_ch_dyn, _traced_config(plan))
    _scatter_ys_to_buffers(plan, buffers, meta, ys)


def _build_program(plan: CompiledSimulation) -> tuple[Callable, _ScanMeta]:
    """Build (host precompute) + jit-compile the device program for one plan *structure*. Returns the
    jitted program (traced inputs: `external_values`, the PE channel dict) and the structural
    `_ScanMeta`. Called once per fingerprint; the result is cached by `_get_program`.

    All numeric config is baked as XLA constants here; only the seed-varying series are traced, so the
    compiled executable is reusable across rollout draws of the same scenario structure. The carry is
    the per-rollout `_ScanState` pytree; each step gathers the month's plan rows by the traced scan
    index and runs the branch-free cores (transfers, purchases/mortgages, sales, obligations, TLH,
    private equity, §121/§168 property accrual, year-end tax)."""
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
    # Traced inputs: placeholders here, rebound (via `nonlocal`) to the traced arguments inside
    # `_program_impl`. `step` / `december_tax` close over these names and read the traced values at
    # trace time. `tcfg` carries the swept numeric VALUES; `plan` stays the concrete structure.
    external_values: jnp.ndarray = None  # type: ignore[assignment]
    pe_ch: dict[str, jnp.ndarray] = None  # type: ignore[assignment]
    cost_basis_per_unit: jnp.ndarray = None  # type: ignore[assignment]
    tcfg: _TracedConfig = None  # type: ignore[assignment]
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
    folded_lifecycle = [
        (i, int(le_event_month[i]), int(le_all.kind[i]), int(le_all.property_slot[i]))
        for i in range(le_all.kind.shape[0])
        if le_event_month[i] >= 0
    ]
    pr_event_month = _event_months(pr_starts, pr_events.agent_slot.shape[0])
    folded_pr = [(i, int(pr_event_month[i])) for i in range(pr_events.agent_slot.shape[0]) if pr_event_month[i] >= 0]
    folded_sale_events = [(i, m) for (i, m, k, _prop) in folded_lifecycle if k == LifecycleKind.SALE]

    # Scheduled asset sales: resolve each real sale's static FIFO data once (host-side); the step
    # applies all firing sales (masked by the traced month). No sales -> the whole block is skipped.
    sales = plan.sales
    folded_sales = [
        _FoldedSale(
            buffer_index=s,
            month=int(sales.month[s]),
            ordered_lots=lot_order_for_pool(
                lot_agent_codes=plan.lot_agent_codes,
                lot_account_codes=plan.lot_account_codes,
                lot_asset_codes=plan.lot_asset_codes,
                lot_purchase_month=plan.lot_purchase_month,
                lot_id_codes=plan.lot_id_codes,
                agent_code=int(sales.agent[s]),
                account_code=int(sales.source_account[s]),
                asset_code=int(sales.asset[s]),
            ),
            quantity=float(sales.quantity[s]),
            proceeds_slot=int(sales.proceeds_slot[s]),
            agent_code=int(sales.agent[s]),
        )
        for s in range(sales.month.shape[0])
        if int(sales.month[s]) >= 0
    ]
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
                    pools.append(_LiquidityPool(asset_idx=asset_idx, series_index=series_index, ordered_lots=ordered))
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
    folded_pe: list[tuple[int, int, np.ndarray]] = []
    for issuer_idx in range(pe_issuer_count):
        if int(pe_issuers.codes[issuer_idx]) < 0:
            continue
        lot_indices = np.flatnonzero(pe_issuers.lot_mask[issuer_idx])
        if lot_indices.size == 0:
            continue
        ordered = lot_indices[np.argsort(plan.lot_purchase_month[lot_indices], kind="stable")]
        folded_pe.append((issuer_idx, int(pe_issuers.policy_index[issuer_idx]), ordered))

    # TLH harvest policies: per-policy static data (the jitted core books a calibrated capital loss).
    harvest = plan.harvest_policies
    folded_harvest: list[tuple] = []
    for policy_idx in range(harvest.gain_profile_index.shape[0]):
        gain_profile = int(harvest.gain_profile_index[policy_idx])
        lot_indices = np.flatnonzero(harvest.lot_mask[policy_idx])
        if gain_profile < 0 or lot_indices.size == 0:
            continue
        params = harvest.params[policy_idx]
        folded_harvest.append(
            (
                policy_idx,
                gain_profile,
                lot_indices,
                int(harvest.series_index[policy_idx]),
                float(params.peak_annual_yield),
                float(params.floor_annual_yield),
                float(params.maturity_decay_exponent),
                float(params.drawdown_sensitivity),
                float(harvest.short_term_fraction[policy_idx]),
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

        # Schedule E: rented-share mortgage interest (handled via MID below) + §168 depreciation.
        for prop in range(property_dep_ytd.shape[0]):
            profile = int(plan.property_owner_profile_index[prop])
            if profile >= 0:
                ordinary = ordinary.at[profile].add(-jnp.where(dec, property_dep_ytd[prop], 0.0))
        for lia in range(liab_rental_ytd.shape[0]):
            profile = int(plan.liability_owner_profile_index[lia])
            if profile >= 0:
                ordinary = ordinary.at[profile].add(-jnp.where(dec, liab_rental_ytd[lia], 0.0))

        # §1211/§1212 netting per capital-gain agent.
        seen: set[int] = set()
        for profile in range(profile_count):
            gp = int(plan.tax_profile_capital_gain_index[profile])
            if gp < 0 or gp in seen:
                continue
            seen.add(gp)
            net_st, net_lt, ord_offset, carry_out = _net_capital_gains_jnp(
                cg_ytd[gp, CapitalGainClassification.SHORT_TERM],
                cg_ytd[gp, CapitalGainClassification.LONG_TERM],
                carryforward[gp],
            )
            cg_ytd = cg_ytd.at[gp, CapitalGainClassification.SHORT_TERM].set(
                jnp.where(dec, net_st, cg_ytd[gp, CapitalGainClassification.SHORT_TERM])
            )
            cg_ytd = cg_ytd.at[gp, CapitalGainClassification.LONG_TERM].set(
                jnp.where(dec, net_lt, cg_ytd[gp, CapitalGainClassification.LONG_TERM])
            )
            rep = int(cg_rep_profile[gp])
            ordinary = ordinary.at[rep].add(-jnp.where(dec, ord_offset, 0.0))
            carryforward = carryforward.at[gp].set(jnp.where(dec, carry_out, carryforward[gp]))

        # Two-pass SALT bracket math; collect per-link tax + breakdown slabs.
        annual_tax_by_link = jnp.zeros((r, max(1, link_count)))
        zero_salt = jnp.zeros(r)
        breakdown = [jnp.zeros((max(1, link_count), r)) for _ in range(13)]

        def run_link(link: int, salt_deduction: jnp.ndarray, ann: jnp.ndarray) -> jnp.ndarray:
            mid, itemized, ord_taxable, cap_taxable, ord_tax, cap_tax = _compute_tax_for_link(
                plan,
                tcfg,
                ordinary,
                cg_ytd,
                recapture,
                liabs_view,
                link=link,
                salt_deduction=salt_deduction,
                rollout_count=r,
            )
            profile = int(taxc.link_profile[link])
            gp = int(plan.tax_profile_capital_gain_index[profile])
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
            profile = int(taxc.link_profile[link])
            state_tax_total = annual_tax_by_link @ jnp.asarray(plan.salt.contributing_mask[link].astype(np.float64))
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
        active = ~failed

        # Primary-residence + lifecycle events (first in the month, eager order). Each event fires when
        # its static month equals the traced month, masked per-rollout. is_primary is precomputed
        # per-month host-side; the SALE path uses the §121 owner-occupancy window for the exclusion.
        pr_fired = [jnp.where(month == pr_m, active, jnp.zeros_like(active)) for _, pr_m in folded_pr]
        le_fired: list[jnp.ndarray] = []
        sale_traces: list[tuple] = []
        for ev_i, ev_month, ev_kind, ev_prop in folded_lifecycle:
            fires = month == ev_month
            active_property = fires & active & property_active[ev_prop]
            if ev_kind == LifecycleKind.FRACTION:
                property_rented_fraction = property_rented_fraction.at[ev_prop].set(
                    jnp.where(active_property, float(le_all.rented_fraction[ev_i]), property_rented_fraction[ev_prop])
                )
            elif ev_kind == LifecycleKind.CAPITAL_IMPROVEMENT:
                amount = float(le_all.amount[ev_i])
                owner_cash_slot = int(props.buyer_slot[ev_prop])
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
                    plan,
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
                    prop=ev_prop,
                    closing_cost_pct=float(le_all.amount[ev_i]),
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

        # Property purchases (after transfers, before sales — eager order). Each real purchase fires
        # when its static month equals the traced month, for the rollouts still active then; the down
        # payment (stake_contribution) moves buyer->seller and, when financed, the mortgage liability
        # is originated (principal + monthly payment set, YTD interest/principal reset).
        purchase_active_rows, transfer_active_rows = [], []
        mortgage_origination_rows: dict[int, jnp.ndarray] = {}
        for fp in folded_purchases:
            fires = month == fp.month
            buy = fires & active  # (rollouts,)
            property_active = property_active.at[fp.buffer_index].set(
                jnp.where(buy, True, property_active[fp.buffer_index])
            )
            # Pure-value purchase amounts read as traced inputs from `tcfg` by index.
            property_basis = property_basis.at[fp.buffer_index].set(
                jnp.where(buy, tcfg.property_adjusted_basis[fp.buffer_index], property_basis[fp.buffer_index])
            )
            property_ownership = property_ownership.at[fp.buffer_index].set(
                jnp.where(buy, tcfg.property_ownership[fp.buffer_index], property_ownership[fp.buffer_index])
            )
            property_contribution = property_contribution.at[fp.buffer_index].set(
                jnp.where(buy, fp.stake_contribution, property_contribution[fp.buffer_index])
            )
            property_equity = property_equity.at[fp.buffer_index].set(
                jnp.where(buy, tcfg.property_equity_ledger[fp.buffer_index], property_equity[fp.buffer_index])
            )
            transfer_fires = buy if fp.stake_contribution > 0.0 else jnp.zeros_like(buy)
            if fp.stake_contribution > 0.0:
                if fp.buyer_slot >= 0:
                    cash = cash.at[fp.buyer_slot].add(jnp.where(buy, -fp.stake_contribution, 0.0))
                if fp.seller_slot >= 0:
                    cash = cash.at[fp.seller_slot].add(jnp.where(buy, fp.stake_contribution, 0.0))
            if fp.mortgage_slot >= 0:
                ms = fp.mortgage_slot
                liab_active = liab_active.at[ms].set(jnp.where(buy, True, liab_active[ms]))
                liab_principal = liab_principal.at[ms].set(
                    jnp.where(buy, tcfg.liability_principal[ms], liab_principal[ms])
                )
                liab_monthly = liab_monthly.at[ms].set(
                    jnp.where(buy, tcfg.liability_monthly_payment[ms], liab_monthly[ms])
                )
                liab_interest_ytd = liab_interest_ytd.at[ms].set(jnp.where(buy, 0.0, liab_interest_ytd[ms]))
                liab_principal_ytd = liab_principal_ytd.at[ms].set(jnp.where(buy, 0.0, liab_principal_ytd[ms]))
                mortgage_origination_rows[ms] = buy
            purchase_active_rows.append(buy)
            transfer_active_rows.append(transfer_fires)

        # Scheduled asset sales (before obligations, matching eager order: proceeds can fund the
        # month's obligations). Each real sale fires when its static month equals the traced month;
        # `_fifo_sell_units` sells nothing when target_units is 0, so non-firing slots are no-ops.
        disp_active, disp_units, disp_basis, disp_proceeds = [], [], [], []
        oversells = []
        for fs in folded_sales:
            fires = month == fs.month
            target_units = jnp.where(active & fires, fs.quantity, 0.0)
            unit_price = _sale_unit_price(sales, external_values, month, fs.buffer_index, r)
            sold_units, proceeds, basis, oversell = _fifo_sell_units(
                lot_remaining.T, fs.ordered_lots, target_units, unit_price, cost_basis_per_unit
            )
            lot_remaining = lot_remaining - sold_units.T
            if fs.proceeds_slot >= 0:
                cash = cash.at[fs.proceeds_slot].add(proceeds.sum(axis=1))
            cg_active, cg_ytd, tlh = _record_capital_gains(
                plan, cg_active, cg_ytd, tlh, lot_remaining, month, fs.agent_code, sold_units, proceeds - basis
            )
            disp_active.append((sold_units > 0.0).T)
            disp_units.append(sold_units.T)
            disp_basis.append(basis.T)
            disp_proceeds.append(proceeds.T)
            oversells.append(oversell.any())

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
                available = lot_remaining[pool.ordered_lots].sum(axis=0) * unit_price
                target = jnp.where(valid_price & active, jnp.minimum(jnp.maximum(remaining, 0.0), available), 0.0)
                sold_units, proceeds, basis, _ovr = _fifo_sell_dollars(
                    lot_remaining.T, pool.ordered_lots, target, unit_price, cost_basis_per_unit
                )
                lot_remaining = lot_remaining - sold_units.T
                total_proceeds = proceeds.sum(axis=1)
                cash = cash.at[lp.cash_slot].add(total_proceeds)
                cg_active, cg_ytd, tlh = _record_capital_gains(
                    plan, cg_active, cg_ytd, tlh, lot_remaining, month, lp.agent, sold_units, proceeds - basis
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
        mort_orig = jnp.zeros((liab_count, r), dtype=bool)
        for ms, buy in mortgage_origination_rows.items():
            mort_orig = mort_orig.at[ms].set(buy)

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
        for (
            hp_policy,
            hp_gain_profile,
            hp_lots,
            hp_series,
            hp_peak,
            hp_floor,
            hp_gamma,
            hp_dd,
            hp_stf,
        ) in folded_harvest:
            hp_price = external_values[hp_series, :, month]
            hp_prior = external_values[hp_series, :, jnp.maximum(0, month - 1)]
            cg_ytd, cg_active, hp_cumulative = _tlh_harvest_policy_jit(
                lot_remaining[hp_lots, :],
                cost_basis_per_unit[hp_lots],
                hp_price,
                hp_prior,
                tlh[hp_policy],
                cg_ytd,
                cg_active,
                active,
                gain_profile=hp_gain_profile,
                has_prior=True,
                peak=hp_peak,
                floor=hp_floor,
                gamma=hp_gamma,
                drawdown_sensitivity=hp_dd,
                short_term_fraction=hp_stf,
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
        for issuer_idx, policy_idx, ordered in folded_pe:
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
            proceeds_slot = int(pe_policies.proceeds_cash_slot[policy_idx])
            owner = int(pe_policies.owner_agent[policy_idx])

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
                    plan, cg_active, cg_ytd, tlh, lot_remaining, month, owner, sold, proceeds - basis
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
                amount_kind=int(pe_policies.floor_kind[policy_idx]),
                amount_fixed=float(pe_policies.floor_fixed[policy_idx]),
                amount_base=float(pe_policies.floor_base[policy_idx]),
                amount_series=int(pe_policies.floor_series[policy_idx]),
                amount_base_month=int(pe_policies.floor_base_month[policy_idx]),
                amount_period=int(pe_policies.floor_period[policy_idx]),
                external_values=external_values,
                month=month,
                rollout_count=r,
            )
            lnw = _compute_liquid_net_worth(plan, cash, lot_remaining, external_values, policy_idx, month)
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
        purchase_ys = (jnp.stack(purchase_active_rows), jnp.stack(transfer_active_rows)) if folded_purchases else ()
        # Mortgage event slabs (per-liability), only when the plan has liabilities (event buffers are
        # padded to max(1, liability_count), so a 0-row emit can't be scattered into them).
        mortgage_ys = (
            (mort_orig, mort_pay_active, mort_pay_interest, mort_pay_principal, mort_pay_total)
            if liab_count > 0
            else ()
        )
        # Per-(sale, lot, rollout) disposition slabs (stacked over real sales) + a per-month oversell
        # flag; all empty when there are no sales.
        sale_ys = (
            (
                jnp.stack(disp_active),
                jnp.stack(disp_units),
                jnp.stack(disp_basis),
                jnp.stack(disp_proceeds),
                jnp.stack(oversells).any(),
            )
            if folded_sales
            else ()
        )
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

    prop0 = jnp.zeros((p.property_count, r))
    liab0 = jnp.zeros((p.liability_count, r))
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
    )
    months = jnp.arange(horizon, dtype=jnp.int32)

    def _program_impl(
        external_values_arg: jnp.ndarray, pe_ch_arg: dict[str, jnp.ndarray], cfg_arg: _TracedConfig
    ) -> tuple:
        # Rebind the traced placeholders to this draw's arguments; `step`, `december_tax` and the cores
        # read them from the enclosing scope (`tcfg` holds the swept numeric VALUES; `plan` stays the
        # concrete structure). The transfer-amount entries of the (closed-over, mutable) `tr` table are
        # overwritten in place.
        nonlocal external_values, pe_ch, cost_basis_per_unit, tcfg
        external_values = external_values_arg
        pe_ch = pe_ch_arg
        tcfg = cfg_arg
        cost_basis_per_unit = cfg_arg.cost_basis_per_unit
        tr["fixed"] = cfg_arg.transfer_amount_fixed
        tr["base"] = cfg_arg.transfer_amount_base
        # Initial cash / lot carry: broadcast the traced per-entity opening balances across rollouts.
        init_traced = init._replace(
            cash=jnp.broadcast_to(cfg_arg.cash_initial_balance[:, None], (p.cash_count, r)),
            lot_remaining=jnp.broadcast_to(cfg_arg.lot_initial_quantity[:, None], (p.lot_count, r)),
        )
        _, ys = jax.lax.scan(step, init_traced, months)
        return ys

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
    return jax.jit(_program_impl), meta


def _scatter_ys_to_buffers(plan: CompiledSimulation, buffers: SimulationBuffers, meta: _ScanMeta, ys: tuple) -> None:
    """Scatter the stacked per-month `ys` from the compiled program back into the NumPy buffers (one
    device->host transfer). Pure host code; uses `meta` for the structural scatter targets."""
    p = plan.slot_plan
    r = p.rollout_count
    horizon = meta.horizon
    link_count = meta.link_count
    folded_sales = meta.folded_sales
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
    n_sale = 5 if folded_sales else 0
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
    sale_h = rest[:o1]
    purchase_h = rest[o1:o2]
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
    if folded_sales:
        disp_active_h, disp_units_h, disp_basis_h, disp_proceeds_h, oversell_h = sale_h
        # Match the eager engine's hard error (it raises mid-loop the first month a sale oversells).
        if bool(np.asarray(oversell_h).any()):
            raise ValueError("scheduled asset sale exceeds available lots")
        # `sale_h` stacks are `(horizon, num_real_sales, L, R)`; scatter each real sale to its column.
        disp = buffers.lot_dispositions.scheduled
        disp_active_np, disp_units_np, disp_basis_np, disp_proceeds_np = (
            np.asarray(a) for a in (disp_active_h, disp_units_h, disp_basis_h, disp_proceeds_h)
        )
        for i, fs in enumerate(folded_sales):
            disp.active[:, fs.buffer_index] = disp_active_np[:, i]
            disp.units[:, fs.buffer_index] = disp_units_np[:, i]
            disp.basis[:, fs.buffer_index] = disp_basis_np[:, i]
            disp.proceeds[:, fs.buffer_index] = disp_proceeds_np[:, i]
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
        for pos, (i, ev_month, _kind, _prop) in enumerate(folded_lifecycle):
            buffers.lifecycle.fired[i] = fired_np[ev_month, pos]
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


def _sale_unit_price(
    sales, external_values: jnp.ndarray, month: int | jnp.ndarray, sale: int, rollout_count: int
) -> jnp.ndarray:
    fixed_price = float(sales.price_fixed[sale])
    if not np.isnan(fixed_price):
        return jnp.full(rollout_count, fixed_price)
    return external_values[int(sales.price_series[sale]), :, month]


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
    plan: CompiledSimulation,
    tlh_cumulative_harvest: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    sold_units: jnp.ndarray,
    gains: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Port of `phases._apply_tlh_give_back`: repay deferred harvested loss as extra gain on sold
    harvest-policy lots. The fraction of the policy's pre-sale units sold here realizes that share
    of `tlh_cumulative_harvest`, distributed across the sold policy-lots by sold units (preserving
    each lot's ST/LT character) and drained from the ledger. Branch-free over rollouts; the per-policy
    Python loop is over static plan data. `lot_remaining` is post-sale (caller already subtracted)."""
    harvest = plan.harvest_policies
    for policy_idx in range(harvest.gain_profile_index.shape[0]):
        if int(harvest.gain_profile_index[policy_idx]) < 0:
            continue
        lot_indices = np.flatnonzero(harvest.lot_mask[policy_idx])
        if lot_indices.size == 0:
            continue
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
    plan: CompiledSimulation,
    capital_gain_active: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    tlh_cumulative_harvest: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    month: int | jnp.ndarray,
    agent_code: int,
    sold_units: jnp.ndarray,
    gains: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Port of `phases._record_capital_gains`: TLH give-back, then classify each lot's gain
    long/short and accrue.

    Branch-free: the per-lot long/short split is a static `(L,)` boolean mask (holding period vs
    the lot's purchase month), so the whole `[2, R]` classification block is one masked sum/any —
    no per-lot scatter loop, no data-dependent branching. The only Python loop is over the
    statically-known capital-gain profiles matching `agent_code`.
    """
    gains, tlh_cumulative_harvest = _apply_tlh_give_back(plan, tlh_cumulative_harvest, lot_remaining, sold_units, gains)
    long_mask = jnp.asarray(month - plan.lot_purchase_month >= 12)  # (L,)
    masks = jnp.stack([long_mask, ~long_mask])  # (2, L), rows ordered LONG_TERM=0, SHORT_TERM=1
    sold = sold_units > 0.0  # (R, L)
    # einsum over lots: (2, L) x (R, L) -> (2, R) per-classification gain sums and activity flags.
    gains_by_class = jnp.einsum("cl,rl->cr", masks.astype(gains.dtype), gains)
    active_by_class = (masks[:, None, :] & sold[None, :, :]).any(axis=2)  # (2, R)
    for profile in np.flatnonzero(plan.capital_gain_agent_codes == agent_code).tolist():
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
    plan: CompiledSimulation,
    cash: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    external_values: jnp.ndarray,
    policy_idx: int,
    month: int | jnp.ndarray,
) -> jnp.ndarray:
    """Port of `phases._compute_liquid_net_worth`: owner cash + non-PE lot value at current marks."""
    owner_cash_mask = jnp.asarray(plan.pe_policies.owner_cash_mask[policy_idx])
    cash_total = (cash * owner_cash_mask[:, None]).sum(axis=0)
    lot_mask = plan.pe_policies.owner_non_pe_lot_mask[policy_idx]
    if not lot_mask.any():
        return cash_total
    lot_indices = np.flatnonzero(lot_mask)
    series_indices = plan.lot_asset_series_index[lot_indices]
    valid = series_indices >= 0
    prices = external_values[np.where(valid, series_indices, 0), :, month]
    prices = jnp.nan_to_num(jnp.where(jnp.asarray(valid)[:, None], prices, 0.0), nan=0.0)
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
    plan: CompiledSimulation,
    tcfg: _TracedConfig,
    ordinary_ytd: jnp.ndarray,
    capital_gain_ytd: jnp.ndarray,
    recapture_section_1250_ytd: jnp.ndarray,
    liabilities: LiabilityState,
    *,
    link: int,
    salt_deduction: jnp.ndarray,
    rollout_count: int,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Port of `phases._compute_tax_for_link`: one link's bracket math (MID + SALT + §1250 + LTCG).
    Bracket values / rates / deduction / MID ratio come from the traced `tcfg`; feature flags, counts
    and the §1250 style rate are read from the concrete `plan`."""
    t = plan.tax
    profile = int(t.link_profile[link])
    gain_profile = int(plan.tax_profile_capital_gain_index[profile])
    ordinary = ordinary_ytd[profile]
    ltcg = capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM]
    stcg = capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM]
    recapture = recapture_section_1250_ytd[profile]
    section_1250_rate = float(t.link_section_1250_rate[link])
    standard_deduction = tcfg.link_standard_deduction[link]
    if bool(plan.mid.link_active[link]):
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
    ordinary_count = int(t.link_ordinary_count[link])
    if int(t.link_has_ltcg[link]) == 1:
        ordinary_taxable = jnp.maximum(ordinary_for_brackets + stcg - deduction_used, 0.0)
        capital_taxable = ltcg
        ordinary_tax = _apply_brackets(ordinary_taxable, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count)
        ltcg_tax = _apply_ltcg_brackets(
            ltcg,
            ordinary_taxable,
            upper=tcfg.link_ltcg_upper[link],
            rate=tcfg.link_ltcg_rate[link],
            count=int(t.link_ltcg_count[link]),
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
    plan: CompiledSimulation,
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
    prop: int,
    closing_cost_pct: float,
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
    owner-occupancy window) + mortgage payoff, returning the updated state and the 7-field sale trace."""
    series_idx = int(plan.property_home_value_series_index[prop])
    market_value = (
        float(plan.properties.purchase_price[prop])
        * external_values[series_idx, :, month]
        / external_values[series_idx, :, 0]
    )
    gross_proceeds = market_value * (1.0 - closing_cost_pct / 100.0)
    capex = property_building_basis[prop] - float(plan.property_building_basis[prop])
    cum_dep = property_cum_dep[prop]
    realized_gain = gross_proceeds - (float(plan.properties.purchase_price[prop]) + capex - cum_dep)
    recapture = jnp.minimum(jnp.maximum(realized_gain, 0.0), cum_dep)
    post_recapture_gain = jnp.maximum(realized_gain - recapture, 0.0)
    # §121: months owner-occupied within the trailing 60-month window (the carried ring's column sum).
    qualifies = oo_window[:, prop, :].sum(axis=0) >= SECTION_121_MIN_QUALIFYING_MONTHS
    owner_profile = int(plan.property_owner_profile_index[prop])
    exclusion_cap = float(plan.tax.profile_section_121_exclusion[owner_profile]) if owner_profile >= 0 else 0.0
    section_121_exclusion = jnp.where(qualifies, jnp.minimum(post_recapture_gain, exclusion_cap), 0.0)
    ltcg = post_recapture_gain - section_121_exclusion
    mortgage_payoff = jnp.zeros(rollout_count)
    for lia in range(plan.liabilities.property_slot.shape[0]):
        if int(plan.liabilities.property_slot[lia]) == prop:
            mortgage_payoff = mortgage_payoff + liab_principal[lia]
            liab_principal = liab_principal.at[lia].set(jnp.where(active_property, 0.0, liab_principal[lia]))
            liab_active = liab_active.at[lia].set(jnp.where(active_property, False, liab_active[lia]))
    net_cash = gross_proceeds - mortgage_payoff
    owner_cash_slot = int(plan.properties.buyer_slot[prop])
    if owner_cash_slot >= 0:
        cash = cash.at[owner_cash_slot].add(jnp.where(active_property, net_cash, 0.0))
    if owner_profile >= 0:
        recapture_ytd = recapture_ytd.at[owner_profile].add(jnp.where(active_property, recapture, 0.0))
        gain_profile = int(plan.tax_profile_capital_gain_index[owner_profile])
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
