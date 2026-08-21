"""Forward simulation entrypoints.

`simulate(scenario, rollout_count) -> SimulationRun` materializes external series and runs the JAX
engine: the whole month loop compiles into one XLA program, whose stacked outputs are scattered into
NumPy buffers. Analytics consumers decode those buffers as Polars frames; selected product rollout
detail projects directly from the plan and buffers.
"""

from __future__ import annotations

import polars as pl

from finance.augur.sim.buffers import SimulationBuffers
from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.compiler import CompiledSimulation
from finance.augur.sim.engine import (
    ProductMetricArrays,
    run_dense_simulation,
    run_dense_simulation_with_product_metrics,
)
from finance.augur.sim.external_series import ExternalSeriesContext, materialize_external_series
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import Scenario, SeriesIndexedAmount, TieredAmount


def simulate(scenario: Scenario, *, rollout_count: int, locations: dict[str, Location]) -> SimulationRun:
    if rollout_count <= 0:
        msg = f"rollout_count must be positive; got {rollout_count}"
        raise ValueError(msg)
    external_series = materialize_external_series(
        scenario.external_series, rollout_seeds=tuple(range(rollout_count)), horizon_months=int(scenario.horizon_months)
    )
    return simulate_with_external_series(
        scenario, rollout_count=rollout_count, external_series=external_series, locations=locations
    )


def simulate_with_external_series(
    scenario: Scenario, *, rollout_count: int, external_series: ExternalSeriesContext, locations: dict[str, Location]
) -> SimulationRun:
    if rollout_count <= 0:
        msg = f"rollout_count must be positive; got {rollout_count}"
        raise ValueError(msg)
    _validate_series_indexed_amounts(scenario, rollout_count=rollout_count, external_series=external_series)
    return run_dense_simulation(
        scenario, rollout_count=rollout_count, external_series=external_series, locations=locations
    )


def simulate_with_external_series_and_product_metrics(
    scenario: Scenario,
    *,
    rollout_count: int,
    external_series: ExternalSeriesContext,
    locations: dict[str, Location],
    primary_agent_id: str,
) -> tuple[CompiledSimulation, SimulationBuffers, ProductMetricArrays]:
    """Return raw dense outputs and selected-product metrics in one engine dispatch."""

    if rollout_count <= 0:
        msg = f"rollout_count must be positive; got {rollout_count}"
        raise ValueError(msg)
    _validate_series_indexed_amounts(scenario, rollout_count=rollout_count, external_series=external_series)
    return run_dense_simulation_with_product_metrics(
        scenario,
        rollout_count=rollout_count,
        external_series=external_series,
        locations=locations,
        primary_agent_id=primary_agent_id,
    )


_EMPTY_SERIES_ROWS = pl.Schema(
    {"rollout_index": pl.Int64(), "month_index": pl.Int64(), "value": pl.Float64()}
).to_frame()


def _validate_series_indexed_amounts(
    scenario: Scenario, *, rollout_count: int, external_series: ExternalSeriesContext
) -> None:
    """Validate path-indexed amount schedules before compiling dense arrays."""

    uses = [
        (label, amount, months)
        for label, amount, months in _series_indexed_amount_uses(scenario)
        if isinstance(amount, SeriesIndexedAmount) and months
    ]
    if not uses:
        # No path-indexed amounts → nothing references the external series here. Skip building
        # the per-(series, month, rollout) lookup entirely; at a 100-year, many-rollout horizon
        # that dict is millions of entries built from a Python row loop, all for nothing.
        return

    rows_by_key = dict(external_series.levels.value_rows())

    for label, amount, months in uses:
        before_base = [month for month in months if month < amount.base_month_index]
        if before_base:
            raise ValueError(
                f"series-indexed amount {label} is active at month {before_base[0]} "
                f"before base month {amount.base_month_index}"
            )
        wire_id = amount.series.wire_id
        base_month = int(amount.base_month_index)
        # An amount only indexes into its reset anchors (one per adjustment period) plus its base
        # month — a handful of months, not the whole horizon. Restrict to those rows and check
        # rollout coverage with a columnar group/count instead of materializing a
        # per-(series, month, rollout) Python dict over the full external frame.
        required_months = sorted({base_month, *(amount._reset_month(month) for month in months)})
        key_rows = rows_by_key.get(amount.series)
        series_rows = (
            _EMPTY_SERIES_ROWS if key_rows is None else key_rows.filter(pl.col("month_index").is_in(required_months))
        )
        present_count = dict(
            series_rows.filter(pl.col("value").is_not_null())
            .group_by("month_index")
            .agg(pl.col("rollout_index").n_unique().alias("present"))
            .iter_rows()
        )
        for month in required_months:
            if present_count.get(month, 0) < rollout_count:
                present_rollouts = set(
                    series_rows.filter((pl.col("month_index") == month) & pl.col("value").is_not_null())
                    .get_column("rollout_index")
                    .to_list()
                )
                missing_rollouts = [rollout for rollout in range(rollout_count) if rollout not in present_rollouts]
                raise KeyError(
                    f"series-indexed amount {label} references external series {wire_id!r} "
                    f"at month {month}, but it is missing rollout(s): {_format_rollout_sample(missing_rollouts)}"
                )
        zero_base_rollouts = sorted(
            series_rows.filter((pl.col("month_index") == base_month) & (pl.col("value") == 0.0))
            .get_column("rollout_index")
            .to_list()
        )
        if zero_base_rollouts:
            raise ValueError(
                f"external series {wire_id!r} has zero base level at month "
                f"{amount.base_month_index} for rollout(s): {_format_rollout_sample(zero_base_rollouts)}"
            )


def _series_indexed_amount_uses(scenario: Scenario) -> list[tuple[str, object, tuple[int, ...]]]:
    horizon = int(scenario.horizon_months)
    uses: list[tuple[str, object, tuple[int, ...]]] = []
    for scheduled_transfer in scenario.scheduled_transfers:
        transfer_months: tuple[int, ...] = (
            (scheduled_transfer.month,) if 0 <= scheduled_transfer.month < horizon else ()
        )
        uses.append((f"scheduled transfer {scheduled_transfer.cause_id!r}", scheduled_transfer.amount, transfer_months))
    for recurring_transfer in scenario.recurring_transfers:
        recurring_transfer_months = tuple(month for month in range(horizon) if recurring_transfer.is_active_at(month))
        uses.append(
            (
                f"recurring transfer {recurring_transfer.cause_id!r}",
                recurring_transfer.amount,
                recurring_transfer_months,
            )
        )
    for scheduled_cashflow in scenario.scheduled_property_cashflows:
        cashflow_months: tuple[int, ...] = (
            (scheduled_cashflow.month,) if 0 <= scheduled_cashflow.month < horizon else ()
        )
        uses.append(
            (f"scheduled property cashflow {scheduled_cashflow.cause_id!r}", scheduled_cashflow.amount, cashflow_months)
        )
    for recurring_cashflow in scenario.recurring_property_cashflows:
        cashflow_months = tuple(month for month in range(horizon) if recurring_cashflow.is_active_at(month))
        uses.append(
            (f"recurring property cashflow {recurring_cashflow.cause_id!r}", recurring_cashflow.amount, cashflow_months)
        )
    for scheduled_obligation in scenario.scheduled_obligations:
        obligation_months: tuple[int, ...] = (
            (scheduled_obligation.month,) if 0 <= scheduled_obligation.month < horizon else ()
        )
        uses.append(
            (
                f"scheduled obligation {scheduled_obligation.obligation_id!r}",
                scheduled_obligation.amount_due,
                obligation_months,
            )
        )
    for recurring_obligation in scenario.recurring_obligations:
        recurring_obligation_months = tuple(
            month for month in range(horizon) if recurring_obligation.is_active_at(month)
        )
        amount = recurring_obligation.amount_due
        if isinstance(amount, TieredAmount):
            uses.extend(
                (f"tier {tier.tier_id!r} monthly spend", tier.monthly_spend, recurring_obligation_months)
                for tier in amount.tiers
            )
            transition_months = tuple(month + 1 for month in recurring_obligation_months)
            uses.extend(
                (label, threshold, transition_months)
                for boundary in amount.boundaries
                for label, threshold in (
                    ("tier drop threshold", boundary.drop_below_liquid_net_worth),
                    ("tier recovery threshold", boundary.recover_above_liquid_net_worth),
                )
            )
        else:
            uses.append(
                (f"recurring obligation {recurring_obligation.obligation_id!r}", amount, recurring_obligation_months)
            )
    return uses


def _format_rollout_sample(rollout_indices: list[int]) -> str:
    sample = ", ".join(str(index) for index in rollout_indices[:5])
    if len(rollout_indices) > 5:
        sample += ", ..."
    return sample
