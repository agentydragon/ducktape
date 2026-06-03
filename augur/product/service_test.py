from __future__ import annotations

from dataclasses import dataclass, field

import pytest
import pytest_bazel
from more_itertools import one

from augur.api.config import Config
from augur.api.finance import FinanceSnapshot
from augur.api.wire import CatalogResponse
from augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle, Sampler
from augur.model.independent import IndependentProviderConfig
from augur.model.provider_config import CompositeProviderConfig
from augur.model.series import (
    CryptoKey,
    CryptoSymbol,
    HomeValueKey,
    InflationKey,
    IssuerId,
    LevelSeriesKey,
    LocationId,
    PrivateEquityEventKindCode,
    PrivateEquityRegimeCode,
    RentKey,
    SP500Key,
)
from augur.model.testing import (
    ConstantFrameModel,
    PrivateEquityChannels,
    event_matrix_with_month_override,
    event_matrix_with_step,
    int_matrix_with_month_override,
    int_matrix_with_step,
    level_matrix_with_step,
)
from augur.product import decode, service
from augur.product.asset_key import CryptoAssetKey, PrivateEquityAssetKey
from augur.product.conftest import MakeProductService
from augur.product.scenarios import build_scenario, resolve_primary_agent_id
from augur.product.testing import TEST_CONFIG_LEVEL_PLACEHOLDERS
from augur.product.wire import (
    CashFinancing,
    ClosingCostPaymentEvent,
    FundingPolicy,
    HoaDuesPaymentEvent,
    HoldingSaleEvent,
    HomeownersInsurancePaymentEvent,
    MetricFanRequest,
    MonthlyExpenseEvent,
    MortgageFinancing,
    MortgagePaymentEvent,
    OutsideRentPaymentEvent,
    PrivateEquityMarkerEvent,
    PrivateEquityOpportunityEvent,
    PropertyMaintenancePaymentEvent,
    PropertyPurchase,
    PropertyPurchaseEvent,
    PropertyTaxPaymentEvent,
    RentalIncomePlan,
    RentalManagement,
    RolloutFailureEvent,
    RolloutRequest,
    ScenarioKey,
    SetPrimaryResidenceEventWire,
    SetPrimaryResidenceMarkerEvent,
    SetRentedFractionEventWire,
)
from augur.sim.external_series import EXTERNAL_SERIES_VALUES_FRAME, ExternalSeriesContext
from augur.sim.scenario import Agent, InitialAccountBalance, InitialLot, Scenario, SeriesIndexedAmount
from augur.sim.simulate import simulate_dense_with_external_series


@dataclass
class CountingModel:
    inner: Sampler
    sample_requests: list[ExogenousSamplingRequest] = field(default_factory=list)

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        return self.inner.emittable_level_keys()

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        return self.inner.emittable_private_equity_issuers()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        self.sample_requests.append(request)
        return self.inner.sample(request)


@dataclass
class MissingRequiredExogenousModel:
    sample_requests: list[ExogenousSamplingRequest] = field(default_factory=list)

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        # The whole point of this fixture is to fail validation by emitting nothing while the
        # request demands something — so it claims to emit "anything" (callers still drive the
        # request keys directly and `validate_sample_satisfies_request` catches the empty bundle).
        return frozenset()

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        return frozenset()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        self.sample_requests.append(request)
        # Empty bundle (all magisteria default to typed-empty frames) — models the
        # provider that fails to satisfy the request's required level series.
        return SampledExogenousBundle()


@pytest.fixture
def counting_model(augur_config: Config) -> CountingModel:
    return CountingModel(inner=augur_config.models[augur_config.default_model_id].realize_model())


@pytest.fixture
def product(counting_model: CountingModel, make_product_service: MakeProductService) -> service.ProductService:
    """ProductService over the fixture deployment driven by the `counting_model`."""
    return make_product_service(counting_model)


def _with_fixed_cash(config: Config, cash_usd: float) -> Config:
    fixed = config.portfolio_sources.fixed
    snapshot = fixed.snapshot or FinanceSnapshot(as_of_date="1970-01-01")
    return config.model_copy(
        update={
            "portfolio_sources": config.portfolio_sources.model_copy(
                update={
                    "fixed": fixed.model_copy(update={"snapshot": snapshot.model_copy(update={"cash_usd": cash_usd})})
                }
            )
        }
    )


@pytest.fixture
def scenario_key() -> ScenarioKey:
    return ScenarioKey(model_id="current_model", horizon_months=3, monthly_spend_usd=1_000.0, spend_index="none")


def test_metric_fan_truncates_one_cached_max_horizon_rollout(
    product: service.ProductService, counting_model: CountingModel
) -> None:
    """A shorter requested horizon reuses the single max-horizon simulation and truncates it: the
    shorter fan is an exact prefix of the longer one, terminal is taken at the truncated endpoint,
    and only one sample (at the server max horizon) is ever drawn."""

    def fan_request(horizon_months: int) -> MetricFanRequest:
        return MetricFanRequest(
            scenario=ScenarioKey(
                model_id="current_model", horizon_months=horizon_months, monthly_spend_usd=1_000.0, spend_index="none"
            ),
            rollout_seeds=(7,),
            metric="net_worth_usd",
            percentiles=(50.0,),
        )

    short = product.metric_fan(fan_request(2))
    long = product.metric_fan(fan_request(5))

    # One simulation, run at the fixture's server max horizon (120) — not the requested 2 or 5.
    assert one(counting_model.sample_requests).horizon_months == 120
    # horizon h → snapshots 0..h, i.e. h+1 monthly points.
    assert len(set(short.monthly_metric_fan["month_index"])) == 3
    assert len(set(long.monthly_metric_fan["month_index"])) == 6

    # `percentiles=(50.0,)` → exactly one value per month, so month_index keys are unique.
    short_by_month = dict(zip(short.monthly_metric_fan["month_index"], short.monthly_metric_fan["value"], strict=True))
    long_by_month = dict(zip(long.monthly_metric_fan["month_index"], long.monthly_metric_fan["value"], strict=True))
    for month, value in short_by_month.items():
        assert value == pytest.approx(long_by_month[month])
    # Terminal percentile is the metric at the requested horizon's last month, not the max horizon's.
    assert one(short.terminal_metric_percentiles["value"]) == pytest.approx(short_by_month[2])


def test_metric_fan_rejects_horizon_above_server_max(product: service.ProductService) -> None:
    request = MetricFanRequest(
        scenario=ScenarioKey(
            model_id="current_model", horizon_months=121, monthly_spend_usd=1_000.0, spend_index="none"
        ),
        rollout_seeds=(7,),
        metric="net_worth_usd",
        percentiles=(50.0,),
    )
    with pytest.raises(ValueError, match="exceeds server max 120"):
        product.metric_fan(request)


def test_product_fails_when_sample_is_missing_required_series(
    make_product_service: MakeProductService, scenario_key: ScenarioKey
) -> None:
    model = MissingRequiredExogenousModel()
    product = make_product_service(model)

    with pytest.raises(ValueError, match=f"missing required level series: .*{SP500Key().wire_id}"):
        product.rollout(RolloutRequest(scenario=scenario_key, seed=7))

    assert model.sample_requests[0].required_level_series


def test_product_fails_when_crypto_holding_price_is_not_modeled(
    augur_config: Config, make_product_service: MakeProductService, scenario_key: ScenarioKey
) -> None:
    provider = augur_config.models[augur_config.default_model_id]
    assert isinstance(provider, CompositeProviderConfig)
    assert isinstance(provider.macro, IndependentProviderConfig)
    model = provider.model_copy(
        update={
            "macro": provider.macro.model_copy(
                update={
                    "asset_prices": provider.macro.asset_prices.model_copy(
                        update={
                            "crypto": {
                                symbol: spec
                                for symbol, spec in provider.macro.asset_prices.crypto.items()
                                if symbol != "btc"
                            }
                        }
                    )
                }
            )
        }
    ).realize_model()
    product = make_product_service(model, config=augur_config)

    with pytest.raises(ValueError, match=r"missing required level series: .*crypto:btc"):
        product.rollout(RolloutRequest(scenario=scenario_key, seed=7))


def test_monthly_metric_decode_fails_when_holding_price_series_is_missing() -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="agent_a")],
        initial_cash=[InitialAccountBalance(agent_id="agent_a", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="unpriced_lot",
                agent_id="agent_a",
                asset=CryptoAssetKey(symbol=CryptoSymbol("missing")),
                purchase_month_index=-1,
                quantity=2.0,
                cost_basis_per_unit_usd=1.0,
            )
        ],
        tax_profiles=[],
        horizon_months=1,
    )
    dense = simulate_dense_with_external_series(
        scenario,
        rollout_count=1,
        external_series=ExternalSeriesContext(series_values=EXTERNAL_SERIES_VALUES_FRAME.empty()),
        locations={},
    )

    with pytest.raises(ValueError, match=r"holding asset 'crypto:missing' has no modeled price series"):
        decode.monthly_metric_arrays(dense, primary_agent_id="agent_a")


def test_metric_fan_and_rollout_detail_share_cached_sim_rollouts(
    product: service.ProductService, counting_model: CountingModel, scenario_key: ScenarioKey
) -> None:
    fan = product.metric_fan(
        MetricFanRequest(scenario=scenario_key, rollout_seeds=(7, 8), metric="cash_usd", percentiles=(0, 50, 100))
    )

    assert [request.rollout_seeds for request in counting_model.sample_requests] == [(7, 8)]
    assert counting_model.sample_requests[0].required_level_series == frozenset(
        {SP500Key(), CryptoKey(symbol=CryptoSymbol("btc")), CryptoKey(symbol=CryptoSymbol("eth"))}
    )
    assert counting_model.sample_requests[0].required_private_equity_issuers == frozenset({"private_holding_a"})
    assert fan.model_id == "composite"
    assert fan.metric == "cash_usd"
    assert fan.failed_count == 0
    assert [summary.seed for summary in fan.rollout_summaries] == [7, 8]
    assert [summary.sort_rank for summary in fan.rollout_summaries] == [0, 1]
    assert [summary.rank_percentile for summary in fan.rollout_summaries] == [25.0, 75.0]
    assert [summary.terminal_metrics.cash_usd for summary in fan.rollout_summaries] == [247_000.0, 247_000.0]
    assert len(fan.monthly_metric_fan["month_index"]) == 12
    assert fan.monthly_metric_fan["month_index"] == [0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3]
    assert fan.monthly_metric_fan["percentile"] == [0.0, 50.0, 100.0] * 4
    assert fan.monthly_metric_fan["value"] == [
        250_000.0,
        250_000.0,
        250_000.0,
        249_000.0,
        249_000.0,
        249_000.0,
        248_000.0,
        248_000.0,
        248_000.0,
        247_000.0,
        247_000.0,
        247_000.0,
    ]
    assert fan.terminal_metric_percentiles == {"percentile": [0.0, 50.0, 100.0], "value": [247_000.0] * 3}

    detail = product.rollout(RolloutRequest(scenario=scenario_key, seed=7))

    assert [request.rollout_seeds for request in counting_model.sample_requests] == [(7, 8)]
    assert detail.model_id == "composite"
    assert detail.rollout.seed == 7
    assert detail.rollout.monthly_metrics["cash_usd"] == [250_000.0, 249_000.0, 248_000.0, 247_000.0]
    assert detail.rollout.monthly_metrics["holding_value_usd"][0] == 835_500.0
    assert detail.rollout.monthly_metrics["liquid_net_worth_usd"][0] == 1_085_500.0
    # +$25k for the PHA private-equity position (1000 units at $25 anchor).
    assert detail.rollout.monthly_metrics["net_worth_usd"][0] == 1_110_500.0
    assert [event.kind for event in detail.rollout.events] == ["monthly_expense"] * 3
    assert [event.amount_paid_usd for event in detail.rollout.events if event.kind == "monthly_expense"] == [
        1_000.0,
        1_000.0,
        1_000.0,
    ]

    holding_fan = product.metric_fan(
        MetricFanRequest(scenario=scenario_key, rollout_seeds=(7, 8), metric="holding_value_usd", percentiles=(50,))
    )

    assert holding_fan.monthly_metric_fan["value"][0] == 835_500.0

    fan_with_one_new_seed = product.metric_fan(
        MetricFanRequest(scenario=scenario_key, rollout_seeds=(7, 8, 9), metric="cash_usd", percentiles=(50,))
    )

    assert [request.rollout_seeds for request in counting_model.sample_requests] == [(7, 8), (9,)]
    assert fan_with_one_new_seed.monthly_metric_fan["percentile"] == [50.0] * 4


def test_metric_fan_decodes_each_rollout_once_per_batch(
    product: service.ProductService, monkeypatch: pytest.MonkeyPatch, scenario_key: ScenarioKey
) -> None:
    original = decode.monthly_metric_arrays
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "monthly_metric_arrays", counted)

    product.metric_fan(
        MetricFanRequest(scenario=scenario_key, rollout_seeds=(7, 8, 9, 10), metric="cash_usd", percentiles=(50,))
    )

    assert calls == 4


def test_metric_fan_does_not_materialize_rollout_events(
    product: service.ProductService, monkeypatch: pytest.MonkeyPatch, scenario_key: ScenarioKey
) -> None:
    def fail_rollout_events(*_args, **_kwargs):
        raise AssertionError("metric fan should not build selected-rollout event detail")

    monkeypatch.setattr(service, "rollout_events_from", fail_rollout_events)

    product.metric_fan(
        MetricFanRequest(scenario=scenario_key, rollout_seeds=(7, 8), metric="cash_usd", percentiles=(50,))
    )


def test_failed_rollout_metrics_freeze_at_zero_after_failure(product: service.ProductService) -> None:
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=3,
        monthly_spend_usd=300_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
    )

    fan = product.metric_fan(
        MetricFanRequest(scenario=scenario, rollout_seeds=(7,), metric="net_worth_usd", percentiles=(50,))
    )

    assert fan.failed_count == 1
    assert fan.monthly_metric_fan["month_index"] == [0, 1, 2, 3]
    # Month 0 = cash 250k + holdings 835.5k + PHA 25k; failure zeros subsequent months.
    assert fan.monthly_metric_fan["value"] == [1_110_500.0, 0.0, 0.0, 0.0]
    [summary] = fan.rollout_summaries
    assert summary.failed is True
    assert summary.terminal_metrics.failed_month_index == 0
    assert summary.terminal_metrics.cash_usd == 0.0
    assert summary.terminal_metrics.holding_value_usd == 0.0
    assert summary.terminal_metrics.net_worth_usd == 0.0
    assert summary.terminal_metrics.shortfall_usd == 300_000.0

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    assert detail.rollout.failed is True
    assert detail.rollout.monthly_metrics["cash_usd"] == [250_000.0, 0.0, 0.0, 0.0]
    assert detail.rollout.monthly_metrics["holding_value_usd"] == [835_500.0, 0.0, 0.0, 0.0]
    assert detail.rollout.monthly_metrics["net_worth_usd"] == [1_110_500.0, 0.0, 0.0, 0.0]
    assert [event.kind for event in detail.rollout.events] == ["monthly_expense", "failure"]
    expense, failure = detail.rollout.events
    assert isinstance(expense, MonthlyExpenseEvent)
    assert isinstance(failure, RolloutFailureEvent)
    assert expense.amount_paid_usd == 0.0
    assert expense.shortfall_usd == 300_000.0
    assert failure.shortfall_usd == 300_000.0


def test_default_funding_policy_sells_holdings_for_required_spend(product: service.ProductService) -> None:
    scenario = ScenarioKey(model_id="current_model", horizon_months=1, monthly_spend_usd=300_000.0, spend_index="none")

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    assert detail.rollout.failed is False
    columns = detail.rollout.monthly_metrics
    assert columns["cash_usd"] == [250_000.0, 0.0]
    holding_value_usd = columns["holding_value_usd"]
    assert holding_value_usd[0] == 835_500.0
    terminal_holding_value_usd = float(holding_value_usd[1])  # type: ignore[arg-type]
    assert 0.0 < terminal_holding_value_usd < 835_500.0
    assert detail.rollout.terminal_metrics.cash_usd == 0.0
    assert detail.rollout.terminal_metrics.shortfall_usd == 0.0
    terminal_pe_value_usd = float(columns["private_equity_value_usd"][1])  # type: ignore[arg-type]
    assert detail.rollout.terminal_metrics.net_worth_usd == pytest.approx(
        terminal_holding_value_usd + terminal_pe_value_usd
    )
    assert [event.kind for event in detail.rollout.events] == ["holding_sale", "monthly_expense"]
    sale, expense = detail.rollout.events
    assert isinstance(sale, HoldingSaleEvent)
    assert isinstance(expense, MonthlyExpenseEvent)
    assert sale.asset_label == "SP500 Proxy (VOO)"
    assert sale.proceeds_usd == pytest.approx(50_000.0)
    assert sale.units == pytest.approx(100.0)
    assert expense.amount_due_usd == 300_000.0
    assert expense.amount_paid_usd == 300_000.0
    assert expense.shortfall_usd == 0.0


def test_product_rollout_includes_private_equity_protocol_event_and_forced_sale(
    forced_private_equity_event_model: ConstantFrameModel, make_product_service: MakeProductService
) -> None:
    product = make_product_service(forced_private_equity_event_model)

    detail = product.rollout(
        RolloutRequest(
            scenario=ScenarioKey(
                model_id="current_model",
                horizon_months=2,
                monthly_spend_usd=1_000.0,
                spend_index="none",
                funding_policy=FundingPolicy(sell_order=()),
            ),
            seed=7,
        )
    )

    [pe_event] = [event for event in detail.rollout.events if event.kind == "private_equity_event"]
    assert isinstance(pe_event, PrivateEquityMarkerEvent)
    assert pe_event.month_index == 1
    assert pe_event.asset == PrivateEquityAssetKey(issuer_id=IssuerId("private_holding_a"))
    assert pe_event.asset_label == "Private Holding A (PHA)"
    assert pe_event.event_kind == "acquisition_cashout"
    assert pe_event.regime == "acquired"
    assert pe_event.mark_usd == pytest.approx(25.0)
    assert pe_event.forced_sale_fraction == pytest.approx(0.25)

    [sale] = [
        event
        for event in detail.rollout.events
        if event.kind == "holding_sale"
        and event.asset == PrivateEquityAssetKey(issuer_id=IssuerId("private_holding_a"))
    ]
    assert isinstance(sale, HoldingSaleEvent)
    assert sale.units == pytest.approx(250.0)
    assert sale.proceeds_usd == pytest.approx(6_250.0)


def test_product_rollout_collapse_revalues_unsold_private_equity(make_product_service: MakeProductService) -> None:
    issuer_id = IssuerId("private_holding_a")
    product = make_product_service(
        ConstantFrameModel(
            levels=TEST_CONFIG_LEVEL_PLACEHOLDERS,
            private_equity={
                issuer_id: PrivateEquityChannels(
                    mark_usd_per_unit=level_matrix_with_step(default=25.0, override=0.5, month=1),
                    event_kind_code=int_matrix_with_month_override(
                        default=int(PrivateEquityEventKindCode.NONE),
                        override=int(PrivateEquityEventKindCode.COLLAPSE),
                        month=1,
                    ),
                    regime_code=int_matrix_with_step(
                        default=int(PrivateEquityRegimeCode.PRIVATE_OPERATING),
                        override=int(PrivateEquityRegimeCode.COLLAPSED),
                        month=1,
                    ),
                    liquidity_blocked=event_matrix_with_step(default=False, override=True, month=1),
                )
            },
            metadata={"model_id": "collapsed_pe_fixture"},
        )
    )

    detail = product.rollout(
        RolloutRequest(
            scenario=ScenarioKey(
                model_id="current_model",
                horizon_months=2,
                monthly_spend_usd=1_000.0,
                spend_index="none",
                funding_policy=FundingPolicy(sell_order=()),
            ),
            seed=7,
        )
    )

    metrics = detail.rollout.monthly_metrics
    assert metrics["private_equity_value_usd"] == [25_000.0, 500.0, 500.0]
    assert detail.rollout.terminal_metrics.private_equity_value_usd == 500.0
    assert [
        event
        for event in detail.rollout.events
        if event.kind == "holding_sale"
        and event.asset == PrivateEquityAssetKey(issuer_id=IssuerId("private_holding_a"))
    ] == []
    [pe_event] = [event for event in detail.rollout.events if event.kind == "private_equity_event"]
    assert isinstance(pe_event, PrivateEquityMarkerEvent)
    assert pe_event.event_kind == "collapse"
    assert pe_event.regime == "collapsed"
    assert pe_event.mark_usd == pytest.approx(0.5)
    assert pe_event.liquidity_blocked is True


def test_product_rollout_includes_private_equity_opportunity_trace(make_product_service: MakeProductService) -> None:
    issuer_id = IssuerId("private_holding_a")
    product = make_product_service(
        ConstantFrameModel(
            levels=TEST_CONFIG_LEVEL_PLACEHOLDERS,
            private_equity={
                issuer_id: PrivateEquityChannels(
                    mark_usd_per_unit=1.0,
                    sale_opportunity_active=event_matrix_with_month_override(default=False, override=True, month=1),
                    event_kind_code=int_matrix_with_month_override(
                        default=int(PrivateEquityEventKindCode.NONE),
                        override=int(PrivateEquityEventKindCode.TENDER),
                        month=1,
                    ),
                )
            },
            metadata={"model_id": "tender_opportunity_fixture"},
        )
    )

    detail = product.rollout(
        RolloutRequest(
            scenario=ScenarioKey(
                model_id="current_model",
                horizon_months=2,
                monthly_spend_usd=1_000.0,
                spend_index="none",
                funding_policy=FundingPolicy(sell_order=()),
            ),
            seed=7,
        )
    )

    [opportunity] = [event for event in detail.rollout.events if event.kind == "private_equity_opportunity"]
    assert isinstance(opportunity, PrivateEquityOpportunityEvent)
    assert opportunity.month_index == 1
    assert opportunity.asset_label == "Private Holding A (PHA)"
    assert opportunity.event_kind == "tender"
    assert opportunity.outcome == "floor_satisfied"
    assert opportunity.shortfall_usd == pytest.approx(0.0)
    assert opportunity.target_units == pytest.approx(0.0)
    assert opportunity.proceeds_usd == pytest.approx(0.0)


def test_product_cash_buffer_uses_sim_trigger_and_fixed_sale_amount(product: service.ProductService) -> None:
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=1,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(cash_buffer_trigger_below_usd=260_000.0, cash_buffer_sale_usd=20_000.0),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    assert detail.rollout.failed is False
    assert detail.rollout.monthly_metrics["cash_usd"] == [250_000.0, 269_000.0]
    assert detail.rollout.terminal_metrics.cash_usd == 269_000.0
    assert detail.rollout.terminal_metrics.shortfall_usd == 0.0
    assert [event.kind for event in detail.rollout.events] == ["holding_sale", "monthly_expense"]
    sale, expense = detail.rollout.events
    assert isinstance(sale, HoldingSaleEvent)
    assert isinstance(expense, MonthlyExpenseEvent)
    assert sale.proceeds_usd == pytest.approx(20_000.0)
    assert expense.amount_paid_usd == 1_000.0


def test_product_rollout_includes_zero_tax_accrual_events_without_taxable_income(
    product: service.ProductService,
) -> None:
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=12,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    tax_accruals = [event for event in detail.rollout.events if event.kind == "tax_accrual"]
    assert {event.jurisdiction_id for event in tax_accruals} == {"federal_us", "california"}
    assert {event.month_index for event in tax_accruals} == {11}
    assert all(event.amount_usd == 0.0 for event in tax_accruals)
    assert [event for event in detail.rollout.events if event.kind == "tax_payment"] == []


def test_product_rollout_includes_federal_and_california_tax_events_for_holding_sales(
    product: service.ProductService,
) -> None:
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=13,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(cash_buffer_trigger_below_usd=260_000.0, cash_buffer_sale_usd=500_000.0),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    events = detail.rollout.events
    tax_accruals = [event for event in events if event.kind == "tax_accrual"]
    assert {event.jurisdiction_id for event in tax_accruals} == {"federal_us", "california"}
    assert {event.month_index for event in tax_accruals} == {11}
    assert all(event.amount_usd > 0 for event in tax_accruals)
    assert sum(event.amount_usd for event in tax_accruals) == pytest.approx(
        sum(event.total_tax_usd for event in tax_accruals)
    )
    federal = one(event for event in tax_accruals if event.jurisdiction_id == "federal_us")
    california = one(event for event in tax_accruals if event.jurisdiction_id == "california")
    assert federal.capital_gain_tax_usd > 0
    assert california.capital_gain_tax_usd == 0.0
    assert california.ordinary_tax_usd > 0

    tax_payments = [event for event in events if event.kind == "tax_payment"]
    [tax_payment] = tax_payments
    assert tax_payment.month_index == 12
    assert tax_payment.obligation_type == "tax_true_up"
    assert tax_payment.amount_due_usd == pytest.approx(sum(event.amount_usd for event in tax_accruals))
    assert tax_payment.amount_paid_usd == pytest.approx(tax_payment.amount_due_usd)
    assert tax_payment.shortfall_usd == 0.0


def test_outside_rent_emits_yearly_re_pegged_obligation(
    product: service.ProductService, counting_model: CountingModel
) -> None:
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=14,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        monthly_rent_usd=3_000.0,
        rental_location_id="location_a",
        funding_policy=FundingPolicy(sell_order=()),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    rent_events = [event for event in detail.rollout.events if event.kind == "outside_rent"]
    # 14 monthly rent payments — one per event month.
    assert len(rent_events) == 14
    assert all(isinstance(event, OutsideRentPaymentEvent) for event in rent_events)
    # Year 0 (months 0..11) all peg at the base amount: rent_series[0]/rent_series[0] = 1.
    year_zero = [event for event in rent_events if event.month_index < 12]
    assert {event.amount_paid_usd for event in year_zero} == {3_000.0}
    # Year 1 (months 12..) rescales by rent_series[12]/rent_series[0] — stochastic, so non-3000.
    year_one = [event for event in rent_events if event.month_index >= 12]
    assert year_one
    assert all(event.amount_paid_usd != 3_000.0 for event in year_one)
    # Within year 1 the amount stays flat.
    assert len({event.amount_paid_usd for event in year_one}) == 1
    # Required-level-series for the request should include the location-keyed rent series.
    assert RentKey(location_id=LocationId("location_a")) in counting_model.sample_requests[0].required_level_series

    # Year-0 cash drops by spend + rent = 4000 each month deterministically.
    cash = detail.rollout.monthly_metrics["cash_usd"]
    assert cash[0] == 250_000.0
    assert cash[12] == pytest.approx(250_000.0 - 12 * 4_000.0)
    # Monthly_expense events are still emitted alongside, distinctly from outside_rent.
    expense_events = [event for event in detail.rollout.events if event.kind == "monthly_expense"]
    assert len(expense_events) == 14
    assert all(event.amount_paid_usd == 1_000.0 for event in expense_events)


def test_outside_rent_zero_omits_rent_series_requirement(
    product: service.ProductService, counting_model: CountingModel, scenario_key: ScenarioKey
) -> None:
    # scenario_key carries no rent.
    product.metric_fan(
        MetricFanRequest(scenario=scenario_key, rollout_seeds=(7,), metric="cash_usd", percentiles=(50,))
    )

    assert not any(isinstance(key, RentKey) for key in counting_model.sample_requests[0].required_level_series)


def test_outside_rent_rejects_unknown_location(product: service.ProductService) -> None:
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=3,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        monthly_rent_usd=3_000.0,
        rental_location_id="not_a_real_location",
    )

    with pytest.raises(ValueError, match=r"unknown rental_location_id"):
        product.rollout(RolloutRequest(scenario=scenario, seed=7))


def test_scenario_key_rejects_rent_without_location() -> None:
    with pytest.raises(ValueError, match=r"rental_location_id is required"):
        ScenarioKey(
            model_id="current_model",
            horizon_months=3,
            monthly_spend_usd=1_000.0,
            spend_index="none",
            monthly_rent_usd=3_000.0,
        )


def test_scenario_key_rejects_location_without_rent() -> None:
    with pytest.raises(ValueError, match=r"rental_location_id must be unset"):
        ScenarioKey(
            model_id="current_model",
            horizon_months=3,
            monthly_spend_usd=1_000.0,
            spend_index="none",
            rental_location_id="location_a",
        )


@pytest.fixture
def mortgage_purchase_scenario() -> ScenarioKey:
    return ScenarioKey(
        model_id="current_model",
        horizon_months=2,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property",
            financing=MortgageFinancing(term_months=360, down_payment_pct=20.0, annual_rate_pct=7.0),
            is_primary_residence=True,
        ),
    )


def test_property_purchase_emits_purchase_mortgage_and_property_tax_events(
    product: service.ProductService, mortgage_purchase_scenario: ScenarioKey
) -> None:
    detail = product.rollout(RolloutRequest(scenario=mortgage_purchase_scenario, seed=7))

    [purchase] = [event for event in detail.rollout.events if event.kind == "property_purchase"]
    assert isinstance(purchase, PropertyPurchaseEvent)
    assert purchase.property_id == "location_a_property"
    assert purchase.month_index == 0
    assert purchase.purchase_price_usd == pytest.approx(900_000.0)
    assert purchase.down_payment_usd == pytest.approx(180_000.0)
    assert purchase.mortgage_principal_usd == pytest.approx(720_000.0)

    [closing] = [event for event in detail.rollout.events if event.kind == "closing_cost_payment"]
    assert isinstance(closing, ClosingCostPaymentEvent)
    assert closing.property_id == "location_a_property"
    assert closing.month_index == 0
    assert closing.amount_usd == pytest.approx(900_000.0 * 0.015)

    mortgage_payments = [event for event in detail.rollout.events if event.kind == "mortgage_payment"]
    monthly_payment = 720_000.0 * (0.07 / 12) / (1.0 - (1.0 + 0.07 / 12) ** -360)
    assert mortgage_payments
    for event in mortgage_payments:
        assert isinstance(event, MortgagePaymentEvent)
        assert event.amount_usd == pytest.approx(monthly_payment)
        assert event.interest_usd + event.principal_usd == pytest.approx(monthly_payment)

    property_taxes = [event for event in detail.rollout.events if event.kind == "property_tax_payment"]
    monthly_property_tax = 900_000.0 * 0.01 / 12.0
    assert property_taxes
    for tax_event in property_taxes:
        assert isinstance(tax_event, PropertyTaxPaymentEvent)
        assert tax_event.amount_due_usd == pytest.approx(monthly_property_tax)
        assert tax_event.amount_paid_usd == pytest.approx(monthly_property_tax)
        assert tax_event.shortfall_usd == 0.0


def test_product_lowers_primary_residence_assignments_to_sim_scenario(
    augur_config: Config, catalog: CatalogResponse
) -> None:
    primary_agent_id = resolve_primary_agent_id(augur_config)
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=36,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property",
            financing=CashFinancing(),
            is_primary_residence=True,
            lifecycle_events=(
                SetPrimaryResidenceEventWire(month=12, is_primary_residence=False),
                SetPrimaryResidenceEventWire(month=24, is_primary_residence=True),
            ),
        ),
    )

    sim_scenario = build_scenario(
        scenario,
        primary_agent_id=primary_agent_id,
        initial_cash_usd=1_200_000.0,
        initial_lots=(),
        properties_by_id=catalog.properties_by_id,
    )

    assert [(row.agent_id, row.property_id) for row in sim_scenario.initial_primary_residences] == [
        (primary_agent_id, "location_a_property")
    ]
    assert [(row.month, row.agent_id, row.property_id) for row in sim_scenario.primary_residence_events] == [
        (12, primary_agent_id, None),
        (24, primary_agent_id, "location_a_property"),
    ]


def test_product_full_property_rent_scales_by_fraction_vacancy_and_rent_denominated_fees(
    augur_config: Config, catalog: CatalogResponse
) -> None:
    primary_agent_id = resolve_primary_agent_id(augur_config)
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=12,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property",
            financing=CashFinancing(),
            is_primary_residence=True,
            initial_rental=RentalIncomePlan(
                full_property_monthly_rent_usd=6_000.0, fraction_rented=0.5, vacancy_pct=0.10
            ),
            rental_management=RentalManagement(management_fee_pct=8.0, leasing_fee_months=1.0, avg_tenancy_months=24),
        ),
    )

    sim_scenario = build_scenario(
        scenario,
        primary_agent_id=primary_agent_id,
        initial_cash_usd=1_200_000.0,
        initial_lots=(),
        properties_by_id=catalog.properties_by_id,
    )

    rent_transfer = one(
        transfer
        for transfer in sim_scenario.recurring_transfers
        if transfer.cause_id == "rental_income:location_a_property"
    )
    assert isinstance(rent_transfer.amount_usd, SeriesIndexedAmount)
    assert rent_transfer.amount_usd.base_amount_usd == pytest.approx(6_000.0 * 0.5 * 0.90)
    assert rent_transfer.amount_usd.series == RentKey(location_id=LocationId("location_a"))

    management_fee = one(
        transfer
        for transfer in sim_scenario.recurring_transfers
        if transfer.cause_id == "management_fee:location_a_property"
    )
    assert isinstance(management_fee.amount_usd, SeriesIndexedAmount)
    assert management_fee.amount_usd.base_amount_usd == pytest.approx(6_000.0 * 0.5 * 0.90 * 0.08)

    leasing_fee = one(
        transfer
        for transfer in sim_scenario.scheduled_transfers
        if transfer.cause_id == "leasing_fee:location_a_property:m0"
    )
    assert isinstance(leasing_fee.amount_usd, SeriesIndexedAmount)
    assert leasing_fee.amount_usd.base_amount_usd == pytest.approx(6_000.0 * 0.5)


def test_product_rental_lifecycle_resizes_tenant_rent_and_management_fees(
    augur_config: Config, catalog: CatalogResponse
) -> None:
    primary_agent_id = resolve_primary_agent_id(augur_config)
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=12,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property",
            financing=CashFinancing(),
            is_primary_residence=True,
            initial_rental=RentalIncomePlan(
                full_property_monthly_rent_usd=6_000.0, fraction_rented=0.25, vacancy_pct=0.10
            ),
            rental_management=RentalManagement(management_fee_pct=8.0, leasing_fee_months=1.0, avg_tenancy_months=24),
            lifecycle_events=(
                SetRentedFractionEventWire(month=3, rented_fraction=0.75),
                SetRentedFractionEventWire(month=6, rented_fraction=0.0),
                SetRentedFractionEventWire(month=8, rented_fraction=0.5),
            ),
        ),
    )

    sim_scenario = build_scenario(
        scenario,
        primary_agent_id=primary_agent_id,
        initial_cash_usd=1_200_000.0,
        initial_lots=(),
        properties_by_id=catalog.properties_by_id,
    )

    rent_transfers = [
        transfer
        for transfer in sim_scenario.recurring_transfers
        if transfer.cause_id == "rental_income:location_a_property"
    ]
    assert [(transfer.start_month, transfer.end_month) for transfer in rent_transfers] == [(0, 2), (3, 5), (8, 11)]
    rent_amounts = []
    for rent_transfer in rent_transfers:
        assert isinstance(rent_transfer.amount_usd, SeriesIndexedAmount)
        rent_amounts.append(rent_transfer.amount_usd.base_amount_usd)
        assert rent_transfer.amount_usd.series == RentKey(location_id=LocationId("location_a"))
    assert rent_amounts == pytest.approx([6_000.0 * 0.25 * 0.90, 6_000.0 * 0.75 * 0.90, 6_000.0 * 0.5 * 0.90])

    management_fees = [
        transfer
        for transfer in sim_scenario.recurring_transfers
        if transfer.cause_id == "management_fee:location_a_property"
    ]
    assert [(transfer.start_month, transfer.end_month) for transfer in management_fees] == [(0, 2), (3, 5), (8, 11)]
    fee_amounts = []
    for management_fee in management_fees:
        assert isinstance(management_fee.amount_usd, SeriesIndexedAmount)
        fee_amounts.append(management_fee.amount_usd.base_amount_usd)
    assert fee_amounts == pytest.approx(
        [6_000.0 * 0.25 * 0.90 * 0.08, 6_000.0 * 0.75 * 0.90 * 0.08, 6_000.0 * 0.5 * 0.90 * 0.08]
    )

    leasing_fees = sorted(
        (
            transfer
            for transfer in sim_scenario.scheduled_transfers
            if transfer.cause_id.startswith("leasing_fee:location_a_property:")
        ),
        key=lambda transfer: transfer.month,
    )
    assert [transfer.month for transfer in leasing_fees] == [0, 3, 8]
    leasing_amounts = []
    for leasing_fee in leasing_fees:
        assert isinstance(leasing_fee.amount_usd, SeriesIndexedAmount)
        leasing_amounts.append(leasing_fee.amount_usd.base_amount_usd)
    assert leasing_amounts == pytest.approx([6_000.0 * 0.25, 6_000.0 * 0.75, 6_000.0 * 0.5])


def test_future_rental_lifecycle_uses_property_rent_estimate_without_initial_rental(
    augur_config: Config, catalog: CatalogResponse
) -> None:
    primary_agent_id = resolve_primary_agent_id(augur_config)
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=6,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property",
            financing=CashFinancing(),
            is_primary_residence=True,
            lifecycle_events=(SetRentedFractionEventWire(month=3, rented_fraction=0.5),),
        ),
    )

    sim_scenario = build_scenario(
        scenario,
        primary_agent_id=primary_agent_id,
        initial_cash_usd=1_200_000.0,
        initial_lots=(),
        properties_by_id=catalog.properties_by_id,
    )

    rent_transfer = one(
        transfer
        for transfer in sim_scenario.recurring_transfers
        if transfer.cause_id == "rental_income:location_a_property"
    )
    assert (rent_transfer.start_month, rent_transfer.end_month) == (3, 5)
    assert isinstance(rent_transfer.amount_usd, SeriesIndexedAmount)
    assert rent_transfer.amount_usd.base_amount_usd == pytest.approx(4_200.0 * 0.5 * 0.95)
    assert rent_transfer.amount_usd.series == RentKey(location_id=LocationId("location_a"))


def test_future_rental_lifecycle_requires_rent_series_at_product_api(
    counting_model: CountingModel, augur_config: Config, make_product_service: MakeProductService
) -> None:
    config = _with_fixed_cash(augur_config, 1_200_000.0)
    product = make_product_service(counting_model, config=config)
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=6,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property",
            financing=CashFinancing(),
            is_primary_residence=True,
            lifecycle_events=(SetRentedFractionEventWire(month=3, rented_fraction=0.5),),
        ),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    assert detail.rollout.failed is False
    assert RentKey(location_id=LocationId("location_a")) in counting_model.sample_requests[0].required_level_series


def test_primary_residence_event_emits_rollout_marker(
    counting_model: CountingModel, augur_config: Config, make_product_service: MakeProductService
) -> None:
    config = _with_fixed_cash(augur_config, 1_200_000.0)
    product = make_product_service(counting_model, config=config)
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=3,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property",
            financing=CashFinancing(),
            is_primary_residence=True,
            lifecycle_events=(SetPrimaryResidenceEventWire(month=1, is_primary_residence=False),),
        ),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    [event] = [event for event in detail.rollout.events if event.kind == "set_primary_residence"]
    assert isinstance(event, SetPrimaryResidenceMarkerEvent)
    assert event.month_index == 1
    assert event.agent_id == resolve_primary_agent_id(config)
    assert event.property_id is None
    assert event.is_primary_residence is False


def test_property_purchase_metrics_track_value_balance_and_equity(
    product: service.ProductService, counting_model: CountingModel, mortgage_purchase_scenario: ScenarioKey
) -> None:
    detail = product.rollout(RolloutRequest(scenario=mortgage_purchase_scenario, seed=7))

    # month_index=0 is the pre-purchase opening snapshot; the property activates at index 1
    # (end of purchase month). Values mark-to-market against the home_value series so the index-1
    # value may deviate from the $900k purchase price, but it must be positive and obey the
    # accounting identities below.
    metrics = detail.rollout.monthly_metrics
    assert float(metrics["property_value_usd"][0]) == 0.0  # type: ignore[arg-type]
    assert float(metrics["mortgage_balance_usd"][0]) == 0.0  # type: ignore[arg-type]
    property_value_usd = float(metrics["property_value_usd"][1])  # type: ignore[arg-type]
    mortgage_balance_usd = float(metrics["mortgage_balance_usd"][1])  # type: ignore[arg-type]
    home_equity_usd = float(metrics["home_equity_usd"][1])  # type: ignore[arg-type]
    liquid_net_worth_usd = float(metrics["liquid_net_worth_usd"][1])  # type: ignore[arg-type]
    private_equity_value_usd = float(metrics["private_equity_value_usd"][1])  # type: ignore[arg-type]
    net_worth_usd = float(metrics["net_worth_usd"][1])  # type: ignore[arg-type]

    assert property_value_usd > 0.0
    assert mortgage_balance_usd == pytest.approx(720_000.0)
    assert home_equity_usd == pytest.approx(property_value_usd - mortgage_balance_usd)
    assert net_worth_usd == pytest.approx(liquid_net_worth_usd + home_equity_usd + private_equity_value_usd)
    # Required-level-series should include the location's home-value series.
    assert HomeValueKey(location_id=LocationId("location_a")) in counting_model.sample_requests[0].required_level_series


def test_cash_property_purchase_omits_mortgage_payments(
    counting_model: CountingModel, augur_config: Config, make_product_service: MakeProductService
) -> None:
    config = _with_fixed_cash(augur_config, 1_200_000.0)
    product = make_product_service(counting_model, config=config)
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=2,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property", financing=CashFinancing(), is_primary_residence=True
        ),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    [purchase] = [event for event in detail.rollout.events if event.kind == "property_purchase"]
    assert isinstance(purchase, PropertyPurchaseEvent)
    assert purchase.down_payment_usd == pytest.approx(900_000.0)
    assert purchase.mortgage_principal_usd == 0.0
    [closing] = [event for event in detail.rollout.events if event.kind == "closing_cost_payment"]
    assert isinstance(closing, ClosingCostPaymentEvent)
    assert closing.amount_usd == pytest.approx(900_000.0 * 0.015)
    assert [event for event in detail.rollout.events if event.kind == "mortgage_payment"] == []
    assert detail.rollout.monthly_metrics["mortgage_balance_usd"][0] == 0.0


def test_property_purchase_emits_hoa_dues_when_property_has_monthly_hoa(
    product: service.ProductService, counting_model: CountingModel
) -> None:
    # location_b_property has hoa_monthly_usd=150 in the public fixture.
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=3,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_b_property",
            financing=MortgageFinancing(term_months=360, down_payment_pct=20.0, annual_rate_pct=7.0),
            is_primary_residence=True,
        ),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    hoa_events = [event for event in detail.rollout.events if event.kind == "hoa_dues_payment"]
    assert hoa_events
    for event in hoa_events:
        assert isinstance(event, HoaDuesPaymentEvent)
        # Base is 150.0 USD/month; inflation-indexed so the realized amount drifts each month, but it
        # must stay near base on a short horizon.
        assert event.amount_due_usd == pytest.approx(150.0, rel=0.1)
        assert event.amount_paid_usd == pytest.approx(event.amount_due_usd)
        assert event.shortfall_usd == 0.0
    assert InflationKey() in counting_model.sample_requests[0].required_level_series


def test_property_purchase_skips_hoa_when_property_has_no_monthly_hoa(product: service.ProductService) -> None:
    # location_a_property has hoa_monthly_usd=0 in the public fixture.
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=3,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property",
            financing=MortgageFinancing(term_months=360, down_payment_pct=20.0, annual_rate_pct=7.0),
            is_primary_residence=True,
        ),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    assert [event for event in detail.rollout.events if event.kind == "hoa_dues_payment"] == []


def test_property_purchase_emits_homeowners_insurance_at_default_pct(product: service.ProductService) -> None:
    # location_a_property is $900k. Default annual_insurance_pct=0.4 → $300/mo at month 0.
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=3,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property",
            financing=MortgageFinancing(term_months=360, down_payment_pct=20.0, annual_rate_pct=7.0),
            is_primary_residence=True,
        ),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    insurance_events = [event for event in detail.rollout.events if event.kind == "homeowners_insurance_payment"]
    assert insurance_events
    monthly_premium = 0.4 / 100.0 * 900_000.0 / 12.0
    for event in insurance_events:
        assert isinstance(event, HomeownersInsurancePaymentEvent)
        assert event.amount_due_usd == pytest.approx(monthly_premium, rel=0.1)
        assert event.amount_paid_usd == pytest.approx(event.amount_due_usd)
        assert event.shortfall_usd == 0.0


def test_property_purchase_with_zero_insurance_pct_omits_insurance(product: service.ProductService) -> None:
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=2,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property", financing=CashFinancing(), is_primary_residence=True
        ),
        annual_insurance_pct=0.0,
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    assert [event for event in detail.rollout.events if event.kind == "homeowners_insurance_payment"] == []


def test_property_purchase_emits_maintenance_at_default_pct(product: service.ProductService) -> None:
    # location_a_property is $900k. Default annual_maintenance_pct=1.0 → $750/mo at month 0.
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=3,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property",
            financing=MortgageFinancing(term_months=360, down_payment_pct=20.0, annual_rate_pct=7.0),
            is_primary_residence=True,
        ),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    maintenance_events = [event for event in detail.rollout.events if event.kind == "property_maintenance_payment"]
    assert maintenance_events
    monthly_amount = 1.0 / 100.0 * 900_000.0 / 12.0
    for event in maintenance_events:
        assert isinstance(event, PropertyMaintenancePaymentEvent)
        assert event.amount_due_usd == pytest.approx(monthly_amount, rel=0.1)
        assert event.amount_paid_usd == pytest.approx(event.amount_due_usd)
        assert event.shortfall_usd == 0.0


def test_property_purchase_with_zero_maintenance_pct_omits_maintenance(product: service.ProductService) -> None:
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=2,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property", financing=CashFinancing(), is_primary_residence=True
        ),
        annual_maintenance_pct=0.0,
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    assert [event for event in detail.rollout.events if event.kind == "property_maintenance_payment"] == []


def test_property_purchase_rejects_unknown_property(product: service.ProductService) -> None:
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=2,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        property_purchase=PropertyPurchase(
            property_id="ghost_property", financing=CashFinancing(), is_primary_residence=True
        ),
    )

    with pytest.raises(ValueError, match=r"unknown property_id"):
        product.rollout(RolloutRequest(scenario=scenario, seed=7))


def test_primary_residence_mortgage_emits_mortgage_interest_deduction_policy(
    counting_model: CountingModel, augur_config: Config, make_product_service: MakeProductService
) -> None:
    """A mortgaged primary residence builds one MortgageInterestDeductionPolicy on the sim
    Scenario; tax_accrual events surface a non-zero mortgage_interest_deduction_usd."""
    config = _with_fixed_cash(augur_config, 400_000.0)
    product = make_product_service(counting_model, config=config)
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=13,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property",
            financing=MortgageFinancing(term_months=360, down_payment_pct=20.0, annual_rate_pct=7.0),
            is_primary_residence=True,
        ),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    accruals = [event for event in detail.rollout.events if event.kind == "tax_accrual"]
    federal_accrual = one(event for event in accruals if event.jurisdiction_id == "federal_us")
    assert federal_accrual.mortgage_interest_deduction_usd > 0.0
    assert federal_accrual.standard_deduction_usd == pytest.approx(14_600.0)
    # MID on a $900k * 80% = $720k mortgage is comfortably above the standard deduction.
    assert federal_accrual.itemized_deduction_usd > federal_accrual.standard_deduction_usd


def test_secondary_residence_mortgage_omits_mortgage_interest_deduction(
    counting_model: CountingModel, augur_config: Config, make_product_service: MakeProductService
) -> None:
    """`is_primary_residence=False` should produce zero MID even with a mortgage."""
    config = _with_fixed_cash(augur_config, 400_000.0)
    product = make_product_service(counting_model, config=config)
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=13,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property",
            financing=MortgageFinancing(term_months=360, down_payment_pct=20.0, annual_rate_pct=7.0),
            is_primary_residence=False,
        ),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    federal_accrual = one(
        event
        for event in detail.rollout.events
        if event.kind == "tax_accrual" and event.jurisdiction_id == "federal_us"
    )
    assert federal_accrual.mortgage_interest_deduction_usd == 0.0
    assert federal_accrual.itemized_deduction_usd == 0.0
    assert federal_accrual.standard_deduction_usd == pytest.approx(14_600.0)


def test_cash_property_purchase_omits_mortgage_interest_deduction(
    counting_model: CountingModel, augur_config: Config, make_product_service: MakeProductService
) -> None:
    """A cash purchase has no mortgage and therefore no MID even when is_primary_residence=True."""
    config = _with_fixed_cash(augur_config, 1_200_000.0)
    product = make_product_service(counting_model, config=config)
    scenario = ScenarioKey(
        model_id="current_model",
        horizon_months=13,
        monthly_spend_usd=1_000.0,
        spend_index="none",
        funding_policy=FundingPolicy(sell_order=()),
        property_purchase=PropertyPurchase(
            property_id="location_a_property", financing=CashFinancing(), is_primary_residence=True
        ),
    )

    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))

    federal_accrual = one(
        event
        for event in detail.rollout.events
        if event.kind == "tax_accrual" and event.jurisdiction_id == "federal_us"
    )
    assert federal_accrual.mortgage_interest_deduction_usd == 0.0
    assert federal_accrual.itemized_deduction_usd == 0.0


if __name__ == "__main__":
    pytest_bazel.main()
