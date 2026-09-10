"""Imported total basis survives display, partial sales and full liquidation exactly."""

from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import (
    HoldingTaxLotConfig,
    PortfolioAccountConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.product.portfolio import product_portfolio_response
from finance.augur.rust.simulator import Action, ActionSession, DecisionActions
from finance.augur.sim.results import Finished
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.testing.fixtures import checking

ASSET = SecurityKey(symbol=SecuritySymbol("test-security"))


@pytest.fixture
def portfolio() -> PortfolioConfig:
    return PortfolioConfig(
        accounts=(PortfolioAccountConfig(account_id="checking", owner_agent_id="test-owner"),),
        holdings=(
            SecurityHoldingConfig(
                position_id="test-position",
                account_id="checking",
                symbol=ASSET.symbol,
                security_kind="stock",
                unit_value=Decimal(1),
                lots=(
                    HoldingTaxLotConfig(
                        lot_id="test-lot", holding_period_months_at_start=24, quantity=3, cost_basis=Decimal(1)
                    ),
                ),
            ),
        ),
    )


def test_product_display_keeps_one_dollar_total_over_three_units(portfolio: PortfolioConfig) -> None:
    response = product_portfolio_response(
        snapshot=FinanceSnapshot(as_of_date="2026-01-01", cash=0), portfolio=portfolio
    )
    [position] = response.holdings
    [lot] = position.lots
    assert lot.quantity == 3
    assert lot.cost_basis_quanta == "100"
    assert position.total_cost_basis_quanta == "100"


def test_total_basis_still_requires_exact_currency_quanta(portfolio: PortfolioConfig) -> None:
    [lot] = portfolio.to_initial_lots()
    case = Case(
        scenario=scenario(
            checking(("test-owner", Decimal(0))),
            initial_lots=[lot.model_copy(update={"cost_basis": Decimal("1.001")})],
            tax_profiles=[],
            horizon_months=1,
        ),
        rollout_count=1,
        series={ASSET: levels([[Decimal(1), Decimal(1)]])},
    )
    with pytest.raises(ValueError, match=r"1\.001 is not an integer multiple of currency quantum 0\.01"):
        _ = case.compiled_run


@pytest.mark.parametrize(("sales", "expected_basis"), [([1, 2], [33, 67]), ([3], [100])])
def test_imported_basis_is_exact_through_sales(
    portfolio: PortfolioConfig, sales: list[int], expected_basis: list[int]
) -> None:
    horizon = len(sales)
    case = Case(
        scenario=scenario(
            checking(("test-owner", Decimal(0))),
            initial_lots=list(portfolio.to_initial_lots()),
            tax_profiles=[],
            horizon_months=horizon,
        ),
        rollout_count=1,
        series={ASSET: levels([[Decimal(1)] * (horizon + 1)])},
    )
    session = ActionSession(case.compiled_run, "test-owner", [0])
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            responses = []
            for decision in batch:
                observation = decision.observation
                [lot] = observation.public_positions
                responses.append(
                    DecisionActions(
                        decision.rollout_id,
                        observation.month,
                        [
                            Action.sell(
                                cause_id=f"test-sale-{observation.month}",
                                agent_id="test-owner",
                                proceeds_account_id="checking",
                                asset_id="test-security",
                                lots=[("checking", "test-lot", sales[observation.month] * lot.quantity_scale)],
                            )
                        ],
                    )
                )
            batch = session.advance(responses)
        [rollout] = batch.rollouts
    finally:
        session.close()
    assert rollout.stop is None
    assert rollout.trace is not None
    books = rollout.trace.books
    assert books[0].lots[0].basis_remaining == 100
    dispositions = rollout.trace.events.lot_dispositions
    assert dispositions.get_column("cost_basis_consumed_quanta").to_list() == expected_basis
    assert dispositions.get_column("proceeds_quanta").sum() == 300
    assert dispositions.get_column("realized_gain_quanta").sum() == 200
    if len(sales) == 2:
        assert books[1].lots[0].basis_remaining == 67
    assert (books[-1].lots[0].units_remaining, books[-1].lots[0].basis_remaining) == (0, 0)
    assert rollout.summary.cash[0].values[-1] == 300


if __name__ == "__main__":
    pytest_bazel.main()
