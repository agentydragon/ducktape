"""Each month books what the world was declared to hold.

An unfundable bill, on a world composed from declared facts and driven by a household that pays
every due claim in full, in order.
"""

from decimal import Decimal

import pytest_bazel

from finance.augur.policy.funding import ClaimPayer
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.ids import AccountId, AgentId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import USD
from finance.augur.sim.schedule import Recurring
from finance.augur.sim.world import World

ALICE = AgentId("alice")
CHECKING = AccountId("checking")


def ref(agent_id: AgentId, account_id: AccountId = CHECKING) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=account_id)


def account(agent_id: AgentId, balance: Decimal | int = 0) -> tuple[AccountRef, int]:
    """An account and its opening balance."""
    return ref(agent_id), USD.quanta(balance)


def world_for(*accounts: tuple[AccountRef, int], horizon_months: int) -> World:
    """An empty world holding the declared cash accounts."""
    world = World(MarketPath((), 0, rollout_count=1), horizon_months=horizon_months, income_sources=(ORDINARY_INCOME,))
    for opened, balance in accounts:
        world.declare_account(account=opened, opening_balance=balance)
    return world


def run(world: World) -> list[Book]:
    """Every month to the horizon or the stop; the books the caller keeps for itself between steps."""
    world.track(ClaimPayer(AgentId(ALICE)))
    books = [world.book()]
    world.start()
    while not world.finished:
        world.step()
        books.append(world.book())
    return books


def cash(books: list[Book], agent_id: AgentId, month: int) -> int:
    [balance] = [
        row.balance
        for row in books[month].balances
        if (row.account.agent_id, row.account.account_id) == (agent_id, CHECKING)
    ]
    return balance


def rent(amount: Decimal | int, *, end_month: int) -> Biller:
    return Biller(
        schedule=Recurring(start_month=0, end_month=end_month),
        obligation_id="rent",
        obligation_type="rent",
        from_account=ref(ALICE),
        to_account=ref(AgentId("landlord")),
        amount_due=USD.quanta(amount),
        property_id=None,
        deduction_category=None,
        deductible_fraction_ppb=1_000_000_000,
    )


def test_unfundable_bill_stops_the_path_keeping_both_parties_balances() -> None:
    # No income: alice can pay rent in month 0 (1000 -> 400) but not month 1 (needs 600), so the
    # rollout stops at month 1, preserving both parties' actual balances.
    world = world_for(account(ALICE, 1000), account(AgentId("landlord")), horizon_months=12)
    world.track(rent(600, end_month=11))
    books = run(world)

    assert cash(books, ALICE, 1) == 40_000  # after month 0: rent paid (1000 -> 400)
    assert cash(books, AgentId("landlord"), 1) == 60_000  # month 0's rent landed pre-failure
    assert cash(books, ALICE, 2) == 40_000
    assert cash(books, AgentId("landlord"), 2) == 60_000
    assert len(books) == 3  # the stopped month is the last one booked


if __name__ == "__main__":
    pytest_bazel.main()
