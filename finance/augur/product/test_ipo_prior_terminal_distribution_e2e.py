"""Prediction-market odds, end to end into a terminal distribution.

`derive_public_market_anchors` turns a catalog's `ipo_by_date` markets into the
`public_market_cdf_anchors` vector the PE realization model accepts, and
`calibration/test_ipo_prior.py` proves that derivation. What nothing covered is the SEAM:
that the anchors reach a running model and change what comes out of `ProductService`.

That seam is three hops — catalog to anchors to issuer config to sampled bundle — and every
one of them is a place the odds could be silently dropped. A CDF that never reaches the
sampler produces a perfectly plausible distribution, just not one informed by the market, so
this cannot be caught by looking at the output alone.
"""

from __future__ import annotations

from datetime import date

import pytest_bazel

from finance.augur.api.config import Config
from finance.augur.calibration.catalog import ExactMarket, IpoByDateMapping, ManifoldRef, MarketCatalog
from finance.augur.calibration.ipo_prior import derive_public_market_anchors
from finance.augur.calibration.testing import mock_price_clients
from finance.augur.model.private_equity_risk import PrivateEquityRiskProviderConfig
from finance.augur.model.provider_config import CompositeProviderConfig
from finance.augur.model.series import IssuerId
from finance.augur.product.conftest import MakeProductService
from finance.augur.product.service import ProductService
from finance.augur.product.wire import FundingPolicy, ScenarioKey, SleeveWeight, TerminalDistributionRequest
from finance.evidence.markets import Platform

# One market, deadline six months after the catalog's model anchor. Its YES price is the whole
# input this test varies: everything else is held identical between arms.
_MARKET_ID = "ipo_by_2026_11"
_ISSUER = IssuerId("private_holding_a")

# Enough spend to make the portfolio work without ruining it — every rollout survives in both
# arms, so a difference in the distribution is the IPO prior and not a difference in who died.
_SCENARIO = ScenarioKey(
    model_id="current_model",
    horizon_months=24,
    monthly_spend_usd=5_000.0,
    spend_index="none",
    funding_policy=FundingPolicy(
        cash_floor_usd=10_000.0,
        cash_ceiling_usd=50_000.0,
        cash_band_index_to_inflation=False,
        sleeve_weights=(
            SleeveWeight(symbol="VOO", weight=1),
            SleeveWeight(symbol="btc", weight=1),
            SleeveWeight(symbol="eth", weight=1),
        ),
    ),
)


def _catalog() -> MarketCatalog:
    return MarketCatalog(
        metadata={"as_of": "2026-05-29", "augur_model_as_of": "2026-05-27"},
        markets=[
            ExactMarket(
                platform_ref=ManifoldRef(manifold_id=_MARKET_ID),
                mapping=IpoByDateMapping(issuer=_ISSUER, by_date=date.fromisoformat("2026-11-01")),
            )
        ],
    )


async def _product_for_ipo_probability(
    probability: float, *, augur_config: Config, make_product_service: MakeProductService
) -> ProductService:
    """A service whose PE issuer carries the going-public CDF implied by one market price.

    The lockup and the public-market pricing noise are zeroed so the only thing separating two
    arms is WHETHER the issuer goes public — not when it becomes sellable, and not what noise
    the public mark picks up. That isolation is what makes the comparison below mean something.
    """

    anchors = await derive_public_market_anchors(
        _catalog(), price_client=mock_price_clients({Platform.MANIFOLD: {_MARKET_ID: probability}})[Platform.MANIFOLD]
    )
    preset = augur_config.models["openai_pe"]
    assert isinstance(preset, CompositeProviderConfig)
    private_equity = preset.private_equity
    assert isinstance(private_equity, PrivateEquityRiskProviderConfig)
    issuers = private_equity.issuers
    issuer = issuers[_ISSUER].model_copy(
        update={
            "public_market_cdf_anchors": anchors,
            "public_market_lockup_months": 0,
            "public_market_price_log_discount_mu": 0.0,
            "public_market_price_log_discount_sigma": 0.0,
        }
    )
    model = preset.model_copy(
        update={"private_equity": PrivateEquityRiskProviderConfig(issuers={**issuers, _ISSUER: issuer})}
    ).realize_model()
    return make_product_service(model)


def _private_equity_spread(product: ProductService) -> tuple[float, int]:
    """Terminal PE value's p10-to-p90 spread, plus the failure count."""

    response = product.terminal_distribution(
        TerminalDistributionRequest(
            scenario=_SCENARIO,
            first_seed=0,
            rollout_count=64,
            metric="private_equity_value_usd",
            percentiles=(10.0, 90.0),
        )
    )
    low, high = (float(value) for value in response.terminal_metric_percentiles["value"])  # type: ignore[arg-type]
    return high - low, response.failed_count


async def test_market_implied_ipo_odds_reach_the_terminal_distribution(
    augur_config: Config, make_product_service: MakeProductService
) -> None:
    """The seam, asserted as a DIFFERENCE the market price causes.

    A near-certain IPO collapses the terminal spread of the private holding: a rollout that has
    gone public prices at the latent value, and this configuration zeroes the public-market
    discount noise, so what remains is the private mark's own dispersion. At 2% the issuer is
    still private in almost every rollout and that dispersion survives.

    Asserted as an ORDERING rather than against pinned percentiles — the numbers move with the
    fitted model, but the ordering is what the market price causes, and it is what would vanish
    if the anchors were dropped anywhere along the three hops.
    """

    cold_spread, cold_failed = _private_equity_spread(
        await _product_for_ipo_probability(0.02, augur_config=augur_config, make_product_service=make_product_service)
    )
    hot_spread, hot_failed = _private_equity_spread(
        await _product_for_ipo_probability(0.95, augur_config=augur_config, make_product_service=make_product_service)
    )

    # Nobody died in either arm, so this is about the holding rather than about survival.
    assert (cold_failed, hot_failed) == (0, 0)
    assert cold_spread > 0.0, "a mostly-private issuer should still have a dispersed mark"
    assert hot_spread < cold_spread / 2.0


async def test_a_near_zero_market_leaves_the_issuer_private(
    augur_config: Config, make_product_service: MakeProductService
) -> None:
    """The floor of the same effect, and what catches an anchors vector being ignored.

    Two arms differing is weak evidence on its own — sampling noise differs too. Sweeping the
    price to ~0 has to move the spread the SAME direction as 2% did and further from 95%, which
    is only true if the CDF is actually driving the going-public hazard rather than the arms
    differing for some other reason.
    """

    near_zero_spread, _ = _private_equity_spread(
        await _product_for_ipo_probability(0.001, augur_config=augur_config, make_product_service=make_product_service)
    )
    hot_spread, _ = _private_equity_spread(
        await _product_for_ipo_probability(0.95, augur_config=augur_config, make_product_service=make_product_service)
    )

    assert near_zero_spread > hot_spread


if __name__ == "__main__":
    pytest_bazel.main()
