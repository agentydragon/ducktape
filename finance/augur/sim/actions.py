"""Exact policy requests; execution owns affordability, ownership and settlement."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, PrivateAttr

from finance.augur.sim.books import AccountRef, Record
from finance.augur.sim.ids import AccountId, AgentId, AssetId, LotId, PortfolioId


class ClaimId(Record):
    month: int
    index: int

    _session_owner: object | None = PrivateAttr(default=None)
    _rollout_id: int | None = PrivateAttr(default=None)

    def _bind(self, owner: object, rollout_id: int) -> None:
        self._session_owner = owner
        self._rollout_id = rollout_id

    def _belongs_to(self, owner: object, rollout_id: int) -> bool:
        return self._session_owner is owner and self._rollout_id == rollout_id


class LotSale(Record):
    account_id: AccountId
    lot_id: LotId
    units: int


class Sell(Record):
    kind: Literal["Sell"] = "Sell"
    cause_id: str
    agent_id: AgentId
    proceeds_account_id: AccountId
    asset_id: AssetId
    lots: tuple[LotSale, ...]


class Buy(Record):
    kind: Literal["Buy"] = "Buy"
    cause_id: str
    agent_id: AgentId
    cash_account_id: AccountId
    holding_account_id: AccountId
    asset_id: AssetId
    lot_id: LotId
    quantity_scale: int
    units: int


class Transfer(Record):
    kind: Literal["Transfer"] = "Transfer"
    cause_id: str
    from_account: AccountRef = Field(alias="from")
    to_account: AccountRef = Field(alias="to")
    amount: int


class PayClaim(Record):
    kind: Literal["PayClaim"] = "PayClaim"
    request_id: int
    cause_id: str
    claim: ClaimId
    from_account: AccountRef = Field(alias="from")
    amount: int


class Consume(Record):
    kind: Literal["Consume"] = "Consume"
    request_id: int
    cause_id: str
    component_id: str
    from_account: AccountRef = Field(alias="from")
    to_account: AccountRef = Field(alias="to")
    amount: int


class Contribute(Record):
    kind: Literal["Contribute"] = "Contribute"
    cause_id: str
    agent_id: AgentId
    portfolio_id: PortfolioId
    cash_account_id: AccountId
    amount: int


class Withdraw(Record):
    kind: Literal["Withdraw"] = "Withdraw"
    cause_id: str
    agent_id: AgentId
    portfolio_id: PortfolioId
    cash_account_id: AccountId
    amount: int


class Liquidate(Record):
    kind: Literal["Liquidate"] = "Liquidate"
    cause_id: str
    agent_id: AgentId
    portfolio_id: PortfolioId
    cash_account_id: AccountId


type Action = Annotated[
    Sell | Buy | Transfer | PayClaim | Consume | Contribute | Withdraw | Liquidate, Field(discriminator="kind")
]


@dataclass(frozen=True)
class DecisionActions:
    rollout_id: int
    month: int
    actions: list[Action]
