"""The shrinker, driven by a stand-in predicate rather than the engines.

A synthetic predicate keeps these cheap and, more usefully, makes the reduction checkable:
with the reproducing condition stated exactly, the minimal case is known in advance, so the
test pins how far the search gets rather than merely that it ran.
"""

from decimal import Decimal

import pytest_bazel

from finance.augur.model.series import InflationKey
from finance.augur.rust.differential.shrink import shrink_case
from finance.augur.sim.scenario import InitialAccountBalance, ScheduledTransfer
from finance.augur.sim.testing.case import Case, levels, scenario

INFLATION = InflationKey()


def _case() -> Case:
    return Case(
        scenario=scenario(
            [
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=Decimal(50)),
                InitialAccountBalance(agent_id="bob", account_id="checking", balance=Decimal(0)),
            ],
            horizon_months=8,
            tax_profiles=[],
            scheduled_transfers=[
                ScheduledTransfer(
                    month=month,
                    cause_id=f"transfer-{month}",
                    from_agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="bob",
                    to_account_id="checking",
                    amount=Decimal(1),
                )
                for month in range(5)
            ],
        ),
        rollout_count=3,
        series={INFLATION: levels([[Decimal(1) + Decimal(month) / 100 for month in range(9)]] * 3)},
    )


def _has_the_guilty_transfer(case: Case) -> bool:
    """Month 3's transfer still runs — so the horizon has a floor as well as the entry list."""

    return any(
        transfer.cause_id == "transfer-3" and transfer.month < case.scenario.horizon_months
        for transfer in case.scenario.scheduled_transfers
    )


def test_shrinking_strips_everything_the_reproducer_does_not_need() -> None:
    minimal, _ = shrink_case(_case(), still_diverges=_has_the_guilty_transfer)
    assert [transfer.cause_id for transfer in minimal.scenario.scheduled_transfers] == ["transfer-3"]
    # The guilty transfer is in month 3, so the horizon cannot go below 4 without the
    # predicate losing it — but everything past it goes.
    assert minimal.scenario.horizon_months == 4
    assert minimal.rollout_count == 1
    assert minimal.scenario.initial_cash == []
    assert minimal.scenario.agents == []


def test_series_stay_consistent_with_the_horizon_and_rollouts_they_are_trimmed_against() -> None:
    # The predicate accepts every reduction, so the search runs to the floor and the series
    # has to still be well-formed there.
    minimal, _ = shrink_case(_case(), still_diverges=lambda _: True)
    for path in minimal.series.values():
        assert path.shape == (minimal.rollout_count, minimal.scenario.horizon_months + 1)


def test_the_candidate_budget_is_respected() -> None:
    _, tried = shrink_case(_case(), still_diverges=_has_the_guilty_transfer, max_candidates=3)
    assert tried == 3


if __name__ == "__main__":
    pytest_bazel.main()
