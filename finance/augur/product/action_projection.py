"""Project finished action-session outcomes; never run a policy or a financial evaluator.

Compact cash/public marks and held-bond principal support the product's fan and terminal reductions. Detailed
events use the same result's typed `trace.events`.
An absent history is not an observed zero holding.
"""

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from finance.augur.sim.backend import CompiledRun
from finance.augur.sim.metric_composition import BASE_METRIC_NAMES
from finance.augur.sim.product_metrics import ProductMetricArrays
from finance.augur.sim.results import CashSeries, ConsumptionTarget, InsufficientCash, PaymentRejected, Rollout, Summary


def _total_series(rows: Sequence[CashSeries], actor_id: str, snapshots: int) -> NDArray[np.int64]:
    for row in rows:
        if row.account.agent_id != actor_id or len(row.values) != snapshots:
            raise ValueError("captured series must belong to the selected actor and cover its observed prefix")
    # Python sums preserve exact integers; conversion rejects an unrepresentable total.
    return np.asarray([sum(row.values[month] for row in rows) for month in range(snapshots)], dtype=np.int64)


def _shortfall(summary: Summary) -> int:
    """Unpaid due claims plus valid attempted consumption's requested/paid gap.

    This is unpaid spending, not incurred debt or cash needed to make a payment.
    A rejected PayClaim is already in unpaid_claims, so it contributes only once.
    Unattempted consumption and malformed actions create no monetary shortfall.
    """
    total = sum(claim.amount_due for claim in summary.unpaid_claims)
    for payment in summary.payments:
        receipt = payment.receipt
        outcome = receipt.outcome
        if (
            isinstance(receipt.target, ConsumptionTarget)
            and isinstance(outcome, PaymentRejected)
            and isinstance(outcome.reason, InsufficientCash)
        ):
            total += receipt.amount_requested
    return total


def metric_arrays(run: CompiledRun, rollouts: Sequence[Rollout], *, primary_agent_id: str) -> ProductMetricArrays:
    """Use finished results from this prepared run, in their supplied selection order.

    Cash, public securities and held-bond principal are supported here. Historical property
    and private-equity values must be captured before those portfolios can use this adapter;
    the configured app retains those capabilities. Masked padding is not observed money.
    """
    scenario = run.execution_input["scenario"]
    if scenario["scheduled_property_purchases"]:
        raise ValueError("property and mortgage histories are not captured for product action projection")
    if any(pool["asset_id"].startswith("private_equity:") for pool in scenario["holding_pools"]):
        raise ValueError("private-equity histories are not captured for product action projection")
    bond_accounts = {
        bond["bond_id"]: bond["account_id"]
        for bond in scenario["initial_bonds"]
        if bond["agent_id"] == primary_agent_id
    }
    ids = [rollout.rollout_id for rollout in rollouts]
    if not ids or len(set(ids)) != len(ids) or any(not 0 <= id_ < run.execution_input["rollout_count"] for id_ in ids):
        raise ValueError("product projection needs a nonempty unique selection of original rollout IDs")
    snapshot_count = scenario["horizon_months"] + 1
    series = {name: np.zeros((snapshot_count, len(rollouts)), dtype=np.int64) for name in BASE_METRIC_NAMES}
    failed_month = np.full(len(rollouts), -1, dtype=np.int64)
    for column, rollout in enumerate(rollouts):
        summary = rollout.summary
        if summary.actor_id != primary_agent_id:
            raise ValueError("result actor does not match the product's primary actor")
        ending = summary.ending_book
        observed = ending.month + 1
        if rollout.stop is not None:
            failed_month[column] = rollout.stop.month
        expected = snapshot_count if failed_month[column] < 0 else int(failed_month[column]) + 2
        if observed != expected or not 1 <= observed <= snapshot_count:
            raise ValueError("finished result does not cover its declared completed or stopped prefix")
        if ending.properties or ending.mortgages:
            raise ValueError("ending book contains a domain without captured historical product values")
        captured_bonds = {row.bond_id: row.account.account_id for row in summary.bond_principal}
        ending_bonds = {bond.bond_id: bond.account_id for bond in ending.bonds if bond.agent_id == primary_agent_id}
        if (
            len(captured_bonds) != len(summary.bond_principal)
            or captured_bonds != bond_accounts
            or ending_bonds != bond_accounts
        ):
            raise ValueError("held-bond principal history must cover each declared actor bond/account exactly once")
        series["cash_quanta"][:observed, column] = _total_series(summary.cash, primary_agent_id, observed)
        series["holding_value_quanta"][:observed, column] = _total_series(
            summary.public_holdings, primary_agent_id, observed
        )
        series["bond_value_quanta"][:observed, column] = _total_series(
            summary.bond_principal, primary_agent_id, observed
        )
        series["shortfall_quanta"][observed - 1, column] = _shortfall(summary)
    return ProductMetricArrays(
        rollout_ids=tuple(ids),
        month_index=np.arange(snapshot_count, dtype=np.int64),
        failed_month=failed_month,
        currency_code=run.execution_input["currency_code"],
        currency_quantum=run.execution_input["currency_quantum"],
        base_series=tuple(series[name] for name in BASE_METRIC_NAMES),
    )
