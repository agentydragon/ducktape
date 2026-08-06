"""Decode a `SimulationRun` into product-shaped metrics and events.

Per-month metric reductions take a `rollout_index` and read that column directly out of a
(possibly batched) run's dense buffers; event decoding operates on an already-R=1 run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import numpy as np
import polars as pl

from finance.augur.model.series import HomeValueKey, LocationId
from finance.augur.product.asset_key import AssetKey, PrivateEquityAssetKey, asset_price_key, parse_asset_key
from finance.augur.product.metric_composition import DERIVED_METRIC_NAMES, compose_metric
from finance.augur.product.wire import (
    CapitalImprovementMarkerEvent,
    ClosingCostPaymentEvent,
    HoaDuesPaymentEvent,
    HoldingSaleEvent,
    HomeownersInsurancePaymentEvent,
    MonthlyExpenseEvent,
    MortgagePaymentEvent,
    OutsideRentPaymentEvent,
    PrivateEquityMarkerEvent,
    PrivateEquityOpportunityEvent,
    PropertyMaintenancePaymentEvent,
    PropertyPurchaseEvent,
    PropertySaleMarkerEvent,
    PropertyTaxPaymentEvent,
    RolloutEvent,
    RolloutFailureEvent,
    SetPrimaryResidenceMarkerEvent,
    SetRentedFractionMarkerEvent,
    TaxAccrualEvent,
    TaxPaymentEvent,
    TerminalMetrics,
)
from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.fixed_point import cents_array_to_usd
from finance.augur.sim.scenario import ObligationType

_TAX_PAYMENT_OBLIGATION_TYPES = (ObligationType.ESTIMATED_TAX, ObligationType.TAX_TRUE_UP)


def monthly_metric_arrays_batch(dense: SimulationRun, *, primary_agent_id: str) -> dict[str, np.ndarray]:
    """Per-month product metrics for **every** rollout of a batched result as `{name: (H+1, R)}`.

    Each metric is reduced over the whole `(…, R)` batch in one vectorized pass. `month_index` is
    the shared `(H+1,)` axis (no rollout dimension).
    """

    plan = dense.plan
    primary_agent_code = _required_string_code(plan.strings, primary_agent_id)
    cash_usd = _cash_by_month(dense, primary_agent_code=primary_agent_code)
    holding_value_usd = _holding_value_by_month(dense, primary_agent_code=primary_agent_code)
    private_equity_value_usd = _private_equity_value_by_month(dense, primary_agent_code=primary_agent_code)
    property_value_usd = _property_value_by_month(dense, primary_agent_code=primary_agent_code)
    mortgage_balance_usd = _mortgage_balance_by_month(dense, primary_agent_code=primary_agent_code)
    bond_value_usd = _bond_value_by_month(dense, primary_agent_code=primary_agent_code)
    base = {
        "cash_usd": cash_usd,
        "holding_value_usd": holding_value_usd,
        "private_equity_value_usd": private_equity_value_usd,
        "property_value_usd": property_value_usd,
        "mortgage_balance_usd": mortgage_balance_usd,
        "bond_value_usd": bond_value_usd,
        "shortfall_usd": _shortfall_by_month(dense, primary_agent_code=primary_agent_code),
    }
    # The derived sums come from `metric_composition` — the same definitions the engine's
    # on-device path composes — so the two cannot disagree about what net worth is.
    return {
        "month_index": np.arange(plan.horizon_months + 1, dtype=np.int64),
        **base,
        **{name: compose_metric(name, base.__getitem__) for name in DERIVED_METRIC_NAMES},
    }


def monthly_metric_arrays(
    dense: SimulationRun, *, primary_agent_id: str, rollout_index: int = 0
) -> dict[str, np.ndarray]:
    """Per-month product metrics for one rollout (column `rollout_index`) as `{name: (H+1,)}`."""
    batch = monthly_metric_arrays_batch(dense, primary_agent_id=primary_agent_id)
    return {name: (values if name == "month_index" else values[:, rollout_index]) for name, values in batch.items()}


def terminal_metrics_from_arrays(arrays: dict[str, np.ndarray], *, failed_month_index: int | None) -> TerminalMetrics:
    """Numpy-direct terminal-metrics extraction from `monthly_metric_arrays`."""

    if arrays["month_index"].size == 0:
        raise ValueError("rollout produced no monthly metrics")
    return TerminalMetrics(
        cash_usd=float(arrays["cash_usd"][-1]),
        holding_value_usd=float(arrays["holding_value_usd"][-1]),
        private_equity_value_usd=float(arrays["private_equity_value_usd"][-1]),
        property_value_usd=float(arrays["property_value_usd"][-1]),
        mortgage_balance_usd=float(arrays["mortgage_balance_usd"][-1]),
        bond_value_usd=float(arrays["bond_value_usd"][-1]),
        home_equity_usd=float(arrays["home_equity_usd"][-1]),
        liquid_net_worth_usd=float(arrays["liquid_net_worth_usd"][-1]),
        net_worth_usd=float(arrays["net_worth_usd"][-1]),
        shortfall_usd=float(arrays["shortfall_usd"].sum()),
        failed_month_index=failed_month_index,
    )


def failed_month_index_batch(dense: SimulationRun) -> np.ndarray:
    """Per-rollout failure month at the final snapshot; `NO_CODE` (-1) = never failed. Shape `(R,)`."""
    return cast(np.ndarray, dense.buffers.state.rollout_failed_month_state[-1, :])


def rollout_events_from(
    run: SimulationRun, *, primary_agent_id: str, asset_label_by_id: dict[str, str]
) -> tuple[RolloutEvent, ...]:
    events = [
        *_holding_sale_events(run, primary_agent_id=primary_agent_id, asset_label_by_id=asset_label_by_id),
        *_property_purchase_events(run, primary_agent_id=primary_agent_id),
        *_private_equity_events(run, primary_agent_id=primary_agent_id, asset_label_by_id=asset_label_by_id),
        *_private_equity_opportunities(run, primary_agent_id=primary_agent_id, asset_label_by_id=asset_label_by_id),
        *_mortgage_payment_events(run, primary_agent_id=primary_agent_id),
        *_property_tax_payment_events(run, primary_agent_id=primary_agent_id),
        *_hoa_dues_events(run, primary_agent_id=primary_agent_id),
        *_homeowners_insurance_events(run, primary_agent_id=primary_agent_id),
        *_property_maintenance_events(run, primary_agent_id=primary_agent_id),
        *_tax_accrual_events(run, primary_agent_id=primary_agent_id),
        *_tax_payment_events(run, primary_agent_id=primary_agent_id),
        *_monthly_expense_events(run, primary_agent_id=primary_agent_id),
        *_outside_rent_events(run, primary_agent_id=primary_agent_id),
        *_failure_events(run, primary_agent_id=primary_agent_id),
        *_set_rented_fraction_events(run),
        *_set_primary_residence_events(run, primary_agent_id=primary_agent_id),
        *_capital_improvement_events(run),
        *_property_sale_events(run),
    ]
    priority = {
        "property_purchase": 0,
        "closing_cost_payment": 1,
        "set_primary_residence": 2,
        "set_rented_fraction": 3,
        "capital_improvement": 4,
        "property_sale": 5,
        "private_equity_event": 6,
        "private_equity_opportunity": 7,
        "holding_sale": 8,
        "tax_accrual": 9,
        "tax_payment": 10,
        "property_tax_payment": 11,
        "hoa_dues_payment": 12,
        "homeowners_insurance_payment": 13,
        "property_maintenance_payment": 14,
        "mortgage_payment": 15,
        "monthly_expense": 16,
        "outside_rent": 17,
        "failure": 18,
    }
    return tuple(sorted(events, key=lambda event: (event.month_index, priority[event.kind])))


def _cash_by_month(dense: SimulationRun, *, primary_agent_code: int) -> np.ndarray:
    cash_slots = np.flatnonzero(dense.plan.cash_agent_codes == primary_agent_code)
    return cast(np.ndarray, cents_array_to_usd(dense.buffers.state.cash_state[:, cash_slots, :].sum(axis=1)))


def _holding_value_by_month(dense: SimulationRun, *, primary_agent_code: int) -> np.ndarray:
    """Sum of liquid-holding lots (stocks + crypto) priced at sampled series.

    Excludes private-equity lots: PE is illiquid (saleable only at tender events) so it
    doesn't count toward liquid net worth. PE valuation surfaces separately via
    `_private_equity_value_by_month`.
    """

    return _lot_value_by_month(
        dense, primary_agent_code=primary_agent_code, include=lambda asset: not isinstance(asset, PrivateEquityAssetKey)
    )


def _private_equity_value_by_month(dense: SimulationRun, *, primary_agent_code: int) -> np.ndarray:
    """Sum of private-equity lots priced at the latest sampled mark for each issuer."""

    return _lot_value_by_month(
        dense, primary_agent_code=primary_agent_code, include=lambda asset: isinstance(asset, PrivateEquityAssetKey)
    )


def _lot_value_by_month(
    dense: SimulationRun, *, primary_agent_code: int, include: Callable[[AssetKey], bool]
) -> np.ndarray:
    plan = dense.plan
    values = np.zeros((plan.horizon_months + 1, plan.rollout_count), dtype=np.float64)
    series_index_by_id = {key: index for index, key in enumerate(plan.series_keys)}
    pe_issuer_index = {str(issuer_id): idx for idx, issuer_id in enumerate(plan.pe_issuers.issuer_ids)}
    for lot in range(plan.lot_id_codes.shape[0]):
        if int(plan.lot_agent_codes[lot]) != primary_agent_code:
            continue
        asset = plan.assets[int(plan.lot_asset_codes[lot])]
        if not include(asset):
            continue
        quantity = dense.buffers.state.lot_state[:, lot, :] / float(plan.lot_quantity_scale[lot])  # (H+1, R)
        # PE lots take their mark from `pe_channels.marks` (typed bundle); non-PE lots
        # read from the series-indexed external_values cube. Both are stored R-major
        # `(…, R, months)`, so transpose to the `(months, R)` = `(H+1, R)` metric layout.
        if isinstance(asset, PrivateEquityAssetKey):
            issuer_idx = pe_issuer_index.get(str(asset.issuer_id))
            if issuer_idx is None:
                raise ValueError(f"holding asset {asset.wire_id!r} has no compiled PE channels")
            price = plan.pe_channels.marks[issuer_idx, :, :].T
        else:
            series_index = series_index_by_id.get(asset_price_key(asset))
            if series_index is None:
                raise ValueError(
                    f"holding asset {asset.wire_id!r} has no modeled price series in the compiled simulation"
                )
            price = plan.external_values[series_index, :, :].T
        missing_price = (np.abs(quantity) > 1e-9) & ~np.isfinite(price)
        if missing_price.any():
            month, rollout = (int(idx) for idx in np.argwhere(missing_price)[0])
            raise ValueError(
                f"holding asset {asset.wire_id!r} has non-finite modeled price at month {month}, rollout {rollout}"
            )
        values += quantity * price
    # Clamp: floating-point rounding in FIFO dollar-sells (sold_units = sold_value / price)
    # can leave lot quantities at ~-1e-10, producing a tiny negative value here.
    return np.maximum(values, 0.0)


def _shortfall_by_month(dense: SimulationRun, *, primary_agent_code: int) -> np.ndarray:
    plan = dense.plan
    shortfall = np.zeros((plan.horizon_months + 1, plan.rollout_count), dtype=np.float64)
    primary_obligations = plan.obligations.agent == primary_agent_code  # [H, O]
    shortfall[1:] = cents_array_to_usd(
        (dense.buffers.obligations.shortfall * primary_obligations[:, :, None].astype(np.int64)).sum(axis=1)
    )
    return shortfall


def _required_string_code(strings: tuple[str, ...], value: str) -> int:
    try:
        return strings.index(value)
    except ValueError as exc:
        raise ValueError(f"compiled simulation string table does not contain {value!r}") from exc


def _holding_sale_events(
    run: SimulationRun, *, primary_agent_id: str, asset_label_by_id: dict[str, str]
) -> tuple[RolloutEvent, ...]:
    sale_rows = (
        run.events_log.lot_dispositions.filter(pl.col("agent_id") == primary_agent_id)
        .group_by(["month_index", "asset_id"])
        .agg(
            pl.col("units_sold").sum(),
            pl.col("proceeds_usd").sum(),
            pl.col("cost_basis_consumed_usd").sum().alias("cost_basis_usd"),
        )
        .sort("month_index", "asset_id")
    )
    return tuple(
        HoldingSaleEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["proceeds_usd"]),
            asset=parse_asset_key(str(row["asset_id"])),
            asset_label=asset_label_by_id.get(str(row["asset_id"])),
            units=float(row["units_sold"]),
            proceeds_usd=float(row["proceeds_usd"]),
            cost_basis_usd=float(row["cost_basis_usd"]),
        )
        for row in sale_rows.iter_rows(named=True)
    )


def _private_equity_events(
    run: SimulationRun, *, primary_agent_id: str, asset_label_by_id: dict[str, str]
) -> tuple[RolloutEvent, ...]:
    # Filter PE asset rows by classifying each asset_id through the typed
    # `AssetKey` discriminator; polars itself can't dispatch on Python types,
    # but we can compute the set of PE asset wire ids in Python and use `is_in`.
    primary_assets = (
        run.asset_lots.filter(pl.col("agent_id") == primary_agent_id)
        .select("asset_id")
        .unique()
        .get_column("asset_id")
        .to_list()
    )
    primary_pe_assets = {
        asset_id for asset_id in primary_assets if isinstance(parse_asset_key(str(asset_id)), PrivateEquityAssetKey)
    }
    if not primary_pe_assets:
        return ()
    rows = run.events_log.private_equity_events.filter(pl.col("asset_id").is_in(primary_pe_assets)).sort(
        "month_index", "issuer_id", "event_kind"
    )
    return tuple(
        PrivateEquityMarkerEvent(
            month_index=int(row["month_index"]),
            amount_usd=0.0,
            issuer_id=str(row["issuer_id"]),
            asset=parse_asset_key(str(row["asset_id"])),
            asset_label=asset_label_by_id.get(str(row["asset_id"])),
            event_kind=str(row["event_kind"]),
            regime=str(row["regime"]),
            mark_usd=float(row["mark_usd"]),
            sale_capacity_fraction=float(row["sale_capacity_fraction"]),
            eligible_fraction=float(row["eligible_fraction"]),
            forced_sale_fraction=float(row["forced_sale_fraction"]),
            liquidity_blocked=bool(row["liquidity_blocked"]),
            forced_recovery_cashout_usd=float(row["forced_recovery_cashout_usd"]),
        )
        for row in rows.iter_rows(named=True)
    )


def _private_equity_opportunities(
    run: SimulationRun, *, primary_agent_id: str, asset_label_by_id: dict[str, str]
) -> tuple[RolloutEvent, ...]:
    primary_assets = (
        run.asset_lots.filter(pl.col("agent_id") == primary_agent_id)
        .select("asset_id")
        .unique()
        .get_column("asset_id")
        .to_list()
    )
    primary_pe_assets = {
        asset_id for asset_id in primary_assets if isinstance(parse_asset_key(str(asset_id)), PrivateEquityAssetKey)
    }
    if not primary_pe_assets:
        return ()
    rows = run.events_log.private_equity_opportunities.filter(pl.col("asset_id").is_in(primary_pe_assets)).sort(
        "month_index", "issuer_id", "outcome"
    )
    return tuple(
        PrivateEquityOpportunityEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["proceeds_usd"]),
            issuer_id=str(row["issuer_id"]),
            asset=parse_asset_key(str(row["asset_id"])),
            asset_label=asset_label_by_id.get(str(row["asset_id"])),
            event_kind=str(row["event_kind"]),
            regime=str(row["regime"]),
            outcome=str(row["outcome"]),
            mark_usd=float(row["mark_usd"]),
            sale_capacity_fraction=float(row["sale_capacity_fraction"]),
            eligible_fraction=float(row["eligible_fraction"]),
            liquidity_blocked=bool(row["liquidity_blocked"]),
            floor_usd=float(row["floor_usd"]),
            liquid_net_worth_usd=float(row["liquid_net_worth_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
            units_held=float(row["units_held"]),
            sellable_units=float(row["sellable_units"]),
            target_units=float(row["target_units"]),
            proceeds_usd=float(row["proceeds_usd"]),
        )
        for row in rows.iter_rows(named=True)
    )


def _monthly_expense_events(run: SimulationRun, *, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    expense_rows = run.events_log.obligation_settlements.filter(
        (pl.col("agent_id") == primary_agent_id) & (pl.col("obligation_type") == ObligationType.CASH_SPEND)
    ).sort("month_index", "obligation_id")
    return tuple(
        MonthlyExpenseEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["amount_paid_usd"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
        )
        for row in expense_rows.iter_rows(named=True)
    )


def _outside_rent_events(run: SimulationRun, *, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    rent_rows = run.events_log.obligation_settlements.filter(
        (pl.col("agent_id") == primary_agent_id) & (pl.col("obligation_type") == ObligationType.OUTSIDE_RENT)
    ).sort("month_index", "obligation_id")
    return tuple(
        OutsideRentPaymentEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["amount_paid_usd"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
        )
        for row in rent_rows.iter_rows(named=True)
    )


def _tax_accrual_events(run: SimulationRun, *, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    keys = ["rollout_index", "month_index", "cause_id", "agent_id", "jurisdiction_id", "tax_year_end_month"]
    breakdown_columns = [
        *keys,
        "ordinary_income_usd",
        "ltcg_usd",
        "stcg_usd",
        "standard_deduction_usd",
        "mortgage_interest_deduction_usd",
        "itemized_deduction_usd",
        "ordinary_tax_usd",
        "capital_gain_tax_usd",
        "total_tax_usd",
    ]
    accrual_rows = (
        run.events_log.tax_accruals.filter(pl.col("agent_id") == primary_agent_id)
        .join(run.events_log.tax_breakdowns.select(breakdown_columns), on=keys, how="left")
        .with_columns(
            ordinary_income_usd=pl.col("ordinary_income_usd").fill_null(0.0),
            ltcg_usd=pl.col("ltcg_usd").fill_null(0.0),
            stcg_usd=pl.col("stcg_usd").fill_null(0.0),
            standard_deduction_usd=pl.col("standard_deduction_usd").fill_null(0.0),
            mortgage_interest_deduction_usd=pl.col("mortgage_interest_deduction_usd").fill_null(0.0),
            itemized_deduction_usd=pl.col("itemized_deduction_usd").fill_null(0.0),
            ordinary_tax_usd=pl.col("ordinary_tax_usd").fill_null(pl.col("amount_usd")),
            capital_gain_tax_usd=pl.col("capital_gain_tax_usd").fill_null(0.0),
            total_tax_usd=pl.col("total_tax_usd").fill_null(pl.col("amount_usd")),
        )
        .sort("month_index", "jurisdiction_id")
    )
    return tuple(
        TaxAccrualEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["amount_usd"]),
            jurisdiction_id=str(row["jurisdiction_id"]),
            tax_year_end_month=int(row["tax_year_end_month"]),
            ordinary_income_usd=float(row["ordinary_income_usd"]),
            ltcg_usd=float(row["ltcg_usd"]),
            stcg_usd=float(row["stcg_usd"]),
            ordinary_tax_usd=float(row["ordinary_tax_usd"]),
            capital_gain_tax_usd=float(row["capital_gain_tax_usd"]),
            total_tax_usd=float(row["total_tax_usd"]),
            mortgage_interest_deduction_usd=float(row["mortgage_interest_deduction_usd"]),
            itemized_deduction_usd=float(row["itemized_deduction_usd"]),
            standard_deduction_usd=float(row["standard_deduction_usd"]),
        )
        for row in accrual_rows.iter_rows(named=True)
    )


def _tax_payment_events(run: SimulationRun, *, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    tax_payment_rows = run.events_log.obligation_settlements.filter(
        (pl.col("agent_id") == primary_agent_id) & pl.col("obligation_type").is_in(_TAX_PAYMENT_OBLIGATION_TYPES)
    ).sort("month_index", "obligation_id")
    return tuple(
        TaxPaymentEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["amount_paid_usd"]),
            obligation_type=str(row["obligation_type"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
        )
        for row in tax_payment_rows.iter_rows(named=True)
    )


def _failure_events(run: SimulationRun, *, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    failure_rows = run.events_log.rollout_failures.filter(pl.col("agent_id") == primary_agent_id)
    return tuple(
        RolloutFailureEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["shortfall_usd"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
        )
        for row in failure_rows.iter_rows(named=True)
    )


def _property_value_by_month(dense: SimulationRun, *, primary_agent_code: int) -> np.ndarray:
    plan = dense.plan
    values = np.zeros((plan.horizon_months + 1, plan.rollout_count), dtype=np.float64)
    series_index_by_id = {key: index for index, key in enumerate(plan.series_keys)}
    for prop in range(plan.properties.id.shape[0]):
        if int(plan.properties.buyer_agent[prop]) != primary_agent_code:
            continue
        active = dense.buffers.state.property_active_state[:, prop, :]  # (H+1, R) bool
        purchase_month = int(plan.properties.month[prop])
        if purchase_month < 0:
            continue
        location_id = plan.strings[int(plan.properties.location_id[prop])]
        series_index = series_index_by_id.get(HomeValueKey(location_id=LocationId(location_id)))
        if series_index is None:
            continue
        levels = np.nan_to_num(plan.external_values[series_index, :, :], nan=0.0).T  # (H+1, R)
        # State snapshots are H+1 rows: index 0 = pre-month-0 opening, index s = end of month s-1.
        # The property is active starting at snapshot index `purchase_month + 1` (end of purchase month).
        base_level = levels[purchase_month]  # (R,) per-rollout base value at the purchase month
        purchase_price = float(cents_array_to_usd(plan.properties.purchase_price[prop]))
        # Per rollout: market = purchase_price × level / base_level. Rollouts whose base level never
        # resolved (0) contribute nothing for this property (the R=1 path skipped it via `continue`).
        safe_base = np.where(base_level == 0.0, 1.0, base_level)
        market = np.where(base_level[None, :] == 0.0, 0.0, purchase_price * levels / safe_base[None, :])
        values += np.where(active, market, 0.0)
    return values


def _bond_value_by_month(dense: SimulationRun, *, primary_agent_code: int) -> np.ndarray:
    """Face still on the books each month, for the primary agent's bonds.

    A par bond held to maturity is never marked, so its value is its face and the whole
    series is a compile-time constant — identical across rollouts. Failed rollouts are
    zeroed to match every other term, which the engine does via its own failure mask; this
    has to reproduce it because bonds carry no state for the failure freeze to act on.
    """

    plan = dense.plan
    face = np.where(plan.bonds.agent == primary_agent_code, plan.bonds.face, 0)
    if plan.bonds.indexed.any():
        # A TIPS is carried at CPI-scaled principal, not par — otherwise net worth understates
        # it in exactly the inflationary scenarios the ladder is held for. Rollout-varying, so
        # this branch cannot use the constant broadcast below.
        levels = plan.external_values[np.maximum(plan.bonds.cpi_series, 0)]  # (bond, R, month)
        base = np.take_along_axis(levels, plan.bonds.index_base_month[:, None, None], axis=2)
        principal = np.round(face[:, None, None] * levels / np.where(base > 0, base, 1.0))
        carried = np.where((plan.bonds.indexed > 0)[:, None, None], principal, face[:, None, None])
        value = cents_array_to_usd(np.einsum("mb,brm->mr", plan.bonds.on_books, carried))
    else:
        per_month = cents_array_to_usd(plan.bonds.on_books @ face)  # (H+1,)
        value = np.broadcast_to(per_month[:, None], (plan.horizon_months + 1, plan.rollout_count)).copy()
    failed_month = failed_month_index_batch(dense)
    months = np.arange(plan.horizon_months + 1)[:, None]
    # Strictly greater, matching every other term. Snapshot `i` is the state ENTERING month `i`,
    # so a rollout that fails DURING month `m` still has a real snapshot at `m` — cash and
    # holdings both keep theirs. `>=` zeroed the opening snapshot too, which showed a portfolio
    # losing its ladder one month before it lost anything else.
    return np.where((failed_month[None, :] >= 0) & (months > failed_month[None, :]), 0.0, value)


def _mortgage_balance_by_month(dense: SimulationRun, *, primary_agent_code: int) -> np.ndarray:
    plan = dense.plan
    balance = np.zeros((plan.horizon_months + 1, plan.rollout_count), dtype=np.float64)
    for lia in range(plan.liabilities.codes.shape[0]):
        if int(plan.liabilities.agent[lia]) != primary_agent_code:
            continue
        balance += cents_array_to_usd(dense.buffers.state.liability_principal_state[:, lia, :])
    return balance


def _property_purchase_events(run: SimulationRun, *, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    primary_purchases = run.events_log.property_purchases.filter(pl.col("buyer_agent_id") == primary_agent_id)
    originations = run.events_log.mortgage_originations.select(
        pl.col("rollout_index"),
        pl.col("month_index"),
        pl.col("property_id"),
        pl.col("principal_usd").alias("mortgage_principal_usd"),
    )
    joined = primary_purchases.join(
        originations, on=["rollout_index", "month_index", "property_id"], how="left"
    ).with_columns(mortgage_principal_usd=pl.col("mortgage_principal_usd").fill_null(0.0))
    events: list[RolloutEvent] = []
    for row in joined.iter_rows(named=True):
        events.append(
            PropertyPurchaseEvent(
                month_index=int(row["month_index"]),
                amount_usd=float(row["purchase_price_usd"]),
                property_id=str(row["property_id"]),
                purchase_price_usd=float(row["purchase_price_usd"]),
                # equity_ledger_usd = purchase_price - mortgage_principal (compiler line 866);
                # equals the cash down payment.
                down_payment_usd=float(row["equity_ledger_usd"]),
                mortgage_principal_usd=float(row["mortgage_principal_usd"]),
            )
        )
        closing_cost = float(row["closing_cost_usd"])
        if closing_cost > 0:
            events.append(
                ClosingCostPaymentEvent(
                    month_index=int(row["month_index"]), amount_usd=closing_cost, property_id=str(row["property_id"])
                )
            )
    return tuple(events)


def _mortgage_payment_events(run: SimulationRun, *, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    payment_rows = run.events_log.mortgage_payments.filter(pl.col("agent_id") == primary_agent_id).sort("month_index")
    return tuple(
        MortgagePaymentEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["total_payment_usd"]),
            interest_usd=float(row["interest_usd"]),
            principal_usd=float(row["principal_usd"]),
        )
        for row in payment_rows.iter_rows(named=True)
    )


def _property_tax_payment_events(run: SimulationRun, *, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    rows = run.events_log.obligation_settlements.filter(
        (pl.col("agent_id") == primary_agent_id) & (pl.col("obligation_type") == ObligationType.PROPERTY_TAX)
    ).sort("month_index")
    return tuple(
        PropertyTaxPaymentEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["amount_paid_usd"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
        )
        for row in rows.iter_rows(named=True)
    )


def _hoa_dues_events(run: SimulationRun, *, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    rows = run.events_log.obligation_settlements.filter(
        (pl.col("agent_id") == primary_agent_id) & (pl.col("obligation_type") == ObligationType.HOA_DUES)
    ).sort("month_index")
    return tuple(
        HoaDuesPaymentEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["amount_paid_usd"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
        )
        for row in rows.iter_rows(named=True)
    )


def _homeowners_insurance_events(run: SimulationRun, *, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    rows = run.events_log.obligation_settlements.filter(
        (pl.col("agent_id") == primary_agent_id) & (pl.col("obligation_type") == ObligationType.HOMEOWNERS_INSURANCE)
    ).sort("month_index")
    return tuple(
        HomeownersInsurancePaymentEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["amount_paid_usd"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
        )
        for row in rows.iter_rows(named=True)
    )


def _property_maintenance_events(run: SimulationRun, *, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    rows = run.events_log.obligation_settlements.filter(
        (pl.col("agent_id") == primary_agent_id) & (pl.col("obligation_type") == ObligationType.PROPERTY_MAINTENANCE)
    ).sort("month_index")
    return tuple(
        PropertyMaintenancePaymentEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["amount_paid_usd"]),
            amount_due_usd=float(row["amount_due_usd"]),
            amount_paid_usd=float(row["amount_paid_usd"]),
            shortfall_usd=float(row["shortfall_usd"]),
        )
        for row in rows.iter_rows(named=True)
    )


def _set_rented_fraction_events(run: SimulationRun) -> tuple[RolloutEvent, ...]:
    """Lifecycle SetRentedFraction markers. Product scenarios only model the primary owner,
    so every lifecycle event in the log belongs to a primary-owned property."""

    rows = run.events_log.set_rented_fraction_events.sort("month_index", "property_id")
    return tuple(
        SetRentedFractionMarkerEvent(
            month_index=int(row["month_index"]),
            amount_usd=0.0,
            property_id=str(row["property_id"]),
            rented_fraction=float(row["rented_fraction"]),
        )
        for row in rows.iter_rows(named=True)
    )


def _set_primary_residence_events(run: SimulationRun, *, primary_agent_id: str) -> tuple[RolloutEvent, ...]:
    rows = run.events_log.set_primary_residence_events.filter(pl.col("agent_id") == primary_agent_id).sort(
        "month_index", "agent_id"
    )
    return tuple(
        SetPrimaryResidenceMarkerEvent(
            month_index=int(row["month_index"]),
            amount_usd=0.0,
            agent_id=str(row["agent_id"]),
            property_id=None if row["property_id"] is None else str(row["property_id"]),
            is_primary_residence=bool(row["is_primary_residence"]),
        )
        for row in rows.iter_rows(named=True)
    )


def _capital_improvement_events(run: SimulationRun) -> tuple[RolloutEvent, ...]:
    rows = run.events_log.capital_improvement_events.sort("month_index", "property_id")
    return tuple(
        CapitalImprovementMarkerEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["amount_usd"]),
            property_id=str(row["property_id"]),
        )
        for row in rows.iter_rows(named=True)
    )


def _property_sale_events(run: SimulationRun) -> tuple[RolloutEvent, ...]:
    rows = run.events_log.property_sale_events.sort("month_index", "property_id")
    return tuple(
        PropertySaleMarkerEvent(
            month_index=int(row["month_index"]),
            amount_usd=float(row["gross_proceeds_usd"]),
            property_id=str(row["property_id"]),
            gross_proceeds_usd=float(row["gross_proceeds_usd"]),
            mortgage_payoff_usd=float(row["mortgage_payoff_usd"]),
            net_cash_to_owner_usd=float(row["net_cash_to_owner_usd"]),
            realized_gain_usd=float(row["realized_gain_usd"]),
            depreciation_recapture_usd=float(row["depreciation_recapture_usd"]),
            section_121_exclusion_usd=float(row["section_121_exclusion_usd"]),
            long_term_capital_gain_usd=float(row["long_term_capital_gain_usd"]),
        )
        for row in rows.iter_rows(named=True)
    )
