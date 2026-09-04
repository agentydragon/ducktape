"""Both backends answer one production-shaped product request identically.

Every other suite here compares the engines on a hand-written integer fixture. This one starts
where the product API starts — a `ScenarioKey` against the deployment's own portfolio, sampled
by the deployment's own exogenous model — and drives `ProductService.projection_summary` twice,
once per backend. What it therefore exercises that the fixture suites cannot is
`fixture_encoder`: the sampled float64 paths, the compiled tax tables and the scenario the
product actually builds, all crossing into the strict integer document, with the JAX run of the
same compiled plan as the answer key.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import pytest_bazel

from finance.augur.api.config import Config
from finance.augur.api.product_service import build_product_service
from finance.augur.product.service import ProductService, SummaryBackend
from finance.augur.product.wire import (
    CashFinancing,
    FundingPolicy,
    PrivateEquityTenderPolicyWire,
    ProductProjectionRequest,
    ProductProjectionResponse,
    PropertyPurchase,
    RentalIncomePlan,
    ScenarioKey,
    SleeveWeight,
    SpendIndex,
)
from finance.augur.rust.fixture_encoder import UnsupportedScenarioError

MODEL_ID = "current_model"
# Long enough for three December tax passes, the estimated-tax quarters between them, and
# semiannual coupons on both bond rungs.
HORIZON_MONTHS = 36
ROLLOUT_COUNT = 8
FAN_PERCENTILES = (5.0, 25.0, 50.0, 75.0, 95.0)


def _renting_owner(**overrides: object) -> ScenarioKey:
    """A renter drawing on the fixture deployment's portfolio.

    Reaches most of what the Rust engine models from one request: an inflation-indexed spend and
    a rent-indexed obligation, the cash band funded by target-allocation sales across three
    sleeves, TIPS and municipal coupons with their opposing interest exemptions, private-equity
    tenders under a liquid-net-worth floor, and the year-end tax machinery over all of it.
    """

    return ScenarioKey(
        **{
            "model_id": MODEL_ID,
            "horizon_months": HORIZON_MONTHS,
            "monthly_spend": Decimal(12000),
            "spend_index": SpendIndex.INFLATION,
            "monthly_rent": Decimal(4200),
            "rental_location_id": "location_a",
            "funding_policy": FundingPolicy(
                cash_floor=Decimal(60000),
                cash_ceiling=Decimal(150000),
                sleeve_weights=(
                    SleeveWeight(symbol="VOO", weight=8),
                    SleeveWeight(symbol="btc", weight=1),
                    SleeveWeight(symbol="eth", weight=1),
                ),
            ),
            "pe_tender_policy": PrivateEquityTenderPolicyWire(liquid_net_worth_floor=Decimal(1500000)),
            **overrides,
        }
    )


def _ruined_owner() -> ScenarioKey:
    """The same portfolio, spending far past it with nothing it is willing to sell.

    An empty target means the cash band can never raise, so the spend obligation goes unpaid
    within the first year and every rollout freezes. That is the only way `shortfall_quanta` and
    the failure vector become non-zero, and with them the frozen-state rules the two engines
    have to agree on month by month after a failure.
    """

    return _renting_owner(monthly_spend=Decimal(90000), funding_policy=FundingPolicy())


def _service(augur_config: Config, backend: SummaryBackend) -> ProductService:
    model = augur_config.models[augur_config.default_model_id].realize_model()
    return build_product_service(augur_config, {MODEL_ID: model}, summary_backend=backend)


def _request(scenario: ScenarioKey, metric: str) -> ProductProjectionRequest:
    return ProductProjectionRequest(
        scenario=scenario,
        first_seed=0,
        rollout_count=ROLLOUT_COUNT,
        metric=metric,
        fan_percentiles=FAN_PERCENTILES,
        terminal_percentiles=(10.0, 50.0, 90.0),
    )


def _quanta(column: list[Any]) -> list[int]:
    """One wire `value_quanta` column. It travels as decimal strings so Int64 money stays exact."""

    return [int(str(value)) for value in column]


def _projections_agree(augur_config: Config, request: ProductProjectionRequest) -> ProductProjectionResponse:
    """Both backends' answers to one request, asserted equal; returns the JAX one to anchor on."""

    expected = _service(augur_config, SummaryBackend.JAX).projection_summary(request)
    actual = _service(augur_config, SummaryBackend.RUST).projection_summary(request)
    assert actual.metric_fan.monthly_metric_fan == expected.metric_fan.monthly_metric_fan
    assert actual.metric_fan.terminal_metric_percentiles == expected.metric_fan.terminal_metric_percentiles
    assert actual.metric_fan.failed_count == expected.metric_fan.failed_count
    assert (
        actual.terminal_distribution.terminal_metric_samples == expected.terminal_distribution.terminal_metric_samples
    )
    assert (
        actual.terminal_distribution.terminal_metric_percentiles
        == expected.terminal_distribution.terminal_metric_percentiles
    )
    return expected


def test_rust_and_jax_agree_on_a_funded_projection(augur_config: Config) -> None:
    """`net_worth` is six of the seven base series, summed off the final snapshot."""

    expected = _projections_agree(augur_config, _request(_renting_owner(), "net_worth"))
    # Without these the comparison above would pass on a fan that never moved.
    assert expected.metric_fan.failed_count == 0
    assert len(set(expected.metric_fan.monthly_metric_fan["value_quanta"])) > 1


def test_rust_and_jax_agree_on_a_private_equity_tender(augur_config: Config) -> None:
    """The scenario's floor sits above the whole liquid portfolio, so every tender sells.

    Private equity is the one holding whose price never touches a level series — it is marked
    and sold off the typed bundle's own channels, which the encoder has to lower into ten
    per-issuer integer series. The anchors below are what say those channels arrived: the
    position starts whole and every percentile ends below where every percentile started,
    which only happens if the tender actually executed.
    """

    expected = _projections_agree(augur_config, _request(_renting_owner(), "private_equity_value"))
    monthly = _quanta(expected.metric_fan.monthly_metric_fan["value_quanta"])
    opening, closing = monthly[: len(FAN_PERCENTILES)], monthly[-len(FAN_PERCENTILES) :]
    assert min(opening) > 0
    assert max(closing) < min(opening)


def test_rust_and_jax_agree_on_a_projection_that_runs_out_of_money(augur_config: Config) -> None:
    """`shortfall` is the seventh base series, and its terminal value sums over months.

    Everything after a failure is the frozen-state contract — zeroed dollar state, a retained
    failure month, a shortfall that keeps accruing — so this is where the two engines have the
    most room to disagree and the funded case has none.
    """

    expected = _projections_agree(augur_config, _request(_ruined_owner(), "shortfall"))
    assert expected.metric_fan.failed_count == ROLLOUT_COUNT
    assert any(_quanta(expected.terminal_distribution.terminal_metric_samples["value_quanta"]))


# Each purchase shape and the first obligation field it needs that `ObligationSpec` lacks.
# An owner-occupied property gates its expenses on still owning it; renting one out also makes
# them a Schedule E deduction.
UNENCODABLE_PROPERTY_PURCHASES = [
    pytest.param(
        PropertyPurchase(property_id="location_a_property", financing=CashFinancing(), is_primary_residence=True),
        "carry no property gate",
        id="owner_occupied",
    ),
    pytest.param(
        PropertyPurchase(
            property_id="location_a_property",
            financing=CashFinancing(),
            is_primary_residence=False,
            initial_rental=RentalIncomePlan(),
        ),
        "carry no deduction",
        id="rented_out",
    ),
]


@pytest.mark.parametrize(("purchase", "reason"), UNENCODABLE_PROPERTY_PURCHASES)
def test_a_property_purchase_is_refused_rather_than_encoded_without_its_expense_wiring(
    augur_config: Config, purchase: PropertyPurchase, reason: str
) -> None:
    """The boundary the encoder is not allowed to paper over.

    A purchased property's recurring HOA, insurance and maintenance obligations are gated on the
    property, and on a rental they are also a Schedule E deduction. `ObligationSpec` has neither
    field, so encoding them anyway would accrue a sold property's bills or drop a year-end
    deduction and quietly change the fan. The request fails instead, which is the standing
    statement of what the Rust engine has to grow before it can serve a homeowner.

    It doubles as the proof that `SummaryBackend.RUST` really routes through the encoder:
    nothing on the JAX path can raise this.
    """

    service = _service(augur_config, SummaryBackend.RUST)
    with pytest.raises(UnsupportedScenarioError, match=reason):
        service.projection_summary(_request(_renting_owner(property_purchase=purchase), "net_worth"))


if __name__ == "__main__":
    pytest_bazel.main()
