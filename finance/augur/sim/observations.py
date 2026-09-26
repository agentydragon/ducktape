"""Current actor facts, with no future market path or other actors' books."""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field

from finance.augur.sim.actions import ClaimId
from finance.augur.sim.books import AccountRef, Record, TaxLiabilityState
from finance.augur.sim.ids import AccountId, AgentId, AssetId, LotId, PortfolioId
from finance.augur.sim.results import Receipt


class HoldingPool(Record):
    account_id: AccountId
    asset_id: AssetId
    quantity_scale: int
    price: int


class PublicPosition(Record):
    account_id: AccountId
    asset_id: AssetId
    lot_id: LotId
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
    account_id: AccountId
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


class TlhPortfolioObservation(Record):
    """A managed portfolio at its mark: money and basis, no units or unit price."""

    portfolio_id: PortfolioId
    owner_agent_id: AgentId
    account_id: AccountId
    # The index whose level moves `value`; the portfolio is not a holding of it.
    asset_id: AssetId
    value: int
    reported_tax_basis: int
    # False at a zero index mark, where the portfolio refuses a contribution. Value and basis
    # cannot say this: a worthless portfolio and an empty one can both show 0 and 0.
    accepts_contributions: bool


class TaxRecords(Record):
    """What the tax book has recorded for the observing taxpayer, as of this month's mail."""

    income: tuple[tuple[str, int], ...] = Field(
        description=(
            "This tax year's income per declared income source (`ordinary`, `interest:<issuer>`), in declaration "
            "order; ordinary income is net of the deductions already taken from it."
        )
    )
    short_term_gain: int = Field(
        description=(
            "This tax year's realized short-term gain so far, negative for a net loss; the close nets it against "
            "long-term results and the carryforward."
        )
    )
    long_term_gain: int = Field(description="This tax year's realized long-term gain so far, negative for a net loss.")
    capital_loss_carryforward: int = Field(
        description=(
            "Net capital loss carried into this tax year from the last close. The book keeps gains and this "
            "carryforward per taxpayer; each of its jurisdictions assesses the same figures."
        )
    )
    liabilities: tuple[TaxLiabilityState, ...] = Field(
        description=(
            "Year-close assessments per jurisdiction that the year's true-up has not yet settled. `amount_owed` is "
            "the assessed tax, gross of estimated payments made toward it."
        )
    )


class Observation(Record):
    """Money uses currency quanta; CPI is current/origin, absent only if unmodeled."""

    agent_id: AgentId
    month: int
    cpi: tuple[int, int] | None
    cash: int
    public_holdings: int
    accounts: tuple[tuple[AccountId, int], ...]
    holding_pools: tuple[HoldingPool, ...]
    public_positions: tuple[PublicPosition, ...]
    held_bonds: tuple[HeldBond, ...]
    tlh_portfolios: tuple[TlhPortfolioObservation, ...]
    claims: tuple[Claim, ...]
    # Absent when the actor is not an enrolled taxpayer.
    tax_records: TaxRecords | None
    previous_receipts: tuple[Receipt, ...] = ()


@dataclass(frozen=True)
class Decision:
    rollout_id: int
    observation: Observation
