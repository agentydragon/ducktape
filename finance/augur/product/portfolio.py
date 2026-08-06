"""Product-facing portfolio read model."""

from __future__ import annotations

from pydantic import NonNegativeFloat, NonNegativeInt

from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import (
    BondHoldingConfig,
    HoldingKind,
    HoldingPositionConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.api.schemas import ApiModel
from finance.augur.product.asset_key import AssetKey


class ProductPublicSecurityLot(ApiModel):
    lot_id: str
    holding_period_months_at_start: NonNegativeInt
    quantity: NonNegativeFloat
    cost_basis_usd: NonNegativeFloat
    cost_basis_per_unit_usd: NonNegativeFloat


class ProductPublicSecurityPosition(ApiModel):
    position_id: str
    account_id: str
    account_label: str | None = None
    label: str | None = None
    symbol: str
    # Display routing for a tradable security (etf / stock / crypto); `None` for private equity,
    # which is not a flavour of security. Consumers testing "is this PE?" read `asset.kind`.
    security_kind: HoldingKind | None = None
    asset: AssetKey
    unit_value_usd: NonNegativeFloat
    quantity: NonNegativeFloat
    current_value_usd: NonNegativeFloat
    total_cost_basis_usd: NonNegativeFloat
    lots: tuple[ProductPublicSecurityLot, ...]


class ProductBondPosition(ApiModel):
    """A held-to-maturity bond, presented on its own terms.

    No `unit_value_usd` and no `current_value_usd`, deliberately: a bond held to maturity is
    never marked, so a "value" field would assert a price the model does not produce. What it
    has instead is face, a coupon, and a maturity — which is what a bond statement shows.
    """

    bond_id: str
    account_id: str
    account_label: str | None = None
    label: str | None = None
    issuer_jurisdiction_id: str | None = None
    face_value_usd: NonNegativeFloat
    annual_coupon_rate: NonNegativeFloat
    coupon_period_months: NonNegativeInt
    inflation_indexed: bool
    months_to_maturity_at_start: NonNegativeInt


class ProductPortfolioResponse(ApiModel):
    as_of_date: str
    cash_usd: float
    holdings: tuple[ProductPublicSecurityPosition, ...]
    bonds: tuple[ProductBondPosition, ...] = ()
    total_holdings_value_usd: NonNegativeFloat
    total_holdings_cost_basis_usd: NonNegativeFloat
    # Kept out of `total_holdings_value_usd` on purpose — see `ProductBondPosition`. Face on the
    # books is not a mark, and one total conflating the two would read as if it were.
    total_bond_face_value_usd: NonNegativeFloat = 0.0


def product_portfolio_response(*, snapshot: FinanceSnapshot, portfolio: PortfolioConfig) -> ProductPortfolioResponse:
    account_label_by_id = {account.account_id: account.label for account in portfolio.accounts}
    holdings = tuple(
        _holding_position(position, account_label=account_label_by_id.get(position.account_id))
        for position in portfolio.holdings
    )
    bonds = tuple(
        _bond_position(bond, account_label=account_label_by_id.get(bond.account_id)) for bond in portfolio.bonds
    )
    return ProductPortfolioResponse(
        as_of_date=snapshot.as_of_date,
        cash_usd=float(snapshot.cash_usd),
        holdings=holdings,
        bonds=bonds,
        total_holdings_value_usd=sum(float(position.current_value_usd) for position in holdings),
        total_holdings_cost_basis_usd=sum(float(position.total_cost_basis_usd) for position in holdings),
        total_bond_face_value_usd=sum(float(bond.face_value_usd) for bond in bonds),
    )


def _bond_position(bond: BondHoldingConfig, *, account_label: str | None) -> ProductBondPosition:
    return ProductBondPosition(
        bond_id=bond.bond_id,
        account_id=bond.account_id,
        account_label=account_label,
        label=bond.label,
        issuer_jurisdiction_id=bond.issuer_jurisdiction_id,
        face_value_usd=float(bond.face_value_usd),
        annual_coupon_rate=float(bond.annual_coupon_rate),
        coupon_period_months=int(bond.coupon_period_months),
        inflation_indexed=bond.inflation_indexed,
        months_to_maturity_at_start=int(bond.months_to_maturity_at_start),
    )


def _holding_position(position: HoldingPositionConfig, *, account_label: str | None) -> ProductPublicSecurityPosition:
    return ProductPublicSecurityPosition(
        position_id=position.position_id,
        account_id=position.account_id,
        account_label=account_label,
        label=position.label,
        symbol=position.display_symbol,
        security_kind=position.security_kind if isinstance(position, SecurityHoldingConfig) else None,
        asset=position.asset,
        unit_value_usd=float(position.unit_value_usd),
        quantity=float(position.total_quantity),
        current_value_usd=float(position.current_value_usd),
        total_cost_basis_usd=float(position.total_cost_basis_usd),
        lots=tuple(
            ProductPublicSecurityLot(
                lot_id=lot.lot_id,
                holding_period_months_at_start=int(lot.holding_period_months_at_start),
                quantity=float(lot.quantity),
                cost_basis_usd=float(lot.cost_basis_usd),
                cost_basis_per_unit_usd=float(lot.cost_basis_per_unit_usd),
            )
            for lot in position.lots
        ),
    )
