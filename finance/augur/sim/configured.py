"""Python phase ownership for remaining configured product and acceptance callers.

The configured allocation/grouped-payment control stays explicit here until its
callers use ordinary batch policies. Component settlement is shared with ActionSession.
"""

from copy import deepcopy

from finance.augur.rust._simulator import PendingBuy, ProductMetrics
from finance.augur.sim import results
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.session import Capture, DecisionActions, _Session


def _run(run: CompiledRun, capture: Capture, product_actor: str | None = None) -> _Session:
    if not isinstance(run, CompiledRun):
        raise TypeError("execution requires a CompiledRun, not serialized input")
    session = _Session(
        run,
        product_actor,
        list(range(run.rollout_count)),
        capture=capture,
        configured=True,
        product_actor=product_actor,
    )
    try:
        session.start()
        while not session.native.is_finished():
            paths = session.native.current_paths()
            session.native.begin_actions([DecisionActions(path.rollout_id, path.month, []) for path in paths])
            pending: list[tuple[int, PendingBuy]] = []
            for path in paths:
                rollout_id = path.rollout_id
                for index, sale in enumerate(run.scenario._scheduled_sales):
                    if sale.month != path.month:
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
                        session.native.scheduled_sale(rollout_id, index)
                        continue
                    # Fixed-quantity scripted sale; the component alone knows its cohort grid.
                    candidate = deepcopy(session.portfolios[rollout_id][spec.portfolio_id])
                    withdrawal = candidate._withdraw_units(sale.units)
                    session.native.apply_component(
                        rollout_id,
                        sale.cause_id,
                        session.effects(
                            spec, candidate, sale.proceeds_account_id, withdrawal.cash_received, withdrawal.realizations
                        ),
                        operation="redemption",
                    )
                    session.portfolios[rollout_id][spec.portfolio_id] = candidate
                for index, _ in enumerate(run.scenario._target_allocation_policies):
                    plan = session.native.allocation_plan(rollout_id, index)
                    rejected = False
                    for action in plan.sales:
                        receipt = session.apply(rollout_id, action)
                        if isinstance(receipt.outcome, results.Rejected):
                            rejected = True
                            break
                    if rejected:
                        break
                    pending.extend((rollout_id, buy) for buy in plan.buys)
            statuses = session.native.settle_claims()
            live = {status.rollout_id for status in statuses if not status.stopped}
            for rollout_id, pending_buy in pending:
                if rollout_id not in live:
                    continue
                buy_action = session.native.allocation_buy(rollout_id, pending_buy)
                if buy_action is not None:
                    receipt = session.apply(rollout_id, buy_action)
                    if isinstance(receipt.outcome, results.Rejected):
                        live.remove(rollout_id)
            session.native.run_private_equity()
            session.close_month()
        return session
    except BaseException:
        session.native.close()
        raise


def simulate_dense_json(run: CompiledRun) -> str:
    return _run(run, "dense").native.finish_configured_json()


def simulate_forensic_json(run: CompiledRun) -> str:
    return _run(run, "forensic").native.finish_configured_json()


def simulate_summaries_json(run: CompiledRun) -> str:
    return _run(run, "summary").native.finish_configured_json()


def simulate_product_metrics(run: CompiledRun, primary_agent_id: str) -> ProductMetrics:
    return _run(run, "summary", primary_agent_id).native.finish_product_metrics()
