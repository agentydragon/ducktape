"""The shrinker, driven by a stand-in predicate rather than the engines.

A synthetic predicate keeps these cheap and, more usefully, makes the reduction checkable:
with the reproducing condition stated exactly, the minimal fixture is known in advance, so
the test pins how far the search gets rather than merely that it ran.
"""

from typing import Any

import pytest_bazel

from finance.augur.rust.differential.shrink import shrink_fixture


def _fixture() -> dict[str, Any]:
    return {
        "schema_version": 8,
        "currency_code": "USD",
        "currency_quantum": "0.01",
        "rollout_count": 3,
        "scenario": {
            "horizon_months": 8,
            "accounts": [
                {"account": {"agent_id": "alice", "account_id": "checking"}, "opening_balance": 5_000},
                {"account": {"agent_id": "bob", "account_id": "checking"}, "opening_balance": 0},
            ],
            "scheduled_transfers": [
                {
                    "month": month,
                    "cause_id": f"transfer-{month}",
                    "from": {"agent_id": "alice", "account_id": "checking"},
                    "to": {"agent_id": "bob", "account_id": "checking"},
                    "amount": 100,
                }
                for month in range(5)
            ],
        },
        "series": [{"series_id": "inflation", "snapshots": 9, "values": list(range(1, 28))}],
    }


def _has_the_guilty_transfer(fixture: dict[str, Any]) -> bool:
    """Month 3's transfer still runs — so the horizon has a floor as well as the entry list."""

    scenario = fixture["scenario"]
    return any(
        transfer["cause_id"] == "transfer-3" and transfer["month"] < scenario["horizon_months"]
        for transfer in scenario["scheduled_transfers"]
    )


def test_shrinking_strips_everything_the_reproducer_does_not_need() -> None:
    minimal, _ = shrink_fixture(_fixture(), still_diverges=_has_the_guilty_transfer)
    assert [transfer["cause_id"] for transfer in minimal["scenario"]["scheduled_transfers"]] == ["transfer-3"]
    # The guilty transfer is in month 3, so the horizon cannot go below 4 without the
    # predicate losing it — but everything past it goes.
    assert minimal["scenario"]["horizon_months"] == 4
    assert minimal["rollout_count"] == 1
    assert minimal["scenario"]["accounts"] == []


def test_series_stay_consistent_with_the_horizon_and_rollouts_they_are_trimmed_against() -> None:
    # The predicate accepts every reduction, so the search runs to the floor and the series
    # has to still be well-formed there.
    minimal, _ = shrink_fixture(_fixture(), still_diverges=lambda _: True)
    for series in minimal["series"]:
        assert series["snapshots"] == minimal["scenario"]["horizon_months"] + 1
        assert len(series["values"]) == minimal["rollout_count"] * series["snapshots"]


def test_the_candidate_budget_is_respected() -> None:
    _, tried = shrink_fixture(_fixture(), still_diverges=_has_the_guilty_transfer, max_candidates=3)
    assert tried == 3


if __name__ == "__main__":
    pytest_bazel.main()
