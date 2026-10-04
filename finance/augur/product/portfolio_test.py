from __future__ import annotations

from decimal import Decimal

import pytest_bazel

from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import (
    LabeledTlhPortfolio,
    PortfolioAccountConfig,
    PortfolioConfig,
    TlhCohort,
    TlhPortfolioSpec,
)
from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.product.portfolio import ProductTlhCohort, product_portfolio_response
from finance.augur.sim.ids import AccountId, AgentId, PortfolioId
from finance.augur.sim.money import USD
from finance.augur.sim.tlh import TlhAssumptions


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
        currency=USD,
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
