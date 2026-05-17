from __future__ import annotations

import numpy as np
import pytest_bazel
from numpy.testing import assert_allclose

from augur.core.accounting import ChartAccountRole, PostingSide
from augur.core.api import simulate_set
from augur.core.market_bundle import MarketBundle, MarketBundleMetadata
from augur.core.scenario_engine import MonthlyColumnSource, monthly_column_specs, run_scenario_vectorized
from augur.core.scenario_set import (
    AccountType,
    AccruePartnerEquityAction,
    ActionType,
    AssetType,
    FundingDecisionType,
    FundingSourceType,
    MarketRequest,
    MonthlySpendDecision,
    ObligationStatus,
    ObligationType,
    PayMortgageAction,
    PrivateEquitySaleDecision,
    PrivateEquitySaleDecisionReason,
    PrivateEquitySaleOpportunityObservation,
    PrivateEquitySaleRuleType,
    ReportMetric,
    RolloutStatusType,
    ScenarioSet,
    SellPublicStockDecision,
    SettlementStatus,
    SettlePropertySaleAction,
    TransferPartnerContributionAction,
)


def _bundle(
    *,
    rollout_count: int = 2,
    horizon_months: int = 3,
    inflation_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0),
    sp500_path: tuple[float, ...] = (1.0, 1.1, 1.2, 1.3),
    private_equity_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0),
    home_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0),
    rent_path: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0),
    private_equity_sale_opportunity_month: int | None = None,
) -> MarketBundle:
    shape = (rollout_count, horizon_months + 1)
    month_index = np.arange(horizon_months + 1, dtype="int64")

    def path(values: tuple[float, ...]) -> np.ndarray:
        return np.broadcast_to(np.asarray(values[: horizon_months + 1], dtype="float64"), shape).copy()

    events = np.zeros(shape, dtype=np.bool_)
    if private_equity_sale_opportunity_month is not None:
        events[:, private_equity_sale_opportunity_month] = True
    metadata = MarketBundleMetadata(
        market_model_id="test",
        seed=7,
        rollout_count=rollout_count,
        horizon_months=horizon_months,
        event_stream_ids=("private_equity_sale_opportunity_event",),
    )
    return MarketBundle(
        month_index=month_index,
        inflation_multipliers=path(inflation_path),
        generic_sp500_multipliers=path(sp500_path),
        home_value_multipliers_by_location={
            "default": path(home_path),
            "san_francisco_ca": path(home_path),
            "vallejo_ca": path(home_path),
            "mare_island_vallejo_ca": path(home_path),
        },
        rent_multipliers_by_location={
            "default": path(rent_path),
            "san_francisco_ca": path(rent_path),
            "vallejo_ca": path(rent_path),
            "mare_island_vallejo_ca": path(rent_path),
        },
        mortgage_30y_rate_pct=np.full(shape, 6.0, dtype="float64"),
        private_equity_value_multipliers=path(private_equity_path),
        private_equity_sale_opportunity_mask=events,
        metadata=metadata,
    )


def _scenario_set_body(*scenarios: dict) -> dict:
    return {
        "scenario_set_id": "engine_test",
        "title": "Engine test",
        "market_request": {"rollout_count": 2, "horizon_months": 3, "seed": 7},
        "scenarios": list(scenarios),
    }


def _scenario_body(
    scenario_id: str,
    *,
    actors: list[dict] | None = None,
    cash_usd: float = 10_000,
    sp500_usd: float = 100_000,
    sp500_basis_usd: float | None = None,
    private_equity_usd: float = 50_000,
    private_equity_basis_usd: float | None = None,
    private_equity_units: float | None = None,
    property_selection: dict | None = None,
    financing: dict | None = None,
    occupancy_plan: dict | None = None,
    rental_plan: dict | None = None,
    tax_profile: dict | None = None,
    transaction_costs: dict | None = None,
    property_assumptions: dict | None = None,
    policies: list[dict] | None = None,
    events: list[dict] | None = None,
    tax_regimes: list[str] | None = None,
) -> dict:
    return {
        "scenario_id": scenario_id,
        "label": scenario_id.replace("_", " ").title(),
        "actors": actors or [{"actor_id": "owner", "label": "Owner", "role": "primary_owner"}],
        "events": events or [],
        "policies": policies or [],
        "property_selection": property_selection or {},
        "financing": financing or {},
        "occupancy_plan": occupancy_plan or {},
        "rental_plan": rental_plan or {"rental_mode": "not_rented"},
        "tax_profile": tax_profile or {},
        "transaction_costs": transaction_costs or {},
        "property_assumptions": property_assumptions or {},
        "initial_balance_sheet": {
            "accounts": [
                {
                    "account_id": "checking",
                    "account_type": "checking",
                    "owner_actor_id": "owner",
                    "balance_usd": cash_usd,
                }
            ],
            "assets": [
                {
                    "asset_id": "sp500",
                    "asset_type": "generic_sp500_stock",
                    "owner_actor_id": "owner",
                    "value_usd": sp500_usd,
                    "cost_basis_usd": sp500_basis_usd if sp500_basis_usd is not None else sp500_usd,
                },
                {
                    "asset_id": "private_equity",
                    "asset_type": "private_equity",
                    "owner_actor_id": "owner",
                    "value_usd": private_equity_usd,
                    "units": private_equity_units,
                    "cost_basis_usd": private_equity_basis_usd,
                },
            ],
        },
        "tax_regimes": tax_regimes or [],
    }


def _assert_liquid_net_worth_matches_cash_and_public_stock(result) -> None:
    assert_allclose(result.liquid_net_worth_usd, result.cash_usd + result.generic_sp500_value_usd)


def test_portfolio_only_baseline_uses_numpy_paths() -> None:
    scenario_set = ScenarioSet.model_validate(_scenario_set_body(_scenario_body("portfolio_only")))
    scenario = scenario_set.scenarios[0]

    result = run_scenario_vectorized(scenario, _bundle(private_equity_path=(1.0, 1.5, 2.0, 2.5)))

    _assert_liquid_net_worth_matches_cash_and_public_stock(result)
    assert result.cash_usd.shape == (2, 4)
    assert_allclose(result.property_value_usd, 0)
    assert_allclose(result.cash_usd[:, 0], 10_000)
    assert_allclose(result.generic_sp500_value_usd[:, 2], 120_000)
    assert_allclose(result.private_equity_value_usd[:, 2], 100_000)
    assert_allclose(result.net_worth_usd[:, 2], 230_000)
    assert result.monthly_columns().row_count == 8


def test_report_metrics_are_explicit_typed_views() -> None:
    scenario_set = ScenarioSet.model_validate(_scenario_set_body(_scenario_body("typed_metrics")))
    run = simulate_set(scenario_set, market_bundle=_bundle())
    result = run.scenario("typed_metrics")

    arrays = result.arrays
    assert arrays is not None
    assert_allclose(result.matrix(ReportMetric.CASH_USD), arrays.cash_usd)
    assert_allclose(result.rollout(0).series(ReportMetric.NET_WORTH_USD), arrays.net_worth_usd[0, :])
    assert result.terminal(ReportMetric.MONTH_INDEX) == 3


def test_monthly_column_specs_name_report_view_sources() -> None:
    specs_by_metric = {spec.metric: spec for spec in monthly_column_specs()}
    scenario_set = ScenarioSet.model_validate(_scenario_set_body(_scenario_body("monthly_sources")))
    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())
    expected_columns = {
        "scenario_id",
        "scenario_label",
        "rollout_index",
        "month_index",
        *(spec.metric.value for spec in monthly_column_specs()),
    }

    assert specs_by_metric[ReportMetric.MONTHLY_SPEND_USD].source is MonthlyColumnSource.LEDGER_ENTRY
    assert specs_by_metric[ReportMetric.PARTNER_EQUITY_LEDGER_USD].source is MonthlyColumnSource.BALANCE_SNAPSHOT
    assert specs_by_metric[ReportMetric.PROPERTY_SALE_ADJUSTED_BASIS_USD].source is (
        MonthlyColumnSource.ACCOUNTING_DETAIL
    )
    assert specs_by_metric[ReportMetric.NET_WORTH_USD].source is MonthlyColumnSource.REPORT_PROJECTION
    assert set(result.monthly_columns().columns) == expected_columns
    assert ReportMetric.MONTH_INDEX not in specs_by_metric


def test_run_scenario_set_samples_shared_market_bundle_once() -> None:
    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        def sample_market_bundle(
            self, *, rollout_count: int, horizon_months: int, seed: int, market_request: MarketRequest
        ) -> MarketBundle:
            self.calls += 1
            return _bundle(rollout_count=rollout_count, horizon_months=horizon_months)

    provider = Provider()
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body("first", private_equity_usd=0), _scenario_body("second", private_equity_usd=0)
        )
    )

    response = simulate_set(scenario_set, market_provider=provider).to_response()

    assert provider.calls == 1
    assert response.scenario_results[0].monthly_columns is not None
    assert response.scenario_results[1].monthly_columns is not None
    assert response.market_metadata is not None
    first_sp500 = response.scenario_results[0].monthly_columns.columns["generic_sp500_value_usd"]
    second_sp500 = response.scenario_results[1].monthly_columns.columns["generic_sp500_value_usd"]
    assert first_sp500 == second_sp500
    assert response.scenario_results[0].metric_fan_columns["net_worth_usd"].row_count == 4
    assert response.market_metadata["seed"] == 7
    path_set_id = response.market_metadata["path_set_id"]
    assert path_set_id.startswith("path_set:")
    assert response.market_metadata["evidence_set_id"] == "unknown"
    assert response.market_metadata["calibration_artifact_id"] == "unknown"
    assert response.market_metadata["evidence_set"]["evidence_set_id"] == "unknown"
    assert response.market_metadata["calibration_run"]["evidence_set_id"] == "unknown"
    assert response.market_metadata["scenario_generator_run"]["scenario_generator_run_id"].startswith(
        "scenario_generator_run:"
    )
    assert response.market_metadata["exogenous_path_set"]["path_set_id"] == path_set_id
    assert response.projection_run is not None
    assert response.projection_run.path_set_id == path_set_id
    assert response.projection_run.projection_run_id.startswith("projection_run:")
    assert response.market_metadata["exogenous_path_ids"] == [f"{path_set_id}:path:0", f"{path_set_id}:path:1"]
    assert [path.exogenous_path_id for path in response.exogenous_paths] == response.market_metadata[
        "exogenous_path_ids"
    ]
    assert (
        response.scenario_results[0].projection_trajectories[0].exogenous_path_id
        == response.scenario_results[1].projection_trajectories[0].exogenous_path_id
    )
    assert (
        response.scenario_results[0].projection_trajectories[0].projection_trajectory_id
        != response.scenario_results[1].projection_trajectories[0].projection_trajectory_id
    )
    first_observation = response.scenario_results[0].market_observations[0]
    assert first_observation.path_set_id == path_set_id
    assert first_observation.exogenous_path_id in response.market_metadata["exogenous_path_ids"]
    assert (
        first_observation.projection_trajectory_id
        == response.scenario_results[0]
        .projection_trajectories[first_observation.rollout_index]
        .projection_trajectory_id
    )
    assert response.scenario_results[0].rollout_statuses[0].status == RolloutStatusType.ACTIVE


def test_projection_trajectory_identity_includes_input_snapshot_provenance() -> None:
    base_scenario = _scenario_body("portfolio", private_equity_usd=0)
    sourced_scenario = _scenario_body("portfolio", private_equity_usd=0)
    sourced_scenario["initial_balance_sheet"]["accounts"][0]["provenance"] = {
        "source_id": "wealthfront",
        "snapshot_id": "snapshot-2026-05-16",
        "as_of": "2026-05-16",
    }
    market_bundle = _bundle()

    base_response = simulate_set(
        ScenarioSet.model_validate(_scenario_set_body(base_scenario)), market_bundle=market_bundle
    ).to_response()
    sourced_response = simulate_set(
        ScenarioSet.model_validate(_scenario_set_body(sourced_scenario)), market_bundle=market_bundle
    ).to_response()

    base_trajectory = base_response.scenario_results[0].projection_trajectories[0]
    sourced_trajectory = sourced_response.scenario_results[0].projection_trajectories[0]
    assert base_trajectory.exogenous_path_id == sourced_trajectory.exogenous_path_id
    assert base_trajectory.scenario_input_id != sourced_trajectory.scenario_input_id
    assert base_trajectory.projection_trajectory_id != sourced_trajectory.projection_trajectory_id
    assert base_response.projection_run is not None
    assert sourced_response.projection_run is not None
    assert base_response.projection_run.path_set_id == sourced_response.projection_run.path_set_id
    assert base_response.projection_run.projection_run_id != sourced_response.projection_run.projection_run_id


def test_response_omits_rollout_status_summary_next_to_full_statuses() -> None:
    disabled = _scenario_body("disabled", private_equity_usd=0)
    disabled["enabled"] = False
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(disabled, _scenario_body("enabled", private_equity_usd=0))
    )

    response = simulate_set(scenario_set, market_bundle=_bundle()).to_response()

    disabled_result = response.scenario_results[0]
    assert disabled_result.rollout_statuses == ()
    disabled_dumped = disabled_result.model_dump(mode="json", exclude_none=True)
    assert "rollout_status_summary" not in disabled_dumped

    enabled_result = response.scenario_results[1]
    assert [status.status for status in enabled_result.rollout_statuses] == [RolloutStatusType.ACTIVE] * 2
    enabled_dumped = enabled_result.model_dump(mode="json", exclude_none=True)
    assert "rollout_status_summary" not in enabled_dumped


def test_property_purchase_with_mortgage_tracks_debt_and_equity() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "sf_house",
                cash_usd=300_000,
                sp500_usd=0,
                private_equity_usd=0,
                actors=[
                    {"actor_id": "owner", "label": "Owner", "role": "primary_owner"},
                    {"actor_id": "occupant", "label": "Occupant", "role": "equity_building_occupant"},
                ],
                property_selection={
                    "property_id": "sf_ashton",
                    "location_id": "san_francisco_ca",
                    "purchase_price_usd": 1_000_000,
                },
                financing={"financing_mode": "fixed_30", "down_payment_pct": 20, "mortgage_rate_pct": 6},
            )
        )
    )

    no_opportunity = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())
    assert_allclose(no_opportunity.private_equity_sale_usd, 0)
    assert_allclose(no_opportunity.private_equity_sale_opportunity_value_usd, 0)

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle(private_equity_sale_opportunity_month=2))

    assert_allclose(result.purchase_closing_cost_usd[:, 0], 25_000)
    assert_allclose(result.cash_usd[:, 0], 75_000)
    assert_allclose(result.property_value_usd[:, 0], 1_000_000)
    assert_allclose(result.mortgage_balance_usd[:, 0], 800_000)
    assert_allclose(result.home_equity_usd[:, 0], 200_000)
    assert np.all(result.mortgage_interest_usd[:, 1] > 0)
    assert np.all(result.mortgage_principal_usd[:, 1] > 0)
    assert np.all(result.mortgage_balance_usd[:, 1] < 800_000)
    assert np.all(result.partner_present)
    assert_allclose(result.partner_home_equity_claim_usd, 0)


def test_property_purchase_with_cash_financing_has_no_mortgage() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "cash_house",
                cash_usd=1_250_000,
                sp500_usd=0,
                private_equity_usd=0,
                property_selection={
                    "property_id": "vallejo_calhoun",
                    "location_id": "vallejo_ca",
                    "purchase_price_usd": 750_000,
                },
                financing={"financing_mode": "cash"},
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())

    assert_allclose(result.purchase_closing_cost_usd[:, 0], 18_750)
    assert_allclose(result.cash_usd[:, 0], 481_250)
    assert_allclose(result.mortgage_balance_usd, 0)
    assert_allclose(result.mortgage_interest_usd, 0)
    assert_allclose(result.home_equity_usd[:, 0], 750_000)


def test_purchase_closing_cost_reduces_month_zero_cash() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "purchase_closing",
                cash_usd=130_000,
                sp500_usd=0,
                private_equity_usd=0,
                property_selection={
                    "property_id": "vallejo_calhoun",
                    "location_id": "vallejo_ca",
                    "purchase_price_usd": 100_000,
                },
                financing={"financing_mode": "cash"},
                transaction_costs={"closing_cost_buy_pct": 2.0},
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())

    assert_allclose(result.purchase_closing_cost_usd[:, 0], 2_000)
    assert_allclose(result.purchase_closing_cost_usd[:, 1:], 0)
    assert_allclose(result.cash_usd[:, 0], 28_000)


def test_terminal_property_sale_proceeds_pay_off_debt() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "sale_debt_payoff",
                cash_usd=100_000,
                sp500_usd=0,
                private_equity_usd=0,
                property_selection={
                    "property_id": "vallejo_calhoun",
                    "location_id": "vallejo_ca",
                    "purchase_price_usd": 100_000,
                },
                financing={"financing_mode": "fixed_30", "down_payment_pct": 50, "mortgage_rate_pct": 0},
                transaction_costs={"closing_cost_sell_pct": 5.0},
                events=[
                    {
                        "event_id": "sale",
                        "event_type": "property_sale",
                        "month_index": 3,
                        "property_id": "vallejo_calhoun",
                    }
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle(home_path=(1.0, 1.0, 1.1, 1.2)))

    expected_gross = 120_000
    expected_sale_cost = 6_000
    expected_debt_payoff = result.mortgage_balance_usd[:, 3]
    expected_adjusted_basis = 102_500
    expected_realized_gain = expected_gross - expected_sale_cost - expected_adjusted_basis
    assert_allclose(result.property_sale_gross_usd[:, 3], expected_gross)
    assert_allclose(result.sale_closing_cost_usd[:, 3], expected_sale_cost)
    assert_allclose(result.property_sale_debt_payoff_usd[:, 3], expected_debt_payoff)
    assert_allclose(result.property_sale_adjusted_basis_usd[:, 3], expected_adjusted_basis)
    assert_allclose(result.property_sale_capital_gain_usd[:, 3], expected_realized_gain)
    assert_allclose(result.property_sale_capital_gain_exclusion_usd[:, 3], expected_realized_gain)
    assert_allclose(result.taxable_property_capital_gain_usd[:, 3], 0)
    assert_allclose(
        result.property_sale_net_proceeds_usd[:, 3], expected_gross - expected_sale_cost - expected_debt_payoff
    )
    assert_allclose(result.net_property_sale_cash_flow_usd[:, 3], result.property_sale_net_proceeds_usd[:, 3])
    sale_actions = [action for action in result.actions if isinstance(action, SettlePropertySaleAction)]
    assert len(sale_actions) == 2
    assert {action.rollout_index for action in sale_actions} == {0, 1}
    for action in sale_actions:
        assert action.event_id == "sale"
        assert action.property_id == "vallejo_calhoun"
        assert action.gross_sale_usd == expected_gross
        assert action.selling_cost_usd == expected_sale_cost
        assert action.adjusted_basis_usd == expected_adjusted_basis
        assert_allclose(action.debt_payoff_usd, expected_debt_payoff[action.rollout_index])
        assert action.capital_gain_exclusion_usd == expected_realized_gain
        assert action.taxable_capital_gain_usd == 0
        assert_allclose(action.net_proceeds_usd, result.property_sale_net_proceeds_usd[action.rollout_index, 3])


def test_location_local_regulation_drives_property_tax() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "vallejo_mainland",
                cash_usd=200_000,
                sp500_usd=0,
                private_equity_usd=0,
                property_selection={
                    "property_id": "vallejo_calhoun",
                    "location_id": "vallejo_ca",
                    "purchase_price_usd": 100_000,
                },
                financing={"financing_mode": "cash"},
                events=[
                    {
                        "event_id": "sale",
                        "event_type": "property_sale",
                        "month_index": 3,
                        "property_id": "vallejo_calhoun",
                    }
                ],
            ),
            _scenario_body(
                "mare_island",
                cash_usd=200_000,
                sp500_usd=0,
                private_equity_usd=0,
                property_selection={
                    "property_id": "vallejo_lighthouse",
                    "location_id": "mare_island_vallejo_ca",
                    "purchase_price_usd": 100_000,
                },
                financing={"financing_mode": "cash"},
                events=[
                    {
                        "event_id": "sale",
                        "event_type": "property_sale",
                        "month_index": 3,
                        "property_id": "vallejo_lighthouse",
                    }
                ],
            ),
        )
    )

    mainland = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())
    mare_island = run_scenario_vectorized(scenario_set.scenarios[1], _bundle())

    assert_allclose(mainland.property_tax_usd[:, 1], 100_000 * 0.011 / 12)
    assert_allclose(mare_island.property_tax_usd[:, 1], 100_000 * 0.024 / 12)


def test_property_sale_stops_operating_cash_flows_after_sale_month() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "early_sale",
                cash_usd=150_000,
                sp500_usd=0,
                private_equity_usd=0,
                property_selection={
                    "property_id": "sf_ashton",
                    "location_id": "san_francisco_ca",
                    "purchase_price_usd": 100_000,
                },
                financing={"financing_mode": "cash"},
                property_assumptions={"insurance_annual_usd": 1_200, "maintenance_pct": 1},
                rental_plan={
                    "rental_mode": "rent_whole_property",
                    "start_month": 1,
                    "end_month": 3,
                    "monthly_rent_usd": 2_000,
                },
                events=[
                    {"event_id": "sale", "event_type": "property_sale", "month_index": 1, "property_id": "sf_ashton"}
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())

    assert np.all(result.rental_income_usd[:, 1] > 0)
    assert np.all(result.property_carrying_cost_usd[:, 1] > 0)
    assert_allclose(result.rental_income_usd[:, 2:], 0)
    assert_allclose(result.property_tax_usd[:, 2:], 0)
    assert_allclose(result.hoa_usd[:, 2:], 0)
    assert_allclose(result.insurance_usd[:, 2:], 0)
    assert_allclose(result.maintenance_usd[:, 2:], 0)
    assert_allclose(result.net_property_cash_flow_usd[:, 2:], 0)


def test_capital_gains_exclusion_offsets_property_sale_gain() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "excluded_gain",
                cash_usd=150_000,
                sp500_usd=0,
                private_equity_usd=0,
                property_selection={
                    "property_id": "sf_ashton",
                    "location_id": "san_francisco_ca",
                    "purchase_price_usd": 100_000,
                },
                financing={"financing_mode": "cash"},
                transaction_costs={"closing_cost_buy_pct": 0, "closing_cost_sell_pct": 0},
                tax_profile={"filing_status": "single"},
                events=[
                    {"event_id": "sale", "event_type": "property_sale", "month_index": 3, "property_id": "sf_ashton"}
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle(home_path=(1.0, 1.0, 1.5, 2.0)))

    assert_allclose(result.realized_property_gain_usd[:, 3], 100_000)
    assert_allclose(result.depreciation_recapture_usd[:, 3], 0)
    assert_allclose(result.taxable_property_gain_usd[:, 3], 0)
    assert_allclose(result.property_sale_tax_usd[:, 3], 0)


def test_rental_depreciation_recaptures_on_sale() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "depreciation_recapture",
                cash_usd=150_000,
                sp500_usd=0,
                private_equity_usd=0,
                property_selection={
                    "property_id": "vallejo_calhoun",
                    "location_id": "vallejo_ca",
                    "purchase_price_usd": 100_000,
                },
                financing={"financing_mode": "cash"},
                transaction_costs={"closing_cost_buy_pct": 0, "closing_cost_sell_pct": 0},
                property_assumptions={"depreciable_basis_pct": 100},
                tax_profile={"annual_ordinary_income_usd": 100_000, "filing_status": "single"},
                rental_plan={
                    "rental_mode": "rent_whole_property",
                    "start_month": 1,
                    "end_month": 3,
                    "monthly_rent_usd": 0,
                },
                events=[
                    {
                        "event_id": "sale",
                        "event_type": "property_sale",
                        "month_index": 3,
                        "property_id": "vallejo_calhoun",
                    }
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())

    expected_monthly_depreciation = 100_000 / (27.5 * 12)
    expected_cumulative_depreciation = expected_monthly_depreciation * 3
    assert_allclose(result.property_depreciation_usd[:, 1:4], expected_monthly_depreciation)
    assert_allclose(result.cumulative_property_depreciation_usd[:, 3], expected_cumulative_depreciation)
    assert_allclose(result.realized_property_gain_usd[:, 3], expected_cumulative_depreciation)
    assert_allclose(result.depreciation_recapture_usd[:, 3], expected_cumulative_depreciation)
    assert_allclose(result.taxable_property_gain_usd[:, 3], expected_cumulative_depreciation)
    # Recapture tax allocated to the sale month: bracket-aware federal + California,
    # net of the SALT and qualified-residence-interest deductions that lower the
    # year's ordinary income on which the recapture stacks.
    sale_month_tax = result.property_sale_tax_usd[:, 3]
    expected_ca_marginal_rate = 0.093
    expected_federal_marginal_rate = 0.22  # ordinary income at \$100k baseline lands in the 22% bracket
    upper_bound_tax = expected_cumulative_depreciation * (expected_federal_marginal_rate + expected_ca_marginal_rate)
    lower_bound_tax = expected_cumulative_depreciation * (0.10 + expected_ca_marginal_rate)
    assert np.all(sale_month_tax > lower_bound_tax)
    assert np.all(sale_month_tax < upper_bound_tax)


def test_no_property_scenario_ignores_real_estate_tax_accounting_parameters() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "no_property_sale_params",
                events=[{"event_id": "sale", "event_type": "property_sale", "month_index": 3}],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle(home_path=(1.0, 2.0, 3.0, 4.0)))

    assert_allclose(result.purchase_closing_cost_usd, 0)
    assert_allclose(result.sale_closing_cost_usd, 0)
    assert_allclose(result.property_depreciation_usd, 0)
    assert_allclose(result.property_sale_gross_usd, 0)
    assert_allclose(result.property_sale_net_proceeds_usd, 0)
    assert_allclose(result.property_sale_tax_usd, 0)
    assert_allclose(result.net_property_sale_cash_flow_usd, 0)
    assert_allclose(result.cash_usd[:, 0], 10_000)
    assert_allclose(result.cash_usd[:, 3], 10_000)


def test_checking_floor_policy_sells_public_stock_with_basis_placeholder() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "sell_stock",
                cash_usd=5_000,
                sp500_usd=50_000,
                sp500_basis_usd=25_000,
                private_equity_usd=0,
                policies=[
                    {
                        "policy_id": "checking_floor",
                        "policy_type": "checking_floor_sell_public_stock",
                        "actor_id": "owner",
                        "floor_usd": 10_000,
                        "sale_amount_usd": 20_000,
                    }
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())

    assert_allclose(result.generic_sp500_sale_usd[:, 0], 20_000)
    assert_allclose(result.generic_sp500_sale_basis_usd[:, 0], 10_000)
    assert_allclose(result.generic_sp500_sale_gain_usd[:, 0], 10_000)
    assert_allclose(result.generic_sp500_sale_tax_usd[:, 0], 42.94)
    assert_allclose(result.checking_floor_action_usd[:, 0], 20_000)
    assert_allclose(result.checking_floor_shortfall_usd[:, 0], 0)
    assert_allclose(result.cash_usd[:, 0], 25_000 - 42.94)
    assert_allclose(result.generic_sp500_value_usd[:, 0], 30_000)
    assert_allclose(result.generic_sp500_sale_usd[:, 1:], 0)
    assert np.all(result.generic_sp500_value_usd[:, 1] > result.generic_sp500_value_usd[:, 0])
    assert len(result.actions) == 2
    assert {action.rollout_index for action in result.actions} == {0, 1}
    for action in result.actions:
        assert action.action_type is ActionType.SELL_SP500
        assert action.month_index == 0
        assert action.actor_id == "owner"
        assert action.policy_id == "checking_floor"
        assert action.amount_usd == 20_000
        assert_allclose(action.after_tax_proceeds_usd, 20_000 - 42.94)
        assert action.basis_usd == 10_000
        assert action.gain_usd == 10_000
        assert_allclose(action.tax_usd, 42.94)
        assert action.shortfall_usd == 0


def test_scenario_set_response_serializes_discriminated_actions() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "sell_stock",
                cash_usd=5_000,
                sp500_usd=50_000,
                policies=[
                    {
                        "policy_id": "checking_floor",
                        "policy_type": "checking_floor_sell_public_stock",
                        "actor_id": "owner",
                        "floor_usd": 10_000,
                        "sale_amount_usd": 20_000,
                    }
                ],
            )
        )
    )

    response = simulate_set(scenario_set, market_bundle=_bundle()).to_response()
    payload = response.model_dump(mode="json")

    action = payload["scenario_results"][0]["actions"][0]
    assert action["action_type"] == "sell_sp500"
    assert action["amount_usd"] == 20_000


def test_checking_floor_policy_does_not_sell_when_cash_is_above_floor() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "no_sale",
                cash_usd=12_000,
                sp500_usd=50_000,
                private_equity_usd=0,
                policies=[
                    {
                        "policy_id": "checking_floor",
                        "policy_type": "checking_floor_sell_public_stock",
                        "actor_id": "owner",
                        "floor_usd": 10_000,
                        "sale_amount_usd": 20_000,
                    }
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())

    assert_allclose(result.generic_sp500_sale_usd, 0)
    assert_allclose(result.checking_floor_shortfall_usd, 0)
    assert_allclose(result.cash_usd, 12_000)
    assert_allclose(result.generic_sp500_value_usd[:, 2], 60_000)


def test_checking_floor_policy_reports_shortfall_when_public_stock_is_exhausted() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "shortfall",
                cash_usd=0,
                sp500_usd=5_000,
                sp500_basis_usd=1_000,
                private_equity_usd=0,
                policies=[
                    {
                        "policy_id": "checking_floor",
                        "policy_type": "checking_floor_sell_public_stock",
                        "actor_id": "owner",
                        "floor_usd": 10_000,
                        "sale_amount_usd": 20_000,
                    }
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())

    assert_allclose(result.generic_sp500_sale_usd[:, 0], 5_000)
    assert_allclose(result.generic_sp500_sale_basis_usd[:, 0], 1_000)
    assert_allclose(result.cash_usd[:, 0], 5_000)
    assert_allclose(result.generic_sp500_value_usd, 0)
    assert_allclose(result.checking_floor_shortfall_usd[:, 0], 5_000)


def test_cross_type_policy_order_changes_cash_management_result() -> None:
    spend_policy = {
        "policy_id": "living_expenses",
        "policy_type": "monthly_spend",
        "actor_id": "owner",
        "monthly_spend_usd": 10_000,
        "inflation_adjusted": False,
    }
    checking_floor_policy = {
        "policy_id": "checking_floor",
        "policy_type": "checking_floor_sell_public_stock",
        "actor_id": "owner",
        "floor_usd": 10_000,
        "sale_amount_usd": 20_000,
    }

    def run_ordered(policies: list[dict]):
        scenario_set = ScenarioSet.model_validate(
            _scenario_set_body(
                _scenario_body(
                    "policy_order",
                    cash_usd=15_000,
                    sp500_usd=100_000,
                    sp500_basis_usd=100_000,
                    private_equity_usd=0,
                    policies=policies,
                )
            )
        )
        return run_scenario_vectorized(scenario_set.scenarios[0], _bundle(horizon_months=1, sp500_path=(1.0, 1.0)))

    spend_then_sale = run_ordered([spend_policy, checking_floor_policy])
    sale_then_spend = run_ordered([checking_floor_policy, spend_policy])

    assert_allclose(spend_then_sale.cash_usd[:, 1], 25_000)
    assert_allclose(spend_then_sale.generic_sp500_sale_usd[:, 1], 20_000)
    assert_allclose(spend_then_sale.generic_sp500_value_usd[:, 1], 80_000)
    assert_allclose(sale_then_spend.cash_usd[:, 1], 5_000)
    assert_allclose(sale_then_spend.generic_sp500_sale_usd[:, 1], 0)
    assert_allclose(sale_then_spend.generic_sp500_value_usd[:, 1], 100_000)

    spend_then_sale_decisions = [
        decision
        for decision in spend_then_sale.policy_decisions
        if decision.month_index == 1 and decision.rollout_index == 0
    ]
    assert [decision.policy_id for decision in spend_then_sale_decisions] == ["living_expenses", "checking_floor"]
    assert [decision.policy_sequence_index for decision in spend_then_sale_decisions] == [0, 1]
    assert isinstance(spend_then_sale_decisions[0], MonthlySpendDecision)
    assert isinstance(spend_then_sale_decisions[1], SellPublicStockDecision)

    sale_then_spend_decisions = [
        decision
        for decision in sale_then_spend.policy_decisions
        if decision.month_index == 1 and decision.rollout_index == 0
    ]
    assert [decision.policy_id for decision in sale_then_spend_decisions] == ["living_expenses"]
    assert [decision.policy_sequence_index for decision in sale_then_spend_decisions] == [1]


def test_partner_equity_accrues_from_principal_then_freezes_and_participates_in_appreciation() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "occupant",
                cash_usd=40_000,
                sp500_usd=0,
                private_equity_usd=0,
                actors=[
                    {"actor_id": "owner", "label": "Owner", "role": "primary_owner"},
                    {"actor_id": "occupant", "label": "Occupant", "role": "equity_building_occupant"},
                ],
                property_selection={
                    "property_id": "vallejo_calhoun",
                    "location_id": "vallejo_ca",
                    "purchase_price_usd": 100_000,
                },
                financing={"financing_mode": "fixed_30", "down_payment_pct": 20, "mortgage_rate_pct": 0},
                occupancy_plan={"occupancy_mode": "owner_lives_in_property", "start_month": 0, "end_month": 2},
                policies=[
                    {
                        "policy_id": "partner_equity",
                        "policy_type": "partner_equity_accrual",
                        "actor_id": "occupant",
                        "base_monthly_payment_usd": 1_000,
                        "grow_with_inflation": False,
                    }
                ],
            )
        )
    )

    result = run_scenario_vectorized(
        scenario_set.scenarios[0],
        _bundle(
            horizon_months=4,
            inflation_path=(1.0, 1.0, 1.0, 1.0, 1.0),
            sp500_path=(1.0, 1.0, 1.0, 1.0, 1.0),
            private_equity_path=(1.0, 1.0, 1.0, 1.0, 1.0),
            home_path=(1.0, 1.0, 1.0, 1.2, 1.5),
            rent_path=(1.0, 1.0, 1.0, 1.0, 1.0),
        ),
    )

    assert np.all(result.partner_contribution_used_usd[:, 1:3] > result.mortgage_principal_usd[:, 1:3])
    assert_allclose(result.partner_contribution_usd[:, 3:], 0)
    assert np.all(result.partner_ownership_pct[:, 2] > 0)
    assert_allclose(result.partner_ownership_pct[:, 3], result.partner_ownership_pct[:, 2])
    assert_allclose(result.partner_ownership_pct[:, 4], result.partner_ownership_pct[:, 2])
    assert np.all(result.partner_home_equity_claim_usd[:, 4] > result.partner_home_equity_claim_usd[:, 2])
    assert_allclose(result.owner_home_equity_claim_usd + result.partner_home_equity_claim_usd, result.home_equity_usd)
    payment_actions = [action for action in result.actions if isinstance(action, TransferPartnerContributionAction)]
    mortgage_actions = [action for action in result.actions if isinstance(action, PayMortgageAction)]
    equity_actions = [action for action in result.actions if isinstance(action, AccruePartnerEquityAction)]
    assert len(payment_actions) == 4
    assert len(mortgage_actions) == 8
    assert len(equity_actions) == 4
    assert {action.month_index for action in payment_actions} == {1, 2}
    assert {action.month_index for action in mortgage_actions} == {1, 2, 3, 4}
    assert {action.month_index for action in equity_actions} == {1, 2}
    for payment in payment_actions:
        assert payment.actor_id == "occupant"
        assert payment.policy_id == "partner_equity"
        assert payment.recipient_actor_id == "owner"
        assert payment.amount_usd == 1_000
        assert payment.applied_to_house_costs_usd > 0
    for mortgage in mortgage_actions:
        assert mortgage.actor_id == "owner"
        assert mortgage.policy_id == "mortgage_servicing"
        assert mortgage.mortgage_payment_usd > 0
        assert mortgage.mortgage_interest_usd == 0
        assert mortgage.mortgage_principal_usd > 0
        assert mortgage.mortgage_balance_after_usd < 80_000
    for equity in equity_actions:
        assert equity.actor_id == "occupant"
        assert equity.policy_id == "partner_equity"
        assert equity.beneficiary_actor_id == "occupant"
        assert equity.property_id == "vallejo_calhoun"
        assert equity.house_costs_usd > equity.mortgage_principal_usd
        assert equity.cash_transfer_used_for_house_costs_usd > equity.principal_credit_usd
        assert equity.principal_credit_usd == equity.mortgage_principal_usd
        assert equity.ownership_pct_after > 0


def test_multiple_partner_equity_policies_execute_in_actor_program_order() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "two_occupants",
                cash_usd=40_000,
                sp500_usd=0,
                private_equity_usd=0,
                actors=[
                    {"actor_id": "owner", "label": "Owner", "role": "primary_owner"},
                    {"actor_id": "partner_a", "label": "Partner A", "role": "equity_building_occupant"},
                    {"actor_id": "partner_b", "label": "Partner B", "role": "equity_building_occupant"},
                ],
                property_selection={
                    "property_id": "vallejo_calhoun",
                    "location_id": "vallejo_ca",
                    "purchase_price_usd": 100_000,
                },
                financing={"financing_mode": "fixed_30", "down_payment_pct": 20, "mortgage_rate_pct": 0},
                property_assumptions={"insurance_annual_usd": 0, "maintenance_pct": 0},
                policies=[
                    {
                        "policy_id": "partner_b_equity",
                        "policy_type": "partner_equity_accrual",
                        "actor_id": "partner_b",
                        "base_monthly_payment_usd": 50,
                        "occupied_months": 2,
                        "grow_with_inflation": False,
                    },
                    {
                        "policy_id": "partner_a_equity",
                        "policy_type": "partner_equity_accrual",
                        "actor_id": "partner_a",
                        "base_monthly_payment_usd": 50,
                        "occupied_months": 2,
                        "grow_with_inflation": False,
                    },
                ],
            )
        )
    )

    result = run_scenario_vectorized(
        scenario_set.scenarios[0],
        _bundle(
            rollout_count=1,
            horizon_months=2,
            inflation_path=(1.0, 1.0, 1.0),
            sp500_path=(1.0, 1.0, 1.0),
            private_equity_path=(1.0, 1.0, 1.0),
            home_path=(1.0, 1.0, 1.0),
            rent_path=(1.0, 1.0, 1.0),
        ),
    )

    assert_allclose(result.partner_contribution_usd[:, 1:], 100)
    assert np.all(result.partner_principal_credit_usd[:, 1:] > 0)
    assert np.all(result.partner_equity_ledger_usd[:, 2] > result.partner_equity_ledger_usd[:, 1])
    account_by_id = {account.chart_account_id: account for account in result.chart_accounts}
    owner_principal_rows = [
        posting
        for posting in result.postings
        if account_by_id[posting.chart_account_id].role is ChartAccountRole.OWNER_PRINCIPAL_CREDIT
        and posting.side is PostingSide.DEBIT
    ]
    assert len(owner_principal_rows) == 2
    assert {account_by_id[posting.chart_account_id].property_id for posting in owner_principal_rows} == {
        "vallejo_calhoun"
    }
    assert_allclose([posting.amount_usd for posting in owner_principal_rows], result.owner_principal_credit_usd[0, 1:])
    partner_principal_rows = [
        posting
        for posting in result.postings
        if account_by_id[posting.chart_account_id].role is ChartAccountRole.PARTNER_PRINCIPAL_CREDIT
        and posting.side is PostingSide.DEBIT
    ]
    assert len(partner_principal_rows) == 4
    assert {account_by_id[posting.chart_account_id].property_id for posting in partner_principal_rows} == {
        "vallejo_calhoun"
    }
    transfers = [action for action in result.actions if isinstance(action, TransferPartnerContributionAction)]
    assert [(action.month_index, action.actor_id, action.amount_usd) for action in transfers] == [
        (1, "partner_a", 50),
        (1, "partner_b", 50),
        (2, "partner_a", 50),
        (2, "partner_b", 50),
    ]


def test_partner_equity_arrays_match_rows_from_same_application() -> None:
    # cleanup_audit item 3 "prove safe by": partner arrays and detail rows must come
    # from the same PartnerOwnershipAccrualApplication / aggregate, not from a parallel
    # recorder. Compare engine-reported partner arrays (which the engine takes directly
    # off the application object) to the journal-entry / balance-snapshot rows
    # (which the recorder writes through from the same application's outputs).
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "occupant",
                cash_usd=40_000,
                sp500_usd=0,
                private_equity_usd=0,
                actors=[
                    {"actor_id": "owner", "label": "Owner", "role": "primary_owner"},
                    {"actor_id": "occupant", "label": "Occupant", "role": "equity_building_occupant"},
                ],
                property_selection={
                    "property_id": "vallejo_calhoun",
                    "location_id": "vallejo_ca",
                    "purchase_price_usd": 100_000,
                },
                financing={"financing_mode": "fixed_30", "down_payment_pct": 20, "mortgage_rate_pct": 0},
                property_assumptions={"insurance_annual_usd": 0, "maintenance_pct": 0},
                occupancy_plan={"occupancy_mode": "owner_lives_in_property", "start_month": 0, "end_month": 2},
                policies=[
                    {
                        "policy_id": "partner_equity",
                        "policy_type": "partner_equity_accrual",
                        "actor_id": "occupant",
                        "base_monthly_payment_usd": 1_000,
                        "grow_with_inflation": False,
                    }
                ],
            )
        )
    )

    result = run_scenario_vectorized(
        scenario_set.scenarios[0],
        _bundle(
            rollout_count=1,
            horizon_months=3,
            inflation_path=(1.0, 1.0, 1.0, 1.0),
            sp500_path=(1.0, 1.0, 1.0, 1.0),
            private_equity_path=(1.0, 1.0, 1.0, 1.0),
            home_path=(1.0, 1.0, 1.0, 1.2),
            rent_path=(1.0, 1.0, 1.0, 1.0),
        ),
    )

    account_by_id = {account.chart_account_id: account for account in result.chart_accounts}

    def snapshot_matrix(role: ChartAccountRole) -> np.ndarray:
        matrix = np.zeros_like(result.partner_equity_ledger_usd)
        for snapshot in result.balance_snapshots:
            if account_by_id[snapshot.chart_account_id].role is role:
                matrix[snapshot.rollout_index, snapshot.month_index] += snapshot.balance_usd
        return matrix

    def posting_matrix(role: ChartAccountRole, side: PostingSide) -> np.ndarray:
        matrix = np.zeros_like(result.partner_principal_credit_usd)
        for posting in result.postings:
            account = account_by_id[posting.chart_account_id]
            if account.role is role and posting.side is side:
                matrix[posting.rollout_index, posting.month_index] += posting.amount_usd
        return matrix

    # Partner-side: the engine takes these arrays straight off the per-agreement
    # PartnerOwnershipAccrualApplication, and the same application's balance_snapshots
    # are what get recorded. Equality here proves there is no parallel array
    # reconstruction in the engine.
    assert_allclose(snapshot_matrix(ChartAccountRole.PARTNER_EQUITY_LEDGER), result.partner_equity_ledger_usd)
    assert_allclose(snapshot_matrix(ChartAccountRole.PARTNER_HOME_EQUITY_CLAIM), result.partner_home_equity_claim_usd)
    assert_allclose(
        posting_matrix(ChartAccountRole.PARTNER_PRINCIPAL_CREDIT, PostingSide.DEBIT),
        result.partner_principal_credit_usd,
    )
    # Owner-side: same property, produced once by apply_partner_ownership_aggregate.
    assert_allclose(snapshot_matrix(ChartAccountRole.OWNER_EQUITY_LEDGER), result.owner_equity_ledger_usd)
    assert_allclose(snapshot_matrix(ChartAccountRole.OWNER_HOME_EQUITY_CLAIM), result.owner_home_equity_claim_usd)
    assert_allclose(
        posting_matrix(ChartAccountRole.OWNER_PRINCIPAL_CREDIT, PostingSide.DEBIT), result.owner_principal_credit_usd
    )


def test_rental_income_and_carrying_costs_feed_cash_flow() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "rental",
                cash_usd=100_000,
                sp500_usd=0,
                private_equity_usd=0,
                property_selection={
                    "property_id": "vallejo_calhoun",
                    "location_id": "vallejo_ca",
                    "purchase_price_usd": 300_000,
                },
                financing={"financing_mode": "fixed_30", "down_payment_pct": 20, "mortgage_rate_pct": 0},
                property_assumptions={"insurance_annual_usd": 1_200, "maintenance_pct": 1.2},
                rental_plan={
                    "rental_mode": "rent_whole_property",
                    "start_month": 1,
                    "end_month": 3,
                    "monthly_rent_usd": 2_000,
                    "vacancy_pct": 10,
                    "management_fee_pct": 5,
                    "leasing_fee_pct": 12,
                },
                events=[
                    {
                        "event_id": "purchase",
                        "event_type": "property_purchase",
                        "month_index": 0,
                        "property_id": "vallejo_calhoun",
                        "hoa_monthly_usd": 100,
                    }
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle(rent_path=(1.0, 1.0, 1.1, 1.2)))

    expected_month_1_income = 2_000 * 0.9
    expected_month_1_management = expected_month_1_income * 0.05
    expected_month_1_leasing = expected_month_1_income * 0.12 / 12
    assert_allclose(result.rental_income_usd[:, 1], expected_month_1_income)
    assert_allclose(result.rental_management_fee_usd[:, 1], expected_month_1_management)
    assert_allclose(result.rental_leasing_fee_usd[:, 1], expected_month_1_leasing)
    assert_allclose(result.property_tax_usd[:, 1], 275)
    assert_allclose(result.hoa_usd[:, 1], 100)
    assert_allclose(result.insurance_usd[:, 1], 100)
    assert_allclose(result.maintenance_usd[:, 1], 300)
    assert_allclose(
        result.property_carrying_cost_usd[:, 1],
        275 + 100 + 100 + 300 + expected_month_1_management + expected_month_1_leasing,
    )
    assert_allclose(
        result.net_property_cash_flow_usd,
        result.rental_income_usd - result.property_carrying_cost_usd - result.mortgage_payment_usd,
    )
    assert_allclose(result.cash_usd[:, 1], result.cash_usd[:, 0] + result.net_property_cash_flow_usd[:, 1])


def test_purchase_event_parameters_drive_property_costs() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "event_costs",
                cash_usd=200_000,
                sp500_usd=0,
                private_equity_usd=0,
                property_selection={
                    "property_id": "sf_ashton",
                    "location_id": "san_francisco_ca",
                    "purchase_price_usd": 120_000,
                },
                financing={"financing_mode": "cash"},
                property_assumptions={"insurance_annual_usd": 600, "maintenance_pct": 1.0},
                events=[
                    {
                        "event_id": "purchase",
                        "event_type": "property_purchase",
                        "month_index": 0,
                        "actor_id": "owner",
                        "property_id": "sf_ashton",
                        "amount_usd": 120_000,
                        "hoa_monthly_usd": 250,
                    }
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())

    assert_allclose(result.property_tax_usd[:, 1], 118)
    assert_allclose(result.hoa_usd[:, 1], 250)
    assert_allclose(result.insurance_usd[:, 1], 50)
    assert_allclose(result.maintenance_usd[:, 1], 100)
    assert_allclose(result.cash_usd[:, 1], result.cash_usd[:, 0] - 518)


def test_private_equity_stock_is_not_sold_without_policy() -> None:
    scenario_set = ScenarioSet.model_validate(_scenario_set_body(_scenario_body("no_sale_policy")))

    market_bundle = _bundle(private_equity_sale_opportunity_month=1)
    result = run_scenario_vectorized(scenario_set.scenarios[0], market_bundle)

    _assert_liquid_net_worth_matches_cash_and_public_stock(result)
    assert_allclose(result.private_equity_sale_usd, 0)
    assert_allclose(result.private_equity_sale_opportunity_value_usd[:, 1], 50_000)
    assert_allclose(result.cash_usd[:, 1], 10_000)


def test_private_equity_sale_opportunity_without_policy_does_not_sell() -> None:
    scenario_set = ScenarioSet.model_validate(_scenario_set_body(_scenario_body("sale_opportunity_without_policy")))

    market_bundle = _bundle(private_equity_sale_opportunity_month=1)
    result = run_scenario_vectorized(scenario_set.scenarios[0], market_bundle)

    _assert_liquid_net_worth_matches_cash_and_public_stock(result)
    assert_allclose(result.private_equity_sale_usd, 0)
    assert_allclose(result.private_equity_sale_opportunity_value_usd[:, 1], 50_000)
    assert_allclose(result.cash_usd[:, 1], 10_000)
    assert np.all(result.private_equity_sale_opportunity_event[:, 1])
    assert result.actions == ()


def test_private_equity_fixed_sale_into_cash_requires_opportunity_and_policy() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "fixed_sale_into_cash",
                private_equity_basis_usd=0,
                private_equity_units=100,
                policies=[
                    {
                        "policy_id": "private_equity_sale",
                        "policy_type": "private_equity_sale",
                        "actor_id": "owner",
                        "proceeds_destination": "cash",
                        "sale_rule": {"sale_rule_type": "fixed_amount_on_opportunity", "amount_usd": 20_000},
                    }
                ],
            )
        )
    )

    no_opportunity = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())
    assert_allclose(no_opportunity.private_equity_sale_usd, 0)
    assert no_opportunity.actions == ()
    no_opportunity_decisions = [
        decision
        for decision in no_opportunity.policy_decisions
        if isinstance(decision, PrivateEquitySaleDecision)
        and decision.decision_reason is PrivateEquitySaleDecisionReason.NO_SALE_OPPORTUNITY
    ]
    assert len(no_opportunity_decisions) == 8
    assert {decision.opportunity_id for decision in no_opportunity_decisions} == {None}
    no_opportunity_path_set_id = _bundle().metadata.path_set_id
    assert {
        decision.opportunity_cause_id
        for decision in no_opportunity_decisions
        if decision.month_index == 1 and decision.rollout_index == 0
    } == {f"{no_opportunity_path_set_id}:path:0:month:1:private_equity_holding:private_equity:no_sale_opportunity"}

    market_bundle = _bundle(private_equity_sale_opportunity_month=1)
    result = run_scenario_vectorized(scenario_set.scenarios[0], market_bundle)

    _assert_liquid_net_worth_matches_cash_and_public_stock(result)
    assert_allclose(result.private_equity_sale_usd[:, 0], 0)
    assert_allclose(result.private_equity_sale_usd[:, 1], 20_000)
    assert_allclose(result.private_equity_sale_usd[:, 2], 0)
    assert_allclose(result.private_equity_sale_opportunity_value_usd[:, 1], 30_000)
    assert_allclose(result.private_equity_sale_basis_usd[:, 1], 0)
    expected_tax = 175.09
    assert_allclose(result.private_equity_sale_tax_usd[:, 1], expected_tax)
    assert_allclose(result.cash_usd[:, 1], 30_000 - expected_tax)
    actions = [action for action in result.actions if action.action_type is ActionType.SELL_PRIVATE_EQUITY]
    assert len(actions) == 2
    for action in actions:
        expected_opportunity_id = (
            f"{market_bundle.metadata.path_set_id}:path:{action.rollout_index}:month:1:"
            "private_equity_holding:private_equity:sale_opportunity"
        )
        assert action.event_id is None
        assert action.event_type is None
        assert action.opportunity_id == expected_opportunity_id
        assert action.opportunity_cause_id == expected_opportunity_id
        assert action.actor_id == "owner"
        assert action.policy_id == "private_equity_sale"
        assert action.amount_usd == 20_000
        assert_allclose(action.after_tax_proceeds_usd, 20_000 - expected_tax)
        assert action.basis_usd == 0
        assert_allclose(action.estimated_tax_usd, expected_tax)
        assert action.units_sold == 40
        assert action.sold_fraction == 0.4
        assert action.proceeds_destination is AccountType.CHECKING
    sale_decisions = [
        decision
        for decision in result.policy_decisions
        if isinstance(decision, PrivateEquitySaleDecision)
        and decision.month_index == 1
        and decision.decision_reason is PrivateEquitySaleDecisionReason.SALE_REQUESTED
    ]
    assert len(sale_decisions) == 2
    assert {decision.source_asset_id for decision in sale_decisions} == {"private_equity"}
    assert {decision.sale_rule_type for decision in sale_decisions} == {
        PrivateEquitySaleRuleType.FIXED_AMOUNT_ON_OPPORTUNITY
    }
    assert {decision.configured_sale_amount_usd for decision in sale_decisions} == {20_000}
    assert {decision.target_liquid_net_worth_floor_usd for decision in sale_decisions} == {None}
    for decision in sale_decisions:
        expected_opportunity_id = (
            f"{market_bundle.metadata.path_set_id}:path:{decision.rollout_index}:month:1:"
            "private_equity_holding:private_equity:sale_opportunity"
        )
        assert decision.opportunity_id == expected_opportunity_id
        assert decision.opportunity_cause_id == expected_opportunity_id


def test_private_equity_sale_policy_reinvests_sale_proceeds_in_sp500() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "reinvest_fixed_sale",
                policies=[
                    {
                        "policy_id": "private_equity_sale",
                        "policy_type": "private_equity_sale",
                        "actor_id": "owner",
                        "proceeds_destination": "generic_sp500_stock",
                        "sale_rule": {"sale_rule_type": "fixed_amount_on_opportunity", "amount_usd": 20_000},
                    }
                ],
            )
        )
    )

    market_bundle = _bundle(private_equity_sale_opportunity_month=1)
    result = run_scenario_vectorized(scenario_set.scenarios[0], market_bundle)

    _assert_liquid_net_worth_matches_cash_and_public_stock(result)
    assert_allclose(result.private_equity_sale_usd[:, 1], 20_000)
    assert_allclose(result.cash_usd[:, 1], 10_000)
    assert_allclose(result.generic_sp500_value_usd[:, 1], 130_000)
    assert_allclose(result.generic_sp500_value_usd[:, 2], 141_818.18181818)


def test_private_equity_liquid_net_worth_floor_policy_sells_to_sp500_on_opportunity() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "liquid_floor_sale",
                events=[],
                policies=[
                    {
                        "policy_id": "private_equity_liquid_floor_sale",
                        "policy_type": "private_equity_sale",
                        "actor_id": "owner",
                        "proceeds_destination": "generic_sp500_stock",
                        "sale_rule": {
                            "sale_rule_type": "liquid_net_worth_floor",
                            "min_liquid_net_worth_usd": 125_000,
                            "sale_amount_usd": 20_000,
                        },
                    }
                ],
            )
        )
    )

    no_opportunity = run_scenario_vectorized(scenario_set.scenarios[0], _bundle())
    _assert_liquid_net_worth_matches_cash_and_public_stock(no_opportunity)
    assert_allclose(no_opportunity.private_equity_sale_usd, 0)

    market_bundle = _bundle(private_equity_sale_opportunity_month=1)
    result = run_scenario_vectorized(scenario_set.scenarios[0], market_bundle)
    _assert_liquid_net_worth_matches_cash_and_public_stock(result)

    assert_allclose(result.private_equity_sale_usd[:, 1], 20_000)
    assert_allclose(result.cash_usd[:, 1], 10_000)
    assert_allclose(result.generic_sp500_value_usd[:, 1], 130_000)
    assert_allclose(result.liquid_net_worth_usd[:, 1], 140_000)
    sale_decisions = [
        decision
        for decision in result.policy_decisions
        if isinstance(decision, PrivateEquitySaleDecision)
        and decision.month_index == 1
        and decision.decision_reason is PrivateEquitySaleDecisionReason.SALE_REQUESTED
    ]
    assert len(sale_decisions) == 2
    assert {decision.target_liquid_net_worth_floor_usd for decision in sale_decisions} == {125_000}
    assert_allclose([decision.liquid_net_worth_usd for decision in sale_decisions], [120_000, 120_000])
    assert_allclose([decision.sale_opportunity_value_usd for decision in sale_decisions], [50_000, 50_000])
    assert {decision.source_asset_id for decision in sale_decisions} == {"private_equity"}
    assert {decision.sale_rule_type for decision in sale_decisions} == {
        PrivateEquitySaleRuleType.LIQUID_NET_WORTH_FLOOR
    }
    assert {decision.configured_sale_amount_usd for decision in sale_decisions} == {20_000}
    for decision in sale_decisions:
        assert decision.opportunity_id == (
            f"{market_bundle.metadata.path_set_id}:path:{decision.rollout_index}:month:1:"
            "private_equity_holding:private_equity:sale_opportunity"
        )
    opportunity_observations = [
        obs
        for obs in result.market_observations
        if isinstance(obs, PrivateEquitySaleOpportunityObservation) and obs.month_index == 1
    ]
    assert len(opportunity_observations) == 2
    assert {obs.source_asset_id for obs in opportunity_observations} == {"private_equity"}


def test_private_equity_liquid_net_worth_floor_records_policy_not_triggered() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "liquid_floor_not_triggered",
                policies=[
                    {
                        "policy_id": "private_equity_liquid_floor_sale",
                        "policy_type": "private_equity_sale",
                        "actor_id": "owner",
                        "proceeds_destination": "generic_sp500_stock",
                        "sale_rule": {
                            "sale_rule_type": "liquid_net_worth_floor",
                            "min_liquid_net_worth_usd": 100_000,
                            "sale_amount_usd": 20_000,
                        },
                    }
                ],
            )
        )
    )

    market_bundle = _bundle(private_equity_sale_opportunity_month=1)
    result = run_scenario_vectorized(scenario_set.scenarios[0], market_bundle)

    assert_allclose(result.private_equity_sale_usd, 0)
    decisions = [
        decision
        for decision in result.policy_decisions
        if isinstance(decision, PrivateEquitySaleDecision)
        and decision.month_index == 1
        and decision.decision_reason is PrivateEquitySaleDecisionReason.POLICY_NOT_TRIGGERED
    ]
    assert len(decisions) == 2
    assert {decision.target_liquid_net_worth_floor_usd for decision in decisions} == {100_000}
    assert_allclose(result.liquid_net_worth_usd[:, 1], [120_000, 120_000])
    assert_allclose([decision.sale_opportunity_value_usd for decision in decisions], [50_000, 50_000])
    assert_allclose([decision.liquid_net_worth_usd for decision in decisions], [120_000, 120_000])
    assert {decision.source_asset_id for decision in decisions} == {"private_equity"}
    assert {decision.sale_rule_type for decision in decisions} == {PrivateEquitySaleRuleType.LIQUID_NET_WORTH_FLOOR}
    assert {decision.configured_sale_amount_usd for decision in decisions} == {20_000}
    for decision in decisions:
        expected_opportunity_id = (
            f"{market_bundle.metadata.path_set_id}:path:{decision.rollout_index}:month:1:"
            "private_equity_holding:private_equity:sale_opportunity"
        )
        assert decision.opportunity_id == expected_opportunity_id
        assert decision.opportunity_cause_id == expected_opportunity_id


def test_required_tax_obligation_can_be_funded_from_cash_account() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "cash_funded_tax_obligation",
                cash_usd=0,
                sp500_usd=0,
                private_equity_usd=200_000,
                private_equity_basis_usd=0,
                private_equity_units=100,
                policies=[
                    {
                        "policy_id": "private_equity_sale",
                        "policy_type": "private_equity_sale",
                        "actor_id": "owner",
                        "proceeds_destination": "cash",
                        "sale_rule": {"sale_rule_type": "fixed_amount_on_opportunity", "amount_usd": 100_000},
                    }
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle(private_equity_sale_opportunity_month=1))

    assert len(result.obligations) == 2
    assert {obligation.status for obligation in result.obligations} == {ObligationStatus.PAID}
    assert {settlement.status for settlement in result.settlement_results} == {SettlementStatus.PAID}
    assert result.failure_events == ()
    assert [status.status for status in result.rollout_statuses()] == [RolloutStatusType.ACTIVE] * 2
    assert {decision.decision_type for decision in result.funding_decisions} == {FundingDecisionType.USE_CASH}
    assert all(decision.funded_cash_usd > 0 for decision in result.funding_decisions)
    assert {
        (decision.source_type, decision.source_account_id, decision.source_account_type)
        for decision in result.funding_decisions
    } == {(FundingSourceType.CASH_ACCOUNT, "checking", AccountType.CHECKING)}


def test_required_tax_obligation_fails_when_policy_does_not_fund_it() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "unfunded_tax_obligation",
                cash_usd=0,
                sp500_usd=0,
                private_equity_usd=200_000,
                private_equity_basis_usd=0,
                private_equity_units=100,
                policies=[
                    {
                        "policy_id": "private_equity_sale",
                        "policy_type": "private_equity_sale",
                        "actor_id": "owner",
                        "proceeds_destination": "generic_sp500_stock",
                        "sale_rule": {"sale_rule_type": "fixed_amount_on_opportunity", "amount_usd": 100_000},
                    }
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle(private_equity_sale_opportunity_month=1))

    assert len(result.obligations) == 2
    assert {obligation.obligation_type for obligation in result.obligations} == {ObligationType.ANNUAL_TAX_PAYMENT}
    assert {obligation.status for obligation in result.obligations} == {ObligationStatus.UNPAID}
    assert all(obligation.amount_due_usd > 0 for obligation in result.obligations)
    assert_allclose([obligation.amount_paid_usd for obligation in result.obligations], 0)
    assert len(result.failure_events) == 2
    assert {decision.decision_type for decision in result.funding_decisions} == {
        FundingDecisionType.USE_CASH,
        FundingDecisionType.UNFUNDED,
    }
    cash_decisions = [
        decision for decision in result.funding_decisions if decision.decision_type is FundingDecisionType.USE_CASH
    ]
    assert {
        (decision.source_type, decision.source_account_id, decision.source_account_type) for decision in cash_decisions
    } == {(FundingSourceType.CASH_ACCOUNT, "checking", AccountType.CHECKING)}
    unfunded_decisions = [
        decision for decision in result.funding_decisions if decision.decision_type is FundingDecisionType.UNFUNDED
    ]
    assert {decision.source_type for decision in unfunded_decisions} == {FundingSourceType.UNFUNDED}
    assert all(
        decision.source_account_id is None and decision.source_asset_id is None for decision in unfunded_decisions
    )
    assert [status.status for status in result.rollout_statuses()] == [RolloutStatusType.FAILED] * 2
    assert result.rollout_statuses()[0].failed_obligation_count == 1
    assert result.rollout_statuses()[0].unpaid_obligation_usd > 0
    assert_allclose(result.cash_usd[:, 1], 0)


def test_required_tax_obligation_can_be_rescued_by_existing_public_stock_sale_policy() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "funded_tax_obligation",
                cash_usd=0,
                sp500_usd=0,
                private_equity_usd=200_000,
                private_equity_basis_usd=0,
                private_equity_units=100,
                policies=[
                    {
                        "policy_id": "private_equity_sale",
                        "policy_type": "private_equity_sale",
                        "actor_id": "owner",
                        "proceeds_destination": "generic_sp500_stock",
                        "sale_rule": {"sale_rule_type": "fixed_amount_on_opportunity", "amount_usd": 100_000},
                    },
                    {
                        "policy_id": "tax_funding_sale",
                        "policy_type": "checking_floor_sell_public_stock",
                        "actor_id": "owner",
                        "floor_usd": 0,
                        "sale_amount_usd": 20_000,
                    },
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle(private_equity_sale_opportunity_month=1))

    assert len(result.obligations) == 2
    assert {obligation.status for obligation in result.obligations} == {ObligationStatus.PAID}
    assert {settlement.status for settlement in result.settlement_results} == {SettlementStatus.PAID}
    assert result.failure_events == ()
    assert [status.status for status in result.rollout_statuses()] == [RolloutStatusType.ACTIVE] * 2
    sale_decisions = [
        decision
        for decision in result.funding_decisions
        if decision.decision_type is FundingDecisionType.SELL_PUBLIC_STOCK
    ]
    assert len(sale_decisions) == 2
    assert {decision.policy_id for decision in sale_decisions} == {"tax_funding_sale"}
    assert {decision.policy_sequence_index for decision in sale_decisions} == {1}
    assert all(decision.funded_cash_usd > 0 for decision in sale_decisions)
    assert {
        (decision.source_type, decision.source_asset_id, decision.source_asset_type) for decision in sale_decisions
    } == {(FundingSourceType.PUBLIC_MARKET_ASSET, "sp500", AssetType.GENERIC_SP500_STOCK)}
    cash_decisions = [
        decision for decision in result.funding_decisions if decision.decision_type is FundingDecisionType.USE_CASH
    ]
    assert {
        (decision.source_type, decision.source_account_id, decision.source_account_type) for decision in cash_decisions
    } == {(FundingSourceType.CASH_ACCOUNT, "checking", AccountType.CHECKING)}
    assert_allclose(result.cash_usd[:, 1], 20_000 - result.total_income_tax_usd[:, 1])
    assert_allclose(result.generic_sp500_value_usd[:, 1], 80_000)


def test_required_tax_obligation_funding_uses_policy_program_order() -> None:
    scenario_set = ScenarioSet.model_validate(
        _scenario_set_body(
            _scenario_body(
                "ordered_tax_funding",
                cash_usd=0,
                sp500_usd=0,
                private_equity_usd=200_000,
                private_equity_basis_usd=0,
                private_equity_units=100,
                policies=[
                    {
                        "policy_id": "private_equity_sale",
                        "policy_type": "private_equity_sale",
                        "actor_id": "owner",
                        "proceeds_destination": "generic_sp500_stock",
                        "sale_rule": {"sale_rule_type": "fixed_amount_on_opportunity", "amount_usd": 100_000},
                    },
                    {
                        "policy_id": "small_tax_funding_sale",
                        "policy_type": "checking_floor_sell_public_stock",
                        "actor_id": "owner",
                        "floor_usd": 0,
                        "sale_amount_usd": 100,
                    },
                    {
                        "policy_id": "large_tax_funding_sale",
                        "policy_type": "checking_floor_sell_public_stock",
                        "actor_id": "owner",
                        "floor_usd": 0,
                        "sale_amount_usd": 20_000,
                    },
                ],
            )
        )
    )

    result = run_scenario_vectorized(scenario_set.scenarios[0], _bundle(private_equity_sale_opportunity_month=1))

    assert {obligation.status for obligation in result.obligations} == {ObligationStatus.PAID}
    sale_decisions = [
        decision
        for decision in result.funding_decisions
        if decision.decision_type is FundingDecisionType.SELL_PUBLIC_STOCK
    ]
    assert [(decision.policy_id, decision.policy_sequence_index) for decision in sale_decisions] == [
        ("small_tax_funding_sale", 1),
        ("large_tax_funding_sale", 2),
        ("small_tax_funding_sale", 1),
        ("large_tax_funding_sale", 2),
    ]
    assert_allclose(
        [decision.funded_cash_usd for decision in sale_decisions if decision.policy_id == "small_tax_funding_sale"], 100
    )
    assert all(
        decision.funded_cash_usd > 0 for decision in sale_decisions if decision.policy_id == "large_tax_funding_sale"
    )
    assert_allclose(result.generic_sp500_value_usd[:, 1], 79_900)


if __name__ == "__main__":
    pytest_bazel.main()
