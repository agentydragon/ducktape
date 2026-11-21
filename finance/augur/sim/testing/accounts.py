"""Checking-account references and opening balances for tests that declare their own accounts."""

from decimal import Decimal

from finance.augur.sim.books import AccountRef
from finance.augur.sim.ids import CHECKING, AccountId, AgentId
from finance.augur.sim.money import USD


def ref(agent_id: AgentId, account_id: AccountId = CHECKING) -> AccountRef:
    return AccountRef(agent_id=agent_id, account_id=account_id)


def account(
    agent_id: AgentId, balance: Decimal | int = 0, *, account_id: AccountId = CHECKING
) -> tuple[AccountRef, int]:
    """An account and its opening balance in dollars, as currency quanta."""
    return ref(agent_id, account_id), USD.quanta(balance)
