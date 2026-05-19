"""Forward simulation loop.

`simulate(scenario, rollout_count) → SimulationRun` runs the
per-month step over the scenario's horizon and produces the
state-over-time long-form frames + the event log.

The loop carries `state_t` (the polars cross-section, no
month_index column) forward. Each iteration:

  events_t = step_emit_events(state_t, scenario, market,
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

import polars as pl

from augur.sim.apply import apply_events
from augur.sim.events import EventLog
from augur.sim.jurisdictions import Jurisdiction, load_jurisdiction
from augur.sim.market import materialize_market
from augur.sim.run import SimulationRun
from augur.sim.scenario import Scenario
from augur.sim.state import (
    ASSET_LOT_SCHEMA,
    CAPITAL_GAINS_YTD_SCHEMA,
    CASH_BALANCES_SCHEMA,
    ORDINARY_INCOME_YTD_SCHEMA,
    TAX_LIABILITIES_SCHEMA,
    StateCrossSection,
)
from augur.sim.step import step_emit_events


def simulate(scenario: Scenario, *, rollout_count: int) -> SimulationRun:
    if rollout_count <= 0:
        msg = f"rollout_count must be positive; got {rollout_count}"
        raise ValueError(msg)
    market = materialize_market(
        scenario.market, rollout_count=rollout_count, horizon_months=int(scenario.horizon_months)
    )
    jurisdictions = _load_jurisdictions_for(scenario)
    state_t = _initial_state(scenario, rollout_count)
    cross_sections: list[StateCrossSection] = [state_t]
    events_by_month: list[EventLog] = []
    for month in range(int(scenario.horizon_months)):
        events_t = step_emit_events(
            state=state_t,
            scenario=scenario,
            market=market,
            jurisdictions=jurisdictions,
            month=month,
            rollout_count=rollout_count,
        )
        state_t = apply_events(state_t, events_t)
        cross_sections.append(state_t)
        events_by_month.append(events_t)
    return SimulationRun(
        cash_balances=_stack_cash_balances(cross_sections),
        asset_lots=_stack_asset_lots(cross_sections),
        ordinary_income_ytd=_stack_income_ytd(cross_sections),
        capital_gains_ytd=_stack_capital_gains(cross_sections),
        tax_liabilities=_stack_tax_liabilities(cross_sections),
        market_prices=market.prices,
        events_log=_concat_events(events_by_month),
    )


def _load_jurisdictions_for(scenario: Scenario) -> dict[str, Jurisdiction]:
    """Load every jurisdiction referenced by any tax profile.
    Loaded once at sim start; the step closes over the dict."""
    ids = {jid for profile in scenario.tax_profiles for jid in profile.jurisdiction_ids}
    return {jid: load_jurisdiction(jid) for jid in ids}


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
    capital_gains_ytd = pl.DataFrame(schema=CAPITAL_GAINS_YTD_SCHEMA)
    tax_liabilities = pl.DataFrame(schema=TAX_LIABILITIES_SCHEMA)
    return StateCrossSection(
        cash_balances=cash,
        asset_lots=asset_lots,
        ordinary_income_ytd=ordinary_income_ytd,
        capital_gains_ytd=capital_gains_ytd,
        tax_liabilities=tax_liabilities,
    )


def _initial_cash(scenario: Scenario, rollouts: pl.DataFrame) -> pl.DataFrame:
    if not scenario.initial_cash:
        return pl.DataFrame(schema=CASH_BALANCES_SCHEMA)
    entries = pl.DataFrame(
        {
            "agent_id": [e.agent_id for e in scenario.initial_cash],
            "account_id": [e.account_id for e in scenario.initial_cash],
            "balance_usd": [e.balance_usd for e in scenario.initial_cash],
        },
        schema={"agent_id": pl.Utf8(), "account_id": pl.Utf8(), "balance_usd": pl.Float64()},
    )
    return rollouts.join(entries, how="cross").select(list(CASH_BALANCES_SCHEMA.keys()))


def _initial_asset_lots(scenario: Scenario, rollouts: pl.DataFrame) -> pl.DataFrame:
    if not scenario.initial_lots:
        return pl.DataFrame(schema=ASSET_LOT_SCHEMA)
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
    return rollouts.join(entries, how="cross").select(list(ASSET_LOT_SCHEMA.keys()))


def _initial_ordinary_income_ytd(scenario: Scenario, rollouts: pl.DataFrame) -> pl.DataFrame:
    """One row per (taxed agent, rollout) at YTD = 0. Agents
    without a tax profile aren't tracked here — there's no use
    case for accumulating income on a non-taxed account."""
    if not scenario.tax_profiles:
        return pl.DataFrame(schema=ORDINARY_INCOME_YTD_SCHEMA)
    profile_rows = pl.DataFrame(
        {
            "agent_id": [p.agent_id for p in scenario.tax_profiles],
            "ordinary_income_usd": [0.0] * len(scenario.tax_profiles),
        },
        schema={"agent_id": pl.Utf8(), "ordinary_income_usd": pl.Float64()},
    )
    return rollouts.join(profile_rows, how="cross").select(list(ORDINARY_INCOME_YTD_SCHEMA.keys()))


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


def _concat_events(events_by_month: list[EventLog]) -> EventLog:
    """Concatenate per-month event logs into one cumulative log."""
    transfer_blocks = [e.transfers for e in events_by_month if not e.transfers.is_empty()]
    purchase_blocks = [e.asset_purchases for e in events_by_month if not e.asset_purchases.is_empty()]
    disposition_blocks = [e.lot_dispositions for e in events_by_month if not e.lot_dispositions.is_empty()]
    accrual_blocks = [e.tax_accruals for e in events_by_month if not e.tax_accruals.is_empty()]
    empty = EventLog.empty()
    return EventLog(
        transfers=pl.concat(transfer_blocks) if transfer_blocks else empty.transfers,
        asset_purchases=pl.concat(purchase_blocks) if purchase_blocks else empty.asset_purchases,
        lot_dispositions=pl.concat(disposition_blocks) if disposition_blocks else empty.lot_dispositions,
        tax_accruals=pl.concat(accrual_blocks) if accrual_blocks else empty.tax_accruals,
    )
