"""Current held-bond facts and compact carrying principal through real Python batches."""

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import InflationKey
from finance.augur.rust.simulator import (
    Action,
    ActionSession,
    Decision,
    DecisionActions,
    Finished,
    FixedCoupon,
    IndexedCoupon,
    simulate_dense_json,
)
from finance.augur.sim.scenario import BondHolding, Currency
from finance.augur.sim.testing.bonds import CORPORATE, MUNI, TREASURY, bond_case
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.fixtures import checking


def execute(
    case: Case,
    policy: Callable[[list[Decision]], list[DecisionActions]],
    capture: Literal["summary", "dense", "forensic"] = "summary",
    ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    session = ActionSession(json.dumps(case.compiled_run.execution_input), "alice", ids or [0], capture=capture)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(policy(batch))
        results: list[dict[str, Any]] = json.loads(batch.rollouts_json)
        return results
    finally:
        session.close()


def held_case(*, indexed: bool = False, future_cpi: float = 2.0, rollout_count: int = 1) -> Case:
    return Case(
        scenario(
            checking(("alice", Decimal(0)), ("bob", Decimal(0)), ("world", Decimal(0))),
            horizon_months=3,
            tax_profiles=[],
            initial_bonds=[
                BondHolding(
                    bond_id=f"{agent}-bond",
                    agent_id=agent,
                    account_id="checking",
                    face_value=Decimal(100),
                    purchase_price=Decimal(100),
                    annual_coupon_rate=0.12,
                    coupon_period_months=1,
                    purchase_month_index=-1,
                    maturity_month_index=2,
                    inflation_indexed=indexed,
                )
                for agent in ("alice", "bob")
            ],
        ),
        rollout_count=rollout_count,
        series={InflationKey(): np.tile([1.0, 2.0, future_cpi, future_cpi], (rollout_count, 1))},
    )


def consume(amount: int) -> Action:
    return Action.consume(0, "bond-funded-spend", "consumption", ("alice", "checking"), ("world", "checking"), amount)


def test_owned_terms_coupon_before_spending_and_maturity_removal() -> None:
    observed = []

    def policy(batch: list[Decision]) -> list[DecisionActions]:
        responses = []
        for decision in batch:
            observation = decision.observation
            assert observation.public_holdings == 0
            assert observation.public_positions == []
            if observation.month < 2:
                [bond] = observation.held_bonds
                assert (bond.bond_id, bond.account_id, bond.issuer_jurisdiction_id) == ("alice-bond", "checking", None)
                assert (bond.face_value, bond.purchase_price, bond.principal) == (10_000, 10_000, 10_000)
                coupon = bond.coupon
                assert isinstance(coupon, FixedCoupon)
                assert (coupon.amount, bond.coupon_period_months) == (100, 1)
                assert (bond.purchase_month, bond.maturity_month) == (-1, 2)
            else:
                assert observation.held_bonds == []
            observed.append((observation.month, observation.cash))
            responses.append(DecisionActions(decision.rollout_id, observation.month, [consume(observation.cash)]))
        return responses

    [result] = execute(held_case(), policy)
    assert observed == [(0, 100), (1, 100), (2, 10_100)]
    assert result["stop"] is None
    assert result["trace"] is None
    assert result["summary"]["bond_principal"] == [
        {
            "account": {"agent_id": "alice", "account_id": "checking"},
            "bond_id": "alice-bond",
            "values": [10_000, 10_000, 10_000, 0],
        }
    ]
    assert result["summary"]["cash"][0]["values"] == [0, 0, 0, 0]
    assert [row["receipt"]["amount_requested"] for row in result["summary"]["payments"]] == [100, 100, 10_100]
    assert all(row["receipt"]["outcome"] == "Paid" for row in result["summary"]["payments"])


def test_indexed_principal_stopped_marks_and_replay_exclude_unobserved_cpi() -> None:
    def policy(batch: list[Decision]) -> list[DecisionActions]:
        responses = []
        for decision in reversed(batch):
            observation = decision.observation
            if observation.month < 2:
                [bond] = observation.held_bonds
                coupon = bond.coupon
                assert isinstance(coupon, IndexedCoupon)
                assert coupon.annual_rate_ppb == 120_000_000
                assert bond.principal == (10_000 if observation.month == 0 else 20_000)
                assert observation.cash == (100 if observation.month == 0 else 300)
            else:
                assert decision.rollout_id == 1
                assert observation.held_bonds == []
            actions = [consume(301), consume(1)] if decision.rollout_id == 0 and observation.month == 1 else []
            responses.append(DecisionActions(decision.rollout_id, observation.month, actions))
        return responses

    baseline = execute(held_case(indexed=True, rollout_count=2), policy, ids=[0, 1])
    captures: tuple[Literal["dense", "forensic"], ...] = ("dense", "forensic")
    for capture in captures:
        replay = execute(held_case(indexed=True, rollout_count=2), policy, capture, ids=[1, 0])
        for row in replay:
            assert row["summary"] == baseline[row["rollout_id"]]["summary"]
            assert row["stop"] == baseline[row["rollout_id"]]["stop"]
            financial = row["trace"]["financial"]
            assert row["summary"]["bond_principal"][0]["values"] == [
                next(bond["principal"] for bond in book["bonds"] if bond["agent_id"] == "alice")
                for book in financial["months"]
            ]
    stopped = baseline[0]
    assert stopped == execute(held_case(indexed=True, future_cpi=99.0, rollout_count=2), policy, ids=[0])[0]
    assert stopped["stop"] == {"RejectedAction": {"month": 1, "action_index": 0}}
    assert stopped["summary"]["ending_book"]["month"] == 2
    assert stopped["summary"]["ending_mark_month"] == 1
    assert stopped["summary"]["bond_principal"][0]["values"] == [10_000, 20_000, 20_000]
    assert stopped["summary"]["cash"][0]["values"] == [0, 100, 300]
    assert len(stopped["summary"]["last_receipts"]) == 1


def pay_claims(batch: list[Decision]) -> list[DecisionActions]:
    return [
        DecisionActions(
            row.rollout_id,
            row.observation.month,
            [
                Action.pay_claim(index, claim.cause_id, claim, claim.from_account, claim.amount_due)
                for index, claim in enumerate(row.observation.claims)
            ],
        )
        for row in batch
    ]


@pytest.mark.parametrize(
    ("issuer", "federal", "state"), [(TREASURY, True, False), (MUNI, False, False), (CORPORATE, True, True)]
)
def test_existing_issuer_exemptions_survive_actor_capture(issuer: str | None, federal: bool, state: bool) -> None:
    [result] = execute(bond_case(issuer=issuer), pay_claims)
    assert result["stop"] is None
    taxes = {row["jurisdiction_id"]: row["total_tax"] for row in result["summary"]["tax_accruals"]}
    assert (taxes["federal_us"] > 0) == federal
    assert (taxes["california"] > 0) == state
    # One $20,000 first-year coupon less the supplied $14,600 deduction, at 10%.
    assert taxes["federal_us"] == (54_000 if federal else 0)


@pytest.mark.parametrize(
    ("face", "rate", "period", "coupon"),
    [(600, 0.01, 1, 1), (180, 0.033333333, 1, 0), (1_250_627, 0.037, 5, 19_280), (600, 0.0, 1, 0)],
)
@pytest.mark.parametrize("quantum", [Decimal("0.01"), Decimal(1)])
def test_compiled_fixed_coupon_funds_both_controls(
    face: int, rate: float, period: int, coupon: int, quantum: Decimal
) -> None:
    case = Case(
        scenario(
            checking(("alice", Decimal(0)), ("world", Decimal(0))),
            horizon_months=2 * period + 1,
            currency=Currency(quantum=quantum),
            initial_bonds=[
                BondHolding(
                    bond_id="fixed-test",
                    agent_id="alice",
                    account_id="checking",
                    face_value=face * quantum,
                    purchase_price=face * quantum,
                    annual_coupon_rate=rate,
                    coupon_period_months=period,
                    purchase_month_index=0,
                    maturity_month_index=2 * period,
                )
            ],
            tax_profiles=[],
        ),
        rollout_count=1,
    )

    def spend(batch: list[Decision]) -> list[DecisionActions]:
        return [
            DecisionActions(
                row.rollout_id, row.observation.month, [consume(row.observation.cash)] if row.observation.cash else []
            )
            for row in batch
        ]

    [actor] = execute(case, spend, "dense")
    [configured] = json.loads(simulate_dense_json(json.dumps(case.compiled_run.execution_input)))["rollouts"]
    expected = [(period, coupon, 0), (2 * period, coupon, face)] if coupon else [(2 * period, 0, face)]
    for cashflows in (actor["trace"]["financial"]["bond_cashflows"], configured["bond_cashflows"]):
        assert [(row["month"], row["coupon"], row["redemption"]) for row in cashflows] == expected
        assert all(row["accretion"] == 0 for row in cashflows)
    assert actor["stop"] is None
    assert sum(row["receipt"]["amount_requested"] for row in actor["summary"]["payments"]) == face + 2 * coupon
    assert actor["summary"]["cash"][0]["values"] == [0] * (2 * period + 2)
    assert actor["summary"]["bond_principal"][0]["values"][-1] == 0


def test_indexed_accretion_income_is_preserved_without_claiming_final_period_coverage() -> None:
    # CPI changes at month 6; maturity is 120. This covers intermediate accretion,
    # not the separately unverified final-period TIPS tax treatment.
    [result] = execute(bond_case(indexed=True, cpi=[100.0] * 6 + [200.0] * 9), pay_claims, "forensic")
    first_year = [row for row in result["summary"]["tax_accruals"] if row["month"] == 11]
    taxes = {row["jurisdiction_id"]: row["total_tax"] for row in first_year}
    assert taxes["federal_us"] > 0
    assert taxes["california"] == 0
    accretion = next(row for row in result["trace"]["financial"]["bond_cashflows"] if row["month"] == 6)
    assert (accretion["accretion"], accretion["coupon"], accretion["redemption"]) == (100_000_000, 4_000_000, 0)


if __name__ == "__main__":
    pytest_bazel.main()
