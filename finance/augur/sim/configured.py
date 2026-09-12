"""The configured runner its remaining test suites drive: Python allocation, grouped claims, scheduled sales.

The app runs on `product.simulation`; this preserves the configured suites' financial
ordering until each moves onto a composed world.
"""

from collections import defaultdict
from copy import deepcopy
from typing import Any

from pydantic import JsonValue

from finance.augur.policy.configured_allocation import PendingBuy, materialize_buy, plan, validate_prepared
from finance.augur.sim import results
from finance.augur.sim.actions import Action, Buy
from finance.augur.sim.agent import assemble
from finance.augur.sim.capture import FinancialCapture, WorldResult, event_log
from finance.augur.sim.events import EVENT_FRAME_SPECS
from finance.augur.sim.ids import AgentId
from finance.augur.sim.observations import Observation
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.product_metrics import product_row
from finance.augur.sim.validation import validate
from finance.augur.sim.world import Capture, World, acting_agent


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
    worlds = {rollout_id: World.from_run(run, rollout_id) for rollout_id in range(run.rollout_count)}
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
                path.managed_portfolios().settle(
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
