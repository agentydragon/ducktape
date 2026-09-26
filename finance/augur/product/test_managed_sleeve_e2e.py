"""A managed-portfolio sleeve, end to end through `ProductService`.

The fixture deployment holds $750k of VOO and $75k of BTC. Each test adds one TLH portfolio
in its own custody account and funds a $1k month from $250k of cash against a nominal
$260k/$280k band, so the band raises what refills the account to the ceiling. The portfolio has
no unit price: it is named by its id, sized in money, and never merged with lots of its index.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.api.config import Config
from finance.augur.api.wire import CatalogResponse
from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.product.scenarios import resolve_primary_agent_id, sim_locations_from_config
from finance.augur.product.service import ProductService
from finance.augur.product.wire import (
    FundingPolicy,
    HoldingSaleEvent,
    ManagedSleeveWeight,
    RolloutRequest,
    RolloutResponse,
    ScenarioKey,
    SecuritySleeveWeight,
    SleeveWeight,
    TlhFinancialEffectEvent,
)
from finance.augur.sim.events import TlhOperation
from finance.augur.sim.scenario import TlhCohort, TlhPortfolioSpec
from finance.augur.sim.tlh import TlhAssumptions

_PORTFOLIO_ID = "test-managed"
_ACCOUNT_ID = "test_managed_brokerage"


def _product(augur_config: Config, catalog: CatalogResponse, *, index: str) -> ProductService:
    """The fixture portfolio plus a $100k TLH portfolio pegged to `index`, harvesting nothing."""
    owner = resolve_primary_agent_id(augur_config)
    return ProductService(
        portfolio=augur_config.portfolio_sources.fixed.portfolio,
        initial_cash=250_000,
        primary_agent_id=owner,
        tlh_portfolios=(
            TlhPortfolioSpec(
                portfolio_id=_PORTFOLIO_ID,
                owner_agent_id=owner,
                account_id=_ACCOUNT_ID,
                asset=SecurityKey(symbol=SecuritySymbol(index)),
                initial_cohorts=[
                    TlhCohort(value=Decimal(100_000), cost_basis=Decimal(100_000), purchase_month_index=-24)
                ],
                assumptions=TlhAssumptions(
                    peak_annual_yield=0,
                    floor_annual_yield=0,
                    maturity_decay_exponent=1,
                    drawdown_sensitivity=0,
                    short_term_fraction=1,
                ),
            ),
        ),
        known_location_ids=catalog.location_ids,
        locations=sim_locations_from_config(augur_config.locations),
        properties_by_id=catalog.properties_by_id,
        models={"current_model": augur_config.models[augur_config.default_model_id].realize_model()},
        max_rollout_samples=augur_config.max_rollout_samples,
        max_horizon_months=augur_config.max_horizon_months,
        result_cache_entries=0,
    )


def _rollout(product: ProductService, *sleeves: SleeveWeight) -> RolloutResponse:
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=1,
        monthly_spend=1_000,
        spend_index="none",
        funding_policy=FundingPolicy(
            cash_floor=260_000, cash_ceiling=280_000, cash_band_index_to_inflation=False, sleeve_weights=sleeves
        ),
    )
    return product.rollout(RolloutRequest(scenario=scenario, seed=7))


def _usd_quanta(value: int) -> str:
    return str(value * 100)


def _redemptions(detail: RolloutResponse) -> list[TlhFinancialEffectEvent]:
    return [
        event
        for event in detail.rollout.events
        if isinstance(event, TlhFinancialEffectEvent) and event.operation == TlhOperation.REDEMPTION
    ]


def _sales(detail: RolloutResponse) -> list[HoldingSaleEvent]:
    return [event for event in detail.rollout.events if isinstance(event, HoldingSaleEvent)]


def test_a_managed_sleeve_funds_the_band_in_money_from_its_portfolio(
    augur_config: Config, catalog: CatalogResponse
) -> None:
    """The TLH portfolio is the only sleeve, and nothing quotes its index as a holding.

    The refill to the ceiling is one exact money withdrawal from the portfolio, and no lot is sold:
    $29,125, the $31k gap between the ceiling and the month's $1k spend less the fixture's $1,875
    of income already in cash when the household decides — what the ordinary-sleeve refill in
    <service_test.py> sells in VOO.
    """
    detail = _rollout(
        _product(augur_config, catalog, index="SPY"), ManagedSleeveWeight(portfolio_id=_PORTFOLIO_ID, weight=1)
    )

    assert detail.rollout.failed is False
    assert detail.rollout.monthly_metrics["cash_quanta"] == [_usd_quanta(250_000), _usd_quanta(280_000)]
    assert _sales(detail) == []
    withdrawal = one(_redemptions(detail))
    assert (withdrawal.portfolio_id, withdrawal.account_id, withdrawal.cash_account_id) == (
        _PORTFOLIO_ID,
        _ACCOUNT_ID,
        "checking",
    )
    assert withdrawal.cause_id.startswith("product_funding_sale")
    assert withdrawal.amount_quanta == _usd_quanta(29_125)


@pytest.mark.parametrize(
    ("sleeves", "sells_lots", "redeems"),
    [
        ((SecuritySleeveWeight(symbol="VOO", weight=1),), True, False),
        (
            (SecuritySleeveWeight(symbol="VOO", weight=0), ManagedSleeveWeight(portfolio_id=_PORTFOLIO_ID, weight=1)),
            False,
            True,
        ),
    ],
    ids=["security_sleeve_sells_only_lots", "managed_sleeve_redeems_only_the_portfolio"],
)
def test_lots_of_an_index_and_a_portfolio_pegged_to_it_stay_separate_sleeves(
    augur_config: Config, catalog: CatalogResponse, sleeves: tuple[SleeveWeight, ...], sells_lots: bool, redeems: bool
) -> None:
    """The portfolio tracks VOO and the household holds VOO lots, yet each sleeve draws on only its own.

    A VOO sleeve sells the lots and leaves the portfolio alone; excluding the VOO lots and naming the
    portfolio redeems the portfolio and sells no lot.
    """
    detail = _rollout(_product(augur_config, catalog, index="VOO"), *sleeves)

    assert detail.rollout.failed is False
    assert detail.rollout.ending_metrics.cash_quanta == _usd_quanta(280_000)
    assert [sale.asset for sale in _sales(detail)] == (
        [SecurityKey(symbol=SecuritySymbol("VOO"))] if sells_lots else []
    )
    assert [event.portfolio_id for event in _redemptions(detail)] == ([_PORTFOLIO_ID] if redeems else [])


def test_a_managed_sleeve_naming_an_unknown_portfolio_is_refused(
    augur_config: Config, catalog: CatalogResponse
) -> None:
    with pytest.raises(ValueError, match="unknown TLH portfolio 'test-absent'"):
        _rollout(
            _product(augur_config, catalog, index="SPY"), ManagedSleeveWeight(portfolio_id="test-absent", weight=1)
        )


if __name__ == "__main__":
    pytest_bazel.main()
