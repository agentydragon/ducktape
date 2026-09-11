"""Remaining configured caller: Python allocation, grouped claims, then PE operations.

This preserves the configured consumers' financial ordering, without giving the
financial kernel another policy or population loop.
"""

from collections import defaultdict
from copy import deepcopy
from typing import Any

import numpy as np
from pydantic import JsonValue

from finance.augur.policy.configured_allocation import PendingBuy, materialize_buy, plan, validate_prepared
from finance.augur.sim import results
from finance.augur.sim.actions import Buy, DecisionActions
from finance.augur.sim.capture import WorldResult
from finance.augur.sim.events import EVENT_FRAME_SPECS, EventLog
from finance.augur.sim.metric_composition import BASE_METRIC_NAMES
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.product_metrics import ProductMetricArrays
from finance.augur.sim.session import Capture, _Session


def execute(run: CompiledRun, capture: Capture, product_actor: str | None = None) -> tuple[WorldResult, ...]:
    if not isinstance(run, CompiledRun):
        raise TypeError("execution requires a CompiledRun, not serialized input")
    validate_prepared(run)
    session = _Session(
        run,
        product_actor,
        list(range(run.rollout_count)),
        capture=capture,
        configured=True,
        product_actor=product_actor,
    )
    lot_sequences: defaultdict[tuple[int, int, int], int] = defaultdict(int)
    try:
        session.start()
        while not session.is_finished():
            paths = session.active()
            session.begin_actions([DecisionActions(id_, session.month, []) for id_ in paths])
            pending: list[tuple[int, PendingBuy]] = []
            for rollout_id, path in paths.items():
                for sale in run.scenario._scheduled_sales:
                    if sale.month != session.month:
                        continue
                    spec = next(
                        (
                            spec
                            for spec in session.specs.values()
                            if (spec.owner_agent_id, spec.account_id, spec.asset_id)
                            == (sale.agent_id, sale.account_id, sale.asset_id)
                        ),
                        None,
                    )
                    if spec is None:
                        path.world.holdings.scheduled_sale(path.world.accounting, path.world.market, sale)
                        continue
                    candidate = deepcopy(path.portfolios[spec.portfolio_id])
                    withdrawal = candidate._withdraw_units(sale.units)
                    path.world.managed.settle(
                        run.scenario,
                        path.world.accounting,
                        session.month,
                        spec.owner_agent_id,
                        sale.cause_id,
                        session.effects(
                            spec, candidate, sale.proceeds_account_id, withdrawal.cash_received, withdrawal.realizations
                        ),
                        operation="redemption",
                    )
                    path.portfolios[spec.portfolio_id] = candidate
                for index, policy in enumerate(run.scenario._target_allocation_policies):
                    proposal = plan(
                        session.observe(rollout_id, policy.agent_id),
                        policy,
                        policy_index=index,
                        floor=path.world.market.amount(policy.cash_floor, session.month),
                        ceiling=path.world.market.amount(policy.cash_ceiling, session.month),
                        prices={
                            sleeve.asset_id: session.price(f"security:{sleeve.asset_id}", rollout_id, session.month)
                            for sleeve in policy.sleeves
                        },
                    )
                    for action in proposal.sales:
                        receipt = session.apply(rollout_id, action)
                        if isinstance(receipt.outcome, results.Rejected):
                            break
                    if path.failed:
                        break
                    pending.extend((rollout_id, buy) for buy in proposal.buys)
            for path in paths.values():
                if not path.failed:
                    settlement = path.world.settle_claims()
                    path.failed = settlement.failed
                    path.shortfall = settlement.product_shortfall
            for rollout_id, pending_buy in pending:
                path = paths[rollout_id]
                if path.failed:
                    continue
                key = (rollout_id, pending_buy.policy_index, pending_buy.sleeve_index)
                buy_action = materialize_buy(
                    session.observe(rollout_id, pending_buy.agent_id), pending_buy, lot_sequence=lot_sequences[key]
                )
                if buy_action is not None:
                    session.apply(rollout_id, buy_action)
                    if isinstance(buy_action, Buy):
                        lot_sequences[key] += 1
            for path in paths.values():
                if not path.failed:
                    path.world.private_equity.advance(
                        run.scenario,
                        path.world.accounting,
                        path.world.holdings,
                        path.world.market,
                        list(path.world.managed.marks.values()),
                        session.month,
                    )
            session.close_month()
        completed = []
        for path in session.paths.values():
            if path.result is None:
                raise RuntimeError("configured execution requires finished rollouts")
            completed.append(path.result)
        return tuple(completed)
    finally:
        session.close()


def export_results(run: CompiledRun, capture: Capture) -> dict[str, Any]:
    completed = execute(run, capture)
    rollouts = []
    frames: dict[str, list[dict[str, JsonValue]]] = {}
    for result in completed:
        if capture == "summary":
            if result.configured_summary is None:
                raise RuntimeError("summary export requires a financial terminal summary")
            rollouts.append(result.configured_summary.export())
        elif result.financial is not None:
            rollouts.append(result.financial.export())
            if result.events is None:
                raise RuntimeError("dense export requires event frames")
            for spec in EVENT_FRAME_SPECS:
                frames.setdefault(spec.name, []).extend(result.events.frame(spec).to_dicts())
        else:
            raise RuntimeError("dense export requires financial capture")
    if capture == "summary":
        return {"schema_version": run._schema_version, "rollouts": rollouts}
    return {"schema_version": run._schema_version, "rollouts": rollouts, "event_frames": frames}


def simulate_events(run: CompiledRun) -> EventLog:
    """Dense canonical frames without the forensic journal."""

    return project_events(execute(run, "dense"))


def project_events(completed: tuple[WorldResult, ...]) -> EventLog:
    logs = []
    for result in completed:
        if result.events is None:
            raise RuntimeError("event projection requires dense or forensic capture")
        logs.append(result.events)
    return EventLog.concat(logs)


def simulate_product_metrics(run: CompiledRun, primary_agent_id: str) -> ProductMetricArrays:
    return project_product_metrics(run, execute(run, "summary", primary_agent_id))


def project_product_metrics(run: CompiledRun, completed: tuple[WorldResult, ...]) -> ProductMetricArrays:
    rollout_count = len(completed)
    snapshots = run.scenario.horizon_months + 1
    base_series = [[0] * (snapshots * rollout_count) for _ in BASE_METRIC_NAMES]
    failures = []
    for column, result in enumerate(completed):
        rows = result.product_metrics
        if result.financial is not None:
            failed = result.financial.failed_month
        elif result.configured_summary is not None:
            failed = result.configured_summary.failed_month
        else:
            raise RuntimeError("product aggregation requires configured financial capture")
        expected = snapshots if failed is None else failed + 2
        if len(rows) != expected:
            raise ValueError(f"rollout produced {len(rows)} product snapshots, expected {expected}")
        failures.append(-1 if failed is None else failed)
        for snapshot, row in enumerate(rows):
            for metric, value in enumerate(row):
                base_series[metric][snapshot * rollout_count + column] = value
    return ProductMetricArrays(
        rollout_ids=tuple(result.rollout_id for result in completed),
        month_index=np.arange(snapshots, dtype=np.int64),
        failed_month=np.asarray(failures, dtype=np.int64),
        currency_code=run.currency_code,
        currency_quantum=run.currency_quantum,
        base_series=tuple(
            np.asarray(block, dtype=np.int64).reshape((snapshots, rollout_count)) for block in base_series
        ),
    )
