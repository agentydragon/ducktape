"""Forward simulation loop.

`simulate(scenario, rollout_count) → SimulationRun` runs the
per-month step over the scenario's horizon and produces the
state-over-time long-form frames + the event log.

The loop carries `state_t` (the polars cross-section, no
month_index column) forward. Each iteration:

  events_t = step_emit_events(state_t, scenario, external_series,
                              jurisdictions, month, rollout_count)
  state_t = apply_events(state_t, events_t)

`apply_events` is the only state-mutation point. The replay
invariant holds by construction: at any month M, `state_t` equals
`apply_events(initial_state, events_log.filter(month < M))`.

The state-over-time frames in the returned `SimulationRun` are
the concatenation of the per-month cross-sections with
`month_index` injected as a column.
"""

from __future__ import annotations

import os
from typing import Literal, cast

import polars as pl

from augur.sim.apply import apply_events
from augur.sim.events import EventLog
from augur.sim.external_series import ExternalSeriesContext, materialize_external_series
from augur.sim.run import SimulationRun
from augur.sim.runtime import load_jurisdictions_for, load_locations_for
from augur.sim.scenario import Scenario
from augur.sim.state import (
    ASSET_LOT_FRAME,
    CAPITAL_GAINS_YTD_FRAME,
    CASH_BALANCES_FRAME,
    LIABILITY_FRAME,
    ORDINARY_INCOME_YTD_FRAME,
    PROPERTY_STAKE_FRAME,
    PROPERTY_STATE_FRAME,
    ROLLOUT_STATUS_FRAME,
    TAX_LIABILITIES_FRAME,
    StateCrossSection,
)
from augur.sim.step import step_emit_policy_events, step_emit_scheduled_events

type SimulationEngineName = Literal["polars", "numba"]


def simulate(scenario: Scenario, *, rollout_count: int, engine: SimulationEngineName | None = None) -> SimulationRun:
    if rollout_count <= 0:
        msg = f"rollout_count must be positive; got {rollout_count}"
        raise ValueError(msg)
    external_series = materialize_external_series(
        scenario.external_series, rollout_seeds=tuple(range(rollout_count)), horizon_months=int(scenario.horizon_months)
    )
    return simulate_with_external_series(
        scenario, rollout_count=rollout_count, external_series=external_series, engine=engine
    )


def simulate_with_external_series(
    scenario: Scenario,
    *,
    rollout_count: int,
    external_series: ExternalSeriesContext,
    engine: SimulationEngineName | None = None,
) -> SimulationRun:
    if rollout_count <= 0:
        msg = f"rollout_count must be positive; got {rollout_count}"
        raise ValueError(msg)
    engine = _resolve_engine(engine)
    if engine == "numba":
        from augur.sim.numba.engine import simulate_with_external_series_numba  # noqa: PLC0415

        return simulate_with_external_series_numba(
            scenario, rollout_count=rollout_count, external_series=external_series
        )
    if engine != "polars":
        msg = f"unsupported simulation engine: {engine!r}"
        raise ValueError(msg)
    return _simulate_with_external_series_polars(scenario, rollout_count=rollout_count, external_series=external_series)


def _resolve_engine(engine: SimulationEngineName | None) -> SimulationEngineName:
    selected = engine or os.environ.get("AUGUR_SIM_ENGINE") or "polars"
    if selected not in ("polars", "numba"):
        msg = f"unsupported simulation engine: {selected!r}"
        raise ValueError(msg)
    return cast(SimulationEngineName, selected)


def _simulate_with_external_series_polars(
    scenario: Scenario, *, rollout_count: int, external_series: ExternalSeriesContext
) -> SimulationRun:
    jurisdictions = load_jurisdictions_for(scenario)
    locations = load_locations_for(scenario)
    state_t = _initial_state(scenario, rollout_count)
    cross_sections: list[StateCrossSection] = [state_t]
    events_by_month: list[EventLog] = []
    for month in range(int(scenario.horizon_months)):
        events_p1 = step_emit_scheduled_events(
            state=state_t,
            scenario=scenario,
            external_series=external_series,
            jurisdictions=jurisdictions,
            locations=locations,
            month=month,
            rollout_count=rollout_count,
        )
        state_t = apply_events(state_t, events_p1)
        events_p2 = step_emit_policy_events(
            state=state_t, scenario=scenario, external_series=external_series, locations=locations, month=month
        )
        state_t = apply_events(state_t, events_p2)
        cross_sections.append(state_t)
        events_by_month.append(EventLog.concat([events_p1, events_p2]))
    return SimulationRun(
        cash_balances=_stack_cash_balances(cross_sections),
        asset_lots=_stack_asset_lots(cross_sections),
        ordinary_income_ytd=_stack_income_ytd(cross_sections),
        capital_gains_ytd=_stack_capital_gains(cross_sections),
        tax_liabilities=_stack_tax_liabilities(cross_sections),
        property_state=_stack_property_state(cross_sections),
        property_stakes=_stack_property_stakes(cross_sections),
        liabilities=_stack_liabilities(cross_sections),
        rollout_status_history=_stack_rollout_status(cross_sections),
        rollout_status=cross_sections[-1].rollout_status,
        series_values=external_series.series_values,
        events_log=EventLog.concat(events_by_month),
    )


def _initial_state(scenario: Scenario, rollout_count: int) -> StateCrossSection:
    """Build the initial (month-0) state cross-section from the
    scenario's `initial_cash` and `initial_lots`. Each entry expands
    to one row per rollout via a cross join — no Python loop over
    rollouts. Pre-horizon `purchase_month_index` (e.g. -24 for a lot
    bought before the sim) is preserved as-is for later holding-
    period classification."""
    rollouts = pl.DataFrame({"rollout_index": list(range(rollout_count))}, schema={"rollout_index": pl.Int64()})
    cash = _initial_cash(scenario, rollouts)
    asset_lots = _initial_asset_lots(scenario, rollouts)
    ordinary_income_ytd = _initial_ordinary_income_ytd(scenario, rollouts)
    capital_gains_ytd = CAPITAL_GAINS_YTD_FRAME.empty()
    tax_liabilities = TAX_LIABILITIES_FRAME.empty()
    property_state = PROPERTY_STATE_FRAME.empty()
    property_stakes = PROPERTY_STAKE_FRAME.empty()
    liabilities = LIABILITY_FRAME.empty()
    rollout_status = _initial_rollout_status(rollouts)
    return StateCrossSection(
        cash_balances=cash,
        asset_lots=asset_lots,
        ordinary_income_ytd=ordinary_income_ytd,
        capital_gains_ytd=capital_gains_ytd,
        tax_liabilities=tax_liabilities,
        property_state=property_state,
        property_stakes=property_stakes,
        liabilities=liabilities,
        rollout_status=rollout_status,
    )


def _initial_rollout_status(rollouts: pl.DataFrame) -> pl.DataFrame:
    """One row per rollout, status = "active", failed_month = null."""
    return ROLLOUT_STATUS_FRAME.normalize(
        rollouts.with_columns(status=pl.lit("active", dtype=pl.Utf8()), failed_month=pl.lit(None, dtype=pl.Int64()))
    )


def _initial_cash(scenario: Scenario, rollouts: pl.DataFrame) -> pl.DataFrame:
    if not scenario.initial_cash:
        return CASH_BALANCES_FRAME.empty()
    entries = pl.DataFrame(
        {
            "agent_id": [e.agent_id for e in scenario.initial_cash],
            "account_id": [e.account_id for e in scenario.initial_cash],
            "balance_usd": [e.balance_usd for e in scenario.initial_cash],
        },
        schema={"agent_id": pl.Utf8(), "account_id": pl.Utf8(), "balance_usd": pl.Float64()},
    )
    return CASH_BALANCES_FRAME.normalize(rollouts.join(entries, how="cross"))


def _initial_asset_lots(scenario: Scenario, rollouts: pl.DataFrame) -> pl.DataFrame:
    if not scenario.initial_lots:
        return ASSET_LOT_FRAME.empty()
    entries = pl.DataFrame(
        {
            "lot_id": [lot.lot_id for lot in scenario.initial_lots],
            "agent_id": [lot.agent_id for lot in scenario.initial_lots],
            "asset_id": [lot.asset_id for lot in scenario.initial_lots],
            "purchase_month_index": [lot.purchase_month_index for lot in scenario.initial_lots],
            "cost_basis_per_unit_usd": [lot.cost_basis_per_unit_usd for lot in scenario.initial_lots],
            "remaining_quantity": [lot.quantity for lot in scenario.initial_lots],
        },
        schema={
            "lot_id": pl.Utf8(),
            "agent_id": pl.Utf8(),
            "asset_id": pl.Utf8(),
            "purchase_month_index": pl.Int64(),
            "cost_basis_per_unit_usd": pl.Float64(),
            "remaining_quantity": pl.Float64(),
        },
    )
    return ASSET_LOT_FRAME.normalize(rollouts.join(entries, how="cross"))


def _initial_ordinary_income_ytd(scenario: Scenario, rollouts: pl.DataFrame) -> pl.DataFrame:
    """One row per (taxed agent, rollout) at YTD = 0. Agents
    without a tax profile aren't tracked here — there's no use
    case for accumulating income on a non-taxed account."""
    if not scenario.tax_profiles:
        return ORDINARY_INCOME_YTD_FRAME.empty()
    profile_rows = pl.DataFrame(
        {
            "agent_id": [p.agent_id for p in scenario.tax_profiles],
            "ordinary_income_usd": [0.0] * len(scenario.tax_profiles),
        },
        schema={"agent_id": pl.Utf8(), "ordinary_income_usd": pl.Float64()},
    )
    return ORDINARY_INCOME_YTD_FRAME.normalize(rollouts.join(profile_rows, how="cross"))


def _stack_cash_balances(cross_sections: list[StateCrossSection]) -> pl.DataFrame:
    """Concatenate per-month cross-sections into the long-form
    state-over-time frame with `month_index` injected."""
    blocks = [
        cs.cash_balances.with_columns(pl.lit(month, dtype=pl.Int64()).alias("month_index"))
        for month, cs in enumerate(cross_sections)
    ]
    return pl.concat(blocks).select(["rollout_index", "month_index", "agent_id", "account_id", "balance_usd"])


def _stack_asset_lots(cross_sections: list[StateCrossSection]) -> pl.DataFrame:
    """Concatenate per-month lot cross-sections with `month_index`
    injected. A lot row exists every month from its creation
    onward; `remaining_quantity` shrinks as the lot is sold off."""
    blocks = [
        cs.asset_lots.with_columns(pl.lit(month, dtype=pl.Int64()).alias("month_index"))
        for month, cs in enumerate(cross_sections)
    ]
    return pl.concat(blocks).select(
        [
            "rollout_index",
            "month_index",
            "lot_id",
            "agent_id",
            "asset_id",
            "purchase_month_index",
            "cost_basis_per_unit_usd",
            "remaining_quantity",
        ]
    )


def _stack_income_ytd(cross_sections: list[StateCrossSection]) -> pl.DataFrame:
    blocks = [
        cs.ordinary_income_ytd.with_columns(pl.lit(month, dtype=pl.Int64()).alias("month_index"))
        for month, cs in enumerate(cross_sections)
    ]
    return pl.concat(blocks).select(["rollout_index", "month_index", "agent_id", "ordinary_income_usd"])


def _stack_capital_gains(cross_sections: list[StateCrossSection]) -> pl.DataFrame:
    blocks = [
        cs.capital_gains_ytd.with_columns(pl.lit(month, dtype=pl.Int64()).alias("month_index"))
        for month, cs in enumerate(cross_sections)
    ]
    return pl.concat(blocks).select(["rollout_index", "month_index", "agent_id", "classification", "gain_usd"])


def _stack_tax_liabilities(cross_sections: list[StateCrossSection]) -> pl.DataFrame:
    blocks = [
        cs.tax_liabilities.with_columns(pl.lit(month, dtype=pl.Int64()).alias("month_index"))
        for month, cs in enumerate(cross_sections)
    ]
    return pl.concat(blocks).select(
        ["rollout_index", "month_index", "agent_id", "jurisdiction_id", "tax_year_end_month", "amount_owed_usd"]
    )


def _stack_property_state(cross_sections: list[StateCrossSection]) -> pl.DataFrame:
    blocks = [
        cs.property_state.with_columns(pl.lit(month, dtype=pl.Int64()).alias("month_index"))
        for month, cs in enumerate(cross_sections)
    ]
    return pl.concat(blocks).select(
        ["rollout_index", "month_index", "property_id", "location_id", "purchase_month_index", "adjusted_basis_usd"]
    )


def _stack_property_stakes(cross_sections: list[StateCrossSection]) -> pl.DataFrame:
    blocks = [
        cs.property_stakes.with_columns(pl.lit(month, dtype=pl.Int64()).alias("month_index"))
        for month, cs in enumerate(cross_sections)
    ]
    return pl.concat(blocks).select(
        [
            "rollout_index",
            "month_index",
            "property_id",
            "agent_id",
            "ownership_pct",
            "contribution_used_usd",
            "equity_ledger_usd",
        ]
    )


def _stack_liabilities(cross_sections: list[StateCrossSection]) -> pl.DataFrame:
    blocks = [
        cs.liabilities.with_columns(pl.lit(month, dtype=pl.Int64()).alias("month_index"))
        for month, cs in enumerate(cross_sections)
    ]
    return pl.concat(blocks).select(
        [
            "rollout_index",
            "month_index",
            "liability_id",
            "agent_id",
            "payment_account_id",
            "counterparty_agent_id",
            "counterparty_account_id",
            "property_id",
            "principal_usd",
            "annual_interest_rate",
            "term_months",
            "origination_month_index",
            "monthly_payment_usd",
            "interest_paid_ytd_usd",
            "principal_paid_ytd_usd",
        ]
    )


def _stack_rollout_status(cross_sections: list[StateCrossSection]) -> pl.DataFrame:
    blocks = [
        cs.rollout_status.with_columns(pl.lit(month, dtype=pl.Int64()).alias("month_index"))
        for month, cs in enumerate(cross_sections)
    ]
    return pl.concat(blocks).select(["rollout_index", "month_index", "status", "failed_month"])
