"""JAX simulation engine: a single always-`lax.scan` device program.

The whole month loop compiles into one `jax.jit` program (`_program_impl`) whose carry is the
per-rollout `_ScanState`; one `lax.scan` over `jnp.arange(horizon)` runs every phase branch-free, and
`run_jax_scan(plan, buffers)` fills the (NumPy-allocated, zeroed) `buffers` from the stacked scan
outputs in one device→host transfer. The scan covers:
- scheduled / recurring transfers;
- property purchases (cash + mortgage origination);
- scheduled asset sales (FIFO lot matching + capital-gain classification + lot-disposition log);
- liquidity-policy sales;
- obligation accruals + settlement with failure tracking and `_zero_failed_state`, for every source
  kind (CONFIGURED_OBLIGATION, PROPERTY_TAX with the SALT/Schedule-E split, MORTGAGE_PAYMENT with the
  interest/principal split, and ESTIMATED_TAX / ESTIMATED_TAX_Q4 / TAX_TRUE_UP);
- TLH harvest (calibrated per-policy capital-loss booking);
- PE tenders (LNW-floor tender / public-market / forced-sale / forced-recovery sales + opportunity
  trace);
- property sale (§1250 recapture + §121 exclusion), §168 depreciation accrual, owner-occupied-month
  tracking, lifecycle events, and primary-residence assignment;
- the December year-end tax machinery: Schedule-E rental-interest/depreciation deductions, §1211/§1212
  capital-loss netting, the two-pass SALT walk over MID + LTCG brackets + the §1250 worksheet,
  tax-liability accrual, and the true-up settlement.

Caching is JAX-native. `_program_impl` is module-level and
`@partial(jax.jit, static_argnames=("p", "structure"))`: its compiled executable is keyed by JAX on
the structural `SlotPlan` `p` and the hashable `_Static` (folded-event tuples + scalars — both
natively hashable, no content hashing) plus the avals of the traced `_Operands` data pytree and the
seed/swept-config inputs. So an identical-structure plan — including sweeps over the traced numeric
config (`_TracedConfig`) or rollout seeds — reuses the compiled program; only a structural change
recompiles. An opt-in on-disk compilation cache (`AUGUR_JAX_COMPILATION_CACHE_DIR`) carries that reuse
across processes.

Integer accounting note: engine monetary state is migrating to int64 cents / explicit quantity
quanta. JAX x64 is required so those int64 arrays do not silently truncate to int32.

Double-entry note: every write to `cash` moves money between two rows of the same tensor, never
into or out of it. A counterparty the scenario does not model is not a hole — it is
`structure.external_cash_slot`, the `rest_of_world` row. So a phase that pays an unmodeled
contractor debits the owner and credits that row, and one that books sale proceeds credits the
owner and debits it. The sum over all cash rows is therefore invariant, which is what
`test_cash_conservation_e2e` asserts and the only thing that catches a one-sided write: a sale
that credits proceeds with no debit leaves net worth correct while it mints money.
"""

from __future__ import annotations

# JAX x64 must be enabled before importing jax.numpy, so this module intentionally
# configures JAX between imports.
# ruff: noqa: E402, I001

import os
from dataclasses import dataclass
from functools import partial
from typing import NamedTuple

import jax

jax.config.update("jax_enable_x64", True)

import jax.numpy as jnp
import numpy as np

from finance.augur.model.series import PrivateEquityRegimeCode
from finance.augur.product.metric_composition import BASE_METRIC_NAMES, compose_metric
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.sim.actor_view import ActorSlots, build_actor_view
from finance.augur.sim.buffers import SimulationBuffers
from finance.augur.sim.codec.plan import CompiledSimulation
from finance.augur.sim.compiler.helpers import AMOUNT_FIXED, NO_CODE
from finance.augur.sim.compiler.plan import SlotPlan, lot_order_for_pool
from finance.augur.sim.engine.jax_scatter import scatter_ys_to_buffers
from finance.augur.sim.engine.jax_types import (
    _CapitalGainTarget,
    _FoldedHarvest,
    _FoldedLifecycleEvent,
    _FoldedLiquidity,
    _FoldedSleeve,
    _FoldedTargetAllocation,
    _FoldedPE,
    _FoldedPurchase,
    _FoldedSale,
    _LinkTaxStatic,
    _LiquidityPool,
    _ScanMeta,
    _Static,
)
from finance.augur.sim.engine.jax_validation import validate_seed_dependent_inputs
from finance.augur.sim.enums import (
    CapitalGainClassification,
    LifecycleKind,
    ObligationSource,
    PrivateEquityDispositionKind,
    PrivateEquityOpportunityOutcome,
)
from finance.augur.sim.fixed_point import USD_CENTS
from finance.augur.sim.target_allocation import SleeveUniverse, decide

# Opt-in JAX persistent on-disk compilation cache: when the env var is set, compiled executables
# survive across processes so the ~6400-instruction scan program need not recompile each run. A no-op
# otherwise (the in-process native cache still reuses across `run_jax_scan` calls of one structure).
_JAX_CACHE_DIR = os.environ.get("AUGUR_JAX_COMPILATION_CACHE_DIR")
if _JAX_CACHE_DIR:
    jax.config.update("jax_compilation_cache_dir", _JAX_CACHE_DIR)
    jax.config.update("jax_persistent_cache_min_compile_time_secs", 0.0)
    jax.config.update("jax_persistent_cache_min_entry_size_bytes", -1)

if jnp.asarray(1, dtype=jnp.int64).dtype != jnp.dtype("int64"):
    raise RuntimeError("Augur JAX engine requires jax_enable_x64=True for int64 fixed-point accounting")

SECTION_121_LOOKBACK_MONTHS = 60
SECTION_121_MIN_QUALIFYING_MONTHS = 24


def _round_int64(value: jnp.ndarray) -> jnp.ndarray:
    value_f = value.astype(jnp.float64)
    rounded = jnp.sign(value_f) * jnp.floor(jnp.abs(value_f) + 0.5)
    return rounded.astype(jnp.int64)


def _zeros_i64(shape: tuple[int, ...]) -> jnp.ndarray:
    return jnp.zeros(shape, dtype=jnp.int64)


def _scale_money(amount_cents: jnp.ndarray, factor: jnp.ndarray | float) -> jnp.ndarray:
    return _round_int64(amount_cents.astype(jnp.float64) * factor)


def _price_usd_to_cents(price_usd: jnp.ndarray) -> jnp.ndarray:
    return _round_int64(price_usd.astype(jnp.float64) * float(USD_CENTS))


def _value_cents_from_quanta(
    quantity_quanta: jnp.ndarray, unit_price_cents: jnp.ndarray, quantity_scale: jnp.ndarray
) -> jnp.ndarray:
    return _round_int64(
        quantity_quanta.astype(jnp.float64) * unit_price_cents.astype(jnp.float64) / quantity_scale.astype(jnp.float64)
    )


def _ceil_quanta_for_value_cents(
    value_cents: jnp.ndarray, unit_price_cents: jnp.ndarray, quantity_scale: jnp.ndarray
) -> jnp.ndarray:
    raw = (
        value_cents.astype(jnp.float64)
        * quantity_scale.astype(jnp.float64)
        / jnp.where(unit_price_cents > 0, unit_price_cents, 1).astype(jnp.float64)
    )
    return jnp.ceil(raw).astype(jnp.int64)


class _ScanState(NamedTuple):
    """`run_jax_scan`'s carry pytree (NamedTuple → native JAX pytree). Grown field-by-field as the
    fold covers more phases; per-rollout state is `(entity, rollouts)` except the failure vectors."""

    cash: jnp.ndarray
    ordinary_ytd: jnp.ndarray
    property_tax_ytd: jnp.ndarray
    lot_remaining: jnp.ndarray
    # `(lot, R)`: per-rollout because a purchased lot's basis is the price its rollout paid.
    # Initial lots broadcast their configured basis and never change it.
    cost_basis_per_unit: jnp.ndarray
    capital_gain_active: jnp.ndarray
    capital_gain_ytd: jnp.ndarray
    tlh: jnp.ndarray
    property_active: jnp.ndarray
    property_basis: jnp.ndarray
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

    `rental_interest_ytd` is the rented-share slice of `interest_ytd` (Schedule E vs MID split).
    """

    active: jnp.ndarray
    principal: jnp.ndarray
    monthly_payment: jnp.ndarray
    interest_ytd: jnp.ndarray
    principal_ytd: jnp.ndarray
    rental_interest_ytd: jnp.ndarray


class _TracedConfig(NamedTuple):
    """JAX-native typed bundle of the swept numeric config the compiled program takes as TRACED inputs
    (a NamedTuple → native JAX pytree, so it passes through `jax.jit` typed). The cores read VALUES from
    here (`jax.Array`s) while reading structure / feature flags / counts / slot indices from the
    concrete `plan` — so nothing puns a traced array into the compiler's NumPy-typed plan fields. Each
    field is a swept numeric value (not baked structure), so sweeping it reuses the compiled program."""

    link_standard_deduction: jnp.ndarray
    link_income_mask: jnp.ndarray
    link_ordinary_upper: jnp.ndarray
    link_ordinary_rate: jnp.ndarray
    link_ltcg_upper: jnp.ndarray
    link_ltcg_rate: jnp.ndarray
    mid_principal_ratio: jnp.ndarray
    transfer_amount_fixed: jnp.ndarray
    transfer_amount_base: jnp.ndarray
    property_cashflow_amount_fixed: jnp.ndarray
    property_cashflow_amount_base: jnp.ndarray
    cost_basis_per_unit: jnp.ndarray
    cash_initial_balance: jnp.ndarray
    lot_initial_quantity: jnp.ndarray
    property_adjusted_basis: jnp.ndarray
    property_equity_ledger: jnp.ndarray
    liability_principal: jnp.ndarray
    liability_monthly_payment: jnp.ndarray


@dataclass(frozen=True)
class _ProductSummaryStatic:
    has_public_lots: bool
    has_pe_lots: bool
    has_properties: bool
    has_bonds: bool


class _ProductSummaryInputs(NamedTuple):
    cash_mask: jnp.ndarray
    public_lot_mask: jnp.ndarray
    pe_lot_mask: jnp.ndarray
    pe_lot_issuer: jnp.ndarray
    property_mask: jnp.ndarray
    property_purchase_month: jnp.ndarray
    property_purchase_price: jnp.ndarray
    property_home_value_series: jnp.ndarray
    liability_mask: jnp.ndarray
    primary_obligation_mask: jnp.ndarray
    # Face in cents, zeroed for bonds the primary agent does not hold, and the (H+1, bond)
    # on-books mask. Both compile-time constants — a par bond held to maturity has no
    # rollout-varying value.
    bond_face: jnp.ndarray
    bond_on_books: jnp.ndarray
    # Indexation inputs, so a TIPS is carried at its CPI-scaled principal rather than at par.
    # Valuing an indexed bond at face would understate it in exactly the inflationary
    # scenarios the ladder is held for.
    bond_indexed: jnp.ndarray
    bond_cpi_series: jnp.ndarray
    bond_index_base_month: jnp.ndarray


def _traced_config(plan: CompiledSimulation) -> _TracedConfig:
    """Build the traced-config bundle of swept numeric values from the (concrete) plan."""
    return _TracedConfig(
        link_standard_deduction=jnp.asarray(plan.tax.link_standard_deduction),
        link_income_mask=jnp.asarray(plan.tax.link_income_mask),
        link_ordinary_upper=jnp.asarray(plan.tax.link_ordinary_upper),
        link_ordinary_rate=jnp.asarray(plan.tax.link_ordinary_rate),
        link_ltcg_upper=jnp.asarray(plan.tax.link_ltcg_upper),
        link_ltcg_rate=jnp.asarray(plan.tax.link_ltcg_rate),
        mid_principal_ratio=jnp.asarray(plan.mid.principal_ratio),
        transfer_amount_fixed=jnp.asarray(plan.transfers.amount_fixed),
        transfer_amount_base=jnp.asarray(plan.transfers.amount_base),
        property_cashflow_amount_fixed=jnp.asarray(plan.property_cashflows.amount_fixed),
        property_cashflow_amount_base=jnp.asarray(plan.property_cashflows.amount_base),
        cost_basis_per_unit=jnp.asarray(plan.lot_cost_basis_per_unit),
        cash_initial_balance=jnp.asarray(plan.cash_initial_balance),
        lot_initial_quantity=jnp.asarray(plan.lot_initial_quantity),
        property_adjusted_basis=jnp.asarray(plan.properties.adjusted_basis),
        property_equity_ledger=jnp.asarray(plan.properties.equity_ledger),
        liability_principal=jnp.asarray(plan.liabilities.principal),
        liability_monthly_payment=jnp.asarray(plan.liabilities.monthly_payment),
    )


class _Operands(NamedTuple):
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
    pc: dict[str, jnp.ndarray]
    bond: dict[str, jnp.ndarray]
    og: dict[str, jnp.ndarray]
    acc: dict[str, jnp.ndarray]
    # Scheduled-sale stacked static data.
    sale_months_t: jnp.ndarray
    sale_qty_t: jnp.ndarray
    sale_prior_t: jnp.ndarray
    sale_cg_map_t: jnp.ndarray
    sale_policy_mask_t: jnp.ndarray
    sale_price_fixed_t: jnp.ndarray
    sale_price_series: jnp.ndarray  # scheduled-sale price-series row indices (traced, dynamic gather)
    # Scheduled asset-purchase stacked static data (all `(n_buys,)`).
    buy_month_t: jnp.ndarray
    buy_amount_t: jnp.ndarray
    buy_price_fixed_t: jnp.ndarray
    buy_price_series: jnp.ndarray
    buy_scale_t: jnp.ndarray
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
    lot_quantity_scale: jnp.ndarray
    pe_owner_cash_mask: jnp.ndarray  # (pe_policy, cash)
    # Series-axis row indices, traced (dynamic gather), in phase-loop order. See `_build_program`.
    pe_floor_series: jnp.ndarray  # (n_folded_pe,)
    harvest_series: jnp.ndarray  # (n_folded_harvest,)
    lifecycle_sale_series: jnp.ndarray  # (n_folded_lifecycle,)
    liq_trigger_series: jnp.ndarray  # (n_folded_liquidity,)
    liq_sale_series: jnp.ndarray  # (n_folded_liquidity,)
    liq_pool_series: list[jnp.ndarray]  # per-policy (n_pools_i,) arrays (ragged)
    ta_floor_series: jnp.ndarray  # (n_folded_target_allocation,)
    ta_ceiling_series: jnp.ndarray  # (n_folded_target_allocation,)
    # Per-policy, per-sleeve, per-pool price rows. Ragged twice over, so a list of lists.
    ta_pool_series: list[list[jnp.ndarray]]


def run_jax_scan(plan: CompiledSimulation, buffers: SimulationBuffers) -> None:
    """Single-program `lax.scan` engine: the whole month loop compiles into one XLA program (one
    dispatch for all months) whose only traced inputs are the seed-varying series and swept numeric
    config. `_build_program` builds + `jax.jit`-wraps the device program for this plan structure; JAX
    compiles it on first invocation."""
    validate_seed_dependent_inputs(plan)

    baked, structure, p, meta = _build_program(plan)
    external, pe, cfg = _program_inputs(plan)
    ys, final_state = _program_impl(external, pe, cfg, baked, p, structure)
    scatter_ys_to_buffers(plan, buffers, meta, ys, final_state)


@dataclass(frozen=True)
class ProductSummary:
    """Reduced product projection for the percentile (fan / terminal) endpoints.

    Carries only what those endpoints consume — the requested metric's monthly percentile bands and
    its per-rollout terminal samples — so the full (H+1, R) per-metric history is reduced on-device
    and never copied to the host or re-stored per rollout.
    """

    month_index: np.ndarray  # (H+1,)
    failed_month: np.ndarray  # (R,) int64; -1 = never failed
    terminal_samples: np.ndarray  # (R,) requested metric's terminal value per rollout
    monthly_bands: np.ndarray | None  # (n_percentiles, H+1) for the requested metric, or None


# The base metrics the scan emits per month, in the order `product_metrics` returns them.
# The wire's derived metrics (home_equity, liquid_net_worth, net_worth) are composed from
# these by `product.metric_composition`, which is also what the decode path uses — the sums
# are defined once so a new asset class cannot reach one and miss the other.
_PRODUCT_BASE_METRICS = BASE_METRIC_NAMES
_PRODUCT_BASE_INDEX = {name: index for index, name in enumerate(_PRODUCT_BASE_METRICS)}


def _product_metric_series(
    metric: str, initial_ys: tuple[jnp.ndarray, ...], monthly_ys: tuple[jnp.ndarray, ...]
) -> jnp.ndarray:
    """Full (H+1, R) device series for one product metric.

    `base` is passed as a callable so only the series the requested metric needs are
    assembled: a single-metric fan never materializes all of them.
    """

    def base(name: str) -> jnp.ndarray:
        index = _PRODUCT_BASE_INDEX[name]
        return jnp.concatenate([jnp.asarray(initial_ys[index])[None, :], jnp.asarray(monthly_ys[index])], axis=0)

    return compose_metric(metric, base)


def run_jax_product_summary(
    plan: CompiledSimulation, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...] | None
) -> ProductSummary:
    """Run the JAX month loop and reduce, on-device, to the requested metric's monthly percentile
    bands (when `percentiles` is given) and its per-rollout terminal samples.

    Shares the exact accounting scan with `run_jax_scan`; only the emitted summary differs. Avoiding
    the full dense `SimulationRun` history/event slabs — and reducing each metric to percentiles
    before the device→host copy — means neither the host nor the response ever holds per-rollout
    monthly state. The full dense trace is reserved for the selected-rollout detail endpoint.
    """
    validate_seed_dependent_inputs(plan)

    baked, structure, p, _meta = _build_program(plan)
    product_static, product_inputs = _product_summary_inputs(plan, primary_agent_id=primary_agent_id)
    external, pe, cfg = _program_inputs(plan)
    product_ys, product_tail = _program_impl(
        external, pe, cfg, baked, p, structure, product_summary=product_static, product_inputs=product_inputs
    )
    oversell, final_failed_month = product_tail
    if bool(np.asarray(jax.device_get(oversell))):
        raise ValueError("scheduled asset sale exceeds available lots")

    initial_ys, monthly_ys = product_ys
    series = _product_metric_series(metric, initial_ys, monthly_ys)  # (H+1, R), on device
    # Terminal sample: cumulative over the horizon for shortfall, end-of-horizon snapshot otherwise.
    terminal = series.sum(axis=0) if metric == "shortfall_usd" else series[-1]
    bands = (
        jnp.quantile(series, jnp.asarray(percentiles, dtype=jnp.float64) / 100.0, axis=1, method="linear")
        if percentiles is not None
        else None
    )

    return ProductSummary(
        month_index=np.arange(plan.horizon_months + 1, dtype=np.int64),
        failed_month=np.asarray(jax.device_get(final_failed_month), dtype=np.int64),
        terminal_samples=np.asarray(jax.device_get(terminal), dtype=np.float64),
        monthly_bands=None if bands is None else np.asarray(jax.device_get(bands), dtype=np.float64),
    )


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
        "forced_recovery": jnp.asarray(pe_channels.forced_recovery_cashout_cents),
    }
    return jnp.asarray(plan.external_values), pe_ch_dyn, _traced_config(plan)


def _product_summary_inputs(
    plan: CompiledSimulation, *, primary_agent_id: str
) -> tuple[_ProductSummaryStatic, _ProductSummaryInputs]:
    try:
        primary_agent_code = plan.strings.index(primary_agent_id)
    except ValueError as exc:
        raise ValueError(f"compiled simulation string table does not contain {primary_agent_id!r}") from exc

    cash_mask = plan.cash_agent_codes == primary_agent_code
    pe_issuer_index = {str(issuer_id): idx for idx, issuer_id in enumerate(plan.pe_issuers.issuer_ids)}
    public_lot_mask = np.zeros(plan.lot_id_codes.shape, dtype=bool)
    pe_lot_mask = np.zeros(plan.lot_id_codes.shape, dtype=bool)
    pe_lot_issuer = np.full(plan.lot_id_codes.shape, NO_CODE, dtype=np.int64)
    for lot, asset_code in enumerate(plan.lot_asset_codes):
        if int(plan.lot_agent_codes[lot]) != primary_agent_code:
            continue
        asset = plan.assets[int(asset_code)]
        if isinstance(asset, PrivateEquityAssetKey):
            pe_lot_mask[lot] = True
            pe_lot_issuer[lot] = pe_issuer_index[str(asset.issuer_id)]
        else:
            public_lot_mask[lot] = True
            if int(plan.lot_asset_series_index[lot]) == NO_CODE:
                raise ValueError(
                    f"holding asset {asset.wire_id!r} has no modeled price series in the compiled simulation"
                )

    property_mask = plan.properties.buyer_agent == primary_agent_code
    bond_mask = plan.bonds.agent == primary_agent_code
    inputs = _ProductSummaryInputs(
        cash_mask=jnp.asarray(cash_mask),
        public_lot_mask=jnp.asarray(public_lot_mask),
        pe_lot_mask=jnp.asarray(pe_lot_mask),
        pe_lot_issuer=jnp.asarray(pe_lot_issuer),
        property_mask=jnp.asarray(property_mask),
        property_purchase_month=jnp.asarray(plan.properties.month.astype(np.int32)),
        property_purchase_price=jnp.asarray(plan.properties.purchase_price),
        property_home_value_series=jnp.asarray(plan.property_home_value_series_index.astype(np.int32)),
        liability_mask=jnp.asarray(plan.liabilities.agent == primary_agent_code),
        primary_obligation_mask=jnp.asarray(plan.obligations.agent == primary_agent_code),
        bond_face=jnp.asarray(np.where(bond_mask, plan.bonds.face, 0)),
        bond_on_books=jnp.asarray(plan.bonds.on_books),
        bond_indexed=jnp.asarray(plan.bonds.indexed),
        bond_cpi_series=jnp.asarray(plan.bonds.cpi_series),
        bond_index_base_month=jnp.asarray(plan.bonds.index_base_month),
    )
    return (
        _ProductSummaryStatic(
            has_public_lots=bool(public_lot_mask.any()),
            has_pe_lots=bool(pe_lot_mask.any()),
            has_properties=bool(property_mask.any()),
            has_bonds=bool(bond_mask.any()),
        ),
        inputs,
    )


def compiled_hlo_text(plan: CompiledSimulation) -> str:
    """Optimized-HLO text of the compiled program for `plan` (introspection / op-count profiling)."""
    baked, structure, p, _ = _build_program(plan)
    external, pe, cfg = _program_inputs(plan)
    text = _program_impl.lower(external, pe, cfg, baked, p, structure).compile().as_text()
    if text is None:
        raise RuntimeError("compiled program exposes no HLO text")
    return text


def _build_program(plan: CompiledSimulation) -> tuple[_Operands, _Static, SlotPlan, _ScanMeta]:
    """Host-only build of the device program inputs for one plan *structure*. Does ALL numpy/Python
    precompute and packs the results into a `_Operands` pytree (every device array the scan closes over,
    a TRACED arg) and a `_Static` frozen dataclass (every natively-hashable Python value the bodies
    read at trace time, a STATIC arg) — plus the plan's `SlotPlan` (`p`, already natively hashable) and
    the host-side `_ScanMeta` for the post-scan scatter. The compiled program is `_program_impl`, whose
    native JAX cache reuses the executable across calls of the same structure (and across traced
    value/seed sweeps) — no hand-rolled hashing."""
    p = plan.slot_plan
    r = p.rollout_count
    horizon = plan.horizon_months
    cash0 = jnp.asarray(np.broadcast_to(plan.cash_initial_balance[:, None], (p.cash_count, r)))
    # One row per (profile, income source) — see `TaxCompileOutput.income_bucket`.
    ordinary0 = _zeros_i64((p.income_bucket_count, r))
    property_tax_ytd0 = _zeros_i64((p.tax_profile_count, r))
    lot0 = jnp.asarray(np.broadcast_to(plan.lot_initial_quantity[:, None], (p.lot_count, r)))
    cg_active0 = jnp.zeros((p.capital_gain_agent_count, 2, r), dtype=bool)
    cg_ytd0 = _zeros_i64((p.capital_gain_agent_count, 2, r))
    # TLH give-back ledger starts at zero (the harvest phase populates it during the scan); the
    # capital-gains core threads it, so carry a zeroed copy.
    tlh0 = _zeros_i64((p.harvest_policy_count, r))
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
        exclusion_cap = int(plan.tax.profile_section_121_exclusion[owner_profile]) if owner_profile >= 0 else 0
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
            amount_cents=int(le_all.amount_cents[i]),
            owner_cash_slot=int(props.buyer_slot[prop]) if prop >= 0 else NO_CODE,
            purchase_price=int(plan.properties.purchase_price[prop]) if prop >= 0 else 0,
            building_basis_initial=int(plan.property_building_basis[prop]) if prop >= 0 else 0,
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
            quantity=int(sales.quantity[s]),
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
    sale_qty_t = jnp.asarray([fs.quantity for fs in folded_sales], dtype=jnp.int64)
    sale_pslot = np.array([fs.proceeds_slot for fs in folded_sales], dtype=np.int64).reshape(n_sales)
    sale_bufidx = np.array([fs.buffer_index for fs in folded_sales], dtype=np.int64).reshape(n_sales)
    sale_olots = np.full((n_sales, sale_max_pool), p.lot_count, dtype=np.int64)  # pad with the dummy lot
    for _i, _fs in enumerate(folded_sales):
        sale_olots[_i, : len(_fs.ordered_lots)] = np.asarray(_fs.ordered_lots, dtype=np.int64)
    sale_price_fixed_t = jnp.asarray([int(sales.price_fixed[fs.buffer_index]) for fs in folded_sales], dtype=jnp.int64)
    sale_price_series = np.array(
        [int(sales.price_series[fs.buffer_index]) for fs in folded_sales], dtype=np.int64
    ).reshape(n_sales)
    # Scheduled asset purchases. Nothing to fold: each fills its own dedicated lot slot, so there
    # is no shared pool to sequence and the compiled rows are already the loop-free form. Real rows
    # only — the compile output pads to one slot so the arrays are never zero-length, and a padded
    # row is NO_CODE-monthed.
    buys = plan.purchases
    n_buys = int((buys.month >= 0).sum())
    # Same-pool ((agent, account, asset)) earlier-sale mask -> cumulative prior demand on each pool.
    _pool_key = [
        (
            int(sales.agent[fs.buffer_index]),
            int(sales.source_account[fs.buffer_index]),
            int(sales.asset[fs.buffer_index]),
        )
        for fs in folded_sales
    ]
    _prior = np.zeros((n_sales, n_sales), dtype=np.int64)
    for _j in range(n_sales):
        for _k in range(_j):
            if _pool_key[_k] == _pool_key[_j]:
                _prior[_j, _k] = 1
    sale_prior_t = jnp.asarray(_prior)
    # Per-sale -> capital-gain-agent accrual map (the sale's agent's cg buckets).
    sale_cg_map_t = jnp.asarray(
        np.array([(plan.capital_gain_agent_codes == fs.agent_code) for fs in folded_sales], dtype=np.int64).reshape(
            n_sales, p.capital_gain_agent_count
        )
    )
    # TLH give-back: active harvest policies' lot masks (others zeroed). Used to drain each policy's
    # cumulative harvested loss proportionally to units sold (the per-sale telescoping reduces to a
    # per-policy rate `tlh0 / pre_sale_units`).
    _hp = plan.harvest_policies
    _hp_active = (_hp.gain_profile_index >= 0)[:, None]
    sale_policy_mask_t = jnp.asarray((_hp.lot_mask & _hp_active).astype(np.int64))  # (policy, L)
    folded_purchases = [
        _FoldedPurchase(
            buffer_index=prop,
            month=int(props.month[prop]),
            stake_contribution=int(props.stake_contribution[prop]),
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
    pur_stake = np.array([fp.stake_contribution for fp in folded_purchases], dtype=np.int64)
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
    pcf = plan.property_cashflows
    pc = {
        "cause": jnp.asarray(pcf.cause),
        "kind": jnp.asarray(pcf.amount_kind),
        "fixed": jnp.asarray(pcf.amount_fixed),
        "base": jnp.asarray(pcf.amount_base),
        "series": jnp.asarray(pcf.amount_series),
        "base_month": jnp.asarray(pcf.amount_base_month),
        "period": jnp.asarray(pcf.amount_period),
        "from_slot": jnp.asarray(pcf.from_slot),
        "to_slot": jnp.asarray(pcf.to_slot),
        "property_slot": jnp.asarray(np.where(pcf.property_slot >= 0, pcf.property_slot, 0)),
        "income_profile": jnp.asarray(pcf.income_profile),
        "deduction_profile": jnp.asarray(pcf.deduction_profile),
    }
    bd = plan.bonds
    bond = {
        "coupon": jnp.asarray(bd.coupon),
        "redemption": jnp.asarray(bd.redemption),
        "to_slot": jnp.asarray(bd.to_slot),
        "income_row": jnp.asarray(bd.income_row),
        "indexed": jnp.asarray(bd.indexed),
        "cpi_series": jnp.asarray(bd.cpi_series),
        "index_base_month": jnp.asarray(bd.index_base_month),
        "period_rate": jnp.asarray(bd.period_rate),
        "face": jnp.asarray(bd.face),
        "pays": jnp.asarray(bd.pays),
        "matures": jnp.asarray(bd.matures),
        "on_books": jnp.asarray(bd.on_books),
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
        "configured_prop_idx": jnp.asarray(np.where(ob.property_slot >= 0, ob.property_slot, 0)),
        "configured_has_prop": jnp.asarray(ob.property_slot >= 0),
        "prop_idx": jnp.asarray(np.where(acc_kind == ObligationSource.PROPERTY_TAX, ob.source_index, 0)),
        "liab_idx": jnp.asarray(np.where(acc_kind == ObligationSource.MORTGAGE_PAYMENT, ob.source_index, 0)),
    }
    acc_prop_idx_np = np.where(acc_kind == ObligationSource.PROPERTY_TAX, ob.source_index, 0)
    acc_pt_rate = np.where(
        np.isnan(ob.property_tax_annual_rate),
        _np_gather(props.location_tax_rate, acc_prop_idx_np, 0.0),
        ob.property_tax_annual_rate,
    )
    raw_pt_amount = (
        _np_gather(props.initial_assessed_value, acc_prop_idx_np, 0) * acc_pt_rate / 12.0
        + _np_gather(props.special_assessment_annual_usd, acc_prop_idx_np, 0) / 12.0
    )
    acc["pt_amount"] = jnp.asarray(np.sign(raw_pt_amount) * np.floor(np.abs(raw_pt_amount) + 0.5), dtype=jnp.int64)
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
    acc_est_prior = _np_gather(plan.tax.profile_prior_year_tax, acc_prof_idx, 0)
    acc["est_prior"] = jnp.asarray(acc_est_prior)
    acc["est_quarterly"] = jnp.asarray(
        np.sign(acc_est_prior / 4.0) * np.floor(np.abs(acc_est_prior / 4.0) + 0.5), dtype=jnp.int64
    )
    tax_year_end = (np.arange(horizon) // 12 - 1) * 12 + 11
    acc["trueup_sel"] = jnp.asarray(
        (
            (plan.tax_liabilities.profile_index[None, None, :] == acc_prof_idx[:, :, None])
            & (plan.tax_liabilities.year_end_month[None, None, :] == tax_year_end[:, None, None])
        ).astype(np.int64)
    )
    # Tax-profile index per estimated/true-up obligation slot (for settlement scatter to profile rows);
    # and the (static, per-month) prior year-end being settled.
    acc["prof_idx"] = jnp.asarray(np.where(acc_kind >= ObligationSource.ESTIMATED_TAX, ob.source_index, -1))
    acc["tax_year_end"] = jnp.asarray(tax_year_end)

    # Liquidity policies: resolve each policy's (asset, source-account) FIFO pools host-side. Sells
    # raise cash to cover the month's obligation demand (+ an optional buffer) before the funding check.
    liq_policies = plan.liquidity_policies
    liq_policy_count = int(liq_policies.cash_slot.shape[0])
    ta_policies = plan.target_allocation_policies
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
                    pools.append(_LiquidityPool(asset_idx=asset_idx, ordered_lots=tuple(int(lot) for lot in ordered)))
        folded_liquidity.append(
            _FoldedLiquidity(
                policy_index=policy,
                agent=agent_code,
                cash_slot=int(liq_policies.cash_slot[policy]),
                # amount-spec tuples drop the series row index (it's a traced operand,
                # `_Operands.liq_{trigger,sale}_series`): (kind, fixed, base, base_month, period).
                trigger=(
                    int(liq_policies.trigger_kind[policy]),
                    int(liq_policies.trigger_fixed[policy]),
                    int(liq_policies.trigger_base[policy]),
                    int(liq_policies.trigger_base_month[policy]),
                    int(liq_policies.trigger_period[policy]),
                ),
                sale=(
                    int(liq_policies.sale_kind[policy]),
                    int(liq_policies.sale_fixed[policy]),
                    int(liq_policies.sale_base[policy]),
                    int(liq_policies.sale_base_month[policy]),
                    int(liq_policies.sale_period[policy]),
                ),
                pools=tuple(pools),
            )
        )
    lot_axis = max(1, p.lot_count)

    folded_target_allocation: list[_FoldedTargetAllocation] = []
    for policy in range(int(ta_policies.cash_slot.shape[0])):
        if int(ta_policies.cash_slot[policy]) < 0:
            continue  # padded sentinel policy
        agent_code = int(ta_policies.agent[policy])
        sleeves: list[_FoldedSleeve] = []
        lot_slots: list[int] = []
        for sleeve_idx in range(int(ta_policies.sleeve_assets.shape[1])):
            asset_code = int(ta_policies.sleeve_assets[policy, sleeve_idx])
            series_index = int(ta_policies.sleeve_series[policy, sleeve_idx])
            if asset_code < 0:
                continue  # padded sleeve column
            sleeve_pools: list[_LiquidityPool] = []
            view_rows: list[int] = []
            # A sleeve with no price series stays in the universe with no pools: it holds value
            # the policy cannot mark, so it must not be sold — and it must still occupy its
            # weight, or the sellable sleeves would inherit its share of the target.
            if series_index >= 0:
                for account in ta_policies.source_accounts[policy]:
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
                    if not ordered.size:
                        continue
                    sleeve_pools.append(
                        _LiquidityPool(asset_idx=sleeve_idx, ordered_lots=tuple(int(x) for x in ordered))
                    )
                    # Pools are disjoint by construction — a sleeve is one asset and its pools are
                    # distinct accounts — so appending never repeats a plan lot on the view axis.
                    view_rows.extend(range(len(lot_slots), len(lot_slots) + int(ordered.size)))
                    lot_slots.extend(int(x) for x in ordered)
            sleeves.append(
                _FoldedSleeve(
                    weight=int(ta_policies.weights[policy, sleeve_idx]),
                    sleeve_idx=sleeve_idx,
                    view_lot_rows=tuple(view_rows),
                    pools=tuple(sleeve_pools),
                )
            )
        folded_target_allocation.append(
            _FoldedTargetAllocation(
                policy_index=policy,
                agent=agent_code,
                cash_slot=int(ta_policies.cash_slot[policy]),
                floor=(
                    int(ta_policies.floor_kind[policy]),
                    int(ta_policies.floor_fixed[policy]),
                    int(ta_policies.floor_base[policy]),
                    int(ta_policies.floor_base_month[policy]),
                    int(ta_policies.floor_period[policy]),
                ),
                ceiling=(
                    int(ta_policies.ceiling_kind[policy]),
                    int(ta_policies.ceiling_fixed[policy]),
                    int(ta_policies.ceiling_base[policy]),
                    int(ta_policies.ceiling_base_month[policy]),
                    int(ta_policies.ceiling_period[policy]),
                ),
                lot_slots=tuple(lot_slots),
                sleeves=tuple(sleeves),
            )
        )

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
                    floor_fixed=int(pe_policies.floor_fixed[policy_idx]),
                    floor_base=int(pe_policies.floor_base[policy_idx]),
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
                    floor_fixed=0,
                    floor_base=0,
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
    # Ordinary-bucket ROWS, not profile indices: the §1211 ordinary offset scatters into the
    # YTD income tensor, whose rows are (profile, source) pairs.
    cg_rep_profile = np.full(max(1, p.capital_gain_agent_count), NO_CODE, dtype=np.int64)
    for profile in range(profile_count):
        gp = int(plan.tax_profile_capital_gain_index[profile])
        if gp >= 0 and cg_rep_profile[gp] < 0:
            cg_rep_profile[gp] = plan.tax.buckets.ordinary_bucket(profile)
    salt_link_active = plan.salt.link_active  # bool array per link
    cap_year_index_by_month = np.minimum(np.arange(horizon) // 12, plan.salt.cap_by_year.shape[1] - 1)
    # Per-(link, month) SALT cap (cap_by_year indexed by the month's tax year), so the traced month
    # can index it directly inside the pass.
    salt_cap_table = (
        jnp.asarray(plan.salt.cap_by_year[:, cap_year_index_by_month])
        if link_count
        else jnp.zeros((0, horizon), dtype=jnp.int64)
    )

    salt_contributing_mask = (
        jnp.asarray(plan.salt.contributing_mask.astype(np.int64))
        if link_count
        else jnp.zeros((0, max(1, link_count)), dtype=jnp.int64)
    )
    pe_owner_cash_mask = (
        jnp.asarray(pe_policies.owner_cash_mask)
        if pe_policies.owner_cash_mask.size
        else jnp.zeros((max(1, pe_issuer_count), p.cash_count), dtype=bool)
    )

    # Series-axis row indices as TRACED operands, NOT baked into the static `_Static` structure. A
    # series index is just a row into `external_values`; threading it as a device scalar (gathered
    # dynamically at the use site) keeps the compiled program independent of WHICH row, so a
    # non-deterministic series order can't trigger a recompile (see the determinism note in
    # `collect_level_series_keys`). Each array is in the SAME order its phase loop iterates the
    # matching folded tuple; `liq_pool_series` is a per-policy list (ragged pools).
    def _series_ops(values: list[int]) -> jnp.ndarray:
        return jnp.asarray(np.asarray(values, dtype=np.int64))

    pe_floor_series = _series_ops(
        [int(pe_policies.floor_series[fpe.policy_idx]) if fpe.policy_idx >= 0 else NO_CODE for fpe in folded_pe]
    )
    harvest_series = _series_ops([int(harvest.series_index[fh.policy_idx]) for fh in folded_harvest])
    lifecycle_sale_series = _series_ops(
        [
            int(plan.property_home_value_series_index[ev.property_slot]) if ev.property_slot >= 0 else 0
            for ev in folded_lifecycle
        ]
    )
    liq_trigger_series = _series_ops([int(liq_policies.trigger_series[lp.policy_index]) for lp in folded_liquidity])
    liq_sale_series = _series_ops([int(liq_policies.sale_series[lp.policy_index]) for lp in folded_liquidity])
    liq_pool_series = [
        _series_ops([int(liq_policies.asset_series[lp.policy_index, pool.asset_idx]) for pool in lp.pools])
        for lp in folded_liquidity
    ]
    ta_floor_series = _series_ops([int(ta_policies.floor_series[tp.policy_index]) for tp in folded_target_allocation])
    ta_ceiling_series = _series_ops(
        [int(ta_policies.ceiling_series[tp.policy_index]) for tp in folded_target_allocation]
    )
    ta_pool_series = [
        [
            _series_ops([int(ta_policies.sleeve_series[tp.policy_index, sleeve.sleeve_idx]) for _ in sleeve.pools])
            for sleeve in tp.sleeves
        ]
        for tp in folded_target_allocation
    ]

    baked = _Operands(
        cash0=cash0,
        ordinary0=ordinary0,
        property_tax_ytd0=property_tax_ytd0,
        lot0=lot0,
        cg_active0=cg_active0,
        cg_ytd0=cg_ytd0,
        tlh0=tlh0,
        property_rented_fraction_0=property_rented_fraction_0,
        property_building_basis_0=property_building_basis_0,
        prop0=_zeros_i64((p.property_count, r)),
        liab0=_zeros_i64((p.liability_count, r)),
        tr=tr,
        pc=pc,
        bond=bond,
        og=og,
        acc=acc,
        sale_months_t=sale_months_t,
        sale_qty_t=sale_qty_t,
        sale_prior_t=sale_prior_t,
        sale_cg_map_t=sale_cg_map_t,
        sale_policy_mask_t=sale_policy_mask_t,
        sale_price_fixed_t=sale_price_fixed_t,
        sale_price_series=jnp.asarray(sale_price_series),
        buy_month_t=jnp.asarray(buys.month[:n_buys], dtype=jnp.int32),
        buy_amount_t=jnp.asarray(buys.amount_cents[:n_buys], dtype=jnp.int64),
        buy_price_fixed_t=jnp.asarray(buys.price_fixed[:n_buys], dtype=jnp.int64),
        buy_price_series=jnp.asarray(buys.price_series[:n_buys], dtype=jnp.int64),
        buy_scale_t=jnp.asarray(buys.quantity_scale[:n_buys], dtype=jnp.int64),
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
        lot_quantity_scale=jnp.asarray(plan.lot_quantity_scale),
        pe_owner_cash_mask=pe_owner_cash_mask,
        pe_floor_series=pe_floor_series,
        harvest_series=harvest_series,
        lifecycle_sale_series=lifecycle_sale_series,
        liq_trigger_series=liq_trigger_series,
        liq_sale_series=liq_sale_series,
        liq_pool_series=liq_pool_series,
        ta_floor_series=ta_floor_series,
        ta_ceiling_series=ta_ceiling_series,
        ta_pool_series=ta_pool_series,
    )

    # Capital-gain accrual targets: each agent code that sells (liquidity / PE owners) maps to the
    # capital-gain profile rows whose agent code matches (the de-`plan`-ed `_record_capital_gains`).
    # Every agent that can DISPOSE of a lot needs a capital-gain target row, or the phase
    # that sells for it has no bucket to book the gain into. Target-allocation policies sell
    # exactly like liquidity policies do, so they belong in the same set.
    cg_agent_codes = (
        {fl.agent for fl in folded_liquidity}
        | {tp.agent for tp in folded_target_allocation}
        | {fpe.owner_agent for fpe in folded_pe if fpe.owner_agent >= 0}
    )
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
    structure = _Static(
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
        ta_policy_count=int(ta_policies.sleeve_assets.shape[0]),
        ta_max_sleeves=int(ta_policies.sleeve_assets.shape[1]),
        pe_issuer_count=pe_issuer_count,
        n_pe_kinds=n_pe_kinds,
        folded_lifecycle=tuple(folded_lifecycle),
        folded_pr=tuple(folded_pr),
        folded_liquidity=tuple(folded_liquidity),
        folded_target_allocation=tuple(folded_target_allocation),
        folded_pe=tuple(folded_pe),
        folded_harvest=tuple(folded_harvest),
        salt_link_active=tuple(bool(salt_link_active[link]) for link in range(link_count)),
        sale_pslot=tuple(int(x) for x in sale_pslot),
        sale_bufidx=tuple(int(x) for x in sale_bufidx),
        sale_olots=tuple(tuple(int(x) for x in row) for row in sale_olots),
        pur_buf=tuple(int(x) for x in pur_buf),
        pur_month=tuple(int(x) for x in pur_month),
        pur_stake=tuple(int(x) for x in pur_stake),
        pur_buyer=tuple(int(x) for x in pur_buyer),
        pur_seller=tuple(int(x) for x in pur_seller),
        pur_mort_rows=tuple(int(x) for x in pur_mort_rows),
        pur_mort_idx=tuple(int(x) for x in pur_mort_idx),
        folded_purchases_present=bool(folded_purchases),
        folded_sales_present=bool(folded_sales),
        buy_lot_slot=tuple(int(x) for x in buys.lot_slot[:n_buys]),
        buy_cash_slot=tuple(int(x) for x in buys.from_slot[:n_buys]),
        asset_buys_present=bool(n_buys),
        external_cash_slot=int(plan.external_cash_slot),
        cg_targets=cg_targets,
        link_tax_static=link_tax_static,
        link_profile=tuple(int(taxc.link_profile[link]) for link in range(link_count)),
        profile_gain_index=tuple(int(x) for x in plan.tax_profile_capital_gain_index),
        has_indexed_bonds=bool(plan.bonds.indexed.any()),
        profile_ordinary_bucket=tuple(
            plan.tax.buckets.ordinary_bucket(profile) for profile in range(p.tax_profile_count)
        ),
    )

    meta = _ScanMeta(
        folded_sales=folded_sales,
        folded_purchases=folded_purchases,
        folded_lifecycle=folded_lifecycle,
        folded_pr=folded_pr,
        folded_sale_events=folded_sale_events,
        folded_liquidity=folded_liquidity,
        folded_target_allocation=folded_target_allocation,
        folded_pe=folded_pe,
        link_count=link_count,
        liability_count=p.liability_count,
        horizon=horizon,
    )
    return baked, structure, p, meta


@partial(jax.jit, static_argnames=("p", "structure", "product_summary"))
def _program_impl(
    external_values: jnp.ndarray,
    pe_ch: dict[str, jnp.ndarray],
    cfg: _TracedConfig,
    baked: _Operands,
    p: SlotPlan,
    structure: _Static,
    product_summary: _ProductSummaryStatic | None = None,
    product_inputs: _ProductSummaryInputs | None = None,
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
    ta_policy_count = structure.ta_policy_count
    ta_max_sleeves = structure.ta_max_sleeves
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
    profile_ordinary_bucket = structure.profile_ordinary_bucket
    cg_profiles_by_agent = {ct.agent_code: ct.profiles for ct in structure.cg_targets}
    # Static index/selection arrays (rebuilt from the hashable tuples carried in `structure`).
    asset_buys = structure.asset_buys_present
    n_buys = len(structure.buy_lot_slot)
    buy_lot_slot = np.asarray(structure.buy_lot_slot, dtype=np.int64).reshape(n_buys)
    buy_cash_slot = np.asarray(structure.buy_cash_slot, dtype=np.int64).reshape(n_buys)
    buy_month_t = baked.buy_month_t
    buy_amount_t = baked.buy_amount_t
    buy_price_fixed_t = baked.buy_price_fixed_t
    buy_price_series = baked.buy_price_series
    buy_scale_t = baked.buy_scale_t
    sale_pslot = np.asarray(structure.sale_pslot, dtype=np.int64).reshape(n_sales)
    sale_bufidx = np.asarray(structure.sale_bufidx, dtype=np.int64).reshape(n_sales)
    sale_olots = np.asarray(structure.sale_olots, dtype=np.int64).reshape(n_sales, sale_max_pool)
    sale_price_series = baked.sale_price_series  # traced (n_sales,) row indices — dynamic gather
    pur_buf = np.asarray(structure.pur_buf, dtype=np.int64)
    pur_month = np.asarray(structure.pur_month, dtype=np.int64)
    pur_stake = np.asarray(structure.pur_stake, dtype=np.int64)
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
    pc = dict(baked.pc)
    bond = baked.bond
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
    lot_quantity_scale = baked.lot_quantity_scale
    pe_owner_cash_mask = baked.pe_owner_cash_mask
    pe_floor_series = baked.pe_floor_series
    harvest_series = baked.harvest_series
    lifecycle_sale_series = baked.lifecycle_sale_series
    liq_trigger_series = baked.liq_trigger_series
    liq_sale_series = baked.liq_sale_series
    liq_pool_series = baked.liq_pool_series
    ta_floor_series = baked.ta_floor_series
    ta_ceiling_series = baked.ta_ceiling_series
    ta_pool_series = baked.ta_pool_series
    folded_target_allocation = structure.folded_target_allocation
    # Swept numeric config (traced): cost basis + amount entries of the transfer/cashflow tables.
    tcfg = cfg
    tr["fixed"] = cfg.transfer_amount_fixed
    tr["base"] = cfg.transfer_amount_base
    pc["fixed"] = cfg.property_cashflow_amount_fixed
    pc["base"] = cfg.property_cashflow_amount_base

    def product_metrics(
        s: _ScanState, *, snapshot_month: jnp.ndarray, obligation_shortfall: jnp.ndarray, obligation_mask: jnp.ndarray
    ) -> tuple[jnp.ndarray, ...]:
        assert product_summary is not None
        assert product_inputs is not None
        cash_usd = jnp.where(product_inputs.cash_mask[:, None], s.cash, 0).sum(axis=0).astype(jnp.float64) / float(
            USD_CENTS
        )
        holding_usd = jnp.zeros((r,), dtype=jnp.float64)
        if product_summary.has_public_lots:
            safe_series = jnp.maximum(lot_asset_series_index, 0)
            public_price = external_values[safe_series, :, snapshot_month]
            public_quantity = s.lot_remaining.astype(jnp.float64) / lot_quantity_scale[:, None].astype(jnp.float64)
            public_value = public_quantity * public_price
            holding_usd = jnp.where(product_inputs.public_lot_mask[:, None], public_value, 0.0).sum(axis=0)

        pe_usd = jnp.zeros((r,), dtype=jnp.float64)
        if product_summary.has_pe_lots:
            safe_issuer = jnp.maximum(product_inputs.pe_lot_issuer, 0)
            pe_price = pe_ch["marks"][safe_issuer, :, snapshot_month]
            pe_quantity = s.lot_remaining.astype(jnp.float64) / lot_quantity_scale[:, None].astype(jnp.float64)
            pe_value = pe_quantity * pe_price
            pe_usd = jnp.where(product_inputs.pe_lot_mask[:, None], pe_value, 0.0).sum(axis=0)

        property_usd = jnp.zeros((r,), dtype=jnp.float64)
        if product_summary.has_properties:
            valid_series = product_inputs.property_home_value_series >= 0
            safe_series = jnp.maximum(product_inputs.property_home_value_series, 0)
            levels = jnp.nan_to_num(external_values[safe_series], nan=0.0)  # (property, rollout, snapshot)
            current = levels[:, :, snapshot_month]
            base_index = product_inputs.property_purchase_month[:, None, None]
            base = jnp.take_along_axis(levels, base_index, axis=2)[:, :, 0]
            market = (
                product_inputs.property_purchase_price[:, None].astype(jnp.float64)
                / float(USD_CENTS)
                * current
                / jnp.where(base > 0, base, 1.0)
            )
            active_property = product_inputs.property_mask[:, None] & valid_series[:, None] & s.property_active
            property_usd = jnp.where(active_property & (base > 0), market, 0.0).sum(axis=0)

        mortgage_usd = jnp.where(product_inputs.liability_mask[:, None], s.liability_principal, 0).sum(axis=0).astype(
            jnp.float64
        ) / float(USD_CENTS)
        shortfall_usd = jnp.where(obligation_mask[:, None], obligation_shortfall, 0).sum(axis=0).astype(
            jnp.float64
        ) / float(USD_CENTS)
        bond_usd = jnp.zeros((r,), dtype=jnp.float64)
        if product_summary.has_bonds:
            carried = product_inputs.bond_face[:, None] * jnp.ones((1, r), dtype=jnp.int64)
            if structure.has_indexed_bonds:
                safe = jnp.maximum(product_inputs.bond_cpi_series, 0)
                base_cpi = external_values[safe, :, product_inputs.bond_index_base_month]
                indexed_principal = jnp.round(
                    product_inputs.bond_face[:, None]
                    * external_values[safe, :, snapshot_month]
                    / jnp.where(base_cpi > 0, base_cpi, 1.0)
                ).astype(jnp.int64)
                carried = jnp.where((product_inputs.bond_indexed > 0)[:, None], indexed_principal, carried)
            held_face = (product_inputs.bond_on_books[snapshot_month][:, None] * carried).sum(axis=0)
            # Identical across rollouts, but zeroed for failed ones so a failed rollout's net
            # worth is zero like every other term rather than reporting the bonds alone.
            bond_usd = jnp.where(s.failed, 0.0, held_face.astype(jnp.float64) / float(USD_CENTS))

        return cash_usd, holding_usd, pe_usd, property_usd, mortgage_usd, shortfall_usd, bond_usd

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
            jnp.zeros_like(ordinary), property_owner_profile_index, -jnp.where(dec_col, property_dep_ytd, 0)
        )
        ordinary = ordinary + _scatter_rows(
            jnp.zeros_like(ordinary), liability_owner_profile_index, -jnp.where(dec_col, liab_rental_ytd, 0)
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
        ordinary = ordinary + _scatter_rows(jnp.zeros_like(ordinary), cg_rep, -jnp.where(do_net, ord_offset, 0))

        # Two-pass SALT bracket math; collect per-link tax + breakdown slabs.
        annual_tax_by_link = _zeros_i64((r, max(1, link_count)))
        zero_salt = _zeros_i64((r,))
        breakdown = [_zeros_i64((max(1, link_count), r)) for _ in range(13)]

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
                dec.astype(jnp.int64),  # accrual_active flag (->bool post-scan)
                jnp.where(dec, tax, 0),
                jnp.where(dec, ordinary[profile_ordinary_bucket[profile]], 0),
                jnp.where(dec, cg_ytd[gp, CapitalGainClassification.LONG_TERM], 0),
                jnp.where(dec, cg_ytd[gp, CapitalGainClassification.SHORT_TERM], 0),
                jnp.where(dec, tcfg.link_standard_deduction[link], 0),  # traced value
                jnp.where(dec, mid, 0),
                jnp.where(dec, salt_deduction, 0),
                jnp.where(dec, itemized, 0),
                jnp.where(dec, ord_taxable, 0),
                jnp.where(dec, cap_taxable, 0),
                jnp.where(dec, ord_tax, 0),
                jnp.where(dec, cap_tax, 0),
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
        written = _scatter_rows(_zeros_i64((taxliab_count, r)), slot_for_link, jnp.where(dec, link_tax, 0))
        written_mask = _scatter_rows(_zeros_i64((taxliab_count, r)), slot_for_link, dec.astype(jnp.int64)) > 0
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
        cost_basis_per_unit = s.cost_basis_per_unit
        cg_active, cg_ytd, tlh = s.capital_gain_active, s.capital_gain_ytd, s.tlh
        property_active, property_basis = s.property_active, s.property_basis
        property_contribution, property_equity = s.property_contribution, s.property_equity
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
        for evi, ev in enumerate(folded_lifecycle):
            ev_month, ev_kind, ev_prop = ev.month, ev.kind, ev.property_slot
            fires = month == ev_month
            active_property = fires & active & property_active[ev_prop]
            if ev_kind == LifecycleKind.FRACTION:
                property_rented_fraction = property_rented_fraction.at[ev_prop].set(
                    jnp.where(active_property, ev.rented_fraction, property_rented_fraction[ev_prop])
                )
            elif ev_kind == LifecycleKind.CAPITAL_IMPROVEMENT:
                amount = ev.amount_cents
                owner_cash_slot = ev.owner_cash_slot
                if owner_cash_slot >= 0:
                    # The contractor doing the work is outside the model.
                    cash = _move_cash(
                        cash,
                        debit=owner_cash_slot,
                        credit=structure.external_cash_slot,
                        amount=jnp.where(active_property, amount, 0),
                        row_of_world=structure.external_cash_slot,
                    )
                property_building_basis = property_building_basis.at[ev_prop].add(jnp.where(active_property, amount, 0))
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
                    lifecycle_sale_series[evi],
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
                    external_cash_slot=structure.external_cash_slot,
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
            structure.external_cash_slot,
        )

        # Bond coupons and redemptions, after transfers and before obligations settle: a coupon
        # is income arriving this month and must be able to fund this month's outflows, the same
        # ordering a paycheck gets.
        cash, ordinary = _bond_cashflows_jit(
            bond["coupon"][month],
            bond["redemption"][month],
            bond["to_slot"],
            bond["income_row"],
            bond["indexed"],
            bond["cpi_series"],
            bond["index_base_month"],
            bond["period_rate"],
            bond["face"],
            bond["pays"][month],
            bond["matures"][month],
            bond["on_books"][month],
            cash,
            ordinary,
            active,
            external_values,
            month,
            structure.external_cash_slot,
            structure.has_indexed_bonds,
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
            stake_pos = (pur_stake > 0)[:, None]  # (P, 1) static
            # Gathered `tcfg` columns are 1-D per-entity (P,)/(M,) -> `[:, None]` to broadcast over R.
            property_active = property_active.at[pur_buf].set(jnp.where(fires, True, property_active[pur_buf]))
            property_basis = property_basis.at[pur_buf].set(
                jnp.where(fires, tcfg.property_adjusted_basis[pur_buf][:, None], property_basis[pur_buf])
            )
            property_contribution = property_contribution.at[pur_buf].set(
                jnp.where(fires, pur_stake[:, None], property_contribution[pur_buf])
            )
            property_equity = property_equity.at[pur_buf].set(
                jnp.where(fires, tcfg.property_equity_ledger[pur_buf][:, None], property_equity[pur_buf])
            )
            stake_flow = jnp.where(fires & stake_pos, pur_stake[:, None], 0)  # (P, R)
            cash = _move_cash(
                cash,
                debit=jnp.asarray(pur_buyer),
                credit=jnp.asarray(pur_seller),
                amount=stake_flow,
                row_of_world=structure.external_cash_slot,
            )
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
                jnp.where(mfires, 0, liab_interest_ytd[pur_mort_idx])
            )
            liab_principal_ytd = liab_principal_ytd.at[pur_mort_idx].set(
                jnp.where(mfires, 0, liab_principal_ytd[pur_mort_idx])
            )
            mort_orig_rows = mort_orig_rows.at[pur_mort_idx].set(mfires)
            # Per-purchase event rows for `ys` (folded order): purchase fired; transfer fired (stake>0).
            purchase_active_rows = fires
            transfer_active_rows = fires & stake_pos

        cash, ordinary, property_cashflow_active, property_cashflow_amount = _property_cashflows_jit(
            pc["cause"][month],
            pc["kind"][month],
            pc["fixed"][month],
            pc["base"][month],
            pc["series"][month],
            pc["base_month"][month],
            pc["period"][month],
            pc["from_slot"][month],
            pc["to_slot"][month],
            pc["property_slot"][month],
            pc["income_profile"][month],
            pc["deduction_profile"][month],
            property_active,
            cash,
            ordinary,
            active,
            external_values,
            month,
            structure.external_cash_slot,
        )

        # Scheduled asset sales (before obligations: proceeds can fund the month's obligations).
        # Vectorized over ALL sales at once — no Python loop. The across-sales FIFO is one
        # cumulative-supply (over each pool's lots) x cumulative-demand (over each pool's sales,
        # `sale_prior_t`) interval overlap, so shared pools fall out without sequencing. Each sale's
        # disposition `(sale, lot, R)` accumulates into the carry at its slot (fires once -> horizon
        # collapsed). `L` is padded with a zero dummy lot so the ragged pools share one shape.
        if folded_sales:
            ld = lot_count
            lot_rem_pad = jnp.concatenate([lot_remaining, _zeros_i64((1, r))], axis=0)  # (L+1, R)
            cost_pad = jnp.concatenate([cost_basis_per_unit, _zeros_i64((1, r))], axis=0)  # (L+1, R)
            scale_pad = jnp.concatenate([lot_quantity_scale, jnp.ones(1, dtype=jnp.int64)])  # (L+1,)
            lpm_pad = jnp.concatenate([lot_purchase_month.astype(jnp.int32), jnp.zeros(1, jnp.int32)])
            pool_qty = lot_rem_pad[sale_olots]  # (N, P, R) supply per pool lot
            target = jnp.where((active[None, :]) & (month == sale_months_t)[:, None], sale_qty_t[:, None], 0)  # (N, R)
            prior = sale_prior_t @ target  # (N, R) demand already claimed by earlier same-pool sales
            oversell = target > (pool_qty.sum(axis=1) - prior)  # (N, R)
            d_lo = prior  # demand interval (D_{j-1}, D_j], with oversold sales selling nothing
            d_hi = prior + jnp.where(oversell, 0, target)
            s_before = jnp.cumsum(pool_qty, axis=1) - pool_qty  # supply prefix S_{k-1} (N, P, R)
            sold = jnp.maximum(
                0, jnp.minimum(d_hi[:, None, :], s_before + pool_qty) - jnp.maximum(d_lo[:, None, :], s_before)
            )

            # TLH give-back (telescoped): each policy drains tlh proportional to units sold of its lots,
            # at rate tlh0 / pre_sale_units; the per-sale realization is `sold * rate` on the sold lots.
            t_policy = sale_policy_mask_t @ lot_remaining  # (policy, R) pre-sale units
            gb_rate = jnp.where(t_policy > 0, tlh / jnp.where(t_policy > 0, t_policy, 1), 0.0)  # (policy, R)
            lot_gb_rate_pad = jnp.concatenate(
                [sale_policy_mask_t.T @ gb_rate, jnp.zeros((1, r), dtype=jnp.float64)], axis=0
            )  # (L+1, R)

            # Per-sale price: fixed if set, else the sampled series at this month. Guarded on the static
            # series count (and the series index clamped) so fixed-only sales never gather an empty cube.
            if external_values.shape[0] > 0:
                safe_series = jnp.where(sale_price_series >= 0, sale_price_series, 0)
                unit_price = jnp.where(
                    (sale_price_series >= 0)[:, None],
                    _price_usd_to_cents(external_values[safe_series, :, month]),
                    sale_price_fixed_t[:, None],
                )  # (N, R)
            else:
                unit_price = jnp.broadcast_to(sale_price_fixed_t[:, None], (n_sales, r))
            proceeds = _value_cents_from_quanta(
                sold, unit_price[:, None, :], scale_pad[sale_olots][:, :, None]
            )  # (N, P, R)
            basis = _value_cents_from_quanta(sold, cost_pad[sale_olots], scale_pad[sale_olots][:, :, None])
            gains = proceeds - basis + _round_int64(sold * lot_gb_rate_pad[sale_olots])  # incl. give-back

            total_sold = _zeros_i64((ld + 1, r)).at[sale_olots].add(sold)  # (L+1, R)
            lot_remaining = lot_remaining - total_sold[:ld]
            tlh = tlh - _round_int64((sale_policy_mask_t @ total_sold[:ld]) * gb_rate)
            # The cash comes from whoever bought the lot, which is `rest_of_world`.
            cash = _move_cash(
                cash,
                debit=structure.external_cash_slot,
                credit=sale_pslot,
                amount=proceeds.sum(axis=1),  # (N, R)
                row_of_world=structure.external_cash_slot,
            )

            # Capital gains: classify each pool lot long/short, accrue per sale's cg agents via cg_map.
            long_m = (month - lpm_pad[sale_olots]) >= 12  # (N, P)
            gains_long = (gains * long_m[:, :, None]).sum(axis=1)  # (N, R)
            gains_short = (gains * (~long_m)[:, :, None]).sum(axis=1)
            sold_pos = sold > 0
            act_long = (sold_pos & long_m[:, :, None]).any(axis=1)  # (N, R)
            act_short = (sold_pos & (~long_m)[:, :, None]).any(axis=1)
            cg_ytd = cg_ytd.at[:, CapitalGainClassification.LONG_TERM, :].add(sale_cg_map_t.T @ gains_long)
            cg_ytd = cg_ytd.at[:, CapitalGainClassification.SHORT_TERM, :].add(sale_cg_map_t.T @ gains_short)
            cg_active = cg_active.at[:, CapitalGainClassification.LONG_TERM, :].set(
                cg_active[:, CapitalGainClassification.LONG_TERM, :]
                | ((sale_cg_map_t.T @ act_long.astype(jnp.int64)) > 0)
            )
            cg_active = cg_active.at[:, CapitalGainClassification.SHORT_TERM, :].set(
                cg_active[:, CapitalGainClassification.SHORT_TERM, :]
                | ((sale_cg_map_t.T @ act_short.astype(jnp.int64)) > 0)
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
            acc["configured_prop_idx"][month],
            acc["configured_has_prop"][month],
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
        liq_disp_units = _zeros_i64((liq_policy_count, liq_max_assets, lot_axis, r))
        liq_disp_basis = _zeros_i64((liq_policy_count, liq_max_assets, lot_axis, r))
        liq_disp_proceeds = _zeros_i64((liq_policy_count, liq_max_assets, lot_axis, r))
        attempt_policy = jnp.full((slot_active.shape[0], r), NO_CODE, dtype=jnp.int64)
        for li, lp in enumerate(folded_liquidity):
            matching = (og["agent"][month] == lp.agent) & (og["from_slot"][month] == lp.cash_slot)  # (slots,)
            hard_demand = jnp.where(matching[:, None] & slot_active, accrual_due, 0).sum(axis=0)  # (R,)
            attempt_policy = jnp.where(matching[:, None] & slot_active, lp.policy_index, attempt_policy)
            cash_balance = cash[lp.cash_slot]
            required_sale = jnp.maximum(hard_demand - cash_balance, 0)
            post_required_cash = cash_balance + required_sale - hard_demand
            trigger_val = _amount_values_tuple(lp.trigger, liq_trigger_series[li], external_values, month, r)
            sale_val = _amount_values_tuple(lp.sale, liq_sale_series[li], external_values, month, r)
            buffer_sale = jnp.where((sale_val > 0) & (post_required_cash < trigger_val), sale_val, 0)
            remaining = jnp.where(active, required_sale + buffer_sale, 0)
            pool_series = liq_pool_series[li]
            for pj, pool in enumerate(lp.pools):
                raw_price = external_values[pool_series[pj], :, month]
                valid_price = jnp.isfinite(raw_price) & (raw_price > 0.0)
                unit_price = jnp.where(valid_price, _price_usd_to_cents(raw_price), 0)
                pool_lots = np.asarray(pool.ordered_lots, dtype=np.int64)
                available = _value_cents_from_quanta(
                    lot_remaining[pool_lots], unit_price[None, :], lot_quantity_scale[pool_lots, None]
                ).sum(axis=0)
                target = jnp.where(valid_price & active, jnp.minimum(jnp.maximum(remaining, 0), available), 0)
                sold_units, proceeds, basis = _fifo_sell_cents(
                    lot_remaining.T, pool_lots, target, unit_price, cost_basis_per_unit, lot_quantity_scale
                )
                lot_remaining = lot_remaining - sold_units.T
                total_proceeds = proceeds.sum(axis=1)
                # The cash comes from whoever bought the lot, which is `rest_of_world`.
                cash = _move_cash(
                    cash,
                    debit=structure.external_cash_slot,
                    credit=lp.cash_slot,
                    amount=total_proceeds,
                    row_of_world=structure.external_cash_slot,
                )
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
                    liq_disp_active[lp.policy_index, pool.asset_idx] | (sold_units > 0).T
                )
                liq_disp_units = liq_disp_units.at[lp.policy_index, pool.asset_idx].add(sold_units.T)
                liq_disp_basis = liq_disp_basis.at[lp.policy_index, pool.asset_idx].add(basis.T)
                liq_disp_proceeds = liq_disp_proceeds.at[lp.policy_index, pool.asset_idx].add(proceeds.T)
                remaining = jnp.maximum(remaining - total_proceeds, 0)

        # Target-allocation policies: observe, decide, execute. Same slot in the month as the
        # liquidity phase — before the funding check, so a raise can cover this month's demand.
        #
        # Unlike the liquidity phase, the decision is NOT taken here: `target_allocation.decide`
        # is a pure function of an `ActorView`, so what the engine does is build the observation,
        # call the policy, and execute what comes back. A learned policy replaces that one call
        # and nothing around it.
        ta_disp_active = jnp.zeros((ta_policy_count, ta_max_sleeves, lot_axis, r), dtype=bool)
        ta_disp_units = _zeros_i64((ta_policy_count, ta_max_sleeves, lot_axis, r))
        ta_disp_basis = _zeros_i64((ta_policy_count, ta_max_sleeves, lot_axis, r))
        ta_disp_proceeds = _zeros_i64((ta_policy_count, ta_max_sleeves, lot_axis, r))
        if folded_target_allocation:
            # Marks for every lot, once for the month rather than once per pool: the observation
            # needs a value for each of the policy's lots, and `_value_cents_from_quanta` is the
            # same helper the sale itself values with — a second implementation here would report
            # a sleeve worth a cent less than selling it yields.
            ta_valid_series = lot_asset_series_index >= 0
            if external_values.shape[0] > 0:
                ta_raw_price = external_values[jnp.where(ta_valid_series, lot_asset_series_index, 0), :, month]
                ta_lot_price = _price_usd_to_cents(
                    jnp.nan_to_num(jnp.where(ta_valid_series[:, None], ta_raw_price, 0.0), nan=0.0)
                )
            else:
                ta_lot_price = _zeros_i64((lot_remaining.shape[0], r))
            lot_value_all = _value_cents_from_quanta(lot_remaining, ta_lot_price, lot_quantity_scale[:, None])

        for ti, tp in enumerate(folded_target_allocation):
            matching = (og["agent"][month] == tp.agent) & (og["from_slot"][month] == tp.cash_slot)  # (slots,)
            # What the month is already committed to paying from this account. The band decides
            # against the balance the month will END at, which is what lets funding happen once.
            hard_demand = jnp.where(matching[:, None] & slot_active, accrual_due, 0).sum(axis=0)  # (R,)
            attempt_policy = jnp.where(matching[:, None] & slot_active, tp.policy_index, attempt_policy)
            view = build_actor_view(
                month=month,
                slots=ActorSlots(
                    cash_slots=(tp.cash_slot,),
                    lot_slots=tp.lot_slots,
                    external_cash_slot=structure.external_cash_slot,
                    cash_count=structure.cash_count,
                    lot_count=structure.lot_count,
                ),
                cash_cents=cash,
                lot_quantity=lot_remaining,
                lot_cost_basis_per_unit_cents=cost_basis_per_unit,
                lot_value_cents=lot_value_all,
                lot_purchase_month=lot_purchase_month,
                scheduled_outflow_cents=hard_demand,
            )
            orders = decide(
                view=view,
                universe=SleeveUniverse(
                    weights=np.asarray([sleeve.weight for sleeve in tp.sleeves], dtype=np.int64),
                    lot_rows=tuple(sleeve.view_lot_rows for sleeve in tp.sleeves),
                    funding_cash_row=0,
                ),
                floor_cents=_amount_values_tuple(tp.floor, ta_floor_series[ti], external_values, month, r),
                ceiling_cents=_amount_values_tuple(tp.ceiling, ta_ceiling_series[ti], external_values, month, r),
            )
            for si, sleeve in enumerate(tp.sleeves):
                # `buy_cents` is deliberately unread: investing surplus needs purchase slots the
                # engine does not have yet. Surplus above the ceiling accumulates, which is what
                # the config docstring promises — the ceiling is a refill target, not a buy rule.
                remaining = jnp.where(active, orders.sell_cents[si], 0)
                sleeve_series_ops = ta_pool_series[ti][si]
                for pj, pool in enumerate(sleeve.pools):
                    raw_price = external_values[sleeve_series_ops[pj], :, month]
                    valid_price = jnp.isfinite(raw_price) & (raw_price > 0.0)
                    unit_price = jnp.where(valid_price, _price_usd_to_cents(raw_price), 0)
                    pool_lots = np.asarray(pool.ordered_lots, dtype=np.int64)
                    available = _value_cents_from_quanta(
                        lot_remaining[pool_lots], unit_price[None, :], lot_quantity_scale[pool_lots, None]
                    ).sum(axis=0)
                    target = jnp.where(valid_price & active, jnp.minimum(jnp.maximum(remaining, 0), available), 0)
                    sold_units, proceeds, basis = _fifo_sell_cents(
                        lot_remaining.T, pool_lots, target, unit_price, cost_basis_per_unit, lot_quantity_scale
                    )
                    lot_remaining = lot_remaining - sold_units.T
                    total_proceeds = proceeds.sum(axis=1)
                    # The cash comes from whoever bought the lot, which is `rest_of_world`.
                    cash = _move_cash(
                        cash,
                        debit=structure.external_cash_slot,
                        credit=tp.cash_slot,
                        amount=total_proceeds,
                        row_of_world=structure.external_cash_slot,
                    )
                    cg_active, cg_ytd, tlh = _record_capital_gains(
                        folded_harvest,
                        lot_purchase_month,
                        cg_profiles_by_agent[tp.agent],
                        cg_active,
                        cg_ytd,
                        tlh,
                        lot_remaining,
                        month,
                        sold_units,
                        proceeds - basis,
                    )
                    ta_disp_active = ta_disp_active.at[tp.policy_index, sleeve.sleeve_idx].set(
                        ta_disp_active[tp.policy_index, sleeve.sleeve_idx] | (sold_units > 0).T
                    )
                    ta_disp_units = ta_disp_units.at[tp.policy_index, sleeve.sleeve_idx].add(sold_units.T)
                    ta_disp_basis = ta_disp_basis.at[tp.policy_index, sleeve.sleeve_idx].add(basis.T)
                    ta_disp_proceeds = ta_disp_proceeds.at[tp.policy_index, sleeve.sleeve_idx].add(proceeds.T)
                    remaining = jnp.maximum(remaining - total_proceeds, 0)

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
                structure.external_cash_slot,
            )
        )

        # Scheduled asset purchases. AFTER settlement on purpose: buying is discretionary and must
        # never be able to starve an obligation into a false ruin. Fully vectorized `(n_buys, R)` —
        # every rollout buys at its own price with its own cash.
        if asset_buys:
            if external_values.shape[0] > 0:
                buy_safe_series = jnp.where(buy_price_series >= 0, buy_price_series, 0)
                buy_price = jnp.where(
                    (buy_price_series >= 0)[:, None],
                    _price_usd_to_cents(external_values[buy_safe_series, :, month]),
                    buy_price_fixed_t[:, None],
                )  # (n_buys, R)
            else:
                buy_price = jnp.broadcast_to(buy_price_fixed_t[:, None], (n_buys, r))
            # `~failed`, not the month-opening `active`: settlement runs just above and can fail
            # the rollout, and a failed rollout must stop transacting immediately.
            buy_fires = (~failed)[None, :] & (month == buy_month_t)[:, None]  # (n_buys, R)
            # Clamp to what the funding account actually holds. Recorded on the event, so a caller
            # comparing executed against requested sees the shortfall.
            budget = jnp.where(buy_fires, jnp.minimum(buy_amount_t[:, None], jnp.maximum(cash[buy_cash_slot], 0)), 0)
            safe_price = jnp.where(buy_price > 0, buy_price, 1)
            # Whole quanta only; the sub-quantum remainder stays in cash. Flooring here and valuing
            # with the same helper the basis math uses keeps `spent` <= `budget` (round(x) <= N for
            # x <= integer N) and makes an immediate full-lot resale net exactly zero gain.
            buy_quanta = jnp.where(buy_price > 0, (budget * buy_scale_t[:, None]) // safe_price, 0)
            buy_spent = _value_cents_from_quanta(buy_quanta, buy_price, buy_scale_t[:, None])
            # The cash leaves for the market, which is `rest_of_world`.
            cash = _move_cash(
                cash,
                debit=buy_cash_slot,
                credit=structure.external_cash_slot,
                amount=buy_spent,
                row_of_world=structure.external_cash_slot,
            )
            lot_remaining = lot_remaining.at[buy_lot_slot].add(buy_quanta)
            bought = buy_quanta > 0
            cost_basis_per_unit = cost_basis_per_unit.at[buy_lot_slot].set(
                jnp.where(bought, buy_price, cost_basis_per_unit[buy_lot_slot])
            )

        # Mortgage payments: split each paid mortgage bill into interest (rate/12 on the outstanding
        # principal, capped at the payment) and principal (the remainder, capped at the balance), then
        # pay down the liability and accrue the YTD interest/principal (+ the rented share for Sch E).
        # Non-mortgage slots route to the sentinel index -1, so `_scatter_rows` ignores them.
        is_mortgage = (og["source_kind"][month] == ObligationSource.MORTGAGE_PAYMENT) & (og["cause"][month] >= 0)
        mort_liab_idx = jnp.where(is_mortgage, acc["liab_idx"][month], -1)
        principal_before = _gather_rows(liab_principal, jnp.where(is_mortgage, acc["liab_idx"][month], 0))
        interest = jnp.minimum(_scale_money(principal_before, acc["mort_rate"][month][:, None] / 12.0), paid_buffer)
        principal_paid = jnp.minimum(jnp.maximum(paid_buffer - interest, 0), principal_before)
        mort_paid = is_mortgage[:, None] & paid
        interest_m = jnp.where(mort_paid, interest, 0)
        principal_m = jnp.where(mort_paid, principal_paid, 0)
        rented_per_slot = _gather_rows(property_rented_fraction, acc["mort_prop_idx"][month])
        liab_principal = _scatter_rows(liab_principal, mort_liab_idx, -principal_m)
        liab_interest_ytd = _scatter_rows(liab_interest_ytd, mort_liab_idx, interest_m)
        liab_principal_ytd = _scatter_rows(liab_principal_ytd, mort_liab_idx, principal_m)
        liab_rental_ytd = _scatter_rows(liab_rental_ytd, mort_liab_idx, _scale_money(interest_m, rented_per_slot))
        # Mortgage-payment event slabs, scattered from obligation slots to their liability rows.
        liab_count = liab_principal.shape[0]
        mort_pay_active = _scatter_rows(_zeros_i64((liab_count, r)), mort_liab_idx, mort_paid.astype(jnp.int64)) > 0
        mort_pay_interest = _scatter_rows(_zeros_i64((liab_count, r)), mort_liab_idx, interest_m)
        mort_pay_principal = _scatter_rows(_zeros_i64((liab_count, r)), mort_liab_idx, principal_m)
        mort_pay_total = _scatter_rows(_zeros_i64((liab_count, r)), mort_liab_idx, jnp.where(mort_paid, paid_buffer, 0))
        mort_orig = mort_orig_rows

        # Tax-liability settlement: a paid TAX_TRUE_UP fully clears its profile-year's liability (the
        # estimated prepayments covered the rest). `trueup_sel` maps each true-up obligation slot to
        # the tax-liability slots of the year it settles; `paid` (active & funded) gates it.
        trueup_sel_m = acc["trueup_sel"][month]  # (slots, taxliab)
        is_trueup = (og["source_kind"][month] == ObligationSource.TAX_TRUE_UP) & (og["cause"][month] >= 0)
        trueup_paid = is_trueup[:, None] & paid  # (slots, R)
        eligible = jnp.where(taxliab_active, taxliab_amount, 0)  # (taxliab, R)
        actual_per_trueup = trueup_sel_m @ eligible  # (slots, R): full year tax owed
        settle_k = (trueup_sel_m.astype(bool)[:, :, None] & trueup_paid[:, None, :]).any(axis=0)  # (taxliab, R)
        taxliab_amount = jnp.where(settle_k, 0, taxliab_amount)
        # Settlement event buffers, scattered to tax-profile rows (one true-up per profile per month).
        settle_prof_idx = jnp.where(is_trueup, acc["prof_idx"][month], -1)
        settle_amount = _scatter_rows(
            _zeros_i64((profile_count, r)), settle_prof_idx, jnp.where(trueup_paid, actual_per_trueup, 0)
        )
        settle_active = (
            _scatter_rows(_zeros_i64((profile_count, r)), settle_prof_idx, trueup_paid.astype(jnp.int64)) > 0
        )
        settle_year_end = jnp.where(settle_active, acc["tax_year_end"][month], NO_CODE)

        # TLH harvest (after settlement, before PE): book a calibrated capital loss per policy. The
        # prior price clamps to month 0 (max(0, month-1)), giving a flat period return there — so the
        # eager engine's month-0 `has_prior=False` special case is unnecessary inside the scan.
        for hi, fh in enumerate(folded_harvest):
            hp_policy = fh.policy_idx
            hp_lots = np.asarray(fh.lot_indices, dtype=np.int64)
            hp_series_row = external_values[harvest_series[hi]]  # (rollouts, H+1) dynamic gather
            hp_price = hp_series_row[:, month]
            hp_prior = hp_series_row[:, jnp.maximum(0, month - 1)]
            cg_ytd, cg_active, hp_cumulative = _tlh_harvest_policy_jit(
                lot_remaining[hp_lots, :],
                cost_basis_per_unit[hp_lots],
                lot_quantity_scale[hp_lots],
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
        pe_disp_units = _zeros_i64((pe_issuer_count, n_pe_kinds, lot_axis, r))
        pe_disp_basis = _zeros_i64((pe_issuer_count, n_pe_kinds, lot_axis, r))
        pe_disp_proceeds = _zeros_i64((pe_issuer_count, n_pe_kinds, lot_axis, r))
        pe_opp = {  # 9 opportunity-trace fields per issuer
            k: _zeros_i64((pe_issuer_count, r))
            for k in ("active", "outcome", "floor", "lnw", "shortfall", "units", "sellable", "target", "proceeds")
        }
        for pei, fpe in enumerate(folded_pe):
            issuer_idx, policy_idx = fpe.issuer_idx, fpe.policy_idx
            ordered = np.asarray(fpe.ordered, dtype=np.int64)
            mark = pe_ch["marks"][issuer_idx, :, month]
            mark_cents = _price_usd_to_cents(mark)
            positive_mark = mark > 0.0
            tender_active = pe_ch["sale_opp"][issuer_idx, :, month] & active
            public_active = pe_ch["regime"][issuer_idx, :, month] == int(PrivateEquityRegimeCode.PUBLIC_MARKET)
            liq_blocked = pe_ch["liq_blocked"][issuer_idx, :, month]
            forced_sale_fraction = pe_ch["forced_sale"][issuer_idx, :, month]
            forced_recovery = pe_ch["forced_recovery"][issuer_idx, :, month]
            capacity = pe_ch["capacity"][issuer_idx, :, month]
            eligible = pe_ch["eligible"][issuer_idx, :, month]
            units_held = lot_remaining[ordered].sum(axis=0)
            issuer_scale = lot_quantity_scale[ordered[0]]
            if policy_idx < 0:
                pe_opp["active"] = pe_opp["active"].at[issuer_idx].set(tender_active.astype(jnp.int64))
                pe_opp["outcome"] = (
                    pe_opp["outcome"]
                    .at[issuer_idx]
                    .set(jnp.where(tender_active, int(PrivateEquityOpportunityOutcome.NO_POLICY), 0))
                )
                pe_opp["units"] = pe_opp["units"].at[issuer_idx].set(units_held)
                pe_opp["sellable"] = (
                    pe_opp["sellable"].at[issuer_idx].set(_round_int64(units_held * capacity * eligible))
                )
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
                sold, proceeds, basis = _fifo_sell_units(
                    lot_remaining.T, ordered, target, price, cost_basis_per_unit, lot_quantity_scale
                )
                lot_remaining = lot_remaining - sold.T
                if proceeds_slot >= 0:
                    # The tender offer / public market is outside the model.
                    cash = _move_cash(
                        cash,
                        debit=structure.external_cash_slot,
                        credit=proceeds_slot,
                        amount=proceeds.sum(axis=1),
                        row_of_world=structure.external_cash_slot,
                    )
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
                da = da.at[issuer_idx, ki].set(da[issuer_idx, ki] | (sold > 0).T)
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
            recovery_active = (forced_recovery > 0) & active & (units_held > 0)
            recovery_price = _round_int64(
                forced_recovery.astype(jnp.float64)
                * issuer_scale.astype(jnp.float64)
                / jnp.where(units_held > 0, units_held, 1).astype(jnp.float64)
            )
            state = book(
                jnp.where(recovery_active, units_held, 0),
                recovery_price,
                PrivateEquityDispositionKind.FORCED_RECOVERY,
                state,
            )
            units_held = state[1][ordered].sum(axis=0)
            # Forced sale: a fraction of the remaining position at the mark.
            forced_active = (forced_sale_fraction > 0.0) & active & positive_mark & (units_held > 0)
            forced_target = jnp.minimum(_round_int64(units_held * forced_sale_fraction), units_held)
            state = book(
                jnp.where(forced_active, forced_target, 0), mark_cents, PrivateEquityDispositionKind.FORCED_SALE, state
            )
            cash, lot_remaining = state[0], state[1]
            # LNW-floor tender: sell to lift liquid net worth to the floor, capped at sellable units.
            floor = _amount_values(
                amount_kind=fpe.floor_kind,
                amount_fixed=fpe.floor_fixed,
                amount_base=fpe.floor_base,
                amount_series=pe_floor_series[pei],
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
                lot_quantity_scale,
                external_values,
                month,
            )
            pe_shortfall = jnp.maximum(0, floor - lnw)  # distinct from the obligation `shortfall` in base_ys
            units_held = lot_remaining[ordered].sum(axis=0)
            sellable = _round_int64(units_held * capacity * eligible)
            shortfall_units = jnp.where(
                positive_mark, _ceil_quanta_for_value_cents(pe_shortfall, mark_cents, issuer_scale), 0
            )
            opp_active = (tender_active | public_active) & active & ~liq_blocked & positive_mark
            target = jnp.where(opp_active, jnp.minimum(shortfall_units, sellable), 0)
            outcome = jnp.full(r, int(PrivateEquityOpportunityOutcome.SOLD))
            outcome = jnp.where(pe_shortfall <= 0.0, int(PrivateEquityOpportunityOutcome.FLOOR_SATISFIED), outcome)
            outcome = jnp.where(
                (capacity * eligible) <= 0.0, int(PrivateEquityOpportunityOutcome.CAPACITY_ZERO), outcome
            )
            outcome = jnp.where(~positive_mark, int(PrivateEquityOpportunityOutcome.NONPOSITIVE_MARK), outcome)
            outcome = jnp.where(liq_blocked, int(PrivateEquityOpportunityOutcome.LIQUIDITY_BLOCKED), outcome)
            outcome = jnp.where(units_held <= 0, int(PrivateEquityOpportunityOutcome.NO_UNITS), outcome)
            for key, val in (
                ("active", tender_active.astype(jnp.int64)),
                ("outcome", jnp.where(tender_active, outcome, 0)),
                ("floor", jnp.where(tender_active, floor, 0)),
                ("lnw", jnp.where(tender_active, lnw, 0)),
                ("shortfall", jnp.where(tender_active, pe_shortfall, 0)),
                ("units", jnp.where(tender_active, units_held, 0)),
                ("sellable", jnp.where(tender_active, sellable, 0)),
                ("target", jnp.where(tender_active, target, 0)),
                ("proceeds", jnp.where(tender_active, _value_cents_from_quanta(target, mark_cents, issuer_scale), 0)),
            ):
                pe_opp[key] = pe_opp[key].at[issuer_idx].set(val)
            state = (cash, lot_remaining, cg_active, cg_ytd, tlh, state[5], state[6], state[7], state[8])
            state = book(
                jnp.where(tender_active & ~public_active, target, 0),
                mark_cents,
                PrivateEquityDispositionKind.TENDER,
                state,
            )
            state = book(
                jnp.where(public_active, target, 0), mark_cents, PrivateEquityDispositionKind.PUBLIC_MARKET, state
            )
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
        property_basis, property_contribution, property_equity = (
            property_basis * keep,
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
            cost_basis_per_unit=cost_basis_per_unit,
            capital_gain_active=cg_active,
            capital_gain_ytd=cg_ytd,
            tlh=tlh,
            property_active=property_active,
            property_basis=property_basis,
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
        if product_summary is not None:
            product_inputs_local = product_inputs
            assert product_inputs_local is not None
            return carry, product_metrics(
                carry,
                snapshot_month=month + 1,
                obligation_shortfall=shortfall,
                obligation_mask=product_inputs_local.primary_obligation_mask[month],
            )
        base_ys = (
            cash,
            ordinary,
            lot_remaining,
            cg_active,
            cg_ytd,
            property_active,
            property_basis,
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
            property_cashflow_active,
            property_cashflow_amount,
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
        # Target-allocation slabs: per-(policy, sleeve) disposition. Its own group rather than the
        # liquidity one — the two policy kinds index their own dense rows, so sharing a buffer would
        # have them overwriting each other's policies.
        target_allocation_ys = (
            (ta_disp_active, ta_disp_units, ta_disp_basis, ta_disp_proceeds) if folded_target_allocation else ()
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
        return carry, (
            *base_ys,
            *sale_ys,
            *purchase_ys,
            *mortgage_ys,
            *tax_ys,
            *liquidity_ys,
            *target_allocation_ys,
            *pe_ys,
            *lifecycle_ys,
        )

    init = _ScanState(
        cash=cash0,
        ordinary_ytd=ordinary0,
        property_tax_ytd=property_tax_ytd0,
        lot_remaining=lot0,
        cost_basis_per_unit=_zeros_i64((p.lot_count, r)),
        capital_gain_active=cg_active0,
        capital_gain_ytd=cg_ytd0,
        tlh=tlh0,
        property_active=jnp.zeros((p.property_count, r), dtype=bool),
        property_basis=prop0,
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
        capital_loss_carryforward=_zeros_i64((p.capital_gain_agent_count, r)),
        recapture_section_1250_ytd=_zeros_i64((p.tax_profile_count, r)),
        tax_liability_active=jnp.zeros((p.tax_liability_count, r), dtype=bool),
        tax_liability_amount=_zeros_i64((p.tax_liability_count, r)),
        failed=jnp.zeros(r, dtype=bool),
        failed_month=jnp.full(r, -1, dtype=jnp.int32),
        sale_disp_units=_zeros_i64((p.scheduled_sale_count, lot_axis, r)),
        sale_disp_basis=_zeros_i64((p.scheduled_sale_count, lot_axis, r)),
        sale_disp_proceeds=_zeros_i64((p.scheduled_sale_count, lot_axis, r)),
        sale_oversell=jnp.zeros((), dtype=bool),
    )
    months = jnp.arange(horizon, dtype=jnp.int32)
    # Initial cash / lot carry: broadcast the traced per-entity opening balances across rollouts.
    init = init._replace(
        cash=jnp.broadcast_to(cfg.cash_initial_balance[:, None], (p.cash_count, r)),
        lot_remaining=jnp.broadcast_to(cfg.lot_initial_quantity[:, None], (p.lot_count, r)),
        cost_basis_per_unit=jnp.broadcast_to(cfg.cost_basis_per_unit[:, None], (p.lot_count, r)),
    )
    if product_summary is not None:
        product_inputs_local = product_inputs
        assert product_inputs_local is not None
        initial_ys = product_metrics(
            init,
            snapshot_month=jnp.asarray(0, dtype=jnp.int32),
            obligation_shortfall=_zeros_i64((product_inputs_local.primary_obligation_mask.shape[1], r)),
            obligation_mask=jnp.zeros(product_inputs_local.primary_obligation_mask.shape[1], dtype=bool),
        )
        final_carry, ys = jax.lax.scan(step, init, months)
        return (initial_ys, ys), (final_carry.sale_oversell, final_carry.failed_month)
    final_carry, ys = jax.lax.scan(step, init, months)
    # Horizon-collapsed outputs, read off the final carry rather than emitted per month: the
    # scheduled-sale dispositions (accumulated at each sale's firing month) and the per-lot cost
    # basis (written once at purchase and never revised — a lot slot is never reused).
    return ys, (
        final_carry.cost_basis_per_unit,
        final_carry.sale_disp_units,
        final_carry.sale_disp_basis,
        final_carry.sale_disp_proceeds,
        final_carry.sale_oversell,
    )


def _amount_values(
    *,
    amount_kind: int,
    amount_fixed: int,
    amount_base: int,
    amount_series: jnp.ndarray,
    amount_base_month: int,
    amount_period: int,
    external_values: jnp.ndarray,
    month: int | jnp.ndarray,
    rollout_count: int,
) -> jnp.ndarray:
    """A fixed or series-indexed per-rollout amount. `amount_series` is
    a TRACED scalar row index (gathered dynamically), so its value never changes the compiled program —
    see the series-index determinism note in `collect_level_series_keys`. `amount_kind` stays static (a
    genuine FIXED-vs-series code branch)."""
    if amount_kind == AMOUNT_FIXED:
        return jnp.full(rollout_count, amount_fixed, dtype=jnp.int64)
    reset_month = amount_base_month + ((month - amount_base_month) // amount_period) * amount_period
    series_row = external_values[amount_series]  # (rollouts, H+1) — dynamic gather on the traced index
    base_level = series_row[:, amount_base_month]
    reset_level = series_row[:, reset_month]
    return _round_int64(amount_base * reset_level / base_level)


def _amount_values_tuple(
    spec: tuple[int, int, int, int, int],
    series_op: jnp.ndarray,
    external_values: jnp.ndarray,
    month: int | jnp.ndarray,
    r: int,
) -> jnp.ndarray:
    """`_amount_values` from a `(kind, fixed, base, base_month, period)` tuple plus a TRACED series row
    index (`series_op`, gathered dynamically — kept out of the static structure)."""
    kind, fixed, base, base_month, period = spec
    return _amount_values(
        amount_kind=kind,
        amount_fixed=fixed,
        amount_base=base,
        amount_series=series_op,
        amount_base_month=base_month,
        amount_period=period,
        external_values=external_values,
        month=month,
        rollout_count=r,
    )


def _move_cash(
    cash: jnp.ndarray,
    *,
    debit: jnp.ndarray | np.ndarray | int,
    credit: jnp.ndarray | np.ndarray | int,
    amount: jnp.ndarray,
    row_of_world: int,
) -> jnp.ndarray:
    """Move `amount` from the `debit` rows to the `credit` rows. The only way cash moves.

    Every phase that used to write `cash.at[...]` twice and hope the two lines stayed adjacent
    calls this instead, so double entry is a property of the primitive rather than a convention
    each phase re-implements. The reason is #3753: five phases had drifted to one-sided writes,
    each minting or destroying money, and each had to be found by hand. There is nothing to find
    here — a caller names both sides or does not move money.

    `debit`/`credit` are row indices, either a scalar or one per row of `amount`; a scalar side
    against a multi-row `amount` accumulates (that is one counterparty facing many flows, which
    is what `rest_of_world` usually is). `amount` is `(rollouts,)` or `(n, rollouts)`.

    A NEGATIVE row index means "outside the model" and settles against `row_of_world`, so an
    unresolved counterparty conserves cash instead of vanishing into `_scatter_rows`'s dump row.
    Both sides are resolved before the scatter, which is why this uses `.at[]` directly: with no
    sentinel left there is no padding row to slice off.
    """

    flow = jnp.asarray(amount).reshape(-1, cash.shape[-1])

    def rows(side: jnp.ndarray | np.ndarray | int) -> jnp.ndarray:
        resolved = jnp.where(jnp.asarray(side).reshape(-1) < 0, row_of_world, jnp.asarray(side).reshape(-1))
        return jnp.broadcast_to(resolved, (flow.shape[0],))

    return cash.at[rows(debit)].add(-flow).at[rows(credit)].add(flow)


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
        return jnp.broadcast_to(amount_fixed[:, None], (amount_kind.shape[0], rollout_count)).astype(jnp.int64)
    safe_period = jnp.where(amount_period > 0, amount_period, 1)
    reset_month = amount_base_month + ((month - amount_base_month) // safe_period) * safe_period
    safe_series = jnp.where(amount_series >= 0, amount_series, 0)
    rows = jnp.arange(rollout_count)
    base_level = external_values[safe_series[:, None], rows[None, :], amount_base_month[:, None]]
    reset_level = external_values[safe_series[:, None], rows[None, :], reset_month[:, None]]
    series_amount = _round_int64(amount_base[:, None] * reset_level / base_level)
    return jnp.where((amount_kind == AMOUNT_FIXED)[:, None], amount_fixed[:, None], series_amount)


@partial(jax.jit, static_argnames=("row_of_world",))
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
    row_of_world: int,
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
    amounts = jnp.where(fire, raw, 0)
    cash = _move_cash(cash, debit=from_slot, credit=to_slot, amount=amounts, row_of_world=row_of_world)
    ordinary_ytd = _scatter_rows(ordinary_ytd, income_profile, amounts)
    ordinary_ytd = _scatter_rows(ordinary_ytd, deduction_profile, -amounts)
    return cash, ordinary_ytd, fire, amounts


@partial(jax.jit, static_argnames=("row_of_world",))
def _property_cashflows_jit(
    cause: jnp.ndarray,
    amount_kind: jnp.ndarray,
    amount_fixed: jnp.ndarray,
    amount_base: jnp.ndarray,
    amount_series: jnp.ndarray,
    amount_base_month: jnp.ndarray,
    amount_period: jnp.ndarray,
    from_slot: jnp.ndarray,
    to_slot: jnp.ndarray,
    property_slot: jnp.ndarray,
    income_profile: jnp.ndarray,
    deduction_profile: jnp.ndarray,
    property_active: jnp.ndarray,
    cash: jnp.ndarray,
    ordinary_ytd: jnp.ndarray,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: jnp.ndarray,
    row_of_world: int,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    rollout_count = cash.shape[1]
    property_gate = _gather_rows(property_active, property_slot)
    fire = (cause >= 0)[:, None] & active[None, :] & property_gate
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
    amounts = jnp.where(fire, raw, 0)
    cash = _move_cash(cash, debit=from_slot, credit=to_slot, amount=amounts, row_of_world=row_of_world)
    ordinary_ytd = _scatter_rows(ordinary_ytd, income_profile, amounts)
    ordinary_ytd = _scatter_rows(ordinary_ytd, deduction_profile, -amounts)
    return cash, ordinary_ytd, fire, amounts


@partial(jax.jit, static_argnames=("row_of_world", "has_indexed"))
def _bond_cashflows_jit(
    coupon: jnp.ndarray,
    redemption: jnp.ndarray,
    to_slot: jnp.ndarray,
    income_row: jnp.ndarray,
    indexed: jnp.ndarray,
    cpi_series: jnp.ndarray,
    index_base_month: jnp.ndarray,
    period_rate: jnp.ndarray,
    face: jnp.ndarray,
    pays: jnp.ndarray,
    matures: jnp.ndarray,
    on_books: jnp.ndarray,
    cash: jnp.ndarray,
    ordinary_ytd: jnp.ndarray,
    active: jnp.ndarray,
    external_values: jnp.ndarray,
    month: jnp.ndarray,
    row_of_world: int,
    has_indexed: bool,
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """This month's bond cashflows. `coupon`/`redemption` are `(bond,)` slices of compile-time
    tables — at par and held to maturity nothing about a bond depends on a rollout, so there is
    no per-rollout arithmetic here and no bond state in the carry.

    Both reach cash; only the coupon reaches income. Redeeming the face is a return of capital,
    and at par against a par basis it is not a capital gain either, so it touches no tax tensor.

    The issuer is not a modeled agent, so both flows are funded by `from_slot` — the external
    account the rest of the world settles on. Without that debit these would create cash from
    nothing, which is exactly what the conservation test caught the moment it existed.
    """

    # Inflation-indexed (TIPS) branch. A nominal bond's amounts came from the compile-time
    # tables above; an indexed bond's ride its CPI-scaled principal, so they are computed per
    # rollout here. `principal_prev` comes from CPI at month-1 rather than a carried value,
    # which is what keeps bonds out of the scan carry even though they stopped being constant.
    if not has_indexed:
        # No TIPS in this scenario. Skipped statically rather than masked, because a scenario
        # with no sampled series has a ZERO-row `external_values` cube — gathering from it is
        # an error, not a value to discard.
        coupons = jnp.where(active[None, :], coupon[:, None], 0)
        redemptions = jnp.where(active[None, :], redemption[:, None], 0)
        paid = coupons + redemptions
        # The rest of the world funds every coupon and redemption: the issuer is not modeled.
        cash = _move_cash(cash, debit=row_of_world, credit=to_slot, amount=paid, row_of_world=row_of_world)
        return cash, _scatter_rows(ordinary_ytd, income_row, coupons)

    is_indexed = (indexed > 0)[:, None]
    safe_series = jnp.maximum(cpi_series, 0)
    cpi_base = external_values[safe_series, :, index_base_month]
    safe_base = jnp.where(cpi_base > 0, cpi_base, 1.0)
    principal = jnp.round(face[:, None] * external_values[safe_series, :, month] / safe_base).astype(jnp.int64)
    principal_prev = jnp.round(
        face[:, None] * external_values[safe_series, :, jnp.maximum(month - 1, 0)] / safe_base
    ).astype(jnp.int64)

    indexed_coupon = jnp.round(period_rate[:, None] * principal).astype(jnp.int64) * pays[:, None]
    # Deflation floor: a TIPS redeems at the greater of its indexed principal and par, which
    # is what makes it a floor in exactly the scenarios the floor exists for.
    indexed_redemption = jnp.maximum(principal, face[:, None]) * matures[:, None]

    coupons = jnp.where(is_indexed, indexed_coupon, coupon[:, None])
    redemptions = jnp.where(is_indexed, indexed_redemption, redemption[:, None])

    # Phantom income: the month's rise in indexed principal is taxable interest with no cash
    # behind it. Gated on `on_books` so it stops at maturity, and on month > 0 so the opening
    # month does not accrete against itself.
    accretion = jnp.where(is_indexed & (on_books > 0)[:, None] & (month > 0), principal - principal_prev, jnp.int64(0))
    accretion = jnp.where(active[None, :], accretion, 0)

    coupons = jnp.where(active[None, :], coupons, 0)
    redemptions = jnp.where(active[None, :], redemptions, 0)
    paid = coupons + redemptions
    cash = _move_cash(cash, debit=row_of_world, credit=to_slot, amount=paid, row_of_world=row_of_world)
    # Accretion reaches income and NOT cash — if it ever reached the cash tensor, the
    # conservation invariant would break immediately, which is the guard on this wiring.
    return cash, _scatter_rows(ordinary_ytd, income_row, coupons + accretion)


def _fifo_sell_units(
    lot_remaining: jnp.ndarray,
    ordered_lots: np.ndarray,
    target_units: jnp.ndarray,
    unit_price: jnp.ndarray,
    cost_basis_per_unit: jnp.ndarray,
    lot_quantity_scale: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """FIFO-sell a quantity target in lot quanta, returning sold quanta and cent values."""
    ordered_quantity = lot_remaining[:, ordered_lots]
    available_units = ordered_quantity.sum(axis=1)
    oversell = target_units > available_units
    effective_target = jnp.where(oversell, 0, target_units)
    before_units = jnp.cumsum(ordered_quantity, axis=1) - ordered_quantity
    sold_ordered = jnp.clip(effective_target[:, None] - before_units, 0, ordered_quantity)
    ordered_scale = lot_quantity_scale[ordered_lots]
    proceeds_ordered = _value_cents_from_quanta(sold_ordered, unit_price[:, None], ordered_scale[None, :])
    basis_ordered = _value_cents_from_quanta(sold_ordered, cost_basis_per_unit[ordered_lots].T, ordered_scale[None, :])
    zeros = jnp.zeros_like(lot_remaining)
    sold_units = zeros.at[:, ordered_lots].set(sold_ordered)
    proceeds = zeros.at[:, ordered_lots].set(proceeds_ordered)
    basis = zeros.at[:, ordered_lots].set(basis_ordered)
    return sold_units, proceeds, basis


def _apply_tlh_give_back(
    folded_harvest: tuple[_FoldedHarvest, ...],
    tlh_cumulative_harvest: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    sold_units: jnp.ndarray,
    gains: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray]:
    """Repay deferred harvested loss as extra gain on sold
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
        give_back = _scale_money(cumulative, fraction_sold)  # (R,)
        per_lot_weight = jnp.where(
            units_sold[:, None] > 0.0, sold_policy / jnp.where(units_sold[:, None] > 0.0, units_sold[:, None], 1.0), 0.0
        )
        gains = gains.at[:, lot_indices].add(_round_int64(per_lot_weight * give_back[:, None]))
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
    """TLH give-back, then classify each lot's gain
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
    sold = sold_units > 0  # (R, L)
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
    configured_prop_idx: jnp.ndarray,
    configured_has_prop: jnp.ndarray,
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
    configured_property_mask = jnp.where(
        configured_has_prop[:, None], _gather_rows(property_active, configured_prop_idx), True
    )
    property_tax = jnp.broadcast_to(pt_amount[:, None], configured.shape)
    property_mask = _gather_rows(property_active, prop_idx) & (pt_prop_month[:, None] < month)
    principal = _gather_rows(liab_principal, liab_idx)
    mortgage_interest = _scale_money(principal, mort_rate[:, None] / 12.0)
    mortgage = jnp.minimum(_gather_rows(liab_monthly, liab_idx), principal + mortgage_interest)
    mortgage_mask = _gather_rows(liab_active, liab_idx) & (principal > 0) & (mort_prop_month[:, None] < month)
    estimated = jnp.broadcast_to(est_quarterly[:, None], configured.shape)
    actual = trueup_sel @ jnp.where(taxliab_active, taxliab_amount, 0)  # (slots, rollouts)
    safe_harbor = jnp.minimum(est_prior[:, None], actual)
    q4 = jnp.maximum(safe_harbor - _scale_money(est_prior[:, None], 0.75), 0)
    true_up = jnp.maximum(actual - safe_harbor, 0)

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
        default=0,
    )
    kind_mask = jnp.select(
        [
            k == ObligationSource.CONFIGURED_OBLIGATION,
            k == ObligationSource.PROPERTY_TAX,
            k == ObligationSource.MORTGAGE_PAYMENT,
        ],
        [configured_property_mask, property_mask, mortgage_mask],
        default=True,
    )
    slot_active = valid_slot[:, None] & active[None, :] & kind_mask & (amount > 0)
    return slot_active, jnp.where(slot_active, amount, 0)


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
    due_masked = jnp.where(accrual_active, accrual_due, 0)  # (slots, rollouts)
    group_due = group_matrix.astype(due_masked.dtype) @ due_masked  # (slots, rollouts)
    cash_padded = jnp.concatenate([cash, jnp.zeros((1, cash.shape[1]), cash.dtype)], axis=0)
    available = cash_padded[jnp.where(from_slot < 0, cash.shape[0], from_slot)]  # (slots, rollouts), -1 -> 0
    return accrual_active & (available >= group_due - 1e-9)


_ESTIMATED_TAX_KINDS = (ObligationSource.ESTIMATED_TAX, ObligationSource.ESTIMATED_TAX_Q4, ObligationSource.TAX_TRUE_UP)


@partial(jax.jit, static_argnames=("row_of_world",))
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
    row_of_world: int,
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
    paid_amount = jnp.where(paid, accrual_due, 0)
    cash = _move_cash(cash, debit=from_slot, credit=to_slot, amount=paid_amount, row_of_world=row_of_world)
    rented = _gather_rows(property_rented_fraction, property_slot_idx)  # (slots, rollouts)
    property_tax_ytd = _scatter_rows(
        property_tax_ytd,
        property_tax_profile,
        jnp.where(has_property_tax_profile[:, None], _scale_money(paid_amount, 1.0 - rented), 0),
    )
    deductible = jnp.where(has_property_slot[:, None], rented, deductible_fraction[:, None])
    ordinary_ytd = _scatter_rows(
        ordinary_ytd, deduction_profile, jnp.where(has_deduction[:, None], -_scale_money(paid_amount, deductible), 0)
    )
    shortfall = jnp.where(slot_failed, accrual_due, 0)
    failed_this = slot_failed.any(axis=0)
    failed_month = jnp.where(failed_this & (failed_month < 0), month, failed_month)
    failed = failed | failed_this
    return paid, paid_amount, cash, ordinary_ytd, property_tax_ytd, shortfall, slot_failed, failed, failed_month


def _fifo_sell_cents(
    lot_remaining: jnp.ndarray,
    ordered_lots: np.ndarray,
    target_cents: jnp.ndarray,
    unit_price_cents: jnp.ndarray,
    cost_basis_per_unit: jnp.ndarray,
    lot_quantity_scale: jnp.ndarray,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """FIFO sell a cent target, ceiling-rounding to quantity quanta."""
    ordered_quantity = lot_remaining[:, ordered_lots]
    ordered_scale = lot_quantity_scale[ordered_lots]
    available_value = _value_cents_from_quanta(ordered_quantity, unit_price_cents[:, None], ordered_scale[None, :])
    oversell = target_cents > available_value.sum(axis=1)
    effective_target = jnp.where(oversell, 0, target_cents)
    before_value = jnp.cumsum(available_value, axis=1) - available_value
    sold_value_ordered = jnp.clip(effective_target[:, None] - before_value, 0, available_value)
    price_col = unit_price_cents[:, None]
    sold_units_before_clip = _ceil_quanta_for_value_cents(sold_value_ordered, price_col, ordered_scale[None, :])
    sold_units_ordered = jnp.clip(sold_units_before_clip, 0, ordered_quantity)
    proceeds_ordered = _value_cents_from_quanta(sold_units_ordered, price_col, ordered_scale[None, :])
    basis_ordered = _value_cents_from_quanta(
        sold_units_ordered, cost_basis_per_unit[ordered_lots].T, ordered_scale[None, :]
    )
    zeros = jnp.zeros_like(lot_remaining)
    sold_units = zeros.at[:, ordered_lots].set(sold_units_ordered)
    proceeds = zeros.at[:, ordered_lots].set(proceeds_ordered)
    basis = zeros.at[:, ordered_lots].set(basis_ordered)
    return sold_units, proceeds, basis


def _compute_liquid_net_worth(
    owner_cash_mask: jnp.ndarray,
    lot_asset_series_index: jnp.ndarray,
    owner_non_pe_lot_indices: tuple[int, ...],
    cash: jnp.ndarray,
    lot_remaining: jnp.ndarray,
    lot_quantity_scale: jnp.ndarray,
    external_values: jnp.ndarray,
    month: int | jnp.ndarray,
) -> jnp.ndarray:
    """Owner cash + non-PE lot value at current marks.
    `owner_cash_mask` (this policy's row, device) and `lot_asset_series_index` (device) come from
    `_Operands`; `owner_non_pe_lot_indices` is the resolved (host) non-PE lot list (no `plan` reference)."""
    cash_total = (cash * owner_cash_mask[:, None]).sum(axis=0)
    if not owner_non_pe_lot_indices:
        return cash_total
    lot_indices = np.asarray(owner_non_pe_lot_indices, dtype=np.int64)
    series_indices = lot_asset_series_index[lot_indices]
    valid = series_indices >= 0
    prices = external_values[jnp.where(valid, series_indices, 0), :, month]
    prices = _price_usd_to_cents(jnp.nan_to_num(jnp.where(valid[:, None], prices, 0.0), nan=0.0))
    lot_value = _value_cents_from_quanta(
        lot_remaining[lot_indices, :], prices, lot_quantity_scale[lot_indices, None]
    ).sum(axis=0)
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
    quantity_scale_lots: jnp.ndarray,
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
    price_cents = _price_usd_to_cents(price)
    market_value = _value_cents_from_quanta(remaining_lots, price_cents[None, :], quantity_scale_lots[:, None]).sum(
        axis=0
    )
    original_basis = _value_cents_from_quanta(remaining_lots, cost_basis_lots, quantity_scale_lots[:, None]).sum(axis=0)
    adjusted_basis = jnp.maximum(0, original_basis - cumulative)
    safe_mv = jnp.where(market_value > 0, market_value, 1)
    embedded_gain = jnp.clip(jnp.where(market_value > 0, (market_value - adjusted_basis) / safe_mv, 0.0), 0.0, 1.0)
    if has_prior:
        safe_prior = jnp.where(prior_price > 0.0, prior_price, 1.0)
        period_return = jnp.where(prior_price > 0.0, (price - prior_price) / safe_prior, 0.0)
    else:
        period_return = jnp.zeros_like(price)  # month 0: no prior price, treat as flat
    base_monthly = (floor + (peak - floor) * (1.0 - embedded_gain) ** gamma) / 12.0
    fraction = base_monthly * (1.0 + drawdown_sensitivity * jnp.maximum(0.0, -period_return))
    ceiling = jnp.maximum(0, original_basis - cumulative)  # never harvest past available below-basis room
    gross = jnp.where(active, jnp.minimum(_scale_money(market_value, fraction), ceiling), 0)
    stf = min(max(short_term_fraction, 0.0), 1.0)
    short_term = int(CapitalGainClassification.SHORT_TERM)
    long_term = int(CapitalGainClassification.LONG_TERM)
    st_gross = _scale_money(gross, stf)
    lt_gross = gross - st_gross
    capital_gain_ytd = capital_gain_ytd.at[gain_profile, short_term].add(-st_gross)
    capital_gain_ytd = capital_gain_ytd.at[gain_profile, long_term].add(-lt_gross)
    capital_gain_active = capital_gain_active.at[gain_profile, short_term].set(
        capital_gain_active[gain_profile, short_term] | (st_gross > 0)
    )
    capital_gain_active = capital_gain_active.at[gain_profile, long_term].set(
        capital_gain_active[gain_profile, long_term] | (lt_gross > 0)
    )
    return capital_gain_ytd, capital_gain_active, cumulative + gross


def _apply_brackets(amount: jnp.ndarray, *, upper: jnp.ndarray, rate: jnp.ndarray, count: int) -> jnp.ndarray:
    """Progressive bracket tax on `amount`, in int64 cents rounded to the whole cent."""
    if count <= 0:
        return jnp.zeros_like(amount)
    upper_edges = jnp.asarray(upper[:count])
    bracket_rates = jnp.asarray(rate[:count])
    previous_upper = jnp.concatenate([jnp.zeros(1, dtype=upper_edges.dtype), upper_edges[:-1]])
    slice_top = jnp.minimum(amount[:, None], upper_edges[None, :])
    in_bracket = jnp.maximum(slice_top - previous_upper[None, :], 0)
    return _round_int64((in_bracket * bracket_rates[None, :]).sum(axis=1))


def _apply_ltcg_brackets(
    ltcg_amount: jnp.ndarray, ordinary_taxable: jnp.ndarray, *, upper: jnp.ndarray, rate: jnp.ndarray, count: int
) -> jnp.ndarray:
    """LTCG bracket walk with the gain stacked on top of ordinary taxable income (§1(h)): each
    bracket taxes the slice of the combined stack that lies above the ordinary income."""
    if count <= 0:
        return jnp.zeros_like(ltcg_amount)
    upper_edges = jnp.asarray(upper[:count])
    bracket_rates = jnp.asarray(rate[:count])
    previous_upper = jnp.concatenate([jnp.zeros(1, dtype=upper_edges.dtype), upper_edges[:-1]])
    total_taxable = ordinary_taxable + ltcg_amount
    slice_top = jnp.minimum(total_taxable[:, None], upper_edges[None, :])
    slice_bottom = jnp.maximum(ordinary_taxable[:, None], previous_upper[None, :])
    in_bracket = jnp.maximum(slice_top - slice_bottom, 0)
    return _round_int64((in_bracket * bracket_rates[None, :]).sum(axis=1))


def _net_capital_gains_jnp(
    short_term: jnp.ndarray,
    long_term: jnp.ndarray,
    carryforward_in: jnp.ndarray,
    *,
    max_ordinary_offset_cents: int = 300_000,
) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Branch-free §1211/§1212 capital-loss netting for one tax year: cross-net ST against LT, consume
    the prior-year carryforward (short-term first, taxpayer-favorable), then split any residual loss
    into this year's ordinary-income offset and the balance carried forward."""
    st, lt = short_term, long_term
    st_loss_vs_lt_gain = jnp.minimum(jnp.maximum(-st, 0), jnp.maximum(lt, 0))
    st, lt = st + st_loss_vs_lt_gain, lt - st_loss_vs_lt_gain
    lt_loss_vs_st_gain = jnp.minimum(jnp.maximum(-lt, 0), jnp.maximum(st, 0))
    lt, st = lt + lt_loss_vs_st_gain, st - lt_loss_vs_st_gain
    carry = carryforward_in
    used_short_term = jnp.minimum(jnp.maximum(st, 0), carry)
    st, carry = st - used_short_term, carry - used_short_term
    used_long_term = jnp.minimum(jnp.maximum(lt, 0), carry)
    lt, carry = lt - used_long_term, carry - used_long_term
    net_short_term, net_long_term = jnp.maximum(st, 0), jnp.maximum(lt, 0)
    residual_loss = jnp.maximum(-(st + lt), 0) + carry
    ordinary_offset = jnp.minimum(residual_loss, max_ordinary_offset_cents)
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
    """One link's bracket math (MID + SALT + §1250 + LTCG).
    Bracket values / rates / deduction / MID ratio come from the traced `tcfg`; feature flags, counts
    and the §1250 style rate are read from the hashable `_LinkTaxStatic` (no `plan` reference)."""
    link = static.link
    profile = static.profile
    gain_profile = static.gain_profile
    # Sum only the income buckets THIS jurisdiction includes. The mask is compiled from the
    # jurisdiction's own exemption rules, so a Treasury coupon reaches the federal link and not
    # the California one without either link knowing what a Treasury is.
    ordinary = tcfg.link_income_mask[link] @ ordinary_ytd
    ltcg = capital_gain_ytd[gain_profile, CapitalGainClassification.LONG_TERM]
    stcg = capital_gain_ytd[gain_profile, CapitalGainClassification.SHORT_TERM]
    recapture = recapture_section_1250_ytd[profile]
    section_1250_rate = static.section_1250_rate
    standard_deduction = tcfg.link_standard_deduction[link]
    if static.mid_active:
        owner_interest_ytd = liabilities.interest_ytd - liabilities.rental_interest_ytd
        mortgage_interest_deduction = _round_int64(tcfg.mid_principal_ratio[link] @ owner_interest_ytd)
    else:
        mortgage_interest_deduction = _zeros_i64((rollout_count,))
    itemized_deduction = mortgage_interest_deduction + salt_deduction
    deduction_used = jnp.maximum(itemized_deduction, standard_deduction)

    federal_style_section_1250 = section_1250_rate > 0.0
    ordinary_for_brackets = ordinary if federal_style_section_1250 else ordinary + recapture

    ordinary_upper = tcfg.link_ordinary_upper[link]
    ordinary_rate = tcfg.link_ordinary_rate[link]
    ordinary_count = static.ordinary_count
    if static.has_ltcg == 1:
        ordinary_taxable = jnp.maximum(ordinary_for_brackets + stcg - deduction_used, 0)
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
        ordinary_taxable = jnp.maximum(ordinary_for_brackets + ltcg + stcg - deduction_used, 0)
        capital_taxable = _zeros_i64((rollout_count,))
        ordinary_tax = _apply_brackets(ordinary_taxable, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count)
        ltcg_tax = _zeros_i64((rollout_count,))

    if federal_style_section_1250:
        ordinary_tax_with_recapture = _apply_brackets(
            ordinary_taxable + recapture, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count
        )
        implied_recapture_tax = jnp.maximum(ordinary_tax_with_recapture - ordinary_tax, 0)
        section_1250_tax = jnp.minimum(implied_recapture_tax, _scale_money(recapture, section_1250_rate))
    else:
        section_1250_tax = _zeros_i64((rollout_count,))

    capital_tax = ltcg_tax + section_1250_tax
    return mortgage_interest_deduction, itemized_deduction, ordinary_taxable, capital_taxable, ordinary_tax, capital_tax


def _scan_property_sale(
    ev: _FoldedLifecycleEvent,
    home_value_series: jnp.ndarray,
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
    external_cash_slot: int,
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
    # `home_value_series` is a TRACED scalar row index (dynamic gather), not a baked static index.
    series_row = external_values[home_value_series]  # (rollouts, H+1)
    market_value = _scale_money(
        jnp.full(rollout_count, ev.purchase_price, dtype=jnp.int64), series_row[:, month] / series_row[:, 0]
    )
    gross_proceeds = _scale_money(market_value, 1.0 - closing_cost_pct / 100.0)
    capex = property_building_basis[prop] - ev.building_basis_initial
    cum_dep = property_cum_dep[prop]
    realized_gain = gross_proceeds - (ev.purchase_price + capex - cum_dep)
    recapture = jnp.minimum(jnp.maximum(realized_gain, 0), cum_dep)
    post_recapture_gain = jnp.maximum(realized_gain - recapture, 0)
    # §121: months owner-occupied within the trailing 60-month window (the carried ring's column sum).
    qualifies = oo_window[:, prop, :].sum(axis=0) >= SECTION_121_MIN_QUALIFYING_MONTHS
    owner_profile = ev.owner_profile
    exclusion_cap = ev.exclusion_cap
    section_121_exclusion = jnp.where(qualifies, jnp.minimum(post_recapture_gain, exclusion_cap), 0)
    ltcg = post_recapture_gain - section_121_exclusion
    mortgage_payoff = _zeros_i64((rollout_count,))
    for lia in ev.mortgage_liabilities:
        mortgage_payoff = mortgage_payoff + liab_principal[lia]
        liab_principal = liab_principal.at[lia].set(jnp.where(active_property, 0, liab_principal[lia]))
        liab_active = liab_active.at[lia].set(jnp.where(active_property, False, liab_active[lia]))
    net_cash = gross_proceeds - mortgage_payoff
    owner_cash_slot = ev.owner_cash_slot
    if owner_cash_slot >= 0:
        # The buyer of the house is `rest_of_world`. Only the NET crosses that boundary — the
        # payoff extinguishes a liability rather than moving cash of its own.
        cash = _move_cash(
            cash,
            debit=external_cash_slot,
            credit=owner_cash_slot,
            amount=jnp.where(active_property, net_cash, 0),
            row_of_world=external_cash_slot,
        )
    if owner_profile >= 0:
        recapture_ytd = recapture_ytd.at[owner_profile].add(jnp.where(active_property, recapture, 0))
        gain_profile = ev.gain_profile
        if gain_profile >= 0:
            lt = int(CapitalGainClassification.LONG_TERM)
            cg_ytd = cg_ytd.at[gain_profile, lt].add(jnp.where(active_property, ltcg, 0))
            cg_active = cg_active.at[gain_profile, lt].set(cg_active[gain_profile, lt] | active_property)
    property_active = property_active.at[prop].set(property_active[prop] & ~active_property)
    property_rented_fraction = property_rented_fraction.at[prop].set(
        jnp.where(active_property, 0.0, property_rented_fraction[prop])
    )
    property_building_basis = property_building_basis.at[prop].set(
        jnp.where(active_property, 0, property_building_basis[prop])
    )
    sale_trace = (
        jnp.where(active_property, gross_proceeds, 0),
        jnp.where(active_property, mortgage_payoff, 0),
        jnp.where(active_property, net_cash, 0),
        jnp.where(active_property, realized_gain, 0),
        jnp.where(active_property, recapture, 0),
        jnp.where(active_property, section_121_exclusion, 0),
        jnp.where(active_property, ltcg, 0),
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
    """§168 straight-line monthly depreciation,
    branch-free over all properties (one masked elementwise accrual)."""
    monthly_dep = jnp.where(
        (~failed)[None, :] & property_active,
        _scale_money(property_building_basis, property_rented_fraction / (27.5 * 12.0)),
        0,
    )
    return property_cumulative_depreciation + monthly_dep, property_depreciation_ytd + monthly_dep
