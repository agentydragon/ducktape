"""Product-facing portfolio read model."""

from __future__ import annotations

from pydantic import NonNegativeFloat, NonNegativeInt

from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import HoldingPositionConfig, PortfolioConfig
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
    security_kind: str
    value_series: AssetKey
    unit_value_usd: NonNegativeFloat
    quantity: NonNegativeFloat
    current_value_usd: NonNegativeFloat
    total_cost_basis_usd: NonNegativeFloat
    lots: tuple[ProductPublicSecurityLot, ...]


class ProductPortfolioResponse(ApiModel):
    as_of_date: str
    cash_usd: float
    holdings: tuple[ProductPublicSecurityPosition, ...]
    total_holdings_value_usd: NonNegativeFloat
    total_holdings_cost_basis_usd: NonNegativeFloat


def product_portfolio_response(*, snapshot: FinanceSnapshot, portfolio: PortfolioConfig) -> ProductPortfolioResponse:
    account_label_by_id = {account.account_id: account.label for account in portfolio.accounts}
    holdings = tuple(
        _holding_position(position, account_label=account_label_by_id.get(position.account_id))
        for position in portfolio.holdings
    )
    return ProductPortfolioResponse(
        as_of_date=snapshot.as_of_date,
        cash_usd=float(snapshot.cash_usd),
        holdings=holdings,
        total_holdings_value_usd=sum(float(position.current_value_usd) for position in holdings),
        total_holdings_cost_basis_usd=sum(float(position.total_cost_basis_usd) for position in holdings),
    )


def _holding_position(position: HoldingPositionConfig, *, account_label: str | None) -> ProductPublicSecurityPosition:
    return ProductPublicSecurityPosition(
        position_id=position.position_id,
        account_id=position.account_id,
        account_label=account_label,
        label=position.label,
        symbol=position.symbol,
        security_kind=str(position.security_kind),
        # Typed, not a wire string: the sell-order UI keys rows by the SERIES symbol, which can
        # differ from the holding's display ticker (a VOO holding is priced by the SPY series).
        # Shipping the string would put prefix-parsing back in the frontend to recover it.
        value_series=position.value_series,
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
