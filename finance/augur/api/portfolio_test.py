from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import ValidationError

from finance.augur.api.portfolio import (
    BondHoldingConfig,
    HoldingKind,
    HoldingTaxLotConfig,
    PortfolioAccountConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.model.series import SecuritySymbol
from finance.augur.sim.ids import AccountId, AgentId, BondId, LotId
from finance.augur.sim.income import Taxable

BROKERAGE = AccountId("brokerage")
TAXABLE_BROKERAGE = AccountId("taxable_brokerage")
AGENT_A = AgentId("agent_a")
ALICE = AgentId("alice")


def make_security_holding(
    *,
    position_id: str,
    account_id: AccountId,
    lot_id: LotId,
    symbol: str = "VOO",
    security_kind: HoldingKind = HoldingKind.OTHER,
    unit_value: Decimal = Decimal(500),
    holding_period_months_at_start: int = 28,
    quantity: float = 10.0,
    cost_basis: Decimal = Decimal(4_000),
) -> SecurityHoldingConfig:
    return SecurityHoldingConfig(
        position_id=position_id,
        account_id=account_id,
        symbol=SecuritySymbol(symbol),
        security_kind=security_kind,
        unit_value=unit_value,
        lots=(
            HoldingTaxLotConfig(
                lot_id=lot_id,
                holding_period_months_at_start=holding_period_months_at_start,
                quantity=quantity,
                cost_basis=cost_basis,
            ),
        ),
    )


def test_holding_positions_must_reference_known_accounts() -> None:
    with pytest.raises(ValidationError):
        PortfolioConfig(
            accounts=(),
            holdings=(
                make_security_holding(
                    position_id="voo_position",
                    account_id=AccountId("missing"),
                    lot_id=LotId("voo_lot"),
                    security_kind=HoldingKind.ETF,
                ),
            ),
        )


def test_holding_lot_ids_must_be_unique() -> None:
    account = PortfolioAccountConfig(account_id=TAXABLE_BROKERAGE, owner_agent_id=AGENT_A)
    with pytest.raises(ValidationError):
        PortfolioConfig(
            accounts=(account,),
            holdings=(
                make_security_holding(
                    position_id="voo_position",
                    account_id=account.account_id,
                    lot_id=LotId("duplicate_lot"),
                    symbol="VOO",
                    security_kind=HoldingKind.ETF,
                ),
                make_security_holding(
                    position_id="goog_position",
                    account_id=account.account_id,
                    lot_id=LotId("duplicate_lot"),
                    symbol="GOOG",
                    security_kind=HoldingKind.STOCK,
                    unit_value=Decimal(180),
                    holding_period_months_at_start=35,
                    quantity=5.0,
                    cost_basis=Decimal(500),
                ),
            ),
        )


def test_holding_positions_sharing_series_must_share_unit_value() -> None:
    account = PortfolioAccountConfig(account_id=TAXABLE_BROKERAGE, owner_agent_id=AGENT_A)
    with pytest.raises(ValidationError):
        PortfolioConfig(
            accounts=(account,),
            holdings=(
                make_security_holding(
                    position_id="sp500_a",
                    account_id=account.account_id,
                    lot_id=LotId("sp500_a_lot"),
                    symbol="VOO",
                    unit_value=Decimal(500),
                ),
                make_security_holding(
                    position_id="sp500_b",
                    account_id=account.account_id,
                    lot_id=LotId("sp500_b_lot"),
                    symbol="VOO",
                    unit_value=Decimal(600),
                    holding_period_months_at_start=35,
                    quantity=5.0,
                    cost_basis=Decimal(500),
                ),
            ),
        )


def test_a_bond_on_an_unknown_account_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PortfolioConfig(
            accounts=(PortfolioAccountConfig(account_id=BROKERAGE, owner_agent_id=ALICE),),
            bonds=(
                BondHoldingConfig(
                    bond_id=BondId("orphan"),
                    account_id=AccountId("nowhere"),
                    character=Taxable(),
                    face_value=Decimal(1_000),
                    purchase_price=Decimal(1_000),
                    annual_coupon_rate=0.01,
                    months_to_maturity_at_start=12,
                ),
            ),
        )


def test_duplicate_bond_ids_are_rejected() -> None:
    """Two rungs sharing an id are two different instruments the ledger cannot tell apart."""

    bond = BondHoldingConfig(
        bond_id=BondId("rung"),
        account_id=BROKERAGE,
        character=Taxable(),
        face_value=Decimal(1_000),
        purchase_price=Decimal(1_000),
        annual_coupon_rate=0.01,
        months_to_maturity_at_start=12,
    )
    with pytest.raises(ValidationError):
        PortfolioConfig(
            accounts=(PortfolioAccountConfig(account_id=BROKERAGE, owner_agent_id=ALICE),), bonds=(bond, bond)
        )


def test_bond_face_is_kept_out_of_the_holdings_value_total(bond_portfolio: Callable[..., PortfolioConfig]) -> None:
    """A held-to-maturity bond is never marked, so one total conflating face with marked value
    would assert a price the model does not produce."""

    portfolio = bond_portfolio()

    assert portfolio.total_holdings_value == Decimal(0)
    assert portfolio.total_bond_face_value == Decimal(100_000)
