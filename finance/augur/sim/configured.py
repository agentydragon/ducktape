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
from finance.augur.sim.actions import Action, Buy
from finance.augur.sim.agent import assemble
from finance.augur.sim.capture import FinancialCapture, WorldResult, event_log
from finance.augur.sim.events import EVENT_FRAME_SPECS, EventLog
from finance.augur.sim.holdings import private_issuer
from finance.augur.sim.ids import AgentId
from finance.augur.sim.metric_composition import BASE_METRIC_NAMES
from finance.augur.sim.money import checked_count, position_value
from finance.augur.sim.observations import Observation
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.product_metrics import ProductMetricArrays
from finance.augur.sim.validation import validate
from finance.augur.sim.world import Capture, World, acting_agent


def product_row(world: World, actor: str) -> tuple[int, int, int, int, int, int, int]:
    """The app's per-month metric slab for one actor, read from world state after a close."""
    mark = world.mark_month
    cash = sum(
        world.accounting.ledger.balance(account) for account in world.accounting.declared if account.agent_id == actor
    )
    private = sum(
        position_value(
            world.market.value(f"private_equity_mark:{issuer}", mark), lot.units_remaining, lot.spec.quantity_scale
        )
        for lot in world.holdings.lots
        if lot.spec.agent_id == actor
        and lot.units_remaining
        and (issuer := private_issuer(lot.spec.asset_id)) is not None
    )
    property_value = sum(
        world.properties.market_value(purchase, world.market, mark)
        for purchase in world.scenario._scheduled_property_purchases
        if purchase.buyer_agent_id == actor
        and purchase.property_id in world.properties.properties
        and world.properties.properties[purchase.property_id].state.active
        and f"home_value:{purchase.location_id}" in world.market.series
    )
    debt = sum(loan.principal for loan in world.mortgage_snapshots() if loan.agent_id == actor)
    bonds = sum(row.principal for row in world.bonds.snapshots(world.month, mark) if row.agent_id == actor)
    return (
        checked_count(cash, "product cash"),
        world.holding_value(actor, mark),
        checked_count(private, "product private equity"),
        checked_count(property_value, "product property"),
        checked_count(debt, "product mortgage"),
        world.shortfall,
        checked_count(bonds, "product bonds"),
    )


def observe(world: World, actor: str) -> Observation:
    """The configured runner's view for its allocation policy: an untracked actor's mail, assembled."""
    return assemble(AgentId(actor), world.month, world.open_mail(AgentId(actor)))


def apply(world: World, action: Action) -> results.Receipt:
    """Per-action execution on behalf of the agent the action names."""
    return world.execute(acting_agent(action), action)


def execute(run: CompiledRun, capture: Capture, product_actor: str | None = None) -> tuple[WorldResult, ...]:
    """Drive every path through the explicit phase methods; nothing here is tracked on the world."""
    if not isinstance(run, CompiledRun):
        raise TypeError("execution requires a CompiledRun, not serialized input")
    validate_prepared(run)
    validate(run)
    worlds = {rollout_id: World(run, rollout_id) for rollout_id in range(run.rollout_count)}
    captures = {id_: FinancialCapture(world, capture=capture) for id_, world in worlds.items()}
    rows: dict[int, list[tuple[int, int, int, int, int, int, int]]] = {id_: [] for id_ in worlds}
    if product_actor is not None:
        for id_, world in worlds.items():
            world.validate_scope(product_actor)
            rows[id_].append(product_row(world, product_actor))
    lot_sequences: defaultdict[tuple[int, int, int], int] = defaultdict(int)
    for world in worlds.values():
        world.start()
    month = 0
    while paths := {id_: world for id_, world in worlds.items() if not world.finished}:
        for path in paths.values():
            path.begin_actions([])
        pending: list[tuple[int, PendingBuy]] = []
        for rollout_id, path in paths.items():
            for sale in run.scenario._scheduled_sales:
                if sale.month != month:
                    continue
                spec = next(
                    (
                        spec
                        for spec in path.specs.values()
                        if (spec.owner_agent_id, spec.account_id, spec.asset_id)
                        == (sale.agent_id, sale.account_id, sale.asset_id)
                    ),
                    None,
                )
                if spec is None:
                    path.holdings.scheduled_sale(path.accounting, path.market, sale)
                    continue
                candidate = deepcopy(path.portfolios[spec.portfolio_id])
                withdrawal = candidate._withdraw_units(sale.units)
                path.managed.settle(
                    run.scenario,
                    path.accounting,
                    month,
                    spec.owner_agent_id,
                    sale.cause_id,
                    path.effects(
                        spec, candidate, sale.proceeds_account_id, withdrawal.cash_received, withdrawal.realizations
                    ),
                    operation="redemption",
                )
                path.portfolios[spec.portfolio_id] = candidate
            for index, policy in enumerate(run.scenario._target_allocation_policies):
                proposal = plan(
                    observe(path, policy.agent_id),
                    policy,
                    policy_index=index,
                    floor=path.market.amount(policy.cash_floor, month),
                    ceiling=path.market.amount(policy.cash_ceiling, month),
                    prices={
                        sleeve.asset_id: path.market.value(f"security:{sleeve.asset_id}", month)
                        for sleeve in policy.sleeves
                    },
                )
                for action in proposal.sales:
                    if isinstance(apply(path, action).outcome, results.Rejected):
                        break
                if path.failed:
                    break
                pending.extend((rollout_id, buy) for buy in proposal.buys)
        for path in paths.values():
            if not path.failed:
                settlement = path.settle_claims(product_actor)
                path.failed = settlement.failed
                path.shortfall = settlement.product_shortfall
        for rollout_id, pending_buy in pending:
            path = paths[rollout_id]
            if path.failed:
                continue
            key = (rollout_id, pending_buy.policy_index, pending_buy.sleeve_index)
            buy_action = materialize_buy(
                observe(path, pending_buy.agent_id), pending_buy, lot_sequence=lot_sequences[key]
            )
            if buy_action is not None:
                apply(path, buy_action)
                if isinstance(buy_action, Buy):
                    lot_sequences[key] += 1
        for path in paths.values():
            if not path.failed:
                path.private_equity.advance(
                    run.scenario, path.accounting, path.holdings, path.market, list(path.managed.marks.values()), month
                )
        for rollout_id, path in paths.items():
            path.close_month()
            captures[rollout_id].record()
            if product_actor is not None:
                rows[rollout_id].append(product_row(path, product_actor))
            if not path.finished:
                path.open_month()
        month += 1
    completed = []
    for rollout_id in worlds:
        financial = captures[rollout_id].financial()
        completed.append(
            WorldResult(
                rollout_id,
                financial,
                event_log(financial) if financial is not None else None,
                captures[rollout_id].configured_summary() if capture == "summary" else None,
                rows[rollout_id],
            )
        )
    return tuple(completed)


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
