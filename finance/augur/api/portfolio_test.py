from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.api.portfolio import (
    BondHoldingConfig,
    HoldingTaxLotConfig,
    PortfolioAccountConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.model.series import SecurityKey, SecuritySymbol


def test_holding_tax_lots_expand_to_sim_initial_lots() -> None:
    portfolio = PortfolioConfig(
        accounts=(PortfolioAccountConfig(account_id="taxable_brokerage", owner_agent_id="agent_a", label="Taxable"),),
        holdings=(
            SecurityHoldingConfig(
                position_id="voo_position",
                account_id="taxable_brokerage",
                symbol=SecuritySymbol("VOO"),
                security_kind="etf",
                unit_value_usd=500.0,
                lots=(
                    HoldingTaxLotConfig(
                        lot_id="voo_2024_05_20",
                        holding_period_months_at_start=24,
                        quantity=100.0,
                        cost_basis_usd=30_000.0,
                    ),
                    HoldingTaxLotConfig(
                        lot_id="voo_2026_05_20", holding_period_months_at_start=0, quantity=20.0, cost_basis_usd=9_000.0
                    ),
                ),
            ),
        ),
    )

    lots = portfolio.to_initial_lots()

    assert portfolio.holdings[0].current_value_usd == 60_000.0
    assert portfolio.holdings[0].total_cost_basis_usd == 39_000.0
    assert portfolio.total_holdings_value_usd == 60_000.0
    assert [(lot.lot_id, lot.agent_id, lot.account_id, lot.asset, lot.purchase_month_index) for lot in lots] == [
        ("voo_2024_05_20", "agent_a", "taxable_brokerage", SecurityKey(symbol=SecuritySymbol("VOO")), -24),
        ("voo_2026_05_20", "agent_a", "taxable_brokerage", SecurityKey(symbol=SecuritySymbol("VOO")), 0),
    ]
    assert lots[0].quantity == 100.0
    assert lots[0].cost_basis_per_unit_usd == 300.0
    assert lots[1].cost_basis_per_unit_usd == 450.0


def test_one_account_can_hold_multiple_holding_positions() -> None:
    portfolio = PortfolioConfig(
        accounts=(PortfolioAccountConfig(account_id="taxable_brokerage", owner_agent_id="agent_a"),),
        holdings=(
            SecurityHoldingConfig(
                position_id="voo_position",
                account_id="taxable_brokerage",
                symbol=SecuritySymbol("VOO"),
                security_kind="etf",
                unit_value_usd=500.0,
                lots=(
                    HoldingTaxLotConfig(
                        lot_id="voo_lot", holding_period_months_at_start=28, quantity=10.0, cost_basis_usd=4_000.0
                    ),
                ),
            ),
            SecurityHoldingConfig(
                position_id="goog_position",
                account_id="taxable_brokerage",
                symbol=SecuritySymbol("GOOG"),
                security_kind="stock",
                unit_value_usd=180.0,
                lots=(
                    HoldingTaxLotConfig(
                        lot_id="goog_lot", holding_period_months_at_start=35, quantity=5.0, cost_basis_usd=500.0
                    ),
                ),
            ),
        ),
    )

    assert portfolio.total_holdings_value_usd == 5_900.0


def test_holding_positions_must_reference_known_accounts() -> None:
    with pytest.raises(ValidationError, match="unknown account_id"):
        PortfolioConfig(
            accounts=(),
            holdings=(
                SecurityHoldingConfig(
                    position_id="voo_position",
                    account_id="missing",
                    symbol=SecuritySymbol("VOO"),
                    security_kind="etf",
                    unit_value_usd=500.0,
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id="voo_lot", holding_period_months_at_start=28, quantity=10.0, cost_basis_usd=4_000.0
                        ),
                    ),
                ),
            ),
        )


def test_holding_lot_ids_must_be_unique() -> None:
    account = PortfolioAccountConfig(account_id="taxable_brokerage", owner_agent_id="agent_a")
    with pytest.raises(ValidationError, match="unique lot_id"):
        PortfolioConfig(
            accounts=(account,),
            holdings=(
                SecurityHoldingConfig(
                    position_id="voo_position",
                    account_id=account.account_id,
                    symbol=SecuritySymbol("VOO"),
                    security_kind="etf",
                    unit_value_usd=500.0,
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id="duplicate_lot",
                            holding_period_months_at_start=28,
                            quantity=10.0,
                            cost_basis_usd=4_000.0,
                        ),
                    ),
                ),
                SecurityHoldingConfig(
                    position_id="goog_position",
                    account_id=account.account_id,
                    symbol=SecuritySymbol("GOOG"),
                    security_kind="stock",
                    unit_value_usd=180.0,
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id="duplicate_lot",
                            holding_period_months_at_start=35,
                            quantity=5.0,
                            cost_basis_usd=500.0,
                        ),
                    ),
                ),
            ),
        )


def test_holding_positions_sharing_series_must_share_unit_value() -> None:
    account = PortfolioAccountConfig(account_id="taxable_brokerage", owner_agent_id="agent_a")
    with pytest.raises(ValidationError, match="must share unit_value_usd"):
        PortfolioConfig(
            accounts=(account,),
            holdings=(
                SecurityHoldingConfig(
                    position_id="sp500_a",
                    account_id=account.account_id,
                    symbol=SecuritySymbol("VOO"),
                    security_kind="other",
                    unit_value_usd=500.0,
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id="sp500_a_lot",
                            holding_period_months_at_start=28,
                            quantity=10.0,
                            cost_basis_usd=4_000.0,
                        ),
                    ),
                ),
                SecurityHoldingConfig(
                    position_id="sp500_b",
                    account_id=account.account_id,
                    symbol=SecuritySymbol("VOO"),
                    security_kind="other",
                    unit_value_usd=600.0,
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id="sp500_b_lot", holding_period_months_at_start=35, quantity=5.0, cost_basis_usd=500.0
                        ),
                    ),
                ),
            ),
        )


def test_negative_holding_period_is_rejected() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        HoldingTaxLotConfig(
            lot_id="future_lot", holding_period_months_at_start=-1, quantity=10.0, cost_basis_usd=4_000.0
        )


# -- Bonds ---------------------------------------------------------------------------------


def _bond_portfolio(**overrides: object) -> PortfolioConfig:
    bond = {
        "bond_id": "tips_rung",
        "account_id": "brokerage",
        "issuer_jurisdiction_id": "federal_us",
        "face_value_usd": 100_000.0,
        "purchase_price_usd": 100_000.0,
        "annual_coupon_rate": 0.02,
        "inflation_indexed": True,
        "holding_period_months_at_start": 24,
        "months_to_maturity_at_start": 96,
    } | overrides
    return PortfolioConfig(
        accounts=(PortfolioAccountConfig(account_id="brokerage", owner_agent_id="alice"),),
        bonds=(BondHoldingConfig.model_validate(bond),),
    )


def test_a_bond_converts_both_months_relative_to_month_zero() -> None:
    """The whole point of the config idiom: a deployment writes "held 24 months, matures in 96"
    and never a calendar date, so the two conversions are where a sign error would hide. A bond
    held 24 months is `purchase_month_index=-24`, in the PAST."""

    [bond] = _bond_portfolio().to_initial_bonds(coupon_account_id="checking")

    assert bond.purchase_month_index == -24
    assert bond.maturity_month_index == 96


def test_a_bonds_owner_comes_through_its_custody_account() -> None:
    """Like a lot: the account is the owner-bearing object, and the bond names no agent."""

    [bond] = _bond_portfolio().to_initial_bonds(coupon_account_id="checking")

    assert bond.agent_id == "alice"


def test_coupons_land_in_the_named_cash_account_not_the_custody_account() -> None:
    """The two are different things that are the same string only by coincidence. A portfolio
    account is custody (`brokerage`) and carries no cash row, so a coupon paid into one would
    have nowhere to go — the caller names the destination because it knows its cash topology."""

    [bond] = _bond_portfolio().to_initial_bonds(coupon_account_id="checking")

    assert bond.account_id == "checking"


def test_a_bond_on_an_unknown_account_is_rejected() -> None:
    with pytest.raises(ValidationError, match="bonds reference unknown account_id"):
        PortfolioConfig(
            accounts=(PortfolioAccountConfig(account_id="brokerage", owner_agent_id="alice"),),
            bonds=(
                BondHoldingConfig(
                    bond_id="orphan",
                    account_id="nowhere",
                    face_value_usd=1_000.0,
                    purchase_price_usd=1_000.0,
                    annual_coupon_rate=0.01,
                    months_to_maturity_at_start=12,
                ),
            ),
        )


def test_duplicate_bond_ids_are_rejected() -> None:
    """Two rungs sharing an id are two different instruments the ledger cannot tell apart."""

    bond = BondHoldingConfig(
        bond_id="rung",
        account_id="brokerage",
        face_value_usd=1_000.0,
        purchase_price_usd=1_000.0,
        annual_coupon_rate=0.01,
        months_to_maturity_at_start=12,
    )
    with pytest.raises(ValidationError, match="unique bond_id"):
        PortfolioConfig(
            accounts=(PortfolioAccountConfig(account_id="brokerage", owner_agent_id="alice"),), bonds=(bond, bond)
        )


def test_a_non_par_purchase_survives_config_to_be_rejected_by_the_sim() -> None:
    """The reason `purchase_price_usd` is carried at all despite having one legal value today.

    Config is where somebody writes what they actually paid. Dropping the field would silently
    promote a bond bought at 98.5 to par — the exact failure the sim's validator exists to make
    loud — so the config accepts it and the conversion is what raises.
    """

    portfolio = _bond_portfolio(purchase_price_usd=98_500.0)

    with pytest.raises(ValidationError, match="bought away from par"):
        portfolio.to_initial_bonds(coupon_account_id="checking")


def test_bond_face_is_kept_out_of_the_holdings_value_total() -> None:
    """A held-to-maturity bond is never marked, so one total conflating face with marked value
    would assert a price the model does not produce."""

    portfolio = _bond_portfolio()

    assert portfolio.total_holdings_value_usd == 0.0
    assert portfolio.total_bond_face_value_usd == 100_000.0


if __name__ == "__main__":
    pytest_bazel.main()
