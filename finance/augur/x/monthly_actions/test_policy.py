"""The authored rule reserves already-due claims before proposing an opening buy."""

import json
from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.model.series import SecurityKey
from finance.augur.rust.simulator import ActionSession, Finished
from finance.augur.sim.scenario import HoldingPool
from finance.augur.sim.testing.case import Case, flat, scenario
from finance.augur.sim.testing.fixtures import cash_spend, checking
from finance.augur.x.monthly_actions.policy import decide


@pytest.fixture
def opening_case(bill_dollars: int) -> Case:
    stock = SecurityKey(symbol="example-stock")
    return Case(
        scenario=scenario(
            checking(("example-household", Decimal(200)), ("example-creditor", Decimal(0))),
            horizon_months=1,
            tax_profiles=[],
            holding_pools=[HoldingPool(agent_id="example-household", account_id="brokerage", asset=stock)],
            scheduled_obligations=[
                cash_spend(
                    "opening-bill",
                    month=0,
                    agent_id="example-household",
                    to_agent_id="example-creditor",
                    amount_due=Decimal(bill_dollars),
                )
            ],
        ),
        rollout_count=1,
        series={stock: flat(Decimal(100), rollout_count=1, horizon_months=1)},
    )


@pytest.mark.parametrize(
    ("bill_dollars", "bought_units", "paid", "ending_cash"),
    [(150, 500_000, 15_000, 0), (200, 0, 20_000, 0), (250, 0, 0, 20_000)],
)
def test_opening_investment_reserves_claims_and_does_not_rescue_shortfalls(
    opening_case: Case, bought_units: int, paid: int, ending_cash: int
) -> None:
    session = ActionSession(json.dumps(opening_case.compiled_run.execution_input), "example-household", [0])
    try:
        batch = session.start()
        assert not isinstance(batch, Finished)
        finished = session.advance(decide(batch))
        assert isinstance(finished, Finished)
        [result] = json.loads(finished.rollouts_json)
    finally:
        session.close()
    financial = result["trace"]["financial"]
    assert [next(iter(receipt["action"])) for receipt in result["trace"]["receipts"]] == (
        ["Buy", "PayClaim"] if bought_units else ["PayClaim"]
    )
    assert financial["obligations"][0]["amount_paid"] == paid
    closing = financial["months"][-1]
    assert (
        next(
            row["balance"]
            for row in closing["balances"]
            if row["account"] == {"agent_id": "example-household", "account_id": "checking"}
        )
        == ending_cash
    )
    assert [(lot["units_remaining"], lot["basis_remaining"]) for lot in closing["lots"]] == (
        [(bought_units, 5_000)] if bought_units else []
    )
    assert result["stop"] == (None if paid else {"RejectedAction": {"month": 0, "action_index": 0}})
    assert financial["dispositions"] == []


if __name__ == "__main__":
    pytest_bazel.main()
