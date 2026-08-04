from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.api.portfolio import (
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


if __name__ == "__main__":
    pytest_bazel.main()
