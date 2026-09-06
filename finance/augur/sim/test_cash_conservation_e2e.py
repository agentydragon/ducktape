"""Cash is conserved. One assertion that catches every leak, anywhere.

This is the payoff of routing unmodeled counterparties to an external account instead of to
`NO_CODE`. Before, a flow to an unknown (agent, account) was scattered into a padding row the
engine then sliced off: the money vanished, nothing failed, and the only way to notice was to
guard each site individually and remember to. A bond paying into a mistyped account was
exactly that bug.

Now every flow debits one real row and credits another, so the sum over ALL cash rows — the
agents' accounts plus the external one — cannot change. Any leak, in any phase, in code
nobody thought to guard, breaks this test.

Disposals are the half that needs saying out loud, because the invariant is the ONLY thing
that sees them go wrong. When a sale credits proceeds with no matching debit, net worth stays
correct — the lot leaves as the cash arrives — so every agent-facing number looks right while
the ledger mints money. So each way of turning something into cash is checked below, over the
cases `sim/testing/cash_conservation.py` authors for the engine-neutral half of this rule.

That neutral half — the modeled agents' total moves by exactly what a disposal recorded — is
what both engines answer. This file is the JAX-only remainder: the external contra row is how
the JAX engine keeps an unmodeled counterparty's flow from vanishing, and Rust has no
counterpart for it. Rust's counterpart is the double-entry journal it validates on every
entry, which is not a row anyone can sum.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel
from numpy.typing import NDArray

from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.engine.jax_engine import run_jax_scan
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    BondHolding,
    InitialLot,
    RecurringTransfer,
    ScheduledAssetSale,
    ScheduledTransfer,
)
from finance.augur.sim.testing.case import Case, flat, scenario
from finance.augur.sim.testing.cash_conservation import (
    private_equity_tender_case,
    property_sale_case,
    scheduled_sale_case,
    target_allocation_sale_case,
)
from finance.augur.sim.testing.fixtures import VTI, checking, taxed
from finance.augur.sim.testing.state_helpers import cash_balances

BUSY_HORIZON = 15


def _run(case: Case) -> SimulationRun:
    return SimulationRun(plan=case.plan, output=run_jax_scan(case.plan), external_series=case.external_series)


def _total_cash_by_month(run: SimulationRun) -> NDArray[np.int64]:
    """Sum over every cash row, including the external account.

    Read off the raw output rather than `cash_balances`, which deliberately shows only the
    agents' own accounts — the contra row is exactly what makes the total balance.
    """

    state: NDArray[np.int64] = np.asarray(run.output.state.cash, dtype=np.int64)
    return np.asarray(state.sum(axis=tuple(range(1, state.ndim))), dtype=np.int64)


def _busy_case(*, rollout_count: int = 1) -> Case:
    """Deliberately busy: money entering from outside (wages, a bond coupon), leaving to
    outside (rent to an unmodeled landlord), moving between modeled agents, and a seeded lot
    sold back to the market at a profit."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(400_000)), ("bob", Decimal(10_000)), ("irs", Decimal(0))),
            initial_bonds=[
                BondHolding(
                    bond_id="rung",
                    agent_id="alice",
                    account_id="checking",
                    issuer_jurisdiction_id="federal_us",
                    face_value=Decimal(500_000),
                    purchase_price=Decimal(500_000),
                    annual_coupon_rate=0.05,
                    coupon_period_months=6,
                    purchase_month_index=0,
                    maturity_month_index=12,
                )
            ],
            initial_lots=[
                InitialLot(
                    lot_id="bought",
                    agent_id="alice",
                    asset=VTI,
                    quantity=2_000,
                    cost_basis_per_unit=Decimal(100),
                    purchase_month_index=0,
                )
            ],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=6,
                    cause_id="sell-vti",
                    agent_id="alice",
                    asset=VTI,
                    quantity=2_000.0,
                    proceeds_account_id="checking",
                )
            ],
            recurring_transfers=[
                # From an agent that does not exist: an employer outside the model.
                RecurringTransfer(
                    start_month=0,
                    cause_id="salary",
                    from_agent_id="megacorp",
                    from_account_id="payroll",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=Decimal(12_000),
                    income_category=ORDINARY_INCOME,
                ),
                # To an agent that does not exist: a landlord outside the model.
                RecurringTransfer(
                    start_month=0,
                    cause_id="rent",
                    from_agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="landlord",
                    to_account_id="deposits",
                    amount=Decimal(4_000),
                ),
            ],
            scheduled_transfers=[
                # Between two modeled agents, which must net to zero across the two rows.
                ScheduledTransfer(
                    month=3,
                    cause_id="gift",
                    from_agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="bob",
                    to_account_id="checking",
                    amount=Decimal(25_000),
                )
            ],
            tax_profiles=[taxed("alice", "federal_us", "california")],
            horizon_months=BUSY_HORIZON,
        ),
        rollout_count=rollout_count,
        series={VTI: flat(Decimal(150), rollout_count=rollout_count, horizon_months=BUSY_HORIZON)},
    )


def test_total_cash_never_changes() -> None:
    totals = _total_cash_by_month(_run(_busy_case(rollout_count=2)))

    assert np.all(totals == totals[0])


def test_the_external_account_is_what_makes_it_balance() -> None:
    """Guards against the test passing vacuously. If nothing actually crossed the model
    boundary, conservation would hold trivially and prove nothing — so assert the external row
    really moved, and in the direction a net-inflow scenario implies (it funds more wages,
    coupons and sale proceeds than it receives in rent, so it goes negative).
    """

    run = _run(_busy_case())
    # cash is (H+1, slot, rollout) — the slot axis is 1, not the last.
    external = np.asarray(run.output.state.cash)[:, run.plan.external_cash_slot, :]

    assert external[0].sum() == 0
    assert external[-1].sum() < 0


def test_agent_facing_cash_excludes_the_external_account() -> None:
    """`cash_balances` is the agents' money. The external row is an accounting device, and
    surfacing it there would put a fictitious agent in every consumer of the frame."""

    assert set(cash_balances(_run(_busy_case())).get_column("agent_id").to_list()) == {"alice", "bob", "irs"}


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(scheduled_sale_case, id="scheduled-asset-sale"),
        pytest.param(target_allocation_sale_case, id="target-allocation-sale"),
        pytest.param(private_equity_tender_case, id="private-equity-tender"),
        pytest.param(property_sale_case, id="property-sale"),
    ],
)
def test_a_disposal_does_not_mint_cash(case) -> None:
    """Every way of turning something into cash, against the row that sees a missing debit.

    That each disposal actually fires, and brings in exactly what it recorded, is asserted
    against both engines in `sim/testing/cash_conservation.py`. What is left here is the leak
    those assertions cannot see.
    """

    totals = _total_cash_by_month(_run(case()))

    assert np.all(totals == totals[0])


if __name__ == "__main__":
    pytest_bazel.main()
