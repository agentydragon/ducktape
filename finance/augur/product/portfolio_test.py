from __future__ import annotations

import pytest_bazel

from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import (
    HoldingTaxLotConfig,
    PortfolioAccountConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.model.series import SecuritySymbol
from finance.augur.product.portfolio import product_portfolio_response


def test_product_portfolio_response_includes_holding_positions_and_lots() -> None:
    response = product_portfolio_response(
        snapshot=FinanceSnapshot(as_of_date="2026-05-14", cash=50_000),
        portfolio=PortfolioConfig(
            accounts=(
                PortfolioAccountConfig(account_id="taxable", owner_agent_id="agent_a", label="Taxable Brokerage"),
            ),
            holdings=(
                SecurityHoldingConfig(
                    position_id="sp500_proxy",
                    account_id="taxable",
                    label="SP500 Proxy",
                    symbol=SecuritySymbol("VOO"),
                    security_kind="etf",
                    unit_value=500,
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id="sp500_2020_01", holding_period_months_at_start=76, quantity=150.0, cost_basis=60_000
                        ),
                        HoldingTaxLotConfig(
                            lot_id="sp500_2024_06",
                            holding_period_months_at_start=23,
                            quantity=150.0,
                            cost_basis="49_999.50",
                        ),
                    ),
                ),
            ),
        ),
    )

    assert response.as_of_date == "2026-05-14"
    assert response.currency_code == "USD"
    assert response.currency_quantum == "0.01"
    assert response.cash_quanta == "5000000"
    assert response.total_holdings_value_quanta == "15000000"
    assert response.total_holdings_cost_basis_quanta == "10999950"
    [position] = response.holdings
    assert position.account_label == "Taxable Brokerage"
    assert position.label == "SP500 Proxy"
    assert position.symbol == "VOO"
    assert position.quantity == 300.0
    assert position.current_value_quanta == "15000000"
    assert [lot.lot_id for lot in position.lots] == ["sp500_2020_01", "sp500_2024_06"]
    assert [lot.cost_basis_quanta for lot in position.lots] == ["6000000", "4999950"]


if __name__ == "__main__":
    pytest_bazel.main()
