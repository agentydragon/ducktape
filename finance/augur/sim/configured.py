"""Remaining configured caller: Python allocation, grouped claims, then PE operations.

This preserves the configured consumers' financial ordering, without giving the
financial kernel another policy or population loop.
"""

import json
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass

from pydantic import JsonValue

from finance.augur.policy.configured_allocation import PendingBuy, materialize_buy, plan, validate_prepared
from finance.augur.sim import _native, results
from finance.augur.sim.actions import Buy, DecisionActions
from finance.augur.sim.metric_composition import BASE_METRIC_NAMES
from finance.augur.sim.prepared import CompiledRun, PreparedAmount, PreparedFixedAmount
from finance.augur.sim.session import Capture, _Session


@dataclass(frozen=True)
class ProductMetrics:
    """Product transport only: observed rows plus zero padding after stopped paths."""

    rollout_count: int
    snapshot_count: int
    base_series: list[list[int]]
    failed_month: list[int]
    metric_names: tuple[str, ...] = BASE_METRIC_NAMES


def _amount(session: _Session, rollout_id: int, amount: PreparedAmount) -> int:
    if isinstance(amount, int):
        return amount
    if isinstance(amount, PreparedFixedAmount):
        return amount.amount
    elapsed = session.month - amount.base_month_index
    reset = amount.base_month_index + elapsed // amount.adjustment_period_months * amount.adjustment_period_months
    numerator = amount.base_amount * session.price(amount.series_id, rollout_id, reset)
    denominator = session.price(amount.series_id, rollout_id, amount.base_month_index)
    # Match the financial amount boundary: round half away from zero, once.
    rounded = (2 * abs(numerator) + denominator) // (2 * denominator)
    return rounded if numerator >= 0 else -rounded


def _run(run: CompiledRun, capture: Capture, product_actor: str | None = None) -> _Session:
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
                for index, sale in enumerate(run.scenario._scheduled_sales):
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
                        path.world.scheduled_sale(index)
                        continue
                    candidate = deepcopy(path.portfolios[spec.portfolio_id])
                    withdrawal = candidate._withdraw_units(sale.units)
                    path.world.apply_component_json(
                        spec.owner_agent_id,
                        sale.cause_id,
                        session.effects(
                            spec, candidate, sale.proceeds_account_id, withdrawal.cash_received, withdrawal.realizations
                        ).model_dump_json(),
                        operation="redemption",
                    )
                    path.portfolios[spec.portfolio_id] = candidate
                for index, policy in enumerate(run.scenario._target_allocation_policies):
                    proposal = plan(
                        session.observe(rollout_id, policy.agent_id),
                        policy,
                        policy_index=index,
                        floor=_amount(session, rollout_id, policy.cash_floor),
                        ceiling=_amount(session, rollout_id, policy.cash_ceiling),
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
                    settlement = _native.Settlement.model_validate_json(path.world.settle_claims_json())
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
                    path.world.run_private_equity()
            session.close_month()
        return session
    except BaseException:
        session.close()
        raise


def _export(run: CompiledRun, capture: Capture) -> str:
    session = _run(run, capture)
    rollouts = []
    frames: dict[str, list[dict[str, JsonValue]]] = {}
    for path in session.paths.values():
        if path.result is None:
            raise RuntimeError("configured export requires finished rollouts")
        if capture == "summary":
            if path.result.configured_summary is None:
                raise RuntimeError("summary export requires a financial terminal summary")
            rollouts.append(path.result.configured_summary.model_dump(mode="json", by_alias=True))
        elif path.result.financial is not None:
            rollouts.append(path.result.financial.model_dump(mode="json", by_alias=True))
            if path.result.event_frames is None:
                raise RuntimeError("dense export requires event frames")
            for name, rows in path.result.event_frames.items():
                frames.setdefault(name, []).extend(rows)
        else:
            raise RuntimeError("dense export requires financial capture")
    if capture == "summary":
        return json.dumps({"schema_version": run._schema_version, "rollouts": rollouts})
    return json.dumps({"schema_version": run._schema_version, "rollouts": rollouts, "event_frames": frames})


def simulate_dense_json(run: CompiledRun) -> str:
    return _export(run, "dense")


def simulate_forensic_json(run: CompiledRun) -> str:
    return _export(run, "forensic")


def simulate_summaries_json(run: CompiledRun) -> str:
    return _export(run, "summary")


def simulate_product_metrics(run: CompiledRun, primary_agent_id: str) -> ProductMetrics:
    session = _run(run, "summary", primary_agent_id)
    snapshots = run.scenario.horizon_months + 1
    base_series = [[0] * (snapshots * run.rollout_count) for _ in BASE_METRIC_NAMES]
    failures = []
    for rollout_id, path in session.paths.items():
        if path.result is None:
            raise RuntimeError("product aggregation requires finished rollouts")
        rows = path.result.product_metrics
        summary = path.result.configured_summary
        if summary is None:
            raise RuntimeError("product aggregation requires a financial terminal summary")
        failed = summary.failed_month
        expected = snapshots if failed is None else failed + 2
        if len(rows) != expected:
            raise ValueError(f"rollout produced {len(rows)} product snapshots, expected {expected}")
        failures.append(-1 if failed is None else failed)
        for snapshot, row in enumerate(rows):
            for metric, value in enumerate(row):
                base_series[metric][snapshot * run.rollout_count + rollout_id] = value
    return ProductMetrics(run.rollout_count, snapshots, base_series, failures)
