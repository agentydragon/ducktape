"""Current actor facts, with no future market path or other actors' books."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from finance.augur.sim.actions import ClaimId
from finance.augur.sim.books import AccountRef, Record
from finance.augur.sim.results import Receipt


class HoldingPool(Record):
    account_id: str
    asset_id: str
    quantity_scale: int
    price: int


class PublicPosition(Record):
    account_id: str
    asset_id: str
    lot_id: str
    purchase_month: int
    units: int
    quantity_scale: int
    book_basis: int
    price: int
    value: int


class FixedCoupon(Record):
    kind: Literal["fixed"] = "fixed"
    amount: int


class IndexedCoupon(Record):
    kind: Literal["indexed"] = "indexed"
    annual_rate_ppb: int


class HeldBond(Record):
    """Unredeemed contract: principal is carrying value, not tradable proceeds."""

    bond_id: str
    account_id: str
    issuer_jurisdiction_id: str | None
    face_value: int
    purchase_price: int
    coupon: Annotated[FixedCoupon | IndexedCoupon, Field(discriminator="kind")]
    coupon_period_months: int
    purchase_month: int
    maturity_month: int
    principal: int


class Claim(ClaimId):
    """A current financial occurrence with a private session/rollout payment handle."""

    cause_id: str
    obligation_type: str
    from_account: AccountRef = Field(alias="from")
    to_account: AccountRef = Field(alias="to")
    amount_due: int

    @property
    def due_month(self) -> int:
        return self.month


class TlhPortfolioObservation(Record):
    portfolio_id: str
    owner_agent_id: str
    account_id: str
    asset_id: str
    value: int
    reported_tax_basis: int


class Observation(Record):
    """Money uses currency quanta; CPI is current/origin, absent only if unmodeled."""

    agent_id: str
    month: int
    cpi: tuple[int, int] | None
    cash: int
    public_holdings: int
    accounts: tuple[tuple[str, int], ...]
    holding_pools: tuple[HoldingPool, ...]
    public_positions: tuple[PublicPosition, ...]
    held_bonds: tuple[HeldBond, ...]
    tlh_portfolios: tuple[TlhPortfolioObservation, ...]
    claims: tuple[Claim, ...]
    previous_receipts: tuple[Receipt, ...] = ()


@dataclass(frozen=True)
class Decision:
    rollout_id: int
    observation: Observation
