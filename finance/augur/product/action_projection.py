"""Project finished action-session outcomes; never run a policy or a financial evaluator.

Compact cash, public/TLH aggregate marks and held-bond principal support the
product's fan and terminal reductions. Detailed events use the same result's
typed `trace.events`; TLH marks do not disclose constituent positions.
An absent history is not an observed zero holding.
"""

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from finance.augur.product.metric_composition import BASE_METRIC_NAMES
from finance.augur.product.metrics import ProductMetricArrays
from finance.augur.sim.holdings import private_issuer
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


def metric_arrays(
    rollouts: Sequence[Rollout],
    *,
    primary_agent_id: str,
    horizon_months: int,
    currency_code: str,
    currency_quantum: str,
) -> ProductMetricArrays:
    """Use finished results over a `horizon_months` horizon, in their supplied selection order.

    Cash, public securities, opaque TLH value and held-bond principal are supported here. Historical property
    and private-equity values must be captured before those portfolios can use this adapter;
    the configured app retains those capabilities. Masked padding is not observed money.
    """
    ids = [rollout.rollout_id for rollout in rollouts]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("product projection needs a nonempty unique selection of original rollout IDs")
    snapshot_count = horizon_months + 1
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
        if any(private_issuer(lot.asset_id) is not None for lot in ending.lots):
            raise ValueError("private-equity histories are not captured for product action projection")
        # The ending book lists every bond the world holds, redeemed ones included.
        captured_bonds = {row.bond_id: row.account.account_id for row in summary.bond_principal}
        ending_bonds = (
            {}
            if ending.bonds is None
            else {bond.bond_id: bond.account_id for bond in ending.bonds if bond.agent_id == primary_agent_id}
        )
        if len(captured_bonds) != len(summary.bond_principal) or captured_bonds != ending_bonds:
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
        currency_code=currency_code,
        currency_quantum=currency_quantum,
        base_series=tuple(series[name] for name in BASE_METRIC_NAMES),
    )
