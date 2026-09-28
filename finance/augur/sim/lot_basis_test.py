"""Imported total basis survives display, partial sales and full liquidation exactly."""

from collections.abc import Sequence
from decimal import Decimal

import numpy as np
import pytest
import pytest_bazel

from finance.augur.api.finance import FinanceSnapshot
from finance.augur.api.portfolio import (
    HoldingKind,
    HoldingTaxLotConfig,
    PortfolioAccountConfig,
    PortfolioConfig,
    SecurityHoldingConfig,
)
from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.product.portfolio import product_portfolio_response
from finance.augur.sim.actions import DecisionActions, LotSale, Sell
from finance.augur.sim.books import AccountRef
from finance.augur.sim.fixed_point import quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.ids import AccountId, AgentId, AssetId, LotId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import USD
from finance.augur.sim.results import Finished
from finance.augur.sim.session import ActionSession
from finance.augur.sim.testing.series import level_series
from finance.augur.sim.world import World

CHECKING = AccountId("checking")

ASSET = SecurityKey(symbol=SecuritySymbol("test-security"))
OWNER = AgentId("test-owner")


@pytest.fixture
def portfolio() -> PortfolioConfig:
    return PortfolioConfig(
        accounts=(PortfolioAccountConfig(account_id=CHECKING, owner_agent_id=OWNER),),
        holdings=(
            SecurityHoldingConfig(
                position_id="test-position",
                account_id=CHECKING,
                symbol=ASSET.symbol,
                security_kind=HoldingKind.STOCK,
                unit_value=Decimal(1),
                lots=(
                    HoldingTaxLotConfig(
                        lot_id=LotId("test-lot"), holding_period_months_at_start=24, quantity=3, cost_basis=Decimal(1)
                    ),
                ),
            ),
        ),
    )


def _compose(lots: Sequence[HoldingTaxLotConfig], *, horizon_months: int) -> World:
    """The owner's cashless `checking` account over a flat $1 mark, holding the imported lots as the books hold
    them: quantity in unit quanta, basis in exact currency quanta."""
    world = World(
        MarketPath(
            level_series(
                {ASSET: np.full((1, horizon_months + 1), 1.0)}, rollout_count=1, horizon_months=horizon_months
            ),
            0,
            rollout_count=1,
        ),
        horizon_months=horizon_months,
        income_sources=(ORDINARY_INCOME,),
    )
    world.declare_account(account=AccountRef(agent_id=OWNER, account_id=CHECKING), opening_balance=0)
    scale = quantity_scale_for_asset(ASSET)
    world.declare_pool(agent_id=OWNER, account_id=CHECKING, asset_id=AssetId(ASSET.symbol), quantity_scale=scale)
    for lot in lots:
        world.hold_lot(
            lot_id=lot.lot_id,
            agent_id=OWNER,
            account_id=CHECKING,
            asset_id=AssetId(ASSET.symbol),
            purchase_month=-lot.holding_period_months_at_start,
            quantity_scale=scale,
            units=quantity_to_quanta(Decimal(lot.quantity), scale=scale),
            basis=USD.quanta(lot.cost_basis),
        )
    return world


def test_product_display_keeps_one_dollar_total_over_three_units(portfolio: PortfolioConfig) -> None:
    response = product_portfolio_response(
        snapshot=FinanceSnapshot(as_of_date="2026-01-01", cash=Decimal(0)),
        portfolio=portfolio,
        tlh_portfolios=(),
        currency=USD,
    )
    [position] = response.holdings
    [lot] = position.lots
    assert lot.quantity == 3
    assert lot.cost_basis_quanta == "100"
    assert position.total_cost_basis_quanta == "100"


def test_total_basis_still_requires_exact_currency_quanta(portfolio: PortfolioConfig) -> None:
    [lot] = portfolio.holdings[0].lots
    with pytest.raises(ValueError, match=r"1\.001 is not an integer multiple of currency quantum 0\.01"):
        _compose([lot.model_copy(update={"cost_basis": Decimal("1.001")})], horizon_months=1)


@pytest.mark.parametrize(("sales", "expected_basis"), [([1, 2], [33, 67]), ([3], [100])])
def test_imported_basis_is_exact_through_sales(
    portfolio: PortfolioConfig, sales: list[int], expected_basis: list[int]
) -> None:
    horizon = len(sales)
    session = ActionSession({0: _compose(portfolio.holdings[0].lots, horizon_months=horizon)}, OWNER)
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
                            Sell(
                                cause_id=f"test-sale-{observation.month}",
                                agent_id=OWNER,
                                proceeds_account_id=CHECKING,
                                asset_id=AssetId("test-security"),
                                lots=(
                                    LotSale(
                                        account_id=CHECKING,
                                        lot_id=LotId("test-lot"),
                                        units=sales[observation.month] * lot.quantity_scale,
                                    ),
                                ),
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
