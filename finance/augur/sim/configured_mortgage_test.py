"""Mortgage ownership controls through the production Python monthly driver."""

from dataclasses import replace
from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.sim.configured import _run
from finance.augur.sim.scenario import MortgageFinancing, PropertySaleEvent, ScheduledObligation
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.testing.fixtures import SF, SF_HOME, checking, financed_property_case, home_purchase


def test_financed_purchase_and_first_installment_match_contract() -> None:
    session = _run(financed_property_case().compiled_run, "forensic")
    path = session.paths[0]
    assert path.result is not None
    assert path.result.financial is not None
    books = path.result.financial.months
    assert not books[0].mortgages
    opening, ending = books[1], books[2]
    assert opening.properties[0].adjusted_basis == 51_000_000
    assert opening.mortgages[0].monthly_payment == 239_820
    assert opening.mortgages[0].principal == 40_000_000
    assert ending.mortgages[0].principal == 39_960_180
    assert ending.mortgages[0].interest_paid_ytd == 200_000
    assert {row.account.agent_id: row.balance for row in ending.balances if row.account.account_id == "checking"} == {
        "alice": 710_180,
        "seller": 11_000_000,
        "bank": 239_820,
        "county": 50_000,
    }
    assert path.mortgages["home-mortgage"].observe(39_960_180) == ending.mortgages[0]
    assert all(sum(posting.amount for posting in entry.postings) == 0 for entry in path.result.financial.journal)


@pytest.mark.parametrize("closing_cost_pct", [0, 10])
@pytest.mark.parametrize("financed", [False, True])
def test_sale_pays_off_ledger_principal_before_sale_month_installment(closing_cost_pct: int, financed: bool) -> None:
    purchase = home_purchase(
        mortgage=MortgageFinancing(
            liability_id="loan", lender_agent_id="bank", principal=600, annual_interest_rate=0, term_months=60
        )
        if financed
        else None,
        purchase_price=Decimal(1000),
        down_payment=Decimal(400 if financed else 1000),
        buyer_closing_cost=Decimal(0),
    ).model_copy(update={"month": 2})
    case = Case(
        scenario=scenario(
            checking(("alice", Decimal(2000)), ("seller", Decimal(0)), ("bank", Decimal(0))),
            horizon_months=6,
            tax_profiles=[],
            scheduled_property_purchases=[purchase],
            property_lifecycle_events=[
                PropertySaleEvent(month=5, property_id="home", closing_cost_pct=closing_cost_pct)
            ],
        ),
        rollout_count=2,
        locations={"sf": SF},
        series={
            SF_HOME: levels(
                [
                    [Decimal(x) for x in row]
                    for row in ([50, 100, 200, 240, 300, 360, 800], [500, 7, 200, 240, 300, 360, 800])
                ]
            )
        },
    )
    session = _run(case.compiled_run, "forensic")
    for path in session.paths.values():
        assert path.result is not None
        assert path.result.financial is not None
        books = path.result.financial.months
        assert not books[2].mortgages
        ending = books[-1]
        if financed:
            assert [book.mortgages[0].principal for book in books[3:]] == [60_000, 59_000, 58_000, 0]
            assert not ending.mortgages[0].active
            assert not path.mortgages["loan"].active
        else:
            assert not ending.mortgages
            assert not path.mortgages
        cash = {row.account.agent_id: row.balance for row in ending.balances if row.account.account_id == "checking"}
        assert cash["alice"] == 158_000 + (122_000 if closing_cost_pct == 0 else 104_000)
        # Configured payoff closes the lender's funding control, not its cash account.
        assert cash["bank"] == (2000 if financed else 0)


@pytest.mark.parametrize("fail_year_end", [False, True])
def test_paid_groups_update_entities_but_failed_year_end_does_not_reset_interest(fail_year_end: bool) -> None:
    case = financed_property_case()
    purchase = case.scenario.scheduled_property_purchases[0]
    assert purchase.mortgage is not None
    bob_purchase = purchase.model_copy(
        update={
            "property_id": "bob-home",
            "cause_id": "bob-buys-home",
            "buyer_agent_id": "bob",
            "mortgage": purchase.mortgage.model_copy(update={"liability_id": "bob-loan"}),
        }
    )
    authored = scenario(
        checking(("alice", Decimal(300_000)), ("bob", Decimal(300_000)), ("seller", Decimal(0)), ("bank", Decimal(0))),
        horizon_months=12,
        tax_profiles=[],
        scheduled_property_purchases=[purchase, bob_purchase],
        scheduled_obligations=[
            ScheduledObligation(
                month=11,
                obligation_id="unfundable",
                obligation_type="cash_spend",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="seller",
                to_account_id="checking",
                amount_due=1_000_000,
            )
        ]
        if fail_year_end
        else [],
    )
    session = _run(replace(case, scenario=authored).compiled_run, "forensic")
    path = session.paths[0]
    assert path.result is not None
    assert path.result.financial is not None
    previous, ending = path.result.financial.months[-2:]
    before = {loan.liability_id: loan for loan in previous.mortgages}
    after = {loan.liability_id: loan for loan in ending.mortgages}
    assert path.failed == fail_year_end
    assert after["bob-loan"].principal < before["bob-loan"].principal
    if fail_year_end:
        assert after["home-mortgage"].principal == before["home-mortgage"].principal
        assert after["home-mortgage"].interest_paid_ytd == before["home-mortgage"].interest_paid_ytd > 0
        assert after["bob-loan"].interest_paid_ytd > before["bob-loan"].interest_paid_ytd
    else:
        assert all(loan.interest_paid_ytd == 0 for loan in after.values())
    for id_, loan in path.mortgages.items():
        assert loan.observe(after[id_].principal) == after[id_]
        liability = next(
            row.balance for row in ending.balances if row.account.account_id == f"liability:mortgage:{id_}"
        )
        assert liability == -after[id_].principal


if __name__ == "__main__":
    pytest_bazel.main()
