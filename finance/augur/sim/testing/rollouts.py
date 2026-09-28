"""Readers over a finished rollout's summary and forensic trace."""

from more_itertools import one

from finance.augur.sim.books import AccountRef, Book
from finance.augur.sim.ids import AccountId, AgentId
from finance.augur.sim.results import Rollout


def book(rollout: Rollout, month: int) -> Book:
    """The traced book at `month`."""
    assert rollout.trace is not None
    return one(entry for entry in rollout.trace.books if entry.month == month)


def cash(rollout: Rollout, agent_id: AgentId, month: int) -> float:
    """The agent's `checking` balance at `month`, in dollars."""
    account = AccountRef(agent_id=agent_id, account_id=AccountId("checking"))
    return one(row.balance for row in book(rollout, month).balances if row.account == account) / 100


def tax_by_jurisdiction(rollout: Rollout) -> dict[str, int]:
    """Each jurisdiction's tax accrued over the whole rollout, in quanta."""
    taxes: dict[str, int] = {}
    for accrual in rollout.summary.tax_accruals:
        taxes[accrual.jurisdiction_id] = taxes.get(accrual.jurisdiction_id, 0) + accrual.total_tax
    return taxes
