"""Product-facing portfolio read model."""

from __future__ import annotations

from decimal import Decimal

from pydantic import NonNegativeFloat, NonNegativeInt

from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import (
    BondHoldingConfig,
    HoldingKind,
    HoldingPositionConfig,
    LabeledTlhPortfolio,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.api.schemas import ApiModel
from finance.augur.model.asset_key import AssetKey
from finance.augur.product.wire import CurrencyQuanta
from finance.augur.sim.fixed_point import currency_amount_to_quanta
from finance.augur.sim.ids import AccountId, AgentId, BondId, LotId, PortfolioId


class ProductPublicSecurityLot(ApiModel):
    lot_id: LotId
    holding_period_months_at_start: NonNegativeInt
    quantity: NonNegativeFloat
    cost_basis_quanta: CurrencyQuanta


class ProductPublicSecurityPosition(ApiModel):
    position_id: str
    account_id: AccountId
    account_label: str | None = None
    label: str | None = None
    symbol: str
    # Display routing for a tradable security (etf / stock / crypto); `None` for private equity,
    # which is not a flavour of security. Consumers testing "is this PE?" read `asset.kind`.
    security_kind: HoldingKind | None = None
    asset: AssetKey
    unit_value_quanta: CurrencyQuanta
    quantity: NonNegativeFloat
    current_value_quanta: CurrencyQuanta
    total_cost_basis_quanta: CurrencyQuanta
    lots: tuple[ProductPublicSecurityLot, ...]


class ProductBondPosition(ApiModel):
    """A held-to-maturity bond, presented on its own terms.

    No unit/current value is exposed: a bond held to maturity is never marked. Face value is
    exact currency quanta, alongside coupon and maturity facts.
    """

    bond_id: BondId
    account_id: AccountId
    account_label: str | None = None
    label: str | None = None
    issuer_jurisdiction_id: str | None = None
    face_value_quanta: CurrencyQuanta
    annual_coupon_rate: NonNegativeFloat
    coupon_period_months: NonNegativeInt
    inflation_indexed: bool
    months_to_maturity_at_start: NonNegativeInt


class ProductTlhCohort(ApiModel):
    holding_period_months_at_start: NonNegativeInt
    value_quanta: CurrencyQuanta
    cost_basis_quanta: CurrencyQuanta


class ProductTlhPortfolio(ApiModel):
    """A managed tax-loss-harvesting portfolio: money and basis by cohort, with no units or unit price.

    `asset` is the index whose price moves the portfolio's value; a sleeve weight names it by its symbol.
    """

    portfolio_id: PortfolioId
    owner_agent_id: AgentId
    account_id: AccountId
    account_label: str | None = None
    label: str
    asset: AssetKey
    current_value_quanta: CurrencyQuanta
    total_cost_basis_quanta: CurrencyQuanta
    cohorts: tuple[ProductTlhCohort, ...]


class ProductPortfolioResponse(ApiModel):
    as_of_date: str
    currency_code: str
    currency_quantum: str
    cash_quanta: CurrencyQuanta
    holdings: tuple[ProductPublicSecurityPosition, ...]
    tlh_portfolios: tuple[ProductTlhPortfolio, ...]
    bonds: tuple[ProductBondPosition, ...] = ()
    # Ordinary `holdings` only; TLH portfolios carry their own values.
    total_holdings_value_quanta: CurrencyQuanta
    total_holdings_cost_basis_quanta: CurrencyQuanta
    # Kept out of `total_holdings_value_quanta` on purpose — face on the books is not a mark.
    total_bond_face_value_quanta: CurrencyQuanta = "0"


def product_portfolio_response(
    *,
    snapshot: FinanceSnapshot,
    portfolio: PortfolioConfig,
    tlh_portfolios: tuple[LabeledTlhPortfolio, ...],
    currency_code: str = "USD",
    currency_quantum: Decimal = Decimal("0.01"),
) -> ProductPortfolioResponse:
    account_label_by_id = {account.account_id: account.label for account in portfolio.accounts}
    holdings = tuple(
        _holding_position(
            position, account_label=account_label_by_id.get(position.account_id), currency_quantum=currency_quantum
        )
        for position in portfolio.holdings
    )
    tlh = tuple(
        _tlh_portfolio(
            portfolio,
            account_label=account_label_by_id.get(portfolio.spec.account_id),
            currency_quantum=currency_quantum,
        )
        for portfolio in tlh_portfolios
    )
    bonds = tuple(
        _bond_position(bond, account_label=account_label_by_id.get(bond.account_id), currency_quantum=currency_quantum)
        for bond in portfolio.bonds
    )
    return ProductPortfolioResponse(
        as_of_date=snapshot.as_of_date,
        currency_code=currency_code,
        currency_quantum=format(currency_quantum, "f"),
        cash_quanta=_quanta(snapshot.cash, quantum=currency_quantum),
        holdings=holdings,
        tlh_portfolios=tlh,
        bonds=bonds,
        total_holdings_value_quanta=_quanta(portfolio.total_holdings_value, quantum=currency_quantum),
        total_holdings_cost_basis_quanta=_quanta(
            sum((position.total_cost_basis for position in portfolio.holdings), start=Decimal(0)),
            quantum=currency_quantum,
        ),
        total_bond_face_value_quanta=_quanta(portfolio.total_bond_face_value, quantum=currency_quantum),
    )


def _tlh_portfolio(
    portfolio: LabeledTlhPortfolio, *, account_label: str | None, currency_quantum: Decimal
) -> ProductTlhPortfolio:
    spec = portfolio.spec
    return ProductTlhPortfolio(
        portfolio_id=spec.portfolio_id,
        owner_agent_id=spec.owner_agent_id,
        account_id=spec.account_id,
        account_label=account_label,
        label=portfolio.label,
        asset=spec.asset,
        current_value_quanta=_quanta(
            sum((cohort.value for cohort in spec.initial_cohorts), start=Decimal(0)), quantum=currency_quantum
        ),
        total_cost_basis_quanta=_quanta(
            sum((cohort.cost_basis for cohort in spec.initial_cohorts), start=Decimal(0)), quantum=currency_quantum
        ),
        cohorts=tuple(
            ProductTlhCohort(
                holding_period_months_at_start=-cohort.purchase_month_index,
                value_quanta=_quanta(cohort.value, quantum=currency_quantum),
                cost_basis_quanta=_quanta(cohort.cost_basis, quantum=currency_quantum),
            )
            for cohort in spec.initial_cohorts
        ),
    )


def _bond_position(
    bond: BondHoldingConfig, *, account_label: str | None, currency_quantum: Decimal
) -> ProductBondPosition:
    return ProductBondPosition(
        bond_id=bond.bond_id,
        account_id=bond.account_id,
        account_label=account_label,
        label=bond.label,
        issuer_jurisdiction_id=bond.issuer_jurisdiction_id,
        face_value_quanta=_quanta(bond.face_value, quantum=currency_quantum),
        annual_coupon_rate=float(bond.annual_coupon_rate),
        coupon_period_months=int(bond.coupon_period_months),
        inflation_indexed=bond.inflation_indexed,
        months_to_maturity_at_start=int(bond.months_to_maturity_at_start),
    )


def _holding_position(
    position: HoldingPositionConfig, *, account_label: str | None, currency_quantum: Decimal
) -> ProductPublicSecurityPosition:
    return ProductPublicSecurityPosition(
        position_id=position.position_id,
        account_id=position.account_id,
        account_label=account_label,
        label=position.label,
        symbol=position.display_symbol,
        security_kind=position.security_kind if isinstance(position, SecurityHoldingConfig) else None,
        asset=position.asset,
        unit_value_quanta=_quanta(position.unit_value, quantum=currency_quantum),
        quantity=float(position.total_quantity),
        current_value_quanta=_quanta(position.current_value, quantum=currency_quantum),
        total_cost_basis_quanta=_quanta(position.total_cost_basis, quantum=currency_quantum),
        lots=tuple(
            ProductPublicSecurityLot(
                lot_id=lot.lot_id,
                holding_period_months_at_start=int(lot.holding_period_months_at_start),
                quantity=float(lot.quantity),
                cost_basis_quanta=_quanta(lot.cost_basis, quantum=currency_quantum),
            )
            for lot in position.lots
        ),
    )


def _quanta(value: Decimal, *, quantum: Decimal) -> str:
    return str(int(currency_amount_to_quanta(value, quantum=quantum)))
