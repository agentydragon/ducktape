from __future__ import annotations

import pytest_bazel

from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import (
    HoldingTaxLotConfig,
    PortfolioAccountConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.model.series import SP500_SYMBOL
from finance.augur.product.portfolio import product_portfolio_response


def test_product_portfolio_response_includes_holding_positions_and_lots() -> None:
    response = product_portfolio_response(
        snapshot=FinanceSnapshot(as_of_date="2026-05-14", cash_usd=50_000.0),
        portfolio=PortfolioConfig(
            accounts=(
                PortfolioAccountConfig(account_id="taxable", owner_agent_id="agent_a", label="Taxable Brokerage"),
            ),
            holdings=(
                SecurityHoldingConfig(
                    position_id="sp500_proxy",
                    account_id="taxable",
                    label="SP500 Proxy",
                    symbol=SP500_SYMBOL,
                    security_kind="etf",
                    unit_value_usd=500.0,
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id="sp500_2020_01",
                            holding_period_months_at_start=76,
                            quantity=150.0,
                            cost_basis_usd=60_000.0,
                        ),
                        HoldingTaxLotConfig(
                            lot_id="sp500_2024_06",
                            holding_period_months_at_start=23,
                            quantity=150.0,
                            cost_basis_usd=50_000.0,
                        ),
                    ),
                ),
            ),
        ),
    )

    assert response.as_of_date == "2026-05-14"
    assert response.cash_usd == 50_000.0
    assert response.total_holdings_value_usd == 150_000.0
    assert response.total_holdings_cost_basis_usd == 110_000.0
    [position] = response.holdings
    assert position.account_label == "Taxable Brokerage"
    assert position.label == "SP500 Proxy"
    assert position.symbol == "VOO"
    assert position.quantity == 300.0
    assert position.current_value_usd == 150_000.0
    assert [lot.lot_id for lot in position.lots] == ["sp500_2020_01", "sp500_2024_06"]
    assert [lot.cost_basis_per_unit_usd for lot in position.lots] == [400.0, 333.3333333333333]


if __name__ == "__main__":
    pytest_bazel.main()
