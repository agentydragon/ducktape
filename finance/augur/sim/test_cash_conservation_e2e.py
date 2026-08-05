"""Cash is conserved. One assertion that catches every leak, anywhere.

This is the payoff of routing unmodeled counterparties to an external account instead of to
`NO_CODE`. Before, a flow to an unknown (agent, account) was scattered into a padding row
the engine then sliced off: the money vanished, nothing failed, and the only way to notice
was to guard each site individually and remember to. A bond paying into a mistyped account
was exactly that bug.

Now every flow debits one real row and credits another, so the sum over ALL cash rows —
the agents' accounts plus the external one — cannot change. Any leak, in any phase, in code
nobody thought to guard, breaks this test.
"""

from __future__ import annotations

import numpy as np
import pytest_bazel
from numpy.typing import NDArray

from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    Agent,
    BondHolding,
    FilingStatus,
    InitialAccountBalance,
    RecurringTransfer,
    Scenario,
    ScheduledTransfer,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate


def _total_cash_by_month(run: SimulationRun) -> NDArray[np.int64]:
    """Sum over every cash row, including the external account.

    Read off the raw buffer rather than `cash_balances`, which deliberately shows only the
    agents' own accounts — the contra row is exactly what makes the total balance.
    """

    state: NDArray[np.int64] = np.asarray(run.buffers.state.cash_state, dtype=np.int64)
    return np.asarray(state.sum(axis=tuple(range(1, state.ndim))), dtype=np.int64)


def _scenario() -> Scenario:
    """Deliberately busy: money entering from outside (wages, a bond coupon), leaving to
    outside (rent to an unmodeled landlord), and moving between modeled agents."""

    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="bob"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=400_000.0),
            InitialAccountBalance(agent_id="bob", account_id="checking", balance_usd=10_000.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_bonds=[
            BondHolding(
                bond_id="rung",
                agent_id="alice",
                account_id="checking",
                issuer_jurisdiction_id="federal_us",
                face_value_usd=500_000.0,
                purchase_price_usd=500_000.0,
                annual_coupon_rate=0.05,
                coupon_period_months=6,
                purchase_month_index=0,
                maturity_month_index=12,
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
                amount_usd=12_000.0,
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
                amount_usd=4_000.0,
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
                amount_usd=25_000.0,
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=15,
    )


def test_total_cash_never_changes() -> None:
    totals = _total_cash_by_month(simulate(_scenario(), rollout_count=2, locations={}))

    assert np.all(totals == totals[0])


def test_the_external_account_is_what_makes_it_balance() -> None:
    """Guards against the test passing vacuously. If nothing actually crossed the model
    boundary, conservation would hold trivially and prove nothing — so assert the external
    row really moved, and in the direction a net-inflow scenario implies (it funds more
    wages and coupons than it receives in rent, so it goes negative).
    """

    run = simulate(_scenario(), rollout_count=1, locations={})
    # cash_state is (H+1, slot, rollout) — the slot axis is 1, not the last.
    external = np.asarray(run.buffers.state.cash_state)[:, run.plan.external_cash_slot, :]

    assert external[0].sum() == 0
    assert external[-1].sum() < 0


def test_agent_facing_cash_excludes_the_external_account() -> None:
    """`cash_balances` is the agents' money. The external row is an accounting device, and
    surfacing it there would put a fictitious agent in every consumer of the frame."""

    run = simulate(_scenario(), rollout_count=1, locations={})

    assert set(run.cash_balances.get_column("agent_id").to_list()) == {"alice", "bob", "irs"}


if __name__ == "__main__":
    pytest_bazel.main()
