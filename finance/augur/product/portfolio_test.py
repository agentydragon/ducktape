from __future__ import annotations

from decimal import Decimal

import pytest_bazel

from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import (
    HoldingKind,
    HoldingTaxLotConfig,
    LabeledTlhPortfolio,
    PortfolioAccountConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.product.portfolio import ProductTlhCohort, product_portfolio_response
from finance.augur.sim.ids import AccountId, AgentId, LotId, PortfolioId
from finance.augur.sim.scenario import TlhCohort, TlhPortfolioSpec
from finance.augur.sim.tlh import TlhAssumptions


def test_product_portfolio_response_includes_holding_positions_and_lots() -> None:
    response = product_portfolio_response(
        snapshot=FinanceSnapshot(as_of_date="2026-05-14", cash=Decimal(50_000)),
        portfolio=PortfolioConfig(
            accounts=(
                PortfolioAccountConfig(
                    account_id=AccountId("taxable"), owner_agent_id=AgentId("agent_a"), label="Taxable Brokerage"
                ),
            ),
            holdings=(
                SecurityHoldingConfig(
                    position_id="sp500_proxy",
                    account_id=AccountId("taxable"),
                    label="SP500 Proxy",
                    symbol=SecuritySymbol("VOO"),
                    security_kind=HoldingKind.ETF,
                    unit_value=Decimal(500),
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id=LotId("sp500_2020_01"),
                            holding_period_months_at_start=76,
                            quantity=150.0,
                            cost_basis=Decimal(60_000),
                        ),
                        HoldingTaxLotConfig(
                            lot_id=LotId("sp500_2024_06"),
                            holding_period_months_at_start=23,
                            quantity=150.0,
                            cost_basis=Decimal("49999.50"),
                        ),
                    ),
                ),
            ),
        ),
        tlh_portfolios=(),
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


def test_product_portfolio_response_carries_tlh_portfolios_as_money_apart_from_holdings() -> None:
    response = product_portfolio_response(
        snapshot=FinanceSnapshot(as_of_date="2026-05-14", cash=Decimal(1_000)),
        portfolio=PortfolioConfig(
            accounts=(
                PortfolioAccountConfig(
                    account_id=AccountId("test-managed-account"),
                    owner_agent_id=AgentId("test-owner"),
                    label="Test Managed",
                ),
            )
        ),
        tlh_portfolios=(
            LabeledTlhPortfolio(
                spec=TlhPortfolioSpec(
                    portfolio_id=PortfolioId("test-managed"),
                    owner_agent_id=AgentId("test-owner"),
                    account_id=AccountId("test-managed-account"),
                    asset=SecurityKey(symbol=SecuritySymbol("test-index")),
                    initial_cohorts=[
                        TlhCohort(value=Decimal("250.01"), cost_basis=Decimal(300), purchase_month_index=-4),
                        TlhCohort(value=Decimal(750), cost_basis=Decimal("299.99"), purchase_month_index=0),
                    ],
                    assumptions=TlhAssumptions(
                        peak_annual_yield=0,
                        floor_annual_yield=0,
                        maturity_decay_exponent=1,
                        drawdown_sensitivity=0,
                        short_term_fraction=1,
                    ),
                ),
                label="Test direct indexing",
            ),
        ),
    )

    # The managed sleeve is not an ordinary holding, so it is not in the holdings total.
    assert (response.holdings, response.total_holdings_value_quanta) == ((), "0")
    [managed] = response.tlh_portfolios
    assert (managed.portfolio_id, managed.owner_agent_id, managed.account_id, managed.account_label, managed.label) == (
        "test-managed",
        "test-owner",
        "test-managed-account",
        "Test Managed",
        "Test direct indexing",
    )
    assert managed.asset == SecurityKey(symbol=SecuritySymbol("test-index"))
    assert (managed.current_value_quanta, managed.total_cost_basis_quanta) == ("100001", "59999")
    assert managed.cohorts == (
        ProductTlhCohort(holding_period_months_at_start=4, value_quanta="25001", cost_basis_quanta="30000"),
        ProductTlhCohort(holding_period_months_at_start=0, value_quanta="75000", cost_basis_quanta="29999"),
    )


if __name__ == "__main__":
    pytest_bazel.main()
