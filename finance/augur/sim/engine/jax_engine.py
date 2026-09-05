"""JAX simulation engine: a single always-`lax.scan` device program.

The whole month loop compiles into one `jax.jit` program (`_program_impl`) whose carry is the
per-rollout `_ScanState`; one `lax.scan` over `jnp.arange(horizon)` runs every phase branch-free, and
`run_jax_scan(plan)` returns the stacked scan outputs as one host-resident tree after a single
device→host transfer. The scan covers:
- scheduled / recurring transfers;
- property purchases (cash + mortgage origination);
- scheduled asset sales (FIFO lot matching + capital-gain classification + lot-disposition log);
- target-allocation sales;
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

Caching is JAX-native. `_program_impl` is a module-level `jax.jit` of one registered
`_SimulationProgram` dataclass. JAX flattens its dynamic input tree and includes its immutable,
hashable structural metadata in the native cache key. So an identical-structure plan — including
sweeps over traced numeric values or rollout seeds — reuses the compiled program; only a structural
change recompiles. An opt-in on-disk compilation cache (`AUGUR_JAX_COMPILATION_CACHE_DIR`) carries
that reuse across processes.

Integer accounting note: engine monetary state uses int64 currency quanta and explicit quantity
quanta. JAX x64 is required so those arrays do not silently truncate to int32.

Double-entry note: every write to `cash` moves money between two rows of the same tensor, never
into or out of it. A counterparty the scenario does not model is not a hole — it is
`structure.external_cash_slot`, the `rest_of_world` row. So a phase that pays an unmodeled
contractor debits the owner and credits that row, and one that books sale proceeds credits the
owner and debits it. The sum over all cash rows is therefore invariant, which is what
`test_cash_conservation_e2e` asserts and the only thing that catches a one-sided write: a sale
that credits proceeds with no debit leaves net worth correct while it mints money.
"""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.

# JAX x64 must be enabled before importing jax.numpy, so this module intentionally
# configures JAX between imports.
# ruff: noqa: E402, I001

import os
from dataclasses import dataclass
from functools import partial
from typing import Any, NamedTuple, cast, overload

import jax

jax.config.update("jax_enable_x64", True)

import jax.numpy as jnp
import numpy as np
from jaxtyping import Array, Bool, Float64, Int, Int64

from finance.augur.model.series import PrivateEquityRegimeCode
from finance.augur.product.metric_composition import BASE_METRIC_NAMES, compose_metric, terminal_series
from finance.augur.sim.product_metrics import (
    ProductMetricArrays,
    ProductMetricFanSummary,
    ProductProjectionSummaries,
    ProductTerminalSummary,
)
from finance.augur.product.asset_key import PrivateEquityAssetKey
from finance.augur.product.quantiles import (
    CurrencyQuantileInterpolation,
    currency_quantile_plan,
    interpolate_currency_quantiles,
)
from finance.augur.sim.actor_view import ActorSlots, build_actor_view
from finance.augur.sim.compiler.bonds import BondExecution
from finance.augur.sim.compiler.cashflows import CashflowExecution
from finance.augur.sim.compiler.distributions import DistributionExecution
from finance.augur.sim.compiler.obligations import ObligationExecution, ObligationMetadataExecution
from finance.augur.sim.compiler.private_equity import PEExecutionChannels
from finance.augur.sim.compiler.plan import CompiledSimulation
from finance.augur.sim.compiler.helpers import AMOUNT_FIXED, NO_CODE
from finance.augur.sim.compiler.plan import lot_order_for_pool
from finance.augur.sim.engine.jax_types import (
    _AssetSaleProgram,
    _CapitalGainTarget,
    _DenseProductTailOutput,
    _FoldedHarvest,
    _FoldedLifecycleEvent,
    _FoldedPE,
    _FoldedSleeve,
    _FoldedTargetAllocation,
    _LinkTaxStatic,
    _ProductTailOutput,
    _PurchaseInputs,
    _Static,
)
from finance.augur.sim.output import (
    DenseFinalOutput,
    DenseScanOutput,
    DenseSimulationOutput,
    DenseStateOutput,
    DispositionOutput,
    LifecycleOutput,
    MortgageOutput,
    ObligationOutput,
    PrivateEquityOpportunityOutput,
    PrivateEquityOutput,
    PropertySaleTraceOutput,
    StateOutput,
    TargetAllocationOutput,
    TaxOutput,
    CashflowOutput,
)
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.engine.jax_validation import validate_seed_dependent_inputs
from finance.augur.sim.enums import (
    CapitalGainClassification,
    LifecycleKind,
    PrivateEquityDispositionKind,
    PrivateEquityOpportunityOutcome,
)
from finance.augur.sim.payment_policy import PayActions, PaymentView, decide as decide_payments
from finance.augur.sim.target_allocation import SleeveUniverse, decide
from finance.augur.sim.tlh_harvest import PPB, harvest_fraction_curve_ppb

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


def _round_int64(value: Float64[Array, " *shape"]) -> Int64[Array, " *shape"]:
    value_f = value.astype(jnp.float64)
    rounded = jnp.sign(value_f) * jnp.floor(jnp.abs(value_f) + 0.5)
    return rounded.astype(jnp.int64)


def _zeros_i64(shape: tuple[int, ...]) -> Int64[Array, " ..."]:
    return jnp.zeros(shape, dtype=jnp.int64)


def _scale_money(amount_quanta: Int64[Array, " ..."], factor: Float64[Array, " ..."] | float) -> Int64[Array, " ..."]:
    """Apply a dimensionless factor without converting money to floating point."""

    factor_numerator = _round_int64(jnp.asarray(factor, dtype=jnp.float64) * MONEY_FACTOR_SCALE)
    return _scale_quanta_by_ratio(amount_quanta, factor_numerator, jnp.int64(MONEY_FACTOR_SCALE))


def _sum_money_with_factors(
    amount_quanta: Int64[Array, " ..."], factor_numerator: Int64[Array, " ..."], *, axis: int
) -> Int64[Array, " ..."]:
    """Sum non-negative scaled-money terms, then round the aggregate once."""

    amount_quotient = amount_quanta // MONEY_FACTOR_SCALE
    amount_remainder = amount_quanta % MONEY_FACTOR_SCALE
    whole = amount_quotient * factor_numerator
    fractional_product = amount_remainder * factor_numerator
    term_quotient = whole + fractional_product // MONEY_FACTOR_SCALE
    term_remainder = fractional_product % MONEY_FACTOR_SCALE
    quotient_sum = term_quotient.sum(axis=axis)
    remainder_sum = term_remainder.sum(axis=axis)
    return (
        quotient_sum
        + remainder_sum // MONEY_FACTOR_SCALE
        + (remainder_sum % MONEY_FACTOR_SCALE >= (MONEY_FACTOR_SCALE + 1) // 2).astype(jnp.int64)
    )


def _sum_scaled_money(
    amount_quanta: Int64[Array, " ..."], factor: Float64[Array, " ..."], *, axis: int
) -> Int64[Array, " ..."]:
    factor_numerator = _round_int64(jnp.asarray(factor, dtype=jnp.float64) * MONEY_FACTOR_SCALE)
    return _sum_money_with_factors(amount_quanta, factor_numerator, axis=axis)


def _scale_money_by_float_ratio(
    amount_quanta: Int64[Array, " ..."] | int, numerator: Float64[Array, " ..."], denominator: Float64[Array, " ..."]
) -> Int64[Array, " ..."]:
    """Apply a sampled non-money level ratio while money remains integer quanta."""

    amount = jnp.asarray(amount_quanta, dtype=jnp.int64)
    numerator_fixed = _round_int64(jnp.asarray(numerator, dtype=jnp.float64) * MONEY_FACTOR_SCALE)
    denominator_fixed = _round_int64(jnp.asarray(denominator, dtype=jnp.float64) * MONEY_FACTOR_SCALE)
    return _scale_quanta_by_ratio(amount, numerator_fixed, denominator_fixed)


def _value_quanta_from_quantity(
    quantity_quanta: Int64[Array, " ..."], unit_price_quanta: Int64[Array, " ..."], quantity_scale: Int64[Array, " ..."]
) -> Int64[Array, " ..."]:
    """Nearest-half-up money value with no float or overflowing direct product."""

    return _scale_quanta_by_ratio(quantity_quanta, unit_price_quanta, quantity_scale)


def _ceil_quantity_for_quanta(
    value_quanta: Int64[Array, " ..."], unit_price_quanta: Int64[Array, " ..."], quantity_scale: Int64[Array, " ..."]
) -> Int64[Array, " ..."]:
    denominator = jnp.where(unit_price_quanta > 0, unit_price_quanta, 1)
    numerator = jnp.maximum(value_quanta, 0) * quantity_scale
    return (numerator + denominator - 1) // denominator


def _scale_quanta_by_ratio(
    amount_quanta: Int64[Array, " ..."], numerator: Int64[Array, " ..."], denominator: Int64[Array, " ..."]
) -> Int64[Array, " ..."]:
    """Apply an integer ratio with nearest-half-up rounding and no large money product.

    Quotient/remainder decomposition avoids evaluating ``amount * numerator`` directly.
    The compiler still owns the normal int64 result-range validation.
    """

    safe_denominator = jnp.where(denominator > 0, denominator, 1)
    sign = jnp.where((amount_quanta < 0) ^ (numerator < 0), -1, 1)
    absolute_amount = jnp.abs(amount_quanta)
    absolute_numerator = jnp.abs(numerator)
    common_factor = jnp.gcd(absolute_numerator, safe_denominator)
    reduced_numerator = absolute_numerator // jnp.where(common_factor > 0, common_factor, 1)
    reduced_denominator = safe_denominator // jnp.where(common_factor > 0, common_factor, 1)
    amount_quotient = absolute_amount // reduced_denominator
    amount_remainder = absolute_amount % reduced_denominator
    whole = amount_quotient * reduced_numerator
    fractional_product = amount_remainder * reduced_numerator
    fractional_quotient = fractional_product // reduced_denominator
    fractional_remainder = fractional_product % reduced_denominator
    rounded_fraction = fractional_quotient + (fractional_remainder >= (reduced_denominator + 1) // 2).astype(jnp.int64)
    return sign * (whole + rounded_fraction)


class _ScanState(NamedTuple):
    """`run_jax_scan`'s carry pytree (NamedTuple → native JAX pytree). Grown field-by-field as the
    fold covers more phases; per-rollout state is `(entity, rollouts)` except the failure vectors."""

    cash: Int64[Array, " cash rollout"]
    ordinary_ytd: Int64[Array, " income_bucket rollout"]
    property_tax_ytd: Int64[Array, " tax_profile rollout"]
    lot_remaining: Int64[Array, " lot rollout"]
    # `(lot, R)`: per-rollout because a purchased lot's basis is the price its rollout paid.
    # Initial lots broadcast their configured basis and never change it.
    cost_basis_per_unit: Int64[Array, " lot rollout"]
    # `(lot, R)`, and per-rollout for the same reason: once a policy decides WHEN to buy, a
    # slot fills in a different month in each rollout, and the holding period that decides
    # long vs short gain is measured from it. FIFO ORDER does not depend on this — a sleeve's
    # slots fill monotonically, so slot index is the order in every rollout, and an unfilled
    # slot holds zero units so a walk reaching it early takes nothing.
    lot_purchase_month: Int64[Array, " lot rollout"]
    capital_gain_active: Bool[Array, " capital_gain_profile gain_class rollout"]
    capital_gain_ytd: Int64[Array, " capital_gain_profile gain_class rollout"]
    tlh: Int64[Array, " harvest_policy rollout"]
    property_active: Bool[Array, " property rollout"]
    property_basis: Int64[Array, " property rollout"]
    property_contribution: Int64[Array, " property rollout"]
    property_equity: Int64[Array, " property rollout"]
    property_cumulative_depreciation: Int64[Array, " property rollout"]
    property_owner_occupied_months: Int64[Array, " property rollout"]
    property_depreciation_ytd: Int64[Array, " property rollout"]
    property_rented_fraction: Float64[Array, " property rollout"]  # mutable: lifecycle FRACTION/SALE events change it
    property_building_basis: Int64[
        Array, " property rollout"
    ]  # mutable: lifecycle CAPITAL_IMPROVEMENT/SALE events change it
    owner_occupied_window: Bool[Array, " lookback property rollout"]  # ring of monthly owner-occupancy flags (§121)
    liability_active: Bool[Array, " liability rollout"]
    liability_principal: Int64[Array, " liability rollout"]
    liability_monthly_payment: Int64[Array, " liability rollout"]
    liability_interest_ytd: Int64[Array, " liability rollout"]
    liability_rental_interest_ytd: Int64[Array, " liability rollout"]
    capital_loss_carryforward: Int64[Array, " capital_gain_profile rollout"]
    recapture_section_1250_ytd: Int64[Array, " tax_profile rollout"]
    tax_liability_active: Bool[Array, " tax_liability rollout"]
    tax_liability_amount: Int64[Array, " tax_liability rollout"]
    failed: Bool[Array, " rollout"]
    failed_month: Int64[Array, " rollout"]
    # Scheduled-sale dispositions accumulated in-carry (`(scheduled_sale, lot, R)`): each sale fires
    # once, so accumulating at the firing month collapses the per-month horizon axis the old ys emitted.
    sale_disp_units: Int64[Array, " scheduled_sale lot rollout"]
    sale_disp_basis: Int64[Array, " scheduled_sale lot rollout"]
    sale_disp_proceeds: Int64[Array, " scheduled_sale lot rollout"]
    sale_oversell: Bool[Array, ""]  # any scheduled sale oversold its pool (post-scan hard error)
    # `(policy, sleeve, R)`: how many purchases a target-allocation sleeve has WANTED so far.
    # The cursor and the exhaustion counter are the same number — it names the next slot to
    # fill, and it keeps counting past the last one, so a run that needed more slots than it
    # was configured says so (post-scan hard error) instead of silently dropping purchases.
    ta_buy_count: Int64[Array, " policy sleeve rollout"]


class _TracedConfig(NamedTuple):
    """JAX-native typed bundle of the swept numeric config the compiled program takes as TRACED inputs
    (a NamedTuple → native JAX pytree, so it passes through `jax.jit` typed). The cores read VALUES from
    here (`jax.Array`s) while reading structure / feature flags / counts / slot indices from the
    concrete `plan` — so nothing puns a traced array into the compiler's NumPy-typed plan fields. Each
    field is a swept numeric value (not baked structure), so sweeping it reuses the compiled program."""

    link_standard_deduction: Int64[Array, " tax_link"]
    link_income_mask: Int64[Array, " tax_link income_bucket"]
    link_ordinary_upper: Int64[Array, " tax_link bracket"]
    link_ordinary_rate: Float64[Array, " tax_link bracket"]
    link_ltcg_upper: Int64[Array, " tax_link bracket"]
    link_ltcg_rate: Float64[Array, " tax_link bracket"]
    mid_principal_factor: Int64[Array, " tax_link liability"]
    cost_basis_per_unit: Int64[Array, " lot"]
    # Month-0 opening value of the carried per-rollout purchase month; policy-chosen
    # purchases overwrite their slot's entry when they fill it.
    lot_purchase_month: Int64[Array, " lot"]
    cash_initial_balance: Int64[Array, " cash"]
    lot_initial_quantity: Int64[Array, " lot"]
    property_adjusted_basis: Int64[Array, " property"]
    property_equity_ledger: Int64[Array, " property"]


@dataclass(frozen=True)
class _ProductSummaryStatic:
    has_public_lots: bool
    has_pe_lots: bool
    has_properties: bool
    has_bonds: bool


class _ProductSummaryInputs(NamedTuple):
    cash_mask: Bool[Array, " cash"]
    public_lot_mask: Bool[Array, " lot"]
    pe_lot_mask: Bool[Array, " lot"]
    pe_lot_issuer: Int64[Array, " lot"]
    property_mask: Bool[Array, " property"]
    property_purchase_month: Int64[Array, " property"]
    property_purchase_price: Int64[Array, " property"]
    property_home_value_series: Int64[Array, " property"]
    liability_mask: Bool[Array, " liability"]
    primary_obligation_mask: Bool[Array, " obligation"]
    # Face in cents, zeroed for bonds the primary agent does not hold, and the (H+1, bond)
    # on-books mask. Both compile-time constants — a par bond held to maturity has no
    # rollout-varying value.
    bond_face: Int64[Array, " bond"]
    bond_on_books: Bool[Array, " month bond"]
    # Indexation inputs, so a TIPS is carried at its CPI-scaled principal rather than at par.
    # Valuing an indexed bond at face would understate it in exactly the inflationary
    # scenarios the ladder is held for.
    bond_indexed: Bool[Array, " bond"]
    bond_cpi_series: Int64[Array, " bond"]
    bond_index_base_month: Int64[Array, " bond"]


def _traced_config(plan: CompiledSimulation) -> _TracedConfig:
    """Build the traced-config bundle of swept numeric values from the (concrete) plan."""
    return _TracedConfig(
        link_standard_deduction=jnp.asarray(plan.tax.link_standard_deduction),
        link_income_mask=jnp.asarray(plan.tax.link_income_mask),
        link_ordinary_upper=jnp.asarray(plan.tax.link_ordinary_upper),
        link_ordinary_rate=jnp.asarray(plan.tax.link_ordinary_rate),
        link_ltcg_upper=jnp.asarray(plan.tax.link_ltcg_upper),
        link_ltcg_rate=jnp.asarray(plan.tax.link_ltcg_rate),
        mid_principal_factor=jnp.asarray(plan.mid.principal_factor),
        cost_basis_per_unit=jnp.asarray(plan.lot_cost_basis_per_unit),
        lot_purchase_month=jnp.asarray(plan.lot_purchase_month),
        cash_initial_balance=jnp.asarray(plan.cash_initial_balance),
        lot_initial_quantity=jnp.asarray(plan.lot_initial_quantity),
        property_adjusted_basis=jnp.asarray(plan.properties.adjusted_basis),
        property_equity_ledger=jnp.asarray(plan.properties.equity_ledger),
    )


def _asset_sale_program(plan: CompiledSimulation) -> _AssetSaleProgram:
    """Pack scheduled sales with their host-resolved FIFO topology."""
    sales = plan.sales
    sale_rows = [sale for sale in range(sales.month.shape[0]) if int(sales.month[sale]) >= 0]
    ordered_lot_rows = [
        tuple(
            int(lot)
            for lot in lot_order_for_pool(
                lot_agent_codes=plan.lot_agent_codes,
                lot_account_codes=plan.lot_account_codes,
                lot_asset_codes=plan.lot_asset_codes,
                lot_fifo_rank=plan.lot_fifo_rank,
                lot_id_codes=plan.lot_id_codes,
                agent_code=int(sales.agent[sale]),
                account_code=int(sales.source_account[sale]),
                asset_code=int(sales.asset[sale]),
            )
        )
        for sale in sale_rows
    ]
    count = len(sale_rows)
    pool_width = max((len(row) for row in ordered_lot_rows), default=1)
    ordered_lots = np.full((count, pool_width), plan.slot_plan.lot_count, dtype=np.int64)
    for row_index, row in enumerate(ordered_lot_rows):
        ordered_lots[row_index, : len(row)] = np.asarray(row, dtype=np.int64)

    pool_keys = [
        (int(sales.agent[sale]), int(sales.source_account[sale]), int(sales.asset[sale])) for sale in sale_rows
    ]
    same_pool_prior = np.zeros((count, count), dtype=np.int64)
    for current in range(count):
        for prior in range(current):
            if pool_keys[prior] == pool_keys[current]:
                same_pool_prior[current, prior] = 1

    capital_gain_map = np.array(
        [(plan.capital_gain_agent_codes == int(sales.agent[sale])) for sale in sale_rows], dtype=np.int64
    ).reshape(count, plan.slot_plan.capital_gain_agent_count)
    harvest_policies = plan.harvest_policies
    active_harvest_policy = (harvest_policies.gain_profile_index >= 0)[:, None]
    tlh_policy_lot_mask = (harvest_policies.lot_mask & active_harvest_policy).astype(np.int64)

    return _AssetSaleProgram(
        month=jnp.asarray([int(sales.month[sale]) for sale in sale_rows], dtype=jnp.int32),
        quantity=jnp.asarray([int(sales.quantity[sale]) for sale in sale_rows], dtype=jnp.int64),
        same_pool_prior=jnp.asarray(same_pool_prior),
        capital_gain_map=jnp.asarray(capital_gain_map),
        tlh_policy_lot_mask=jnp.asarray(tlh_policy_lot_mask),
        price_fixed=jnp.asarray([int(sales.price_fixed[sale]) for sale in sale_rows], dtype=jnp.int64),
        price_series=jnp.asarray([int(sales.price_series[sale]) for sale in sale_rows], dtype=jnp.int64),
        proceeds_slot=tuple(int(sales.proceeds_slot[sale]) for sale in sale_rows),
        buffer_index=tuple(sale_rows),
        ordered_lots=tuple(tuple(int(lot) for lot in row) for row in ordered_lots),
    )


class _Operands(NamedTuple):
    """Device arrays the scan closes over, nested in `_SimulationProgram.dynamic`.

    JAX keys the native compile cache on their avals, so identical shapes/dtypes reuse the executable
    when values change—without hand-rolled hashing of array contents.
    """

    # Carry-init device constants.
    ordinary0: Int64[Array, " income_bucket rollout"]
    property_tax_ytd0: Int64[Array, " tax_profile rollout"]
    cg_active0: Bool[Array, " capital_gain_profile gain_class rollout"]
    cg_ytd0: Int64[Array, " capital_gain_profile gain_class rollout"]
    tlh0: Int64[Array, " harvest_policy rollout"]
    property_rented_fraction_0: Float64[Array, " property rollout"]
    property_building_basis_0: Int64[Array, " property rollout"]
    prop0: Bool[Array, " property rollout"]
    liab0: Bool[Array, " liability rollout"]
    # Whole-horizon static tables sliced by the traced month.
    cashflows: CashflowExecution[jax.Array]
    bonds: BondExecution[jax.Array]
    distributions: DistributionExecution[jax.Array]
    obligations: ObligationExecution[jax.Array]
    purchases: _PurchaseInputs
    # Year-end / property tables.
    property_is_primary_table: Bool[Array, " month property"]
    tax_slot_table: Int64[Array, " month tax_link"]
    salt_cap_table: Int64[Array, " tax_link month"]
    # Device arrays the bodies + de-`plan`-ed cores read directly.
    capital_gain_agent_codes: Int64[Array, " capital_gain_profile"]
    cg_rep_profile: Int64[Array, " capital_gain_profile"]
    cg_offset_cap: Int64[Array, " capital_gain_profile"]
    property_owner_ordinary_row: Int64[Array, " property"]
    liability_owner_profile_index: Int64[Array, " liability"]
    salt_contributing_mask: Int64[Array, " tax_link other_tax_link"]
    lot_asset_series_index: Int64[Array, " lot"]
    lot_quantity_scale: Int64[Array, " lot"]
    pe_owner_cash_mask: Bool[Array, " policy cash"]
    # Series-axis row indices, traced (dynamic gather), in phase-loop order. See `_build_program`.
    pe_floor_series: Int64[Array, " issuer"]
    harvest_series: Int64[Array, " harvest_policy"]
    lifecycle_sale_series: Int64[Array, " event"]
    ta_floor_series: Int64[Array, " policy"]
    ta_ceiling_series: Int64[Array, " policy"]
    # Per-policy, per-sleeve, per-pool price rows. Ragged twice over, so a list of lists.
    # Per policy, a `(sleeve,)` series row. Per SLEEVE, not per pool: every pool in a sleeve
    # holds the same asset, so a per-pool list was the same price repeated.
    ta_sleeve_series: list[Int64[Array, " sleeve"]]
    # Per policy, a `(sleeve,)` weight row. TRACED, not folded into `_Static`: a sleeve weight is
    # swept numeric config, and baking it into the static key made every distinct weight vector a
    # separate XLA compile — so an allocation sweep paid one full compile PER ARM.
    ta_sleeve_weights: list[Int64[Array, " sleeve"]]


class _ProgramDynamic(NamedTuple):
    """Traced components; registered children may carry their own static topology."""

    external_values: Float64[Array, " series rollout snapshot"]
    external_money_values: Int64[Array, " series rollout snapshot"]
    pe_channels: PEExecutionChannels[jax.Array]
    swept: _TracedConfig
    asset_sales: _AssetSaleProgram
    operands: _Operands
    product_inputs: _ProductSummaryInputs | None


@dataclass(frozen=True)
class _ProgramStatic:
    """Hashable topology and output mode included by value in JAX's cache key."""

    structure: _Static
    product_summary: _ProductSummaryStatic | None
    emit_dense: bool


@partial(jax.tree_util.register_dataclass, data_fields=("dynamic",), meta_fields=("static",))
@dataclass(frozen=True)
class _SimulationProgram:
    """The single JIT boundary: dynamic leaves plus immutable static metadata."""

    dynamic: _ProgramDynamic
    static: _ProgramStatic


def run_jax_scan(plan: CompiledSimulation) -> DenseSimulationOutput:
    """Single-program `lax.scan` engine: the whole month loop compiles into one XLA program (one
    dispatch for all months) whose only traced inputs are the seed-varying series and swept numeric
    config. `_build_program` packs the registered device-program PyTree; the module-level JIT compiles
    it on first invocation."""
    validate_seed_dependent_inputs(plan)

    program = _build_program(plan)
    ys, final_state = _program_impl(program)
    return _dense_output_from_device(plan, program.static.structure, ys, final_state)


def run_jax_scan_with_product_metrics(
    plan: CompiledSimulation, *, primary_agent_id: str
) -> tuple[DenseSimulationOutput, ProductMetricArrays]:
    """Return dense output and the selected actor's metrics from the same scan."""

    validate_seed_dependent_inputs(plan)
    product_static, product_inputs = _product_summary_inputs(plan, primary_agent_id=primary_agent_id)
    program = _build_program(plan, product_summary=product_static, product_inputs=product_inputs, emit_dense=True)
    outputs, final_state = _program_impl(program)
    dense_ys, product_ys = outputs
    initial_ys, monthly_ys = product_ys
    output = _dense_output_from_device(plan, program.static.structure, dense_ys, final_state.dense)
    metrics = _product_metric_arrays_from_device(plan, initial_ys, monthly_ys, final_state.failed_month)
    return output, metrics


def _dense_output_from_device(
    plan: CompiledSimulation,
    structure: _Static,
    ys: DenseScanOutput[jax.Array],
    final_state: DenseFinalOutput[jax.Array],
) -> DenseSimulationOutput:
    """Convert the device output tree into the sole host-side dense result."""

    # JAX's type stub preserves the device-array parameter even though device_get returns
    # NumPy arrays on the CPU host. This is the one explicit typing handoff at that boundary.
    host_ys, host_final_state = cast(
        tuple[DenseScanOutput[np.ndarray], DenseFinalOutput[np.ndarray]], jax.device_get((ys, final_state))
    )
    if bool(np.asarray(host_final_state.sale_oversell)):
        raise ValueError("scheduled asset sale exceeds available lots")
    _check_purchase_slot_exhaustion(plan, np.asarray(host_final_state.target_allocation_buy_count))

    p = plan.slot_plan
    r = p.rollout_count
    state = host_ys.state
    cash0 = np.broadcast_to(plan.cash_initial_balance[:, None], (p.cash_count, r))
    lot0 = np.broadcast_to(plan.lot_initial_quantity[:, None], (p.lot_count, r))
    state_history = cast(StateOutput[np.ndarray], jax.tree.map(_prepend_zero_snapshot, state))._replace(
        cash=_prepend_snapshot(state.cash, cash0),
        lots=_prepend_snapshot(state.lots, lot0),
        property_owner_occupied_months=_prepend_zero_snapshot(state.property_owner_occupied_months, dtype=np.int64),
        failed_month=_prepend_snapshot(state.failed_month, np.full((r,), NO_CODE, dtype=np.int64), dtype=np.int64),
    )
    # Purchase output already uses the compiler's full property axis, including its zero-property
    # sentinel row. Keep that axis intact through the scan and host boundary; there is no folded
    # purchase remap to reconstruct here.
    purchase_active = np.asarray(host_ys.property_purchases, dtype=np.bool_)

    lifecycle_fired = _event_rows(
        np.asarray(host_ys.lifecycle.fired),
        tuple((event.event_index, event.month) for event in structure.folded_lifecycle),
        row_count=int(plan.lifecycle_events.month.shape[0]),
        rollout_count=r,
    )
    sale_traces = host_ys.lifecycle.property_sales
    sale_mapping = structure.folded_sale_events
    property_sales = PropertySaleTraceOutput(
        *(
            _event_rows(
                np.asarray(values), sale_mapping, row_count=int(plan.lifecycle_events.month.shape[0]), rollout_count=r
            )
            for values in sale_traces
        )
    )
    primary_residence_fired = _event_rows(
        np.asarray(host_ys.primary_residence_fired),
        structure.folded_pr,
        row_count=int(plan.primary_residence_events.month.shape[0]),
        rollout_count=r,
    )
    taxes = host_ys.taxes._replace(
        breakdown=np.moveaxis(np.asarray(host_ys.taxes.breakdown), 1, 0),
        settlement_year_end=np.asarray(host_ys.taxes.settlement_year_end, dtype=np.int64),
    )
    private_equity = host_ys.private_equity._replace(
        opportunities=host_ys.private_equity.opportunities._replace(
            active=np.asarray(host_ys.private_equity.opportunities.active, dtype=np.bool_),
            outcome=np.asarray(host_ys.private_equity.opportunities.outcome, dtype=np.int64),
        )
    )
    target_allocation = host_ys.target_allocation._replace(
        obligation_attempt_policy=np.asarray(host_ys.target_allocation.obligation_attempt_policy, dtype=np.int64)
    )
    return DenseSimulationOutput(
        state=DenseStateOutput(
            lot_cost_basis=np.asarray(host_final_state.lot_cost_basis),
            lot_purchase_month=np.asarray(host_final_state.lot_purchase_month, dtype=np.int64),
            **state_history._asdict(),
        ),
        cashflows=host_ys.cashflows,
        obligations=host_ys.obligations,
        property_purchases=purchase_active,
        mortgages=host_ys.mortgages,
        taxes=taxes,
        scheduled_dispositions=host_final_state.scheduled_dispositions,
        target_allocation=target_allocation,
        private_equity=private_equity,
        lifecycle=LifecycleOutput(fired=lifecycle_fired, property_sales=property_sales),
        primary_residence_fired=primary_residence_fired,
    )


def _prepend_snapshot(values: Any, initial: np.ndarray, *, dtype: Any | None = None) -> np.ndarray:
    history = np.asarray(values, dtype=dtype)
    initial_array = np.asarray(initial, dtype=history.dtype)
    return np.concatenate((initial_array[None, ...], history), axis=0)


def _prepend_zero_snapshot(values: Any, *, dtype: Any | None = None) -> np.ndarray:
    history = np.asarray(values, dtype=dtype)
    initial = np.zeros(history.shape[1:], dtype=history.dtype)
    return np.concatenate((initial[None, ...], history), axis=0)


def _event_rows(
    values: np.ndarray, mapping: tuple[tuple[int, int], ...], *, row_count: int, rollout_count: int
) -> np.ndarray:
    rows = np.zeros((row_count, rollout_count), dtype=values.dtype)
    for position, (row, month) in enumerate(mapping):
        rows[row] = values[month, position]
    return rows


def _check_purchase_slot_exhaustion(plan: CompiledSimulation, ta_buy_count: np.ndarray) -> None:
    """Abort rather than silently dropping purchases after a policy exhausts its lot slots."""

    if not ta_buy_count.size:
        return
    configured = (plan.target_allocation_purchase_slots >= 0).sum(axis=2)
    wanted = ta_buy_count.max(axis=2)
    over = np.argwhere(wanted > configured)
    if not over.size:
        return
    policy_idx, sleeve_idx = (int(x) for x in over[0])
    prefixes = plan.target_allocation_policies.cause_id_prefixes
    raise ValueError(
        f"target-allocation policy {prefixes[policy_idx]!r} sleeve {sleeve_idx} ran out of purchase slots: "
        f"{int(configured[policy_idx, sleeve_idx])} configured, {int(wanted[policy_idx, sleeve_idx])} needed. "
        "Raise `purchase_slots_per_sleeve` — every purchase needs its own lot, because it has its own "
        "basis and its own holding period."
    )


class _ProductMetricFanDeviceSummary(NamedTuple):
    """Device-side order statistics needed to build one exact metric fan."""

    failed_count: Int64[Array, ""]
    monthly_lower: Int64[Array, " snapshot percentile"]
    monthly_upper: Int64[Array, " snapshot percentile"]
    terminal_lower: Int64[Array, " percentile"]
    terminal_upper: Int64[Array, " percentile"]


# The base metrics the scan emits per month, in the order `product_metrics` returns them.
# The wire's derived metrics (home_equity, liquid_net_worth, net_worth) are composed from
# these by `product.metric_composition`, which is also what the decode path uses — the sums
# are defined once so a new asset class cannot reach one and miss the other.
_PRODUCT_BASE_METRICS = BASE_METRIC_NAMES
_PRODUCT_BASE_INDEX = {name: index for index, name in enumerate(_PRODUCT_BASE_METRICS)}


def _product_metric_series(
    metric: str,
    initial_ys: tuple[Int64[Array, " rollout"], ...],
    monthly_ys: tuple[Int64[Array, " month rollout"], ...],
) -> Int64[Array, " snapshot rollout"]:
    """Full (H+1, R) device series for one product metric.

    `base` is passed as a callable so only the series the requested metric needs are
    assembled: a single-metric fan never materializes all of them.
    """

    def base(name: str) -> Int64[Array, " snapshot rollout"]:
        index = _PRODUCT_BASE_INDEX[name]
        return jnp.concatenate([jnp.asarray(initial_ys[index])[None, :], jnp.asarray(monthly_ys[index])], axis=0)

    return compose_metric(metric, base)


def _product_metric_arrays_from_device(
    plan: CompiledSimulation,
    initial_ys: tuple[Int64[Array, " rollout"], ...],
    monthly_ys: tuple[Int64[Array, " month rollout"], ...],
    final_failed_month: Int64[Array, " rollout"],
) -> ProductMetricArrays:
    """Copy JAX-emitted base series into the selected-rollout host read model."""

    base_series = tuple(
        jnp.concatenate([jnp.asarray(initial)[None, :], jnp.asarray(monthly)], axis=0)
        for initial, monthly in zip(initial_ys, monthly_ys, strict=True)
    )
    *base_series_host, failed_month = jax.device_get((*base_series, final_failed_month))
    return ProductMetricArrays(
        month_index=np.arange(plan.horizon_months + 1, dtype=np.int64),
        failed_month=np.asarray(failed_month, dtype=np.int64),
        currency_code=plan.currency_code,
        currency_quantum=format(plan.currency_quantum, "f"),
        base_series=tuple(np.asarray(series, dtype=np.int64) for series in base_series_host),
    )


def run_jax_product_metric_arrays(plan: CompiledSimulation, *, primary_agent_id: str) -> ProductMetricArrays:
    """Return every base metric directly from the JAX reducer without dense output."""

    validate_seed_dependent_inputs(plan)
    product_static, product_inputs = _product_summary_inputs(plan, primary_agent_id=primary_agent_id)
    program = _build_program(plan, product_summary=product_static, product_inputs=product_inputs, emit_dense=False)
    product_ys, product_tail = _program_impl(program)
    oversell_host, buy_count_host = jax.device_get(
        (product_tail.sale_oversell, product_tail.target_allocation_buy_count)
    )
    _validate_product_tail(plan, oversell_host, buy_count_host)
    initial_ys, monthly_ys = product_ys
    return _product_metric_arrays_from_device(plan, initial_ys, monthly_ys, product_tail.failed_month)


def _run_jax_product_series(
    plan: CompiledSimulation, *, primary_agent_id: str, metric: str
) -> tuple[Int64[Array, " snapshot rollout"], _ProductTailOutput]:
    """Execute the product reducer once and materialize the requested metric series."""
    validate_seed_dependent_inputs(plan)
    product_static, product_inputs = _product_summary_inputs(plan, primary_agent_id=primary_agent_id)
    program = _build_program(plan, product_summary=product_static, product_inputs=product_inputs, emit_dense=False)
    product_ys, product_tail = _program_impl(program)
    initial_ys, monthly_ys = product_ys
    return _product_metric_series(metric, initial_ys, monthly_ys), product_tail


def _product_metric_fan_device_summary(
    plan: CompiledSimulation,
    *,
    metric: str,
    percentiles: tuple[float, ...],
    series: Int64[Array, " snapshot rollout"],
    terminal: Int64[Array, " rollout"],
    failed_month: Int64[Array, " rollout"],
) -> tuple[tuple[CurrencyQuantileInterpolation, ...], _ProductMetricFanDeviceSummary]:
    """Select exact quantile brackets on-device for one metric fan."""
    quantile_plan = currency_quantile_plan(plan.rollout_count, percentiles)
    lower_indices = jnp.asarray([item.lower_index for item in quantile_plan], dtype=jnp.int32)
    upper_indices = jnp.asarray([item.upper_index for item in quantile_plan], dtype=jnp.int32)
    ordered = jnp.sort(series, axis=1)
    monthly_lower = ordered[:, lower_indices]
    monthly_upper = ordered[:, upper_indices]
    if metric == "shortfall_quanta":
        ordered_terminal = jnp.sort(terminal)
        terminal_lower = ordered_terminal[lower_indices]
        terminal_upper = ordered_terminal[upper_indices]
    else:
        terminal_lower = monthly_lower[-1]
        terminal_upper = monthly_upper[-1]
    return quantile_plan, _ProductMetricFanDeviceSummary(
        failed_count=(failed_month >= 0).sum(),
        monthly_lower=monthly_lower,
        monthly_upper=monthly_upper,
        terminal_lower=terminal_lower,
        terminal_upper=terminal_upper,
    )


def _product_metric_fan_summary_from_device(
    plan: CompiledSimulation,
    *,
    percentiles: tuple[float, ...],
    quantile_plan: tuple[CurrencyQuantileInterpolation, ...],
    device_summary: _ProductMetricFanDeviceSummary,
) -> ProductMetricFanSummary:
    """Build the host read model from transferred quantile brackets."""
    return ProductMetricFanSummary(
        month_index=np.arange(plan.horizon_months + 1, dtype=np.int64),
        failed_count=int(device_summary.failed_count),
        currency_code=plan.currency_code,
        currency_quantum=format(plan.currency_quantum, "f"),
        percentiles=percentiles,
        terminal_percentiles=interpolate_currency_quantiles(
            np.asarray(device_summary.terminal_lower, dtype=np.int64),
            np.asarray(device_summary.terminal_upper, dtype=np.int64),
            quantile_plan,
        ),
        monthly_percentiles=interpolate_currency_quantiles(
            np.asarray(device_summary.monthly_lower, dtype=np.int64),
            np.asarray(device_summary.monthly_upper, dtype=np.int64),
            quantile_plan,
        ),
    )


@overload
def run_jax_product_summary(
    plan: CompiledSimulation, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
) -> ProductMetricFanSummary: ...


@overload
def run_jax_product_summary(
    plan: CompiledSimulation, *, primary_agent_id: str, metric: str, percentiles: None
) -> ProductTerminalSummary: ...


def run_jax_product_summary(
    plan: CompiledSimulation, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...] | None
) -> ProductMetricFanSummary | ProductTerminalSummary:
    """Run the JAX month loop for one product metric.

    A metric-fan request sorts on-device and transfers only the two order statistics needed for
    each percentile. Exact half-up interpolation remains host-side, avoiding float64's unsafe-integer
    boundary without copying the full `(month, rollout)` sample matrix. A terminal-distribution
    request transfers only the per-rollout terminal vector.
    """
    series, product_tail = _run_jax_product_series(plan, primary_agent_id=primary_agent_id, metric=metric)
    terminal = terminal_series(metric, series)
    if percentiles is None:
        oversell_host, failed_host, buy_count_host, terminal_host = jax.device_get(
            (product_tail.sale_oversell, product_tail.failed_month, product_tail.target_allocation_buy_count, terminal)
        )
        _validate_product_tail(plan, oversell_host, buy_count_host)
        return ProductTerminalSummary(
            failed_month=np.asarray(failed_host, dtype=np.int64),
            currency_code=plan.currency_code,
            currency_quantum=format(plan.currency_quantum, "f"),
            terminal_samples=np.asarray(terminal_host, dtype=np.int64),
        )

    quantile_plan, fan_device = _product_metric_fan_device_summary(
        plan,
        metric=metric,
        percentiles=percentiles,
        series=series,
        terminal=terminal,
        failed_month=product_tail.failed_month,
    )
    oversell_host, buy_count_host, fan_host = jax.device_get(
        (product_tail.sale_oversell, product_tail.target_allocation_buy_count, fan_device)
    )
    _validate_product_tail(plan, oversell_host, buy_count_host)
    return _product_metric_fan_summary_from_device(
        plan, percentiles=percentiles, quantile_plan=quantile_plan, device_summary=fan_host
    )


def run_jax_product_summaries(
    plan: CompiledSimulation, *, primary_agent_id: str, metric: str, percentiles: tuple[float, ...]
) -> ProductProjectionSummaries:
    """Return fan and terminal summaries from one JAX month-loop execution.

    The product page renders both projections for the same scenario and seed batch. Keeping the
    two reductions in one invocation means the expensive simulation and product-series materialize
    once; only the requested order statistics and terminal sample vector cross the device boundary.
    """
    series, product_tail = _run_jax_product_series(plan, primary_agent_id=primary_agent_id, metric=metric)
    terminal = terminal_series(metric, series)
    quantile_plan, fan_device = _product_metric_fan_device_summary(
        plan,
        metric=metric,
        percentiles=percentiles,
        series=series,
        terminal=terminal,
        failed_month=product_tail.failed_month,
    )

    oversell_host, failed_host, buy_count_host, fan_host, terminal_host = jax.device_get(
        (
            product_tail.sale_oversell,
            product_tail.failed_month,
            product_tail.target_allocation_buy_count,
            fan_device,
            terminal,
        )
    )
    _validate_product_tail(plan, oversell_host, buy_count_host)
    failed_month = np.asarray(failed_host, dtype=np.int64)
    metric_fan = _product_metric_fan_summary_from_device(
        plan, percentiles=percentiles, quantile_plan=quantile_plan, device_summary=fan_host
    )
    terminal_distribution = ProductTerminalSummary(
        failed_month=failed_month,
        currency_code=plan.currency_code,
        currency_quantum=format(plan.currency_quantum, "f"),
        terminal_samples=np.asarray(terminal_host, dtype=np.int64),
    )
    return ProductProjectionSummaries(metric_fan=metric_fan, terminal_distribution=terminal_distribution)


def _validate_product_tail(plan: CompiledSimulation, oversell: np.ndarray, ta_buy_count: np.ndarray) -> None:
    if bool(np.asarray(oversell)):
        raise ValueError("scheduled asset sale exceeds available lots")
    _check_purchase_slot_exhaustion(plan, np.asarray(ta_buy_count))


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
    bond_execution = plan.bonds.execution
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
        primary_obligation_mask=jnp.asarray(plan.obligations.metadata.agent == primary_agent_code),
        bond_face=jnp.asarray(np.where(bond_mask, bond_execution.face, 0)),
        bond_on_books=jnp.asarray(bond_execution.on_books),
        bond_indexed=jnp.asarray(bond_execution.indexed),
        bond_cpi_series=jnp.asarray(bond_execution.cpi_series),
        bond_index_base_month=jnp.asarray(bond_execution.index_base_month),
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
    program = _build_program(plan)
    text = _program_impl.lower(program).compile().as_text()
    if text is None:
        raise RuntimeError("compiled program exposes no HLO text")
    return text


def _build_program(
    plan: CompiledSimulation,
    *,
    product_summary: _ProductSummaryStatic | None = None,
    product_inputs: _ProductSummaryInputs | None = None,
    emit_dense: bool = True,
) -> _SimulationProgram:
    """Build the one registered PyTree consumed by `_program_impl`.

    Array values are dynamic leaves; immutable topology and output mode are static metadata. JAX
    therefore owns flattening and cache-key construction without parallel JIT argument protocols.
    """
    p = plan.slot_plan
    r = p.rollout_count
    horizon = plan.horizon_months
    # One row per (profile, income source) — see `TaxCompileOutput.income_bucket`.
    ordinary0 = _zeros_i64((p.income_bucket_count, r))
    property_tax_ytd0 = _zeros_i64((p.tax_profile_count, r))
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
            amount_quanta=int(le_all.amount_quanta[i]),
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

    # Scheduled asset sales own both their traced values and host-resolved FIFO topology.
    asset_sales = _asset_sale_program(plan)
    # Compiler execution records are explicit native PyTrees. Convert every numeric leaf once;
    # decode-only identity columns never cross this boundary.
    cashflows = cast(CashflowExecution[jax.Array], jax.tree.map(jnp.asarray, plan.cashflows.execution))
    bonds = cast(BondExecution[jax.Array], jax.tree.map(jnp.asarray, plan.bonds.execution))
    distributions = cast(DistributionExecution[jax.Array], jax.tree.map(jnp.asarray, plan.distributions.execution))
    obligations = cast(ObligationExecution[jax.Array], jax.tree.map(jnp.asarray, plan.obligations.execution))

    ta_policies = plan.target_allocation_policies
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
            sleeve_pools: list[tuple[int, ...]] = []
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
                        lot_fifo_rank=plan.lot_fifo_rank,
                        lot_id_codes=plan.lot_id_codes,
                        agent_code=agent_code,
                        account_code=account_code,
                        asset_code=asset_code,
                    )
                    if not ordered.size:
                        continue
                    sleeve_pools.append(tuple(int(x) for x in ordered))
                    # Pools are disjoint by construction — a sleeve is one asset and its pools are
                    # distinct accounts — so appending never repeats a plan lot on the view axis.
                    view_rows.extend(range(len(lot_slots), len(lot_slots) + int(ordered.size)))
                    lot_slots.extend(int(x) for x in ordered)
            sleeves.append(
                _FoldedSleeve(
                    sleeve_idx=sleeve_idx,
                    view_lot_rows=tuple(view_rows),
                    pools=tuple(sleeve_pools),
                    quantity_scale=int(ta_policies.sleeve_quantity_scale[policy, sleeve_idx]),
                    # Slots stay in compile order, which is fill order: the cursor hands out slot
                    # `k` on the k-th purchase, and the plan gave slot `k` a FIFO rank below slot
                    # `k+1`. That is what keeps the sale order derivable without a month.
                    purchase_slots=tuple(
                        int(slot) for slot in plan.target_allocation_purchase_slots[policy, sleeve_idx] if slot >= 0
                    ),
                )
            )
        folded_target_allocation.append(
            _FoldedTargetAllocation(
                policy_index=policy,
                agent=agent_code,
                cash_slot=int(ta_policies.cash_slot[policy]),
                rebalance_tolerance=ta_policies.rebalance_tolerances[policy],
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
        ordered = lot_indices[np.argsort(plan.lot_fifo_rank[lot_indices], kind="stable")]
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
                peak_annual_yield_ppb=params.peak_annual_yield_ppb,
                floor_annual_yield_ppb=params.floor_annual_yield_ppb,
                maturity_decay_half_exponent=params.maturity_decay_half_exponent,
                drawdown_sensitivity_ppb=params.drawdown_sensitivity_ppb,
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
    # The IRC 1211(b) cap the netting clamps against, on the same axis. It is a taxpayer's
    # figure, and `compile_tax` has already refused a profile whose jurisdictions disagree on
    # it, so the first profile reaching a cg-agent carries the answer for all of them.
    cg_offset_cap = np.zeros(max(1, p.capital_gain_agent_count), dtype=np.int64)
    for profile in range(profile_count):
        gp = int(plan.tax_profile_capital_gain_index[profile])
        if gp >= 0 and cg_rep_profile[gp] < 0:
            cg_rep_profile[gp] = plan.tax.buckets.ordinary_bucket(profile)
            cg_offset_cap[gp] = int(plan.tax.profile_max_capital_loss_ordinary_offset[profile])
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
    # matching folded tuple; `ta_pool_series` is a per-policy list of per-sleeve arrays (ragged).
    def _series_ops(values: list[int]) -> Int64[Array, " item"]:
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
    ta_floor_series = _series_ops([int(ta_policies.floor_series[tp.policy_index]) for tp in folded_target_allocation])
    ta_ceiling_series = _series_ops(
        [int(ta_policies.ceiling_series[tp.policy_index]) for tp in folded_target_allocation]
    )
    ta_sleeve_series = [
        _series_ops([int(ta_policies.sleeve_series[tp.policy_index, sleeve.sleeve_idx]) for sleeve in tp.sleeves])
        for tp in folded_target_allocation
    ]
    for _tp in folded_target_allocation:
        _row = np.asarray(
            [int(ta_policies.weights[_tp.policy_index, _s.sleeve_idx]) for _s in _tp.sleeves], dtype=np.int64
        )
        # Positivity is checked HERE, on concrete values, because the operand below is traced and
        # `allocation._validate_weights` can then only check shape. `SleeveTarget.weight` already
        # makes this unrepresentable; this is the belt to that braces.
        if np.any(_row <= 0):
            raise ValueError(f"target-allocation weights must all be positive; got {_row.tolist()}")
    ta_sleeve_weights = [
        jnp.asarray(
            [int(ta_policies.weights[tp.policy_index, sleeve.sleeve_idx]) for sleeve in tp.sleeves], dtype=jnp.int64
        )
        for tp in folded_target_allocation
    ]

    purchases = _PurchaseInputs(
        month=jnp.asarray(props.month),
        stake_contribution=jnp.asarray(props.stake_contribution),
        buyer_slot=jnp.asarray(props.buyer_slot),
        seller_slot=jnp.asarray(props.seller_slot),
        mortgage_slot=jnp.asarray(props.mortgage_slot),
        mortgage_principal=jnp.asarray(_np_gather(plan.liabilities.principal, props.mortgage_slot, 0)),
        mortgage_monthly_payment=jnp.asarray(_np_gather(plan.liabilities.monthly_payment, props.mortgage_slot, 0)),
    )

    baked = _Operands(
        ordinary0=ordinary0,
        property_tax_ytd0=property_tax_ytd0,
        cg_active0=cg_active0,
        cg_ytd0=cg_ytd0,
        tlh0=tlh0,
        property_rented_fraction_0=property_rented_fraction_0,
        property_building_basis_0=property_building_basis_0,
        prop0=_zeros_i64((p.property_count, r)),
        liab0=_zeros_i64((p.liability_count, r)),
        cashflows=cashflows,
        bonds=bonds,
        distributions=distributions,
        obligations=obligations,
        purchases=purchases,
        property_is_primary_table=property_is_primary_table,
        tax_slot_table=tax_slot_table,
        salt_cap_table=salt_cap_table,
        capital_gain_agent_codes=jnp.asarray(plan.capital_gain_agent_codes),
        cg_rep_profile=jnp.asarray(cg_rep_profile),
        cg_offset_cap=jnp.asarray(cg_offset_cap),
        property_owner_ordinary_row=jnp.asarray(plan.property_owner_ordinary_row),
        liability_owner_profile_index=jnp.asarray(plan.liability_owner_profile_index),
        salt_contributing_mask=salt_contributing_mask,
        lot_asset_series_index=jnp.asarray(plan.lot_asset_series_index),
        lot_quantity_scale=jnp.asarray(plan.lot_quantity_scale),
        pe_owner_cash_mask=pe_owner_cash_mask,
        pe_floor_series=pe_floor_series,
        harvest_series=harvest_series,
        lifecycle_sale_series=lifecycle_sale_series,
        ta_floor_series=ta_floor_series,
        ta_ceiling_series=ta_ceiling_series,
        ta_sleeve_series=ta_sleeve_series,
        ta_sleeve_weights=ta_sleeve_weights,
    )

    # Capital-gain accrual targets: each agent code that sells (funding policies / PE owners) maps to the
    # capital-gain profile rows whose agent code matches (the de-`plan`-ed `_record_capital_gains`).
    # Every agent that can DISPOSE of a lot needs a capital-gain target row, or the phase
    # that sells for it has no bucket to book the gain into.
    cg_agent_codes = {tp.agent for tp in folded_target_allocation} | {
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
        )
        for link in range(link_count)
    )
    structure = _Static(
        slot_plan=p,
        lot_axis=lot_axis,
        ta_policy_count=int(ta_policies.sleeve_assets.shape[0]),
        ta_max_sleeves=int(ta_policies.sleeve_assets.shape[1]),
        pe_issuer_count=pe_issuer_count,
        n_pe_kinds=n_pe_kinds,
        folded_lifecycle=tuple(folded_lifecycle),
        folded_pr=tuple(folded_pr),
        folded_sale_events=tuple(folded_sale_events),
        folded_target_allocation=tuple(folded_target_allocation),
        folded_pe=tuple(folded_pe),
        folded_harvest=tuple(folded_harvest),
        salt_link_active=tuple(bool(salt_link_active[link]) for link in range(link_count)),
        external_cash_slot=int(plan.external_cash_slot),
        cg_targets=cg_targets,
        link_tax_static=link_tax_static,
        link_profile=tuple(int(taxc.link_profile[link]) for link in range(link_count)),
        profile_gain_index=tuple(int(x) for x in plan.tax_profile_capital_gain_index),
        has_indexed_bonds=bool(plan.bonds.execution.indexed.any()),
        has_distributions=bool(plan.distributions.execution.series.size),
        profile_ordinary_bucket=tuple(
            plan.tax.buckets.ordinary_bucket(profile) for profile in range(p.tax_profile_count)
        ),
    )
    return _SimulationProgram(
        dynamic=_ProgramDynamic(
            external_values=jnp.asarray(plan.external_values),
            external_money_values=jnp.asarray(plan.external_money_values),
            pe_channels=jax.tree.map(jnp.asarray, plan.pe_channels.execution),
            swept=_traced_config(plan),
            asset_sales=asset_sales,
            operands=baked,
            product_inputs=product_inputs,
        ),
        static=_ProgramStatic(structure=structure, product_summary=product_summary, emit_dense=emit_dense),
    )


@jax.jit
def _program_impl(program: _SimulationProgram) -> tuple:
    """Module-level scan program whose one registered PyTree defines the complete JIT boundary."""
    dynamic = program.dynamic
    static = program.static
    external_values = dynamic.external_values
    external_money_values = dynamic.external_money_values
    pe_ch = dynamic.pe_channels
    cfg = dynamic.swept
    asset_sales = dynamic.asset_sales
    baked = dynamic.operands
    product_inputs = dynamic.product_inputs
    structure = static.structure
    product_summary = static.product_summary
    emit_dense = static.emit_dense
    p = structure.slot_plan
    r = p.rollout_count
    horizon = p.event_months
    lot_count = p.lot_count
    link_count = len(structure.link_tax_static)
    profile_count = p.tax_profile_count
    taxliab_count = p.tax_liability_count
    lot_axis = structure.lot_axis
    ta_policy_count = structure.ta_policy_count
    ta_max_sleeves = structure.ta_max_sleeves
    pe_issuer_count = structure.pe_issuer_count
    n_pe_kinds = structure.n_pe_kinds
    folded_lifecycle = structure.folded_lifecycle
    folded_pr = structure.folded_pr
    folded_pe = structure.folded_pe
    folded_harvest = structure.folded_harvest
    salt_link_active = structure.salt_link_active
    link_tax_static = structure.link_tax_static
    link_profile = structure.link_profile
    profile_gain_index = structure.profile_gain_index
    profile_ordinary_bucket = structure.profile_ordinary_bucket
    cg_profiles_by_agent = {ct.agent_code: ct.profiles for ct in structure.cg_targets}
    # Static index/selection arrays (rebuilt from the hashable tuples carried in `structure`).
    n_asset_sales = len(asset_sales.proceeds_slot)
    asset_sale_pool_width = len(asset_sales.ordered_lots[0]) if asset_sales.ordered_lots else 1
    asset_sale_proceeds_slot = np.asarray(asset_sales.proceeds_slot, dtype=np.int64).reshape(n_asset_sales)
    asset_sale_buffer_index = np.asarray(asset_sales.buffer_index, dtype=np.int64).reshape(n_asset_sales)
    asset_sale_ordered_lots = np.asarray(asset_sales.ordered_lots, dtype=np.int64).reshape(
        n_asset_sales, asset_sale_pool_width
    )
    # Device arrays unpacked from the baked pytree (SAME names the bodies use).
    ordinary0 = baked.ordinary0
    property_tax_ytd0 = baked.property_tax_ytd0
    cg_active0 = baked.cg_active0
    cg_ytd0 = baked.cg_ytd0
    tlh0 = baked.tlh0
    property_rented_fraction_0 = baked.property_rented_fraction_0
    property_building_basis_0 = baked.property_building_basis_0
    prop0 = baked.prop0
    liab0 = baked.liab0
    cashflows = baked.cashflows
    bonds = baked.bonds
    distributions = baked.distributions
    obligation_inputs = baked.obligations
    purchases = baked.purchases
    property_is_primary_table = baked.property_is_primary_table
    tax_slot_table = baked.tax_slot_table
    salt_cap_table = baked.salt_cap_table
    cg_rep_profile = baked.cg_rep_profile
    cg_offset_cap = baked.cg_offset_cap
    property_owner_ordinary_row = baked.property_owner_ordinary_row
    liability_owner_profile_index = baked.liability_owner_profile_index
    salt_contributing_mask = baked.salt_contributing_mask
    lot_asset_series_index = baked.lot_asset_series_index
    lot_quantity_scale = baked.lot_quantity_scale
    pe_owner_cash_mask = baked.pe_owner_cash_mask
    pe_floor_series = baked.pe_floor_series
    harvest_series = baked.harvest_series
    lifecycle_sale_series = baked.lifecycle_sale_series
    ta_floor_series = baked.ta_floor_series
    ta_ceiling_series = baked.ta_ceiling_series
    ta_sleeve_series = baked.ta_sleeve_series
    ta_sleeve_weights = baked.ta_sleeve_weights
    folded_target_allocation = structure.folded_target_allocation

    def product_metrics(
        s: _ScanState,
        *,
        snapshot_month: Int[Array, ""],
        obligation_shortfall: Int64[Array, " obligation rollout"],
        obligation_mask: Bool[Array, " obligation"],
    ) -> tuple[Int64[Array, " rollout"], ...]:
        assert product_summary is not None
        assert product_inputs is not None
        cash_quanta = jnp.where(product_inputs.cash_mask[:, None], s.cash, 0).sum(axis=0)
        holding_quanta = jnp.zeros((r,), dtype=jnp.int64)
        if product_summary.has_public_lots:
            safe_series = jnp.maximum(lot_asset_series_index, 0)
            public_price = external_money_values[safe_series, :, snapshot_month]
            public_value = _value_quanta_from_quantity(s.lot_remaining, public_price, lot_quantity_scale[:, None])
            holding_quanta = jnp.where(product_inputs.public_lot_mask[:, None], public_value, 0).sum(axis=0)

        pe_quanta = jnp.zeros((r,), dtype=jnp.int64)
        if product_summary.has_pe_lots:
            safe_issuer = jnp.maximum(product_inputs.pe_lot_issuer, 0)
            pe_price = pe_ch.mark_quanta[safe_issuer, :, snapshot_month]
            pe_value = _value_quanta_from_quantity(s.lot_remaining, pe_price, lot_quantity_scale[:, None])
            pe_quanta = jnp.where(product_inputs.pe_lot_mask[:, None], pe_value, 0).sum(axis=0)

        property_quanta = jnp.zeros((r,), dtype=jnp.int64)
        if product_summary.has_properties:
            valid_series = product_inputs.property_home_value_series >= 0
            safe_series = jnp.maximum(product_inputs.property_home_value_series, 0)
            levels = external_money_values[safe_series]  # (property, rollout, snapshot)
            current = levels[:, :, snapshot_month]
            base_index = product_inputs.property_purchase_month[:, None, None]
            base = jnp.take_along_axis(levels, base_index, axis=2)[:, :, 0]
            market = _scale_quanta_by_ratio(product_inputs.property_purchase_price[:, None], current, base)
            active_property = product_inputs.property_mask[:, None] & valid_series[:, None] & s.property_active
            property_quanta = jnp.where(active_property & (base > 0), market, 0).sum(axis=0)

        mortgage_quanta = jnp.where(product_inputs.liability_mask[:, None], s.liability_principal, 0).sum(axis=0)
        shortfall_quanta = jnp.where(obligation_mask[:, None], obligation_shortfall, 0).sum(axis=0)
        bond_quanta = jnp.zeros((r,), dtype=jnp.int64)
        if product_summary.has_bonds:
            carried = product_inputs.bond_face[:, None] * jnp.ones((1, r), dtype=jnp.int64)
            if structure.has_indexed_bonds:
                safe = jnp.maximum(product_inputs.bond_cpi_series, 0)
                base_cpi = external_values[safe, :, product_inputs.bond_index_base_month]
                indexed_principal = _scale_money_by_float_ratio(
                    product_inputs.bond_face[:, None],
                    external_values[safe, :, snapshot_month],
                    jnp.where(base_cpi > 0, base_cpi, 1.0),
                )
                carried = jnp.where((product_inputs.bond_indexed > 0)[:, None], indexed_principal, carried)
            held_face = (product_inputs.bond_on_books[snapshot_month][:, None] * carried).sum(axis=0)
            # Identical across rollouts, but zeroed for failed ones so a failed rollout's net
            # worth is zero like every other term rather than reporting the bonds alone.
            bond_quanta = jnp.where(s.failed, 0, held_face)

        return (cash_quanta, holding_quanta, pe_quanta, property_quanta, mortgage_quanta, shortfall_quanta, bond_quanta)

    def december_tax(
        ordinary: Int64[Array, " income_bucket rollout"],
        cg_ytd: Int64[Array, " capital_gain_profile gain_class rollout"],
        carryforward: Int64[Array, " capital_gain_profile rollout"],
        recapture: Int64[Array, " tax_profile rollout"],
        property_tax_ytd: Int64[Array, " tax_profile rollout"],
        liab_interest_ytd: Int64[Array, " liability rollout"],
        liab_rental_ytd: Int64[Array, " liability rollout"],
        property_dep_ytd: Int64[Array, " property rollout"],
        taxliab_active: Bool[Array, " tax_liability rollout"],
        taxliab_amount: Int64[Array, " tax_liability rollout"],
        active: Bool[Array, " rollout"],
        month: Int[Array, ""],
    ):
        """Branch-free December (`month % 12 == 11`) year-end tax pass, gated per-rollout by `dec`.

        Returns the post-pass YTD/carryforward/tax-liability state plus the 12 per-link tax output
        slabs `(link_count, R)`. For non-December months every output reduces to the inputs / zeros.
        """
        dec = (month % 12 == 11) & active  # (R,)
        # Schedule E: §168 depreciation + rented-share mortgage interest, deducted from each entity's
        # owner tax profile. Vectorized over the property / liability axis: scatter-add the (December-
        # masked) amounts to their owner-profile rows; entities with no owner profile (index < 0) route
        # to `_scatter_rows`'s dump row and contribute nothing.
        dec_col = dec[None, :]
        ordinary = ordinary + _scatter_rows(
            jnp.zeros_like(ordinary), property_owner_ordinary_row, -jnp.where(dec_col, property_dep_ytd, 0)
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
            max_ordinary_offset_quanta=cg_offset_cap[: cg_ytd.shape[0], None],
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

        annual_tax_by_link = _zeros_i64((r, max(1, link_count)))
        zero_salt = _zeros_i64((r,))
        breakdown = [_zeros_i64((max(1, link_count), r)) for _ in range(11)]

        def run_link(
            link: int, salt_deduction: Int64[Array, " rollout"], ann: Int64[Array, " rollout tax_link"]
        ) -> Int64[Array, " rollout tax_link"]:
            mid, itemized, ord_taxable, cap_taxable, ord_tax, cap_tax = _compute_tax_for_link(
                link_tax_static[link],
                cfg,
                ordinary,
                cg_ytd,
                recapture,
                liab_interest_ytd,
                liab_rental_ytd,
                salt_deduction=salt_deduction,
                rollout_count=r,
            )
            profile = link_profile[link]
            gp = profile_gain_index[profile]
            tax = ord_tax + cap_tax
            cols = [
                dec.astype(jnp.int64),  # accrual_active flag (->bool post-scan)
                jnp.where(dec, ordinary[profile_ordinary_bucket[profile]], 0),
                jnp.where(dec, cg_ytd[gp, CapitalGainClassification.LONG_TERM], 0),
                jnp.where(dec, cg_ytd[gp, CapitalGainClassification.SHORT_TERM], 0),
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

    def step(s: _ScanState, month: Int[Array, ""]) -> tuple[_ScanState, Any]:
        cash, ordinary, property_tax_ytd, lot_remaining = s.cash, s.ordinary_ytd, s.property_tax_ytd, s.lot_remaining
        cost_basis_per_unit = s.cost_basis_per_unit
        lot_purchase_month = s.lot_purchase_month
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
        liab_interest_ytd = s.liability_interest_ytd
        liab_rental_ytd = s.liability_rental_interest_ytd
        capital_loss_carryforward, recapture_ytd = s.capital_loss_carryforward, s.recapture_section_1250_ytd
        taxliab_active, taxliab_amount = s.tax_liability_active, s.tax_liability_amount
        failed, failed_month = s.failed, s.failed_month
        sale_disp_units, sale_disp_basis = s.sale_disp_units, s.sale_disp_basis
        sale_disp_proceeds, sale_oversell = s.sale_disp_proceeds, s.sale_oversell
        ta_buy_count = s.ta_buy_count
        active = ~failed

        # Primary-residence + lifecycle events (first in the month, eager order). Each event fires when
        # its static month equals the traced month, masked per-rollout. is_primary is precomputed
        # per-month host-side; the SALE path uses the §121 owner-occupancy window for the exclusion.
        pr_fired = [jnp.where(month == pr_m, active, jnp.zeros_like(active)) for _, pr_m in folded_pr]
        le_fired: list[Bool[Array, " event rollout"]] = []
        sale_traces: list[PropertySaleTraceOutput[jax.Array]] = []
        for evi, ev in enumerate(folded_lifecycle):
            ev_month, ev_kind, ev_prop = ev.month, ev.kind, ev.property_slot
            fires = month == ev_month
            active_property = fires & active & property_active[ev_prop]
            if ev_kind == LifecycleKind.FRACTION:
                property_rented_fraction = property_rented_fraction.at[ev_prop].set(
                    jnp.where(active_property, ev.rented_fraction, property_rented_fraction[ev_prop])
                )
            elif ev_kind == LifecycleKind.CAPITAL_IMPROVEMENT:
                amount = ev.amount_quanta
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

        # Bond coupons and redemptions arrive before obligations settle. They and the consolidated
        # cashflow program below are additive phases with no intervening affordability decision, so
        # every incoming dollar remains available for this month's outflows.
        cash, ordinary = _bond_cashflows_jit(
            bonds,
            cash,
            ordinary,
            active,
            external_values,
            month,
            structure.external_cash_slot,
            structure.has_indexed_bonds,
        )

        # Fund distributions, immediately after coupons and for the same reason: a payout is
        # income arriving this month that must be able to fund this month's outflows. Reading
        # `lot_remaining` here means the units are the ones held BEFORE this month's trading,
        # which is what a record date means — a fund bought this month pays next month.
        if structure.has_distributions:
            cash, ordinary = _distribution_payouts_jit(
                distributions,
                lot_remaining,
                cash,
                ordinary,
                active,
                external_money_values,
                month,
                structure.external_cash_slot,
            )

        # Property purchases (after market income, before cashflows and scheduled asset sales).
        # Vectorized over all real purchases at once (no Python loop): each fires when its static month equals the traced month
        # for the rollouts still active then, into its own property row (distinct indices, no
        # cross-purchase dependency). Pure-value purchase amounts are gathered from `cfg` by index.
        # The down payment (stake_contribution) moves buyer->seller via sentinel-aware scatter-add
        # (shared/absent cash slots fall out, duplicates accumulate); financed purchases originate the
        # mortgage liability (principal + monthly payment set, YTD interest/principal reset).
        purchase_month = purchases.month
        purchase_stake = purchases.stake_contribution
        purchase_buyer = purchases.buyer_slot
        purchase_seller = purchases.seller_slot
        purchase_mortgage = purchases.mortgage_slot
        purchase_principal = purchases.mortgage_principal
        purchase_monthly_payment = purchases.mortgage_monthly_payment
        fires = (month == purchase_month)[:, None] & active[None, :]  # (P, R), full property axis
        stake_pos = (purchase_stake > 0)[:, None]
        # Every purchase column is already aligned with the mutable property state and the
        # compiler's full property axis. The padded no-property row has month=-1 and therefore
        # remains inert without a host-side fold/remap.
        property_active = jnp.where(fires, True, property_active)
        property_basis = jnp.where(fires, cfg.property_adjusted_basis[:, None], property_basis)
        property_contribution = jnp.where(fires, purchase_stake[:, None], property_contribution)
        property_equity = jnp.where(fires, cfg.property_equity_ledger[:, None], property_equity)
        stake_flow = jnp.where(fires & stake_pos, purchase_stake[:, None], 0)
        cash = _move_cash(
            cash,
            debit=purchase_buyer,
            credit=purchase_seller,
            amount=stake_flow,
            row_of_world=structure.external_cash_slot,
        )
        # Mortgage origination is the only phase that narrows the full property axis: each
        # financed purchase has one distinct liability row, while cash purchases remain untouched.
        financed = purchase_mortgage >= 0
        mfires = fires & financed[:, None]
        mort_orig_rows = (
            _scatter_rows(jnp.zeros(liab_active.shape, dtype=jnp.int64), purchase_mortgage, mfires.astype(jnp.int64))
            > 0
        )
        mort_principal = _scatter_rows(
            jnp.zeros_like(liab_principal), purchase_mortgage, jnp.where(mfires, purchase_principal[:, None], 0)
        )
        mort_monthly = _scatter_rows(
            jnp.zeros_like(liab_monthly), purchase_mortgage, jnp.where(mfires, purchase_monthly_payment[:, None], 0)
        )
        liab_active = liab_active | mort_orig_rows
        liab_principal = jnp.where(mort_orig_rows, mort_principal, liab_principal)
        liab_monthly = jnp.where(mort_orig_rows, mort_monthly, liab_monthly)
        liab_interest_ytd = jnp.where(mort_orig_rows, 0, liab_interest_ytd)
        # Per-purchase event rows for `ys`, retaining the full property axis.
        purchase_active_rows = fires

        # Every authored transfer lowers to one cashflow program. Property-linked rows gate on
        # the post-purchase active state; ordinary rows carry NO_CODE and are unconditional.
        cash, ordinary, cashflow_active, cashflow_amount = _cashflows_jit(
            cashflows, property_active, cash, ordinary, active, external_values, month, structure.external_cash_slot
        )

        # Scheduled asset sales (before obligations: proceeds can fund the month's obligations).
        # Vectorized over ALL sales at once — no Python loop. The across-sales FIFO is one
        # cumulative-supply (over each pool's lots) x cumulative-demand (over each pool's sales,
        # `same_pool_prior`) interval overlap, so shared pools fall out without sequencing. Each
        # sale's disposition `(sale, lot, R)` accumulates into the carry at its slot (fires once ->
        # horizon collapsed). `L` is padded with a zero dummy lot so the ragged pools share one shape.
        if n_asset_sales:
            ld = lot_count
            lot_rem_pad = jnp.concatenate([lot_remaining, _zeros_i64((1, r))], axis=0)  # (L+1, R)
            cost_pad = jnp.concatenate([cost_basis_per_unit, _zeros_i64((1, r))], axis=0)  # (L+1, R)
            scale_pad = jnp.concatenate([lot_quantity_scale, jnp.ones(1, dtype=jnp.int64)])  # (L+1,)
            lpm_pad = jnp.concatenate([lot_purchase_month.astype(jnp.int32), _zeros_i64((1, r)).astype(jnp.int32)])
            pool_qty = lot_rem_pad[asset_sale_ordered_lots]  # (N, P, R) supply per pool lot
            target = jnp.where(
                (active[None, :]) & (month == asset_sales.month)[:, None], asset_sales.quantity[:, None], 0
            )  # (N, R)
            prior = asset_sales.same_pool_prior @ target  # demand claimed by earlier same-pool sales
            oversell = target > (pool_qty.sum(axis=1) - prior)  # (N, R)
            d_lo = prior  # demand interval (D_{j-1}, D_j], with oversold sales selling nothing
            d_hi = prior + jnp.where(oversell, 0, target)
            s_before = jnp.cumsum(pool_qty, axis=1) - pool_qty  # supply prefix S_{k-1} (N, P, R)
            sold = jnp.maximum(
                0, jnp.minimum(d_hi[:, None, :], s_before + pool_qty) - jnp.maximum(d_lo[:, None, :], s_before)
            )

            # TLH give-back: allocate each policy's money-quanta ledger directly by the integer
            # fraction of its pre-sale quantity sold. Money never becomes a per-unit float rate.
            t_policy = asset_sales.tlh_policy_lot_mask @ lot_remaining  # (policy, R) pre-sale units
            lot_gb_total_pad = jnp.concatenate(
                [asset_sales.tlh_policy_lot_mask.T @ tlh, jnp.zeros((1, r), dtype=jnp.int64)], axis=0
            )
            lot_policy_units_pad = jnp.concatenate(
                [asset_sales.tlh_policy_lot_mask.T @ t_policy, jnp.ones((1, r), dtype=jnp.int64)], axis=0
            )

            # Per-sale price: fixed if set, else the sampled series at this month. Guarded on the static
            # series count (and the series index clamped) so fixed-only sales never gather an empty cube.
            if external_money_values.shape[0] > 0:
                safe_series = jnp.where(asset_sales.price_series >= 0, asset_sales.price_series, 0)
                unit_price = jnp.where(
                    (asset_sales.price_series >= 0)[:, None],
                    external_money_values[safe_series, :, month],
                    asset_sales.price_fixed[:, None],
                )  # (N, R)
            else:
                unit_price = jnp.broadcast_to(asset_sales.price_fixed[:, None], (n_asset_sales, r))
            proceeds = _value_quanta_from_quantity(
                sold, unit_price[:, None, :], scale_pad[asset_sale_ordered_lots][:, :, None]
            )  # (N, P, R)
            basis = _value_quanta_from_quantity(
                sold, cost_pad[asset_sale_ordered_lots], scale_pad[asset_sale_ordered_lots][:, :, None]
            )
            give_back = _scale_quanta_by_ratio(
                lot_gb_total_pad[asset_sale_ordered_lots], sold, lot_policy_units_pad[asset_sale_ordered_lots]
            )
            gains = proceeds - basis + give_back

            total_sold = _zeros_i64((ld + 1, r)).at[asset_sale_ordered_lots].add(sold)  # (L+1, R)
            lot_remaining = lot_remaining - total_sold[:ld]
            give_back_by_lot = _zeros_i64((ld + 1, r)).at[asset_sale_ordered_lots].add(give_back)
            tlh = tlh - asset_sales.tlh_policy_lot_mask @ give_back_by_lot[:ld]
            # The cash comes from whoever bought the lot, which is `rest_of_world`.
            cash = _move_cash(
                cash,
                debit=structure.external_cash_slot,
                credit=asset_sale_proceeds_slot,
                amount=proceeds.sum(axis=1),  # (N, R)
                row_of_world=structure.external_cash_slot,
            )

            # Capital gains: classify each pool lot long/short, accrue per sale's cg agents via cg_map.
            # `(N, P, R)`: the purchase month is per-rollout, so one rollout's long-term gain
            # on a pool lot is another's short-term.
            long_m = (month - lpm_pad[asset_sale_ordered_lots]) >= 12
            gains_long = (gains * long_m).sum(axis=1)  # (N, R)
            gains_short = (gains * ~long_m).sum(axis=1)
            sold_pos = sold > 0
            act_long = (sold_pos & long_m).any(axis=1)  # (N, R)
            act_short = (sold_pos & ~long_m).any(axis=1)
            cg_ytd = cg_ytd.at[:, CapitalGainClassification.LONG_TERM, :].add(
                asset_sales.capital_gain_map.T @ gains_long
            )
            cg_ytd = cg_ytd.at[:, CapitalGainClassification.SHORT_TERM, :].add(
                asset_sales.capital_gain_map.T @ gains_short
            )
            cg_active = cg_active.at[:, CapitalGainClassification.LONG_TERM, :].set(
                cg_active[:, CapitalGainClassification.LONG_TERM, :]
                | ((asset_sales.capital_gain_map.T @ act_long.astype(jnp.int64)) > 0)
            )
            cg_active = cg_active.at[:, CapitalGainClassification.SHORT_TERM, :].set(
                cg_active[:, CapitalGainClassification.SHORT_TERM, :]
                | ((asset_sales.capital_gain_map.T @ act_short.astype(jnp.int64)) > 0)
            )

            # Dispositions: scatter sold/basis/proceeds into each sale's slot (dummy lot clamped; sold 0).
            disp_sale = np.broadcast_to(asset_sale_buffer_index[:, None], asset_sale_ordered_lots.shape)
            disp_lot = np.minimum(asset_sale_ordered_lots, ld - 1)
            sale_disp_units = sale_disp_units.at[disp_sale, disp_lot].add(sold)
            sale_disp_basis = sale_disp_basis.at[disp_sale, disp_lot].add(basis)
            sale_disp_proceeds = sale_disp_proceeds.at[disp_sale, disp_lot].add(proceeds)
            sale_oversell = sale_oversell | oversell.any()

        month_obligations = jax.tree.map(lambda value: value[month], obligation_inputs)
        slot_active, accrual_due = _obligation_accruals_jit(
            month_obligations,
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
        payment_metadata = month_obligations.metadata

        # Obligations are environment facts. The current clock policy turns every due invoice
        # into a concrete, full `Pay` action; its compile-time slot already fixes the actor and
        # source/destination accounts. Feeding settlement this emitted batch rather than the
        # accrual directly is the first behavior-preserving step toward one actor-action path.
        pay_actions = decide_payments(PaymentView(invoice_active=slot_active, invoice_due_quanta=accrual_due))

        attempt_policy = jnp.full((slot_active.shape[0], r), NO_CODE, dtype=jnp.int64)

        # Target-allocation policies: observe, decide, execute. Runs before the funding check,
        # so a raise can cover this month's demand.
        #
        # The decision is NOT taken here: `target_allocation.decide` is a pure function of an
        # `ActorView`, so what the engine does is build the observation, call the policy, and
        # execute what comes back. A learned policy replaces that one call and nothing around it.
        ta_disp_active = jnp.zeros((ta_policy_count, ta_max_sleeves, lot_axis, r), dtype=bool)
        ta_disp_units = _zeros_i64((ta_policy_count, ta_max_sleeves, lot_axis, r))
        ta_disp_basis = _zeros_i64((ta_policy_count, ta_max_sleeves, lot_axis, r))
        ta_disp_proceeds = _zeros_i64((ta_policy_count, ta_max_sleeves, lot_axis, r))
        # Buy orders are DECIDED here and EXECUTED after settlement, so they wait in this list.
        # `(policy, sleeve, wanted quanta, unit price)`, one entry per sleeve that ordered.
        ta_buy_orders: list[
            tuple[_FoldedTargetAllocation, _FoldedSleeve, Int64[Array, " rollout"], Int64[Array, " rollout"]]
        ] = []
        if folded_target_allocation:
            # Marks for every lot, once for the month rather than once per pool: the observation
            # needs a value for each of the policy's lots, and `_value_quanta_from_quantity` is the
            # same helper the sale itself values with — a second implementation here would report
            # a sleeve worth a cent less than selling it yields.
            ta_valid_series = lot_asset_series_index >= 0
            if external_money_values.shape[0] > 0:
                ta_lot_price = external_money_values[jnp.where(ta_valid_series, lot_asset_series_index, 0), :, month]
                ta_lot_price = jnp.where(ta_valid_series[:, None], ta_lot_price, 0)
            else:
                ta_lot_price = _zeros_i64((lot_remaining.shape[0], r))
            lot_value_all = _value_quanta_from_quantity(lot_remaining, ta_lot_price, lot_quantity_scale[:, None])

        for ti, tp in enumerate(folded_target_allocation):
            matching = (payment_metadata.agent == tp.agent) & (payment_metadata.from_slot == tp.cash_slot)
            # What the month is already committed to paying from this account. The band decides
            # against the balance the month will END at, which is what lets funding happen once.
            hard_demand = jnp.where(matching[:, None] & slot_active, accrual_due, 0).sum(axis=0)  # (R,)
            attempt_policy = jnp.where(matching[:, None] & slot_active, tp.policy_index, attempt_policy)
            sleeve_prices = _sleeve_prices_quanta(ta_sleeve_series[ti], external_money_values, month)
            view = build_actor_view(
                month=month,
                slots=ActorSlots(
                    cash_slots=(tp.cash_slot,),
                    lot_slots=tp.lot_slots,
                    external_cash_slot=structure.external_cash_slot,
                    cash_count=p.cash_count,
                    lot_count=p.lot_count,
                ),
                cash_quanta=cash,
                lot_quantity=lot_remaining,
                lot_cost_basis_per_unit_quanta=cost_basis_per_unit,
                lot_value_quanta=lot_value_all,
                lot_purchase_month=lot_purchase_month,
                scheduled_outflow_quanta=hard_demand,
                # The market price of everything this policy can trade, held or not — the same
                # number the executor sells at below, read once so the two cannot disagree.
                instrument_price_quanta=sleeve_prices,
                instrument_quantity_scale=np.asarray([sleeve.quantity_scale for sleeve in tp.sleeves], dtype=np.int64),
            )
            orders = decide(
                view=view,
                universe=SleeveUniverse(
                    weights=ta_sleeve_weights[ti],
                    lot_rows=tuple(sleeve.view_lot_rows for sleeve in tp.sleeves),
                    funding_cash_row=0,
                ),
                floor_quanta=_amount_values_tuple(tp.floor, ta_floor_series[ti], external_values, month, r),
                ceiling_quanta=_amount_values_tuple(tp.ceiling, ta_ceiling_series[ti], external_values, month, r),
                rebalance_tolerance=tp.rebalance_tolerance,
            )
            for si, sleeve in enumerate(tp.sleeves):
                # Buys are queued rather than executed: they must not run until obligations have
                # settled, or a purchase could starve a bill into a false ruin. A sleeve with no
                # purchase slots queues nothing, so surplus above the ceiling just accumulates.
                if sleeve.purchase_slots:
                    ta_buy_orders.append((tp, sleeve, orders.buy_quanta[si], sleeve_prices[si]))
                # The order arrives in quanta, so nothing here converts anything: the units are
                # spread across the sleeve's pools until they run out. How many units a cent
                # target was worth is the policy's decision, made against this same price.
                remaining = jnp.where(active, orders.sell_quanta[si], 0)
                unit_price = sleeve_prices[si]
                for pool in sleeve.pools:
                    pool_lots = np.asarray(pool, dtype=np.int64)
                    available = lot_remaining[pool_lots].sum(axis=0)
                    target = jnp.where(active, jnp.minimum(jnp.maximum(remaining, 0), available), 0)
                    sold_units, proceeds, basis = _fifo_sell(
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
                    remaining = jnp.maximum(remaining - sold_units.sum(axis=1), 0)

        agent_row = payment_metadata.agent
        from_row = payment_metadata.from_slot
        group_matrix = (agent_row[:, None] == agent_row[None, :]) & (from_row[:, None] == from_row[None, :])
        funded = _obligation_group_funded_jit(
            group_matrix, from_row, cash, pay_actions.active, pay_actions.amount_quanta
        )

        paid, paid_buffer, cash, ordinary, property_tax_ytd, shortfall, failure_active, failed, failed_month = (
            _settlement_core_jit(
                payment_metadata,
                pay_actions,
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

        # Target-allocation purchases, decided above and executed here: after settlement because
        # buying is discretionary and must never be able to starve an obligation into a false ruin.
        # `~failed` rather than the month-opening `active`,
        # because settlement runs between the decision and this line.
        for tp, sleeve, want, unit_price in ta_buy_orders:
            scale = jnp.asarray(sleeve.quantity_scale, dtype=jnp.int64)
            priced = unit_price > 0
            # Affordability is measured against cash as it stands NOW, not as the policy saw it:
            # settlement or a second policy on the same account can have spent in between. Flooring
            # keeps `spent <= cash`, so the clamp cannot overdraw. This
            # is not the engine choosing a size — the policy's own order already fits the cash it
            # observed; the clamp binds only when something else took that cash first.
            affordable = jnp.where(
                priced, (jnp.maximum(cash[tp.cash_slot], 0) * scale) // jnp.where(priced, unit_price, 1), 0
            )
            wanted = jnp.where(~failed, jnp.minimum(want, affordable), 0)
            used = ta_buy_count[tp.policy_index, sleeve.sleeve_idx]
            fires = wanted > 0
            executes = fires & (used < len(sleeve.purchase_slots))
            quanta = jnp.where(executes, wanted, 0)
            # Gated on `executes`, not `fires`: an exhausted sleeve that still debited cash would
            # be paying the market for nothing.
            spent = _value_quanta_from_quantity(quanta, unit_price, scale)
            cash = _move_cash(
                cash,
                debit=tp.cash_slot,
                credit=structure.external_cash_slot,
                amount=spent,
                row_of_world=structure.external_cash_slot,
            )
            for k, slot in enumerate(sleeve.purchase_slots):
                # Which slot a purchase lands in is per-rollout, so the write is a static sweep over
                # the budget masked by `used == k` rather than a dynamic index: in one traced step a
                # rollout on its second purchase writes slot 1 while its neighbour writes slot 0.
                hit = executes & (used == k)
                lot_remaining = lot_remaining.at[slot].add(jnp.where(hit, quanta, 0))
                cost_basis_per_unit = cost_basis_per_unit.at[slot].set(
                    jnp.where(hit, unit_price, cost_basis_per_unit[slot])
                )
                lot_purchase_month = lot_purchase_month.at[slot].set(
                    jnp.where(hit, month.astype(lot_purchase_month.dtype), lot_purchase_month[slot])
                )
            # Counted on `fires`, not `executes`: a purchase that found no free slot is precisely
            # what the post-scan exhaustion check exists to catch.
            ta_buy_count = ta_buy_count.at[tp.policy_index, sleeve.sleeve_idx].add(fires.astype(jnp.int64))

        # Mortgage payments: split each paid mortgage bill into interest (rate/12 on the outstanding
        # principal, capped at the payment) and principal (the remainder, capped at the balance), then
        # pay down the liability and accrue the YTD interest/principal (+ the rented share for Sch E).
        # Non-mortgage rows route to the sentinel index -1, so `_scatter_rows` ignores them.
        mortgage_source = month_obligations.mortgage
        is_mortgage = mortgage_source.active
        mort_liab_idx = jnp.where(is_mortgage, mortgage_source.liability_slot, -1)
        principal_before = _gather_rows(liab_principal, jnp.where(is_mortgage, mortgage_source.liability_slot, 0))
        interest = jnp.minimum(_scale_money(principal_before, mortgage_source.annual_rate[:, None] / 12.0), paid_buffer)
        principal_paid = jnp.minimum(jnp.maximum(paid_buffer - interest, 0), principal_before)
        mort_paid = is_mortgage[:, None] & paid
        interest_m = jnp.where(mort_paid, interest, 0)
        principal_m = jnp.where(mort_paid, principal_paid, 0)
        rented_per_slot = _gather_rows(property_rented_fraction, jnp.maximum(payment_metadata.property_slot, 0))
        liab_principal = _scatter_rows(liab_principal, mort_liab_idx, -principal_m)
        liab_interest_ytd = _scatter_rows(liab_interest_ytd, mort_liab_idx, interest_m)
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
        true_up_source = month_obligations.tax_true_up
        trueup_sel_m = true_up_source.tax_liability_selector  # (slots, taxliab)
        is_trueup = true_up_source.active
        trueup_paid = is_trueup[:, None] & paid  # (slots, R)
        eligible = jnp.where(taxliab_active, taxliab_amount, 0)  # (taxliab, R)
        actual_per_trueup = trueup_sel_m @ eligible  # (slots, R): full year tax owed
        settle_k = (trueup_sel_m.astype(bool)[:, :, None] & trueup_paid[:, None, :]).any(axis=0)  # (taxliab, R)
        taxliab_amount = jnp.where(settle_k, 0, taxliab_amount)
        # Settlement event arrays, scattered to tax-profile rows (one true-up per profile per month).
        settle_prof_idx = jnp.where(is_trueup, true_up_source.profile_index, -1)
        settle_amount = _scatter_rows(
            _zeros_i64((profile_count, r)), settle_prof_idx, jnp.where(trueup_paid, actual_per_trueup, 0)
        )
        settle_active = (
            _scatter_rows(_zeros_i64((profile_count, r)), settle_prof_idx, trueup_paid.astype(jnp.int64)) > 0
        )
        settled_year_end = _scatter_rows(
            _zeros_i64((profile_count, r)),
            settle_prof_idx,
            jnp.where(trueup_paid, true_up_source.tax_year_end_month[:, None], 0),
        )
        settle_year_end = jnp.where(settle_active, settled_year_end, NO_CODE)

        # TLH harvest (after settlement, before PE): book a calibrated capital loss per policy. The
        # prior price clamps to month 0 (max(0, month-1)), giving a flat period return there — so the
        # eager engine's month-0 `has_prior=False` special case is unnecessary inside the scan.
        for hi, fh in enumerate(folded_harvest):
            hp_policy = fh.policy_idx
            hp_lots = np.asarray(fh.lot_indices, dtype=np.int64)
            # The return-shaping curve reads the same integer money cube as the tax basis
            # and market-value calculation below, so no float level is gathered here at all.
            hp_price_row = external_money_values[harvest_series[hi]]  # (rollouts, H+1) dynamic gather
            hp_price_quanta = hp_price_row[:, month]
            hp_prior_price_quanta = hp_price_row[:, jnp.maximum(0, month - 1)]
            cg_ytd, cg_active, hp_cumulative = _tlh_harvest_policy_jit(
                lot_remaining[hp_lots, :],
                cost_basis_per_unit[hp_lots],
                lot_quantity_scale[hp_lots],
                hp_price_quanta,
                hp_prior_price_quanta,
                tlh[hp_policy],
                cg_ytd,
                cg_active,
                active,
                gain_profile=fh.gain_profile,
                has_prior=True,
                peak_ppb=fh.peak_annual_yield_ppb,
                floor_ppb=fh.floor_annual_yield_ppb,
                half_exponent=fh.maturity_decay_half_exponent,
                sensitivity_ppb=fh.drawdown_sensitivity_ppb,
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
            mark_quanta = pe_ch.mark_quanta[issuer_idx, :, month]
            positive_mark = mark_quanta > 0
            tender_active = pe_ch.sale_opportunity_active[issuer_idx, :, month] & active
            public_active = pe_ch.regime_codes[issuer_idx, :, month] == int(PrivateEquityRegimeCode.PUBLIC_MARKET)
            liq_blocked = pe_ch.liquidity_blocked[issuer_idx, :, month]
            forced_sale_fraction = pe_ch.forced_sale_fractions[issuer_idx, :, month]
            forced_recovery = pe_ch.forced_recovery_cashout_quanta[issuer_idx, :, month]
            capacity = pe_ch.sale_capacity_fractions[issuer_idx, :, month]
            eligible = pe_ch.eligible_fractions[issuer_idx, :, month]
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
                sold, proceeds, basis = _fifo_sell(
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
            recovery_price = _scale_quanta_by_ratio(
                forced_recovery, issuer_scale, jnp.where(units_held > 0, units_held, 1)
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
                jnp.where(forced_active, forced_target, 0), mark_quanta, PrivateEquityDispositionKind.FORCED_SALE, state
            )
            # Forced recovery / forced sale can both precede the discretionary tender in
            # this issuer-month. Carry their capital-gain and TLH mutations forward too;
            # retaining only cash + lots silently discarded forced-disposition tax facts.
            cash, lot_remaining, cg_active, cg_ytd, tlh = state[0], state[1], state[2], state[3], state[4]
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
                external_money_values,
                month,
            )
            pe_shortfall = jnp.maximum(0, floor - lnw)  # distinct from the obligation `shortfall` in base_ys
            units_held = lot_remaining[ordered].sum(axis=0)
            sellable = _round_int64(units_held * capacity * eligible)
            shortfall_units = jnp.where(
                positive_mark, _ceil_quantity_for_quanta(pe_shortfall, mark_quanta, issuer_scale), 0
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
                (
                    "proceeds",
                    jnp.where(tender_active, _value_quanta_from_quantity(target, mark_quanta, issuer_scale), 0),
                ),
            ):
                pe_opp[key] = pe_opp[key].at[issuer_idx].set(val)
            state = (cash, lot_remaining, cg_active, cg_ytd, tlh, state[5], state[6], state[7], state[8])
            state = book(
                jnp.where(tender_active & ~public_active, target, 0),
                mark_quanta,
                PrivateEquityDispositionKind.TENDER,
                state,
            )
            state = book(
                jnp.where(public_active, target, 0), mark_quanta, PrivateEquityDispositionKind.PUBLIC_MARKET, state
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
        liab_interest_ytd = liab_interest_ytd * keep
        carry = _ScanState(
            cash=cash,
            ordinary_ytd=ordinary,
            property_tax_ytd=property_tax_ytd,
            lot_remaining=lot_remaining,
            cost_basis_per_unit=cost_basis_per_unit,
            lot_purchase_month=lot_purchase_month,
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
            ta_buy_count=ta_buy_count,
        )
        product_output = None
        if product_summary is not None:
            product_inputs_local = product_inputs
            assert product_inputs_local is not None
            product_output = product_metrics(
                carry,
                snapshot_month=month + 1,
                obligation_shortfall=shortfall,
                obligation_mask=product_inputs_local.primary_obligation_mask[month],
            )
            if not emit_dense:
                return carry, product_output
        empty_events_bool = jnp.zeros((0, r), dtype=bool)
        empty_events_i64 = _zeros_i64((0, r))
        lifecycle_fired = jnp.stack(le_fired) if le_fired else empty_events_bool
        primary_residence_fired = jnp.stack(pr_fired) if pr_fired else empty_events_bool
        sale_trace_columns = PropertySaleTraceOutput(
            gross_proceeds=(
                jnp.stack([trace.gross_proceeds for trace in sale_traces]) if sale_traces else empty_events_i64
            ),
            mortgage_payoff=(
                jnp.stack([trace.mortgage_payoff for trace in sale_traces]) if sale_traces else empty_events_i64
            ),
            net_cash=jnp.stack([trace.net_cash for trace in sale_traces]) if sale_traces else empty_events_i64,
            realized_gain=(
                jnp.stack([trace.realized_gain for trace in sale_traces]) if sale_traces else empty_events_i64
            ),
            depreciation_recapture=(
                jnp.stack([trace.depreciation_recapture for trace in sale_traces]) if sale_traces else empty_events_i64
            ),
            section_121_exclusion=(
                jnp.stack([trace.section_121_exclusion for trace in sale_traces]) if sale_traces else empty_events_i64
            ),
            long_term_capital_gain=(
                jnp.stack([trace.long_term_capital_gain for trace in sale_traces]) if sale_traces else empty_events_i64
            ),
        )
        dense_output = DenseScanOutput(
            state=StateOutput(
                cash=cash,
                ordinary=ordinary,
                lots=lot_remaining,
                capital_gain_active=cg_active,
                capital_gain_ytd=cg_ytd,
                property_active=property_active,
                property_basis=property_basis,
                property_contribution=property_contribution,
                property_equity=property_equity,
                property_cumulative_depreciation=property_cum_dep,
                property_owner_occupied_months=property_owner_occupied,
                liability_active=liab_active,
                liability_principal=liab_principal,
                liability_monthly_payment=liab_monthly,
                liability_interest_ytd=liab_interest_ytd,
                failed=failed,
                failed_month=failed_month,
            ),
            cashflows=CashflowOutput(active=cashflow_active, amount=cashflow_amount),
            obligations=ObligationOutput(
                active=slot_active,
                due=accrual_due,
                paid=paid_buffer,
                shortfall=shortfall,
                failure_active=failure_active,
            ),
            property_purchases=purchase_active_rows,
            mortgages=MortgageOutput(
                origination_active=mort_orig,
                payment_active=mort_pay_active,
                payment_interest=mort_pay_interest,
                payment_principal=mort_pay_principal,
                payment_total=mort_pay_total,
            ),
            taxes=TaxOutput(
                breakdown=jnp.stack(tax_breakdown),
                liability_amount=taxliab_amount,
                liability_active=taxliab_active,
                settlement_active=settle_active,
                settlement_amount=settle_amount,
                settlement_year_end=settle_year_end,
            ),
            target_allocation=TargetAllocationOutput(
                dispositions=DispositionOutput(
                    active=ta_disp_active, units=ta_disp_units, basis=ta_disp_basis, proceeds=ta_disp_proceeds
                ),
                obligation_attempt_policy=attempt_policy,
            ),
            private_equity=PrivateEquityOutput(
                dispositions=DispositionOutput(
                    active=pe_disp_active, units=pe_disp_units, basis=pe_disp_basis, proceeds=pe_disp_proceeds
                ),
                opportunities=PrivateEquityOpportunityOutput(
                    active=pe_opp["active"],
                    outcome=pe_opp["outcome"],
                    floor=pe_opp["floor"],
                    liquid_net_worth=pe_opp["lnw"],
                    shortfall=pe_opp["shortfall"],
                    units_held=pe_opp["units"],
                    sellable_units=pe_opp["sellable"],
                    target_units=pe_opp["target"],
                    proceeds=pe_opp["proceeds"],
                ),
            ),
            lifecycle=LifecycleOutput(fired=lifecycle_fired, property_sales=sale_trace_columns),
            primary_residence_fired=primary_residence_fired,
        )
        return carry, (dense_output, product_output) if product_output is not None else dense_output

    init = _ScanState(
        cash=jnp.broadcast_to(cfg.cash_initial_balance[:, None], (p.cash_count, r)),
        ordinary_ytd=ordinary0,
        property_tax_ytd=property_tax_ytd0,
        lot_remaining=jnp.broadcast_to(cfg.lot_initial_quantity[:, None], (p.lot_count, r)),
        cost_basis_per_unit=jnp.broadcast_to(cfg.cost_basis_per_unit[:, None], (p.lot_count, r)),
        lot_purchase_month=jnp.broadcast_to(cfg.lot_purchase_month[:, None], (p.lot_count, r)),
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
        ta_buy_count=_zeros_i64((ta_policy_count, ta_max_sleeves, r)),
    )
    months = jnp.arange(horizon, dtype=jnp.int32)
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
        if not emit_dense:
            return (initial_ys, ys), _ProductTailOutput(
                sale_oversell=final_carry.sale_oversell,
                failed_month=final_carry.failed_month,
                target_allocation_buy_count=final_carry.ta_buy_count,
            )
        dense_ys, product_ys = ys
        dense_tail = _dense_final_output(final_carry)
        return (dense_ys, (initial_ys, product_ys)), _DenseProductTailOutput(
            dense=dense_tail, failed_month=final_carry.failed_month
        )
    final_carry, ys = jax.lax.scan(step, init, months)
    # Horizon-collapsed outputs, read off the final carry rather than emitted per month: the
    # scheduled-sale dispositions (accumulated at each sale's firing month) and the per-lot cost
    # basis (written once at purchase and never revised — a lot slot is never reused).
    return ys, _dense_final_output(final_carry)


def _dense_final_output(final_carry: _ScanState) -> DenseFinalOutput[jax.Array]:
    return DenseFinalOutput(
        lot_cost_basis=final_carry.cost_basis_per_unit,
        lot_purchase_month=final_carry.lot_purchase_month,
        scheduled_dispositions=DispositionOutput(
            active=final_carry.sale_disp_units > 0,
            units=final_carry.sale_disp_units,
            basis=final_carry.sale_disp_basis,
            proceeds=final_carry.sale_disp_proceeds,
        ),
        sale_oversell=final_carry.sale_oversell,
        target_allocation_buy_count=final_carry.ta_buy_count,
    )


def _amount_values(
    *,
    amount_kind: int,
    amount_fixed: int,
    amount_base: int,
    amount_series: Int[Array, ""],
    amount_base_month: int,
    amount_period: int,
    external_values: Float64[Array, " series rollout snapshot"],
    month: int | Int[Array, ""],
    rollout_count: int,
) -> Int64[Array, " rollout"]:
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
    return _scale_money_by_float_ratio(amount_base, reset_level, base_level)


def _amount_values_tuple(
    spec: tuple[int, int, int, int, int],
    series_op: Int[Array, ""],
    external_values: Float64[Array, " series rollout snapshot"],
    month: int | Int[Array, ""],
    r: int,
) -> Int64[Array, " rollout"]:
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
    cash: Int64[Array, " cash rollout"],
    *,
    debit: Int[Array, ""] | Int[Array, " flow"] | Int[np.ndarray, ""] | Int[np.ndarray, " flow"] | int,
    credit: Int[Array, ""] | Int[Array, " flow"] | Int[np.ndarray, ""] | Int[np.ndarray, " flow"] | int,
    amount: Int64[Array, " rollout"] | Int64[Array, " flow rollout"],
    row_of_world: int,
) -> Int64[Array, " cash rollout"]:
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

    def rows(
        side: Int[Array, ""] | Int[Array, " flow"] | Int[np.ndarray, ""] | Int[np.ndarray, " flow"] | int,
    ) -> Int[Array, " flow"]:
        resolved = jnp.where(jnp.asarray(side).reshape(-1) < 0, row_of_world, jnp.asarray(side).reshape(-1))
        return jnp.broadcast_to(resolved, (flow.shape[0],))

    return cash.at[rows(debit)].add(-flow).at[rows(credit)].add(flow)


def _scatter_rows(
    target: Int64[Array, " row rollout"], indices: Int[Array, " source"], values: Int64[Array, " source rollout"]
) -> Int64[Array, " row rollout"]:
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


def _gather_rows(source: jax.Array, idx: Int[Array, " source"]) -> jax.Array:
    """Gather `source[idx[s]]` into `(slots, rollouts)`, tolerating an empty source (`idx` is
    expected pre-clamped to valid rows; rows for inapplicable slots are masked off by the caller).
    A 0-row source (e.g. a scenario with no properties/liabilities) yields zeros."""
    if source.shape[0] == 0:
        return jnp.zeros((idx.shape[0], *source.shape[1:]), source.dtype)
    return source[idx]


def _amount_values_vec(
    amount_kind: Int64[Array, " slot"],
    amount_fixed: Int64[Array, " slot"],
    amount_base: Int64[Array, " slot"],
    amount_series: Int64[Array, " slot"],
    amount_base_month: Int64[Array, " slot"],
    amount_period: Int64[Array, " slot"],
    external_values: Float64[Array, " series rollout snapshot"],
    month: Int[Array, ""],
    rollout_count: int,
) -> Int64[Array, " slot rollout"]:
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
    series_amount = _scale_money_by_float_ratio(amount_base[:, None], reset_level, base_level)
    return jnp.where((amount_kind == AMOUNT_FIXED)[:, None], amount_fixed[:, None], series_amount)


@partial(jax.jit, static_argnames=("row_of_world",))
def _cashflows_jit(
    cashflows: CashflowExecution[jax.Array],
    property_active: Bool[Array, " property rollout"],
    cash: Int64[Array, " cash rollout"],
    ordinary_ytd: Int64[Array, " income_bucket rollout"],
    active: Bool[Array, " rollout"],
    external_values: Float64[Array, " series rollout snapshot"],
    month: Int[Array, ""],
    row_of_world: int,
) -> tuple[
    Int64[Array, " cash rollout"],
    Int64[Array, " income_bucket rollout"],
    Bool[Array, " cashflow rollout"],
    Int64[Array, " cashflow rollout"],
]:
    rollout_count = cash.shape[1]
    cashflow = jax.tree.map(lambda value: value[month], cashflows)
    property_gated = cashflow.property_slot >= 0
    safe_property_slot = jnp.maximum(cashflow.property_slot, 0)
    property_gate = _gather_rows(property_active, safe_property_slot)
    fire = cashflow.active[:, None] & active[None, :] & (~property_gated[:, None] | property_gate)
    raw = _amount_values_vec(
        cashflow.amount_kind,
        cashflow.amount_fixed,
        cashflow.amount_base,
        cashflow.amount_series,
        cashflow.amount_base_month,
        cashflow.amount_period,
        external_values,
        month,
        rollout_count,
    )
    amounts = jnp.where(fire, raw, 0)
    cash = _move_cash(
        cash, debit=cashflow.from_slot, credit=cashflow.to_slot, amount=amounts, row_of_world=row_of_world
    )
    ordinary_ytd = _scatter_rows(ordinary_ytd, cashflow.income_profile, amounts)
    ordinary_ytd = _scatter_rows(ordinary_ytd, cashflow.deduction_profile, -amounts)
    return cash, ordinary_ytd, fire, amounts


@partial(jax.jit, static_argnames=("row_of_world", "has_indexed"))
def _bond_cashflows_jit(
    bonds: BondExecution[jax.Array],
    cash: Int64[Array, " cash rollout"],
    ordinary_ytd: Int64[Array, " income_bucket rollout"],
    active: Bool[Array, " rollout"],
    external_values: Float64[Array, " series rollout snapshot"],
    month: Int[Array, ""],
    row_of_world: int,
    has_indexed: bool,
) -> tuple[Int64[Array, " cash rollout"], Int64[Array, " income_bucket rollout"]]:
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
        coupons = jnp.where(active[None, :], bonds.coupon[month, :, None], 0)
        redemptions = jnp.where(active[None, :], bonds.redemption[month, :, None], 0)
        paid = coupons + redemptions
        # The rest of the world funds every coupon and redemption: the issuer is not modeled.
        cash = _move_cash(cash, debit=row_of_world, credit=bonds.to_slot, amount=paid, row_of_world=row_of_world)
        return cash, _scatter_rows(ordinary_ytd, bonds.income_row, coupons)

    is_indexed = (bonds.indexed > 0)[:, None]
    safe_series = jnp.maximum(bonds.cpi_series, 0)
    cpi_base = external_values[safe_series, :, bonds.index_base_month]
    safe_base = jnp.where(cpi_base > 0, cpi_base, 1.0)
    principal = _scale_money_by_float_ratio(bonds.face[:, None], external_values[safe_series, :, month], safe_base)
    principal_prev = _scale_money_by_float_ratio(
        bonds.face[:, None], external_values[safe_series, :, jnp.maximum(month - 1, 0)], safe_base
    )

    indexed_coupon = _scale_money(principal, bonds.period_rate[:, None]) * bonds.pays[month, :, None]
    # Deflation floor: a TIPS redeems at the greater of its indexed principal and par, which
    # is what makes it a floor in exactly the scenarios the floor exists for.
    indexed_redemption = jnp.maximum(principal, bonds.face[:, None]) * bonds.matures[month, :, None]

    coupons = jnp.where(is_indexed, indexed_coupon, bonds.coupon[month, :, None])
    redemptions = jnp.where(is_indexed, indexed_redemption, bonds.redemption[month, :, None])

    # Phantom income: the month's rise in indexed principal is taxable interest with no cash
    # behind it. Gated on `on_books` so it stops at maturity, and on month > 0 so the opening
    # month does not accrete against itself.
    accretion = jnp.where(
        is_indexed & (bonds.on_books[month] > 0)[:, None] & (month > 0), principal - principal_prev, jnp.int64(0)
    )
    accretion = jnp.where(active[None, :], accretion, 0)

    coupons = jnp.where(active[None, :], coupons, 0)
    redemptions = jnp.where(active[None, :], redemptions, 0)
    paid = coupons + redemptions
    cash = _move_cash(cash, debit=row_of_world, credit=bonds.to_slot, amount=paid, row_of_world=row_of_world)
    # Accretion reaches income and NOT cash — if it ever reached the cash tensor, the
    # conservation invariant would break immediately, which is the guard on this wiring.
    return cash, _scatter_rows(ordinary_ytd, bonds.income_row, coupons + accretion)


def _distribution_payouts_jit(
    distributions: DistributionExecution[jax.Array],
    lot_remaining: Int64[Array, " lot rollout"],
    cash: Int64[Array, " cash rollout"],
    ordinary_ytd: Int64[Array, " income_bucket rollout"],
    active: Bool[Array, " rollout"],
    external_money_values: Int64[Array, " series rollout snapshot"],
    month: Int[Array, ""],
    row_of_world: int,
) -> tuple[Int64[Array, " cash rollout"], Int64[Array, " income_bucket rollout"]]:
    """This month's fund distributions, one row per (pool, tax slice).

    `units * dollars_per_unit` — the same multiplication the engine performs to mark a
    holding, which is the whole point of the model emitting a per-unit primitive instead of a
    yield. No rate appears here and nothing is divided by a price.

    The slices of one pool round independently, and the payout IS their sum, so there is no
    unrounded total for them to fail to add up to. Like a coupon, every payout is funded by
    `row_of_world`: the fund is not a modeled agent, so without that debit the cash would come
    from nowhere.
    """

    # `(slice, R)` units held, from the pool masks against the current lot quantities. A pool's
    # lots share one quantum size, so one divide per slice recovers whole units.
    quanta_held = distributions.lot_mask @ lot_remaining
    per_unit_money = external_money_values[distributions.series, :, month]
    # The fraction is a non-monetary model parameter; the resulting cash is
    # rounded once to the scenario currency quantum.
    value_quanta = _value_quanta_from_quantity(quanta_held, per_unit_money, distributions.quantity_scale[:, None])
    paid = _scale_money(value_quanta, distributions.fraction[:, None])
    paid = jnp.where(active[None, :], paid, 0)
    cash = _move_cash(cash, debit=row_of_world, credit=distributions.to_slot, amount=paid, row_of_world=row_of_world)
    return cash, _scatter_rows(ordinary_ytd, distributions.income_row, paid)


def _sleeve_prices_quanta(
    sleeve_series: Int64[Array, " sleeve"],
    external_money_values: Int64[Array, " series rollout snapshot"],
    month: int | Int[Array, ""],
) -> Int64[Array, " sleeve rollout"]:
    """This month's market price per sleeve, `(sleeve, R)`, in cents per unit.

    One read, shared by the observation the policy sizes against and the execution that
    follows — if those two disagreed, an order would mean something other than what it said.
    A sleeve whose asset has no modeled price series comes back zero: unpriceable, not free.
    """

    if external_money_values.shape[0] == 0:
        return jnp.zeros((sleeve_series.shape[0], 1), dtype=jnp.int64)
    valid = sleeve_series >= 0
    price = external_money_values[jnp.where(valid, sleeve_series, 0), :, month]
    return jnp.where(valid[:, None] & (price > 0), price, 0)


def _fifo_sell(
    lot_remaining: Int64[Array, " rollout lot"],
    ordered_lots: np.ndarray,
    target: Int64[Array, " rollout"],
    unit_price: Int64[Array, " rollout"],
    cost_basis_per_unit: Int64[Array, " lot rollout"],
    lot_quantity_scale: Int64[Array, " lot"],
) -> tuple[Int64[Array, " rollout lot"], Int64[Array, " rollout lot"], Int64[Array, " rollout lot"]]:
    """FIFO-sell a quanta target down a pool's lots, returning sold quanta plus cent values.

    Quanta is the only denomination. A caller wanting to raise a dollar amount converts it
    first — `target_allocation._quanta_for_quanta` — because converting here would mean the
    engine dividing by a price and rounding, which is it choosing how much to trade rather
    than executing what it was told.

    Oversell zeroes the whole target rather than part-filling it: a caller that asked for more
    than the pool holds gets nothing, and the obligation it could not fund fails the rollout at
    settlement, where the failure reads as an unpaid bill instead of a silent short fill.
    """

    ordered_quantity = lot_remaining[:, ordered_lots]
    ordered_scale = lot_quantity_scale[ordered_lots]
    price_col = unit_price[:, None]
    effective_target = jnp.where(target > ordered_quantity.sum(axis=1), 0, target)
    before = jnp.cumsum(ordered_quantity, axis=1) - ordered_quantity
    sold_ordered = jnp.clip(effective_target[:, None] - before, 0, ordered_quantity)
    proceeds_ordered = _value_quanta_from_quantity(sold_ordered, price_col, ordered_scale[None, :])
    basis_ordered = _value_quanta_from_quantity(
        sold_ordered, cost_basis_per_unit[ordered_lots].T, ordered_scale[None, :]
    )
    zeros = jnp.zeros_like(lot_remaining)
    sold_units = zeros.at[:, ordered_lots].set(sold_ordered)
    proceeds = zeros.at[:, ordered_lots].set(proceeds_ordered)
    basis = zeros.at[:, ordered_lots].set(basis_ordered)
    return sold_units, proceeds, basis


def _apply_tlh_give_back(
    folded_harvest: tuple[_FoldedHarvest, ...],
    tlh_cumulative_harvest: Int64[Array, " harvest_policy rollout"],
    lot_remaining: Int64[Array, " lot rollout"],
    sold_units: Int64[Array, " rollout lot"],
    gains: Int64[Array, " rollout lot"],
) -> tuple[Int64[Array, " rollout lot"], Int64[Array, " harvest_policy rollout"]]:
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
        give_back = _scale_quanta_by_ratio(cumulative, units_sold, jnp.where(pre_sale_units > 0, pre_sale_units, 1))
        per_lot_give_back = _scale_quanta_by_ratio(
            give_back[:, None], sold_policy, jnp.where(units_sold[:, None] > 0, units_sold[:, None], 1)
        )
        allocated = per_lot_give_back.sum(axis=1)
        gains = gains.at[:, lot_indices].add(per_lot_give_back)
        tlh_cumulative_harvest = tlh_cumulative_harvest.at[policy_idx].set(cumulative - allocated)
    return gains, tlh_cumulative_harvest


def _record_capital_gains(
    folded_harvest: tuple[_FoldedHarvest, ...],
    lot_purchase_month: Int64[Array, " lot rollout"],
    cg_profiles: tuple[int, ...],
    capital_gain_active: Bool[Array, " capital_gain_profile gain_class rollout"],
    capital_gain_ytd: Int64[Array, " capital_gain_profile gain_class rollout"],
    tlh_cumulative_harvest: Int64[Array, " harvest_policy rollout"],
    lot_remaining: Int64[Array, " lot rollout"],
    month: int | Int[Array, ""],
    sold_units: Int64[Array, " rollout lot"],
    gains: Int64[Array, " rollout lot"],
) -> tuple[
    Bool[Array, " capital_gain_profile gain_class rollout"],
    Int64[Array, " capital_gain_profile gain_class rollout"],
    Int64[Array, " harvest_policy rollout"],
]:
    """TLH give-back, then classify each lot's gain
    long/short and accrue.

    Branch-free: the long/short split is a `(L, R)` boolean mask (holding period vs the lot's
    purchase month), so the whole `[2, R]` classification block is one masked sum/any — no
    per-lot scatter loop, no data-dependent branching. The only Python loop is over the
    statically-resolved capital-gain profiles (`cg_profiles`) of the selling agent.

    Per-rollout rather than `(L,)`: a slot a policy chose to fill is bought in a different
    month in each rollout, and one rollout's short-term gain is another's long-term.
    """
    gains, tlh_cumulative_harvest = _apply_tlh_give_back(
        folded_harvest, tlh_cumulative_harvest, lot_remaining, sold_units, gains
    )
    long_mask = (month - lot_purchase_month) >= 12  # (L, R)
    masks = jnp.stack([long_mask, ~long_mask])  # (2, L, R), rows ordered LONG_TERM=0, SHORT_TERM=1
    sold = sold_units > 0  # (R, L)
    # einsum over lots: (2, L, R) x (R, L) -> (2, R) per-classification gain sums.
    gains_by_class = jnp.einsum("clr,rl->cr", masks.astype(gains.dtype), gains)
    active_by_class = (masks & sold.T[None, :, :]).any(axis=1)  # (2, R)
    for profile in cg_profiles:
        capital_gain_active = capital_gain_active.at[profile].set(capital_gain_active[profile] | active_by_class)
        capital_gain_ytd = capital_gain_ytd.at[profile].add(gains_by_class)
    return capital_gain_active, capital_gain_ytd, tlh_cumulative_harvest


@jax.jit
def _obligation_accruals_jit(
    inputs: ObligationExecution[jax.Array],
    property_active: Bool[Array, " property rollout"],
    liab_principal: Int64[Array, " liability rollout"],
    liab_monthly: Int64[Array, " liability rollout"],
    liab_active: Bool[Array, " liability rollout"],
    taxliab_active: Bool[Array, " tax_liability rollout"],
    taxliab_amount: Int64[Array, " tax_liability rollout"],
    active: Bool[Array, " rollout"],
    external_values: Float64[Array, " series rollout snapshot"],
    month: Int[Array, ""],
) -> tuple[Bool[Array, " obligation rollout"], Int64[Array, " obligation rollout"]]:
    """Compute each obligation source, then merge their disjoint active and due rows."""

    rollout_count = active.shape[0]

    def batch(
        source_active: Bool[Array, " obligation"],
        due: Int64[Array, " obligation rollout"],
        source_mask: Bool[Array, " obligation rollout"],
    ) -> tuple[Bool[Array, " obligation rollout"], Int64[Array, " obligation rollout"]]:
        payment_active = source_active[:, None] & active[None, :] & source_mask & (due > 0)
        return payment_active, jnp.where(payment_active, due, 0)

    configured = inputs.configured
    configured_due = _amount_values_vec(
        configured.amount_kind,
        configured.amount_fixed,
        configured.amount_base,
        configured.amount_series,
        configured.amount_base_month,
        configured.amount_period,
        external_values,
        month,
        rollout_count,
    )
    configured_property_slot = inputs.metadata.property_slot
    configured_property_mask = jnp.where(
        configured_property_slot[:, None] >= 0,
        _gather_rows(property_active, jnp.maximum(configured_property_slot, 0)),
        True,
    )
    configured_batch = batch(configured.active, configured_due, configured_property_mask)

    property_tax = inputs.property_tax
    property_tax_due = jnp.broadcast_to(property_tax.amount[:, None], configured_due.shape)
    property_tax_mask = _gather_rows(property_active, jnp.maximum(inputs.metadata.property_slot, 0)) & (
        property_tax.property_purchase_month[:, None] < month
    )
    property_tax_batch = batch(property_tax.active, property_tax_due, property_tax_mask)

    mortgage = inputs.mortgage
    principal = _gather_rows(liab_principal, mortgage.liability_slot)
    mortgage_interest = _scale_money(principal, mortgage.annual_rate[:, None] / 12.0)
    mortgage_due = jnp.minimum(_gather_rows(liab_monthly, mortgage.liability_slot), principal + mortgage_interest)
    mortgage_mask = (
        _gather_rows(liab_active, mortgage.liability_slot)
        & (principal > 0)
        & (mortgage.property_purchase_month[:, None] < month)
    )
    mortgage_batch = batch(mortgage.active, mortgage_due, mortgage_mask)

    estimated_tax = inputs.estimated_tax
    estimated_due = jnp.broadcast_to(estimated_tax.quarterly_amount[:, None], configured_due.shape)
    estimated_batch = batch(estimated_tax.active, estimated_due, jnp.ones_like(estimated_due, dtype=bool))

    eligible_tax = jnp.where(taxliab_active, taxliab_amount, 0)

    q4 = inputs.q4_estimated_tax
    q4_actual = q4.tax_liability_selector @ eligible_tax
    q4_safe_harbor = jnp.minimum(q4.prior_year_tax[:, None], q4_actual)
    q4_due = jnp.maximum(q4_safe_harbor - _scale_money(q4.prior_year_tax[:, None], 0.75), 0)
    q4_batch = batch(q4.active, q4_due, jnp.ones_like(q4_due, dtype=bool))

    true_up = inputs.tax_true_up
    true_up_actual = true_up.tax_liability_selector @ eligible_tax
    true_up_safe_harbor = jnp.minimum(true_up.prior_year_tax[:, None], true_up_actual)
    true_up_due = jnp.maximum(true_up_actual - true_up_safe_harbor, 0)
    true_up_batch = batch(true_up.active, true_up_due, jnp.ones_like(true_up_due, dtype=bool))

    batches = (configured_batch, property_tax_batch, mortgage_batch, estimated_batch, q4_batch, true_up_batch)
    return (
        jnp.stack([source_active for source_active, _ in batches]).any(axis=0),
        jnp.stack([source_due for _, source_due in batches]).sum(axis=0),
    )


@jax.jit
def _obligation_group_funded_jit(
    group_matrix: Int64[Array, " obligation other_obligation"],
    from_slot: Int64[Array, " obligation"],
    cash: Int64[Array, " cash rollout"],
    payment_active: Bool[Array, " obligation rollout"],
    payment_amount: Int64[Array, " obligation rollout"],
) -> Bool[Array, " obligation rollout"]:
    """Branch-free funding check for one emitted Pay batch.

    Every same-agent/source-account group must have enough cash for its complete active batch;
    no per-slot ordering can turn an unaffordable set into a partial success. The per-slot group
    is a static `(slots, slots)` membership matrix, so the group sums are one matmul.
    """
    amount_masked = jnp.where(payment_active, payment_amount, 0)  # (slots, rollouts)
    group_due = group_matrix.astype(amount_masked.dtype) @ amount_masked  # (slots, rollouts)
    cash_padded = jnp.concatenate([cash, jnp.zeros((1, cash.shape[1]), cash.dtype)], axis=0)
    available = cash_padded[jnp.where(from_slot < 0, cash.shape[0], from_slot)]  # (slots, rollouts), -1 -> 0
    return payment_active & (available >= group_due - 1e-9)


@partial(jax.jit, static_argnames=("row_of_world",))
def _settlement_core_jit(
    metadata: ObligationMetadataExecution[jax.Array],
    actions: PayActions,
    funded: Bool[Array, " obligation rollout"],
    cash: Int64[Array, " cash rollout"],
    ordinary_ytd: Int64[Array, " income_bucket rollout"],
    property_tax_ytd: Int64[Array, " tax_profile rollout"],
    property_rented_fraction: Float64[Array, " property rollout"],
    failed: Bool[Array, " rollout"],
    failed_month: Int64[Array, " rollout"],
    month: Int[Array, ""],
    row_of_world: int,
) -> tuple[
    Bool[Array, " obligation rollout"],
    Int64[Array, " obligation rollout"],
    Int64[Array, " cash rollout"],
    Int64[Array, " income_bucket rollout"],
    Int64[Array, " tax_profile rollout"],
    Int64[Array, " obligation rollout"],
    Bool[Array, " obligation rollout"],
    Bool[Array, " rollout"],
    Int64[Array, " rollout"],
]:
    """Branch-free settlement of an emitted Pay batch: per-slot pay/fail, the funded cash move,
    property-tax owner-share YTD accumulation, and Schedule-E/itemized deduction — all
    vectorized over slots (duplicate from/to/profile indices accumulate via `_scatter_rows`).

    Failure ordering is month-stable: every slot that fails this month would stamp the same
    `month`, so the per-rollout first-failure month is `month` iff any slot fails and it had not
    failed before. Mortgage liability updates and tax settlement are handled by the caller.
    """
    paid = actions.active & funded
    slot_failed = actions.active & ~funded
    paid_amount = jnp.where(paid, actions.amount_quanta, 0)
    cash = _move_cash(
        cash, debit=metadata.from_slot, credit=metadata.to_slot, amount=paid_amount, row_of_world=row_of_world
    )
    property_slot = metadata.property_slot
    has_property_slot = property_slot >= 0
    rented = _gather_rows(property_rented_fraction, jnp.maximum(property_slot, 0))  # (slots, rollouts)
    property_tax_ytd = _scatter_rows(
        property_tax_ytd,
        metadata.property_tax_profile,
        jnp.where(metadata.property_tax_profile[:, None] >= 0, _scale_money(paid_amount, 1.0 - rented), 0),
    )
    deductible = jnp.where(has_property_slot[:, None], rented, metadata.deductible_fraction[:, None])
    ordinary_ytd = _scatter_rows(
        ordinary_ytd,
        metadata.deduction_profile,
        jnp.where(metadata.deduction_profile[:, None] >= 0, -_scale_money(paid_amount, deductible), 0),
    )
    shortfall = jnp.where(slot_failed, actions.amount_quanta, 0)
    failed_this = slot_failed.any(axis=0)
    failed_month = jnp.where(failed_this & (failed_month < 0), month, failed_month)
    failed = failed | failed_this
    return paid, paid_amount, cash, ordinary_ytd, property_tax_ytd, shortfall, slot_failed, failed, failed_month


def _compute_liquid_net_worth(
    owner_cash_mask: Bool[Array, " cash"],
    lot_asset_series_index: Int64[Array, " lot"],
    owner_non_pe_lot_indices: tuple[int, ...],
    cash: Int64[Array, " cash rollout"],
    lot_remaining: Int64[Array, " lot rollout"],
    lot_quantity_scale: Int64[Array, " lot"],
    external_money_values: Int64[Array, " series rollout snapshot"],
    month: int | Int[Array, ""],
) -> Int64[Array, " rollout"]:
    """Owner cash + non-PE lot value at current marks.
    `owner_cash_mask` (this policy's row, device) and `lot_asset_series_index` (device) come from
    `_Operands`; `owner_non_pe_lot_indices` is the resolved (host) non-PE lot list (no `plan` reference)."""
    cash_total = (cash * owner_cash_mask[:, None]).sum(axis=0)
    if not owner_non_pe_lot_indices:
        return cash_total
    lot_indices = np.asarray(owner_non_pe_lot_indices, dtype=np.int64)
    series_indices = lot_asset_series_index[lot_indices]
    valid = series_indices >= 0
    prices = external_money_values[jnp.where(valid, series_indices, 0), :, month]
    prices = jnp.where(valid[:, None], prices, 0)
    lot_value = _value_quanta_from_quantity(
        lot_remaining[lot_indices, :], prices, lot_quantity_scale[lot_indices, None]
    ).sum(axis=0)
    return cash_total + lot_value


@partial(
    jax.jit,
    static_argnames=(
        "gain_profile",
        "has_prior",
        "peak_ppb",
        "floor_ppb",
        "half_exponent",
        "sensitivity_ppb",
        "short_term_fraction",
    ),
)
def _tlh_harvest_policy_jit(
    remaining_lots: Int64[Array, " policy_lot rollout"],
    cost_basis_lots: Int64[Array, " policy_lot rollout"],
    quantity_scale_lots: Int64[Array, " policy_lot"],
    price_quanta: Int64[Array, " rollout"],
    prior_price_quanta: Int64[Array, " rollout"],
    cumulative: Int64[Array, " rollout"],
    capital_gain_ytd: Int64[Array, " capital_gain_profile gain_class rollout"],
    capital_gain_active: Bool[Array, " capital_gain_profile gain_class rollout"],
    active: Bool[Array, " rollout"],
    *,
    gain_profile: int,
    has_prior: bool,
    peak_ppb: int,
    floor_ppb: int,
    half_exponent: int,
    sensitivity_ppb: int,
    short_term_fraction: float,
) -> tuple[
    Int64[Array, " capital_gain_profile gain_class rollout"],
    Bool[Array, " capital_gain_profile gain_class rollout"],
    Int64[Array, " rollout"],
]:
    """Apply one `HarvestPolicy`'s reduced-form monthly harvest, vectorized over rollouts:
    book a calibrated capital loss as a NEGATIVE in
    `capital_gain_ytd` and accumulate it into the give-back ledger `cumulative`. Per-policy params
    are static (the jitted core compiles once per policy)."""
    market_value = _value_quanta_from_quantity(remaining_lots, price_quanta[None, :], quantity_scale_lots[:, None]).sum(
        axis=0
    )
    original_basis = _value_quanta_from_quantity(remaining_lots, cost_basis_lots, quantity_scale_lots[:, None]).sum(
        axis=0
    )
    adjusted_basis = jnp.maximum(0, original_basis - cumulative)
    safe_market_value = jnp.where(market_value > 0, market_value, 1)
    embedded_gain_ppb = jnp.clip(
        _scale_quanta_by_ratio(market_value - adjusted_basis, jnp.int64(PPB), safe_market_value), 0, PPB
    )
    if has_prior:
        safe_prior = jnp.where(prior_price_quanta > 0, prior_price_quanta, 1)
        drawdown_ppb = jnp.maximum(
            0, _scale_quanta_by_ratio(prior_price_quanta - price_quanta, jnp.int64(PPB), safe_prior)
        )
    else:
        drawdown_ppb = jnp.zeros_like(price_quanta)  # month 0: no prior price, treat as flat
    # The curve is written to run eager and traced alike, so its return type is the union
    # of both array libraries; here it is always the traced one.
    fraction_ppb = cast(
        Int64[Array, " rollout"],
        harvest_fraction_curve_ppb(
            embedded_gain_ppb,
            drawdown_ppb,
            peak_annual_yield_ppb=peak_ppb,
            floor_annual_yield_ppb=floor_ppb,
            maturity_decay_half_exponent=half_exponent,
            drawdown_sensitivity_ppb=sensitivity_ppb,
        ),
    )
    ceiling = jnp.maximum(0, original_basis - cumulative)  # never harvest past available below-basis room
    gross = jnp.where(
        active, jnp.minimum(_scale_quanta_by_ratio(market_value, fraction_ppb, jnp.int64(PPB)), ceiling), 0
    )
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


def _apply_brackets(
    amount: Int64[Array, " rollout"], *, upper: Int64[Array, " bracket"], rate: Float64[Array, " bracket"], count: int
) -> Int64[Array, " rollout"]:
    """Progressive bracket tax on `amount`, in int64 cents rounded to the whole cent."""
    if count <= 0:
        return jnp.zeros_like(amount)
    upper_edges = jnp.asarray(upper[:count])
    bracket_rates = jnp.asarray(rate[:count])
    previous_upper = jnp.concatenate([jnp.zeros(1, dtype=upper_edges.dtype), upper_edges[:-1]])
    slice_top = jnp.minimum(amount[:, None], upper_edges[None, :])
    in_bracket = jnp.maximum(slice_top - previous_upper[None, :], 0)
    return _sum_scaled_money(in_bracket, bracket_rates[None, :], axis=1)


def _apply_ltcg_brackets(
    ltcg_amount: Int64[Array, " rollout"],
    ordinary_taxable: Int64[Array, " rollout"],
    *,
    upper: Int64[Array, " bracket"],
    rate: Float64[Array, " bracket"],
    count: int,
) -> Int64[Array, " rollout"]:
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
    return _sum_scaled_money(in_bracket, bracket_rates[None, :], axis=1)


def _net_capital_gains_jnp(
    short_term: Int64[Array, " rollout"],
    long_term: Int64[Array, " rollout"],
    carryforward_in: Int64[Array, " rollout"],
    *,
    # Broadcast against the loss: one cap per taxpayer in the engine, a bare row in tests.
    max_ordinary_offset_quanta: Int64[Array, " *cap"],
) -> tuple[Int64[Array, " rollout"], Int64[Array, " rollout"], Int64[Array, " rollout"], Int64[Array, " rollout"]]:
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
    ordinary_offset = jnp.minimum(residual_loss, max_ordinary_offset_quanta)
    return net_short_term, net_long_term, ordinary_offset, residual_loss - ordinary_offset


def _compute_tax_for_link(
    static: _LinkTaxStatic,
    tcfg: _TracedConfig,
    ordinary_ytd: Int64[Array, " income_bucket rollout"],
    capital_gain_ytd: Int64[Array, " capital_gain_profile gain_class rollout"],
    recapture_section_1250_ytd: Int64[Array, " tax_profile rollout"],
    liability_interest_ytd: Int64[Array, " liability rollout"],
    liability_rental_interest_ytd: Int64[Array, " liability rollout"],
    *,
    salt_deduction: Int64[Array, " rollout"],
    rollout_count: int,
) -> tuple[
    Int64[Array, " rollout"],
    Int64[Array, " rollout"],
    Int64[Array, " rollout"],
    Int64[Array, " rollout"],
    Int64[Array, " rollout"],
    Int64[Array, " rollout"],
]:
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
        owner_interest_ytd = liability_interest_ytd - liability_rental_interest_ytd
        mortgage_interest_deduction = _sum_money_with_factors(
            owner_interest_ytd, tcfg.mid_principal_factor[link][:, None], axis=0
        )
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
        # §63 nets the deduction against taxable income, which includes the gain, and §1(h)
        # rates what is left of it — the order the Qualified Dividends and Capital Gain Tax
        # Worksheet starts from, opening at Form 1040 line 15. Flooring the ordinary side at
        # zero and rating the whole gain on top would discard a deduction larger than
        # ordinary income and tax a return that owes nothing.
        total_taxable = jnp.maximum(ordinary_for_brackets + stcg + ltcg - deduction_used, 0)
        ordinary_taxable = jnp.maximum(ordinary_for_brackets + stcg - deduction_used, 0)
        capital_taxable = total_taxable - ordinary_taxable
        ordinary_tax = _apply_brackets(ordinary_taxable, upper=ordinary_upper, rate=ordinary_rate, count=ordinary_count)
        ltcg_tax = _apply_ltcg_brackets(
            capital_taxable,
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
    home_value_series: Int[Array, ""],
    external_values: Float64[Array, " series rollout snapshot"],
    *,
    cash: Int64[Array, " cash rollout"],
    property_active: Bool[Array, " property rollout"],
    property_rented_fraction: Float64[Array, " property rollout"],
    property_building_basis: Int64[Array, " property rollout"],
    property_cum_dep: Int64[Array, " property rollout"],
    oo_window: Bool[Array, " lookback property rollout"],
    liab_active: Bool[Array, " liability rollout"],
    liab_principal: Int64[Array, " liability rollout"],
    recapture_ytd: Int64[Array, " tax_profile rollout"],
    cg_active: Bool[Array, " capital_gain_profile gain_class rollout"],
    cg_ytd: Int64[Array, " capital_gain_profile gain_class rollout"],
    month: Int[Array, ""],
    active_property: Bool[Array, " rollout"],
    rollout_count: int,
    external_cash_slot: int,
) -> tuple[
    Int64[Array, " cash rollout"],
    Bool[Array, " property rollout"],
    Float64[Array, " property rollout"],
    Int64[Array, " property rollout"],
    Bool[Array, " liability rollout"],
    Int64[Array, " liability rollout"],
    Int64[Array, " tax_profile rollout"],
    Bool[Array, " capital_gain_profile gain_class rollout"],
    Int64[Array, " capital_gain_profile gain_class rollout"],
    PropertySaleTraceOutput[jax.Array],
]:
    """Branch-free `lax.scan` port of `_apply_property_sale`: §1250 recapture + §121 exclusion (via the
    owner-occupancy window) + mortgage payoff, returning the updated state and the 7-field sale trace.
    All per-property statics come from the hashable `_FoldedLifecycleEvent` (no `plan` reference)."""
    prop = ev.property_slot
    closing_cost_pct = ev.amount
    # `home_value_series` is a TRACED scalar row index (dynamic gather), not a baked static index.
    series_row = external_values[home_value_series]  # (rollouts, H+1)
    market_value = _scale_money_by_float_ratio(
        jnp.full(rollout_count, ev.purchase_price, dtype=jnp.int64), series_row[:, month], series_row[:, 0]
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
    sale_trace = PropertySaleTraceOutput(
        gross_proceeds=jnp.where(active_property, gross_proceeds, 0),
        mortgage_payoff=jnp.where(active_property, mortgage_payoff, 0),
        net_cash=jnp.where(active_property, net_cash, 0),
        realized_gain=jnp.where(active_property, realized_gain, 0),
        depreciation_recapture=jnp.where(active_property, recapture, 0),
        section_121_exclusion=jnp.where(active_property, section_121_exclusion, 0),
        long_term_capital_gain=jnp.where(active_property, ltcg, 0),
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
    property_active: Bool[Array, " property rollout"],
    property_rented_fraction: Float64[Array, " property rollout"],
    property_building_basis: Int64[Array, " property rollout"],
    property_cumulative_depreciation: Int64[Array, " property rollout"],
    property_depreciation_ytd: Int64[Array, " property rollout"],
    failed: Bool[Array, " rollout"],
) -> tuple[Int64[Array, " property rollout"], Int64[Array, " property rollout"]]:
    """§168 straight-line monthly depreciation,
    branch-free over all properties (one masked elementwise accrual)."""
    monthly_dep = jnp.where(
        (~failed)[None, :] & property_active,
        _scale_money(property_building_basis, property_rented_fraction / (27.5 * 12.0)),
        0,
    )
    return property_cumulative_depreciation + monthly_dep, property_depreciation_ytd + monthly_dep
