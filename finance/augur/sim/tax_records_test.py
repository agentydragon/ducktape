"""A household's tax records: its own recorded facts at month open, and no assessment before its close."""

from collections.abc import Mapping

import pytest
import pytest_bazel

from finance.augur.sim.actions import Action, LotSale, PayClaim, Sell
from finance.augur.sim.agent import EconomicAgent
from finance.augur.sim.books import TaxLiabilityState
from finance.augur.sim.ids import AccountId, AgentId, AssetId, LotId
from finance.augur.sim.observations import Observation, TaxRecords
from finance.augur.sim.prepared import PreparedHoldingPool, PreparedLot, PreparedRecurringTransfer, PreparedSeries
from finance.augur.sim.results import Executed
from finance.augur.sim.scenario import ORDINARY_INCOME
from finance.augur.sim.testing.accounting import (
    CASH,
    EXOGENOUS,
    HOUSEHOLD,
    OTHER,
    RECIPIENT,
    opening,
    taxpayer,
    world_on,
)
from finance.augur.sim.world import World

HORIZON = 14
PRICE = 100_000
# Paid monthly through the first year's close and into the next year.
HOUSEHOLD_WAGE, OTHER_WAGE = 50_000, 7_000


def lot(id_: LotId, agent: AgentId, *, basis: int, purchase_month: int) -> PreparedLot:
    """One unit of stock, worth `PRICE` on the flat path."""
    return PreparedLot(
        lot_id=id_,
        agent_id=agent,
        account_id=AccountId("checking"),
        asset_id=AssetId("stock"),
        purchase_month=purchase_month,
        quantity_scale=10,
        units=10,
        basis=basis,
    )


def sale(agent: AgentId, lot_id: LotId) -> Sell:
    return Sell(
        cause_id=f"sell-{lot_id}",
        agent_id=agent,
        proceeds_account_id=AccountId("checking"),
        asset_id=AssetId("stock"),
        lots=(LotSale(account_id=AccountId("checking"), lot_id=lot_id, units=10),),
    )


def wage(payee: AgentId, amount: int) -> PreparedRecurringTransfer:
    return PreparedRecurringTransfer(
        start_month=0,
        end_month=None,
        cause_id=f"wage-{payee}",
        from_account=EXOGENOUS,
        to_account=CASH if payee == HOUSEHOLD else RECIPIENT,
        amount=amount,
        income_category=ORDINARY_INCOME,
        deduction_category=None,
    )


class _Recorder(EconomicAgent):
    """Keeps every observation, pays every due claim and makes the scripted sales."""

    def __init__(self, sales: Mapping[int, Sell]) -> None:
        super().__init__(HOUSEHOLD)
        self.sales = sales
        self.seen: list[Observation] = []

    def decide(self, observation: Observation) -> list[Action]:
        self.seen.append(observation)
        actions: list[Action] = [
            PayClaim(
                request_id=index, cause_id="pay", claim=claim, from_account=claim.from_account, amount=claim.amount_due
            )
            for index, claim in enumerate(observation.claims)
        ]
        if observation.month in self.sales:
            actions.append(self.sales[observation.month])
        return actions


def run(sales: Mapping[int, Sell], *, taxed: bool = True, other_trades: bool = True) -> tuple[World, list[Observation]]:
    """Both actors are flat-10% taxpayers with wages; the other may also sell a short-term winner in month 0."""
    world = world_on(
        (PreparedSeries(series_id="security:stock", snapshots=HORIZON + 1, values=(PRICE,) * (HORIZON + 1)),),
        horizon_months=HORIZON,
        accounts=opening({EXOGENOUS: 10_000_000}),
        taxpayers=(taxpayer(HOUSEHOLD), taxpayer(OTHER)) if taxed else (),
    )
    for agent in (HOUSEHOLD, OTHER):
        world.declare_pool(
            PreparedHoldingPool(
                agent_id=agent, account_id=AccountId("checking"), asset_id=AssetId("stock"), quantity_scale=10
            )
        )
    world.hold(lot(LotId("loser"), HOUSEHOLD, basis=1_000_000, purchase_month=-24))
    world.hold(lot(LotId("winner"), OTHER, basis=0, purchase_month=-2))
    world.declare_flow(wage(HOUSEHOLD, HOUSEHOLD_WAGE))
    if other_trades:
        world.declare_flow(wage(OTHER, OTHER_WAGE))
    recorder = _Recorder(sales)
    world.track(recorder)
    world.start()
    if other_trades:
        assert isinstance(world.apply(OTHER, sale(OTHER, LotId("winner")), 0), Executed)
    while not world.finished:
        world.step()
    assert world.stop is None
    return world, recorder.seen


def records(observation: Observation) -> TaxRecords:
    assert observation.tax_records is not None
    return observation.tax_records


@pytest.fixture(scope="module")
def seen() -> list[Observation]:
    """The household sells its long-term loser (basis 1,000,000 for 100,000) in month 1."""
    _, observations = run({1: sale(HOUSEHOLD, LotId("loser"))})
    return observations


def test_the_years_income_accumulates_from_the_months_already_opened(seen: list[Observation]) -> None:
    # Wages move when the month opens, before its mail is posted.
    assert [records(o).income for o in seen[:12]] == [
        (("ordinary", HOUSEHOLD_WAGE * (month + 1)), ("interest:corporate", 0)) for month in range(12)
    ]


def test_a_sale_is_recorded_in_the_mail_after_it_executes(seen: list[Observation]) -> None:
    assert [(records(o).short_term_gain, records(o).long_term_gain) for o in seen[:3]] == [
        (0, 0),
        (0, 0),
        (0, -900_000),
    ]


def test_no_liability_is_visible_before_the_close_assesses_it(seen: list[Observation]) -> None:
    # Month 11 carries the whole year's income and loss, but the year closes only after it.
    assert all(records(o).liabilities == () for o in seen[:12])
    assert records(seen[11]).capital_loss_carryforward == 0


def test_the_close_resets_the_year_and_posts_the_assessment_until_the_true_up_settles_it(
    seen: list[Observation],
) -> None:
    # 600,000 of wages less the 300,000 loss offset, at 10%; the other 600,000 of loss carries.
    first_month = records(seen[12])
    assert (
        first_month.income,
        first_month.short_term_gain,
        first_month.long_term_gain,
        first_month.capital_loss_carryforward,
        first_month.liabilities,
    ) == (
        (("ordinary", HOUSEHOLD_WAGE), ("interest:corporate", 0)),
        0,
        0,
        600_000,
        (
            TaxLiabilityState(
                agent_id=HOUSEHOLD,
                jurisdiction_id="test_federal",
                tax_year_end_month=11,
                amount_owed=30_000,
                active=True,
            ),
        ),
    )
    assert [claim.obligation_type for claim in seen[12].claims] == ["tax_true_up"]
    assert (records(seen[13]).liabilities, records(seen[13]).capital_loss_carryforward) == ((), 600_000)


def test_another_actors_income_gains_and_assessment_are_not_visible() -> None:
    busy, seen = run({1: sale(HOUSEHOLD, LotId("loser"))})
    _, quiet = run({1: sale(HOUSEHOLD, LotId("loser"))}, other_trades=False)
    # The other actor's wages and short-term gain were assessed and its true-up is still owed.
    assert [(row.agent_id, row.amount_owed) for row in busy.book().tax_liabilities if row.amount_owed] == [
        (OTHER, (OTHER_WAGE * 12 + PRICE) // 10)
    ]
    assert [o.tax_records for o in seen] == [o.tax_records for o in quiet]


def test_an_actor_who_is_not_a_taxpayer_has_no_tax_records() -> None:
    _, seen = run({}, taxed=False)
    assert {o.tax_records for o in seen} == {None}


if __name__ == "__main__":
    pytest_bazel.main()
