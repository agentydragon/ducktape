"""Imported total basis survives display, partial sales and full liquidation exactly."""

from decimal import Decimal

import numpy as np
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
from finance.augur.sim.actions import DecisionActions, LotSale, Sell
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import PreparedAccount, PreparedHoldingPool, PreparedLot
from finance.augur.sim.results import Finished
from finance.augur.sim.scenario import ORDINARY_INCOME, InitialLot
from finance.augur.sim.session import ActionSession
from finance.augur.sim.world import World

ASSET = SecurityKey(symbol=SecuritySymbol("test-security"))
QUANTUM = Decimal("0.01")
OWNER = "test-owner"


@pytest.fixture
def portfolio() -> PortfolioConfig:
    return PortfolioConfig(
        accounts=(PortfolioAccountConfig(account_id="checking", owner_agent_id=OWNER),),
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


def _prepared(lot: InitialLot) -> PreparedLot:
    """The imported lot as the books hold it: quantity in unit quanta, basis in exact currency quanta."""
    asset = lot.asset
    if not isinstance(asset, SecurityKey):
        raise TypeError(f"an imported portfolio holds public securities; got {asset!r}")
    scale = quantity_scale_for_asset(asset)
    return PreparedLot(
        lot_id=lot.lot_id,
        agent_id=lot.agent_id,
        account_id=lot.account_id,
        asset_id=str(asset.symbol),
        purchase_month=int(lot.purchase_month_index),
        quantity_scale=scale,
        units=int(quantity_to_quanta(lot.quantity, scale=scale)),
        basis=int(currency_amount_to_quanta(lot.cost_basis, quantum=QUANTUM)),
    )


def _compose(lots: list[PreparedLot], *, horizon_months: int) -> World:
    """The owner's cashless `checking` account over a flat $1 mark, holding the imported lots."""
    paths = ExternalSeriesContext.from_level_blocks(
        [(ASSET, np.full((1, horizon_months + 1), 1.0))], rollout_count=1, horizon_months=horizon_months
    )
    world = World(
        MarketPath(
            compile_series(paths, rollout_count=1, horizon_months=horizon_months, currency_quantum=QUANTUM),
            0,
            rollout_count=1,
        ),
        horizon_months=horizon_months,
        income_sources=(ORDINARY_INCOME,),
    )
    world.declare_account(PreparedAccount(account=AccountRef(agent_id=OWNER, account_id="checking"), opening_balance=0))
    world.declare_pool(
        PreparedHoldingPool(
            agent_id=OWNER,
            account_id="checking",
            asset_id=str(ASSET.symbol),
            quantity_scale=quantity_scale_for_asset(ASSET),
        )
    )
    for lot in lots:
        world.hold(lot)
    return world


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
    with pytest.raises(ValueError, match=r"1\.001 is not an integer multiple of currency quantum 0\.01"):
        _prepared(lot.model_copy(update={"cost_basis": Decimal("1.001")}))


@pytest.mark.parametrize(("sales", "expected_basis"), [([1, 2], [33, 67]), ([3], [100])])
def test_imported_basis_is_exact_through_sales(
    portfolio: PortfolioConfig, sales: list[int], expected_basis: list[int]
) -> None:
    horizon = len(sales)
    session = ActionSession(
        {0: _compose([_prepared(lot) for lot in portfolio.to_initial_lots()], horizon_months=horizon)}, OWNER
    )
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
                                proceeds_account_id="checking",
                                asset_id="test-security",
                                lots=(
                                    LotSale(
                                        account_id="checking",
                                        lot_id="test-lot",
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
