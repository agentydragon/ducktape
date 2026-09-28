from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.api.portfolio import (
    BondHoldingConfig,
    HoldingKind,
    HoldingTaxLotConfig,
    PortfolioAccountConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.model.series import SecuritySymbol
from finance.augur.product.holdings import opening_holdings, prepared_bonds, prepared_lots
from finance.augur.sim.ids import AccountId, AgentId, LotId
from finance.augur.sim.money import USD

BROKERAGE = AccountId("brokerage")
CHECKING = AccountId("checking")
TAXABLE_BROKERAGE = AccountId("taxable_brokerage")
AGENT_A = AgentId("agent_a")
ALICE = AgentId("alice")


def test_holding_tax_lots_expand_to_prepared_lots() -> None:
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
                        lot_id=LotId("voo_2024_05_20"),
                        holding_period_months_at_start=24,
                        quantity=100.0,
                        cost_basis=Decimal(30_000),
                    ),
                    HoldingTaxLotConfig(
                        lot_id=LotId("voo_2026_05_20"),
                        holding_period_months_at_start=0,
                        quantity=20.0,
                        cost_basis=Decimal(9_000),
                    ),
                ),
            ),
        ),
    )

    lots = prepared_lots(portfolio, currency=USD)

    assert [(lot.lot_id, lot.agent_id, lot.account_id, lot.asset_id, lot.purchase_month) for lot in lots] == [
        ("voo_2024_05_20", "agent_a", "taxable_brokerage", "VOO", -24),
        ("voo_2026_05_20", "agent_a", "taxable_brokerage", "VOO", 0),
    ]
    assert lots[0].units == 100 * lots[0].quantity_scale
    assert (lots[0].basis, lots[1].basis) == (3_000_000, 900_000)


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


def test_a_bond_converts_both_months_relative_to_month_zero() -> None:
    """The whole point of the config idiom: a deployment writes "held 24 months, matures in 96"
    and never a calendar date, so the two conversions are where a sign error would hide. A bond
    held 24 months is `purchase_month_index=-24`, in the PAST."""

    [bond] = prepared_bonds(_bond_portfolio(), coupon_account_id=CHECKING, currency=USD)

    assert bond.purchase_month_index == -24
    assert bond.maturity_month_index == 96


def test_a_bonds_owner_comes_through_its_custody_account() -> None:
    """Like a lot: the account is the owner-bearing object, and the bond names no agent."""

    [bond] = prepared_bonds(_bond_portfolio(), coupon_account_id=CHECKING, currency=USD)

    assert bond.agent_id == "alice"


def test_coupons_land_in_the_named_cash_account_not_the_custody_account() -> None:
    """The two are different things that are the same string only by coincidence. A portfolio
    account is custody (`brokerage`) and carries no cash row, so a coupon paid into one would
    have nowhere to go — the caller names the destination because it knows its cash topology."""

    [bond] = prepared_bonds(_bond_portfolio(), coupon_account_id=CHECKING, currency=USD)

    assert bond.account_id == "checking"


def test_a_non_par_purchase_survives_config_to_be_rejected_by_the_app() -> None:
    """The reason `purchase_price` is carried at all despite having one legal value today.

    Config is where somebody writes what they actually paid. Dropping the field would silently
    promote a bond bought at 98.5 to par — the exact failure this check exists to make loud —
    so the config accepts it and preparing the holdings is what raises.
    """

    portfolio = _bond_portfolio(purchase_price=98_500)

    with pytest.raises(ValueError, match="bought away from par"):
        opening_holdings(portfolio, (), tlh_portfolios=(), primary_agent_id=ALICE, payout_account_id=CHECKING)


if __name__ == "__main__":
    pytest_bazel.main()
