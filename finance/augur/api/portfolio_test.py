from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_bazel
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


def test_one_account_can_hold_multiple_holding_positions() -> None:
    portfolio = PortfolioConfig(
        accounts=(PortfolioAccountConfig(account_id=TAXABLE_BROKERAGE, owner_agent_id=AGENT_A),),
        holdings=(
            SecurityHoldingConfig(
                position_id="voo_position",
                account_id=TAXABLE_BROKERAGE,
                symbol=SecuritySymbol("VOO"),
                security_kind=HoldingKind.ETF,
                unit_value=Decimal(500),
                lots=(
                    HoldingTaxLotConfig(
                        lot_id=LotId("voo_lot"),
                        holding_period_months_at_start=28,
                        quantity=10.0,
                        cost_basis=Decimal(4_000),
                    ),
                ),
            ),
            SecurityHoldingConfig(
                position_id="goog_position",
                account_id=TAXABLE_BROKERAGE,
                symbol=SecuritySymbol("GOOG"),
                security_kind=HoldingKind.STOCK,
                unit_value=Decimal(180),
                lots=(
                    HoldingTaxLotConfig(
                        lot_id=LotId("goog_lot"),
                        holding_period_months_at_start=35,
                        quantity=5.0,
                        cost_basis=Decimal(500),
                    ),
                ),
            ),
        ),
    )

    assert portfolio.total_holdings_value == Decimal(5_900)


def test_holding_positions_must_reference_known_accounts() -> None:
    with pytest.raises(ValidationError, match="unknown account_id"):
        PortfolioConfig(
            accounts=(),
            holdings=(
                SecurityHoldingConfig(
                    position_id="voo_position",
                    account_id=AccountId("missing"),
                    symbol=SecuritySymbol("VOO"),
                    security_kind=HoldingKind.ETF,
                    unit_value=Decimal(500),
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id=LotId("voo_lot"),
                            holding_period_months_at_start=28,
                            quantity=10.0,
                            cost_basis=Decimal(4_000),
                        ),
                    ),
                ),
            ),
        )


def test_holding_lot_ids_must_be_unique() -> None:
    account = PortfolioAccountConfig(account_id=TAXABLE_BROKERAGE, owner_agent_id=AGENT_A)
    with pytest.raises(ValidationError, match="unique lot_id"):
        PortfolioConfig(
            accounts=(account,),
            holdings=(
                SecurityHoldingConfig(
                    position_id="voo_position",
                    account_id=account.account_id,
                    symbol=SecuritySymbol("VOO"),
                    security_kind=HoldingKind.ETF,
                    unit_value=Decimal(500),
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id=LotId("duplicate_lot"),
                            holding_period_months_at_start=28,
                            quantity=10.0,
                            cost_basis=Decimal(4_000),
                        ),
                    ),
                ),
                SecurityHoldingConfig(
                    position_id="goog_position",
                    account_id=account.account_id,
                    symbol=SecuritySymbol("GOOG"),
                    security_kind=HoldingKind.STOCK,
                    unit_value=Decimal(180),
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id=LotId("duplicate_lot"),
                            holding_period_months_at_start=35,
                            quantity=5.0,
                            cost_basis=Decimal(500),
                        ),
                    ),
                ),
            ),
        )


def test_holding_positions_sharing_series_must_share_unit_value() -> None:
    account = PortfolioAccountConfig(account_id=TAXABLE_BROKERAGE, owner_agent_id=AGENT_A)
    with pytest.raises(ValidationError, match="must share unit_value"):
        PortfolioConfig(
            accounts=(account,),
            holdings=(
                SecurityHoldingConfig(
                    position_id="sp500_a",
                    account_id=account.account_id,
                    symbol=SecuritySymbol("VOO"),
                    security_kind=HoldingKind.OTHER,
                    unit_value=Decimal(500),
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id=LotId("sp500_a_lot"),
                            holding_period_months_at_start=28,
                            quantity=10.0,
                            cost_basis=Decimal(4_000),
                        ),
                    ),
                ),
                SecurityHoldingConfig(
                    position_id="sp500_b",
                    account_id=account.account_id,
                    symbol=SecuritySymbol("VOO"),
                    security_kind=HoldingKind.OTHER,
                    unit_value=Decimal(600),
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id=LotId("sp500_b_lot"),
                            holding_period_months_at_start=35,
                            quantity=5.0,
                            cost_basis=Decimal(500),
                        ),
                    ),
                ),
            ),
        )


# -- Bonds ---------------------------------------------------------------------------------


def _bond_portfolio(**overrides: object) -> PortfolioConfig:
    bond = {
        "bond_id": "tips_rung",
        "account_id": "brokerage",
        "character": {"kind": "treasury"},
        "face_value": 100_000,
        "purchase_price": 100_000,
        "annual_coupon_rate": 0.02,
        "inflation_indexed": True,
        "holding_period_months_at_start": 24,
        "months_to_maturity_at_start": 96,
    } | overrides
    return PortfolioConfig(
        accounts=(PortfolioAccountConfig(account_id=BROKERAGE, owner_agent_id=ALICE),),
        bonds=(BondHoldingConfig.model_validate(bond),),
    )


def test_a_bond_on_an_unknown_account_is_rejected() -> None:
    with pytest.raises(ValidationError, match="bonds reference unknown account_id"):
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
    with pytest.raises(ValidationError, match="unique bond_id"):
        PortfolioConfig(
            accounts=(PortfolioAccountConfig(account_id=BROKERAGE, owner_agent_id=ALICE),), bonds=(bond, bond)
        )


def test_bond_face_is_kept_out_of_the_holdings_value_total() -> None:
    """A held-to-maturity bond is never marked, so one total conflating face with marked value
    would assert a price the model does not produce."""

    portfolio = _bond_portfolio()

    assert portfolio.total_holdings_value == Decimal(0)
    assert portfolio.total_bond_face_value == Decimal(100_000)


if __name__ == "__main__":
    pytest_bazel.main()
