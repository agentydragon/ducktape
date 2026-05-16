from __future__ import annotations

import copy
import re
from typing import Any

import pytest
import pytest_bazel
from pydantic import ValidationError

from augur.core.local_regulation import LocationId
from augur.core.scenario_set import (
    AccountType,
    ActorRole,
    AssetType,
    FinancingMode,
    FixedAmountPrivateEquitySaleRule,
    LiquidNetWorthFloorPrivateEquitySaleRule,
    MarketRequest,
    OccupancyMode,
    PolicyType,
    PrivateEquitySalePolicy,
    PrivateEquitySaleProceedsDestination,
    RentalMode,
    RolloutStatus,
    RolloutStatusSummary,
    RolloutStatusType,
    ScenarioAcceptedSummary,
    ScenarioResult,
    ScenarioSet,
    TaxRegime,
)

_SNAKE_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


def _scenario_set_body(*scenario_ids: str) -> dict[str, Any]:
    return {
        "scenario_set_id": "compare_sf_and_vallejo",
        "title": "Compare SF and Vallejo",
        "market_request": {
            "market_model_id": "current_market_model",
            "rollout_count": 32,
            "horizon_months": 120,
            "seed": 7,
        },
        "report_spec": {"percentiles": [5, 50, 95], "include_monthly_columns": True},
        "scenarios": [
            {
                "scenario_id": scenario_id,
                "label": scenario_id.replace("_", " ").title(),
                "enabled": True,
                "color": "#2563eb",
                "actors": [
                    {"actor_id": "owner", "label": "Owner", "role": "primary_owner"},
                    {"actor_id": "occupant", "label": "Occupant", "role": "equity_building_occupant"},
                ],
                "events": [
                    {
                        "event_id": "purchase",
                        "event_type": "property_purchase",
                        "month_index": 0,
                        "property_id": "sf_ashton",
                        "amount_usd": 998000,
                    }
                ],
                "policies": [
                    {
                        "policy_id": "checking_floor",
                        "policy_type": "checking_floor_sell_public_stock",
                        "actor_id": "owner",
                        "floor_usd": 10000,
                        "sale_amount_usd": 20000,
                    }
                ],
                "property_selection": {
                    "property_id": "sf_ashton",
                    "location_id": "san_francisco_ca",
                    "purchase_price_usd": 998000,
                    "tax_regime": "san_francisco_secured_property_tax",
                },
                "financing": {"financing_mode": "fixed_30", "down_payment_pct": 25, "credit_score": 776},
                "occupancy_plan": {
                    "occupancy_mode": "owner_lives_in_property",
                    "owner_residence_property_id": "sf_ashton",
                    "start_month": 0,
                    "end_month": 36,
                },
                "rental_plan": {
                    "rental_mode": "rent_rooms_while_owner_lives_there",
                    "start_month": 0,
                    "end_month": 36,
                    "room_rent_monthly_usd": 1500,
                    "rooms_rented": 1,
                    "room_vacancy_pct": 5,
                    "management_fee_pct": 0,
                    "leasing_fee_pct": 0,
                },
                "initial_balance_sheet": {
                    "accounts": [
                        {
                            "account_id": "checking",
                            "account_type": "checking",
                            "owner_actor_id": "owner",
                            "balance_usd": 25000,
                        }
                    ],
                    "assets": [
                        {
                            "asset_id": "sp500",
                            "asset_type": "generic_sp500_stock",
                            "owner_actor_id": "owner",
                            "value_usd": 2120000,
                            "cost_basis_usd": 1500000,
                        },
                        {
                            "asset_id": "private_equity",
                            "asset_type": "private_equity",
                            "owner_actor_id": "owner",
                            "value_usd": 0,
                            "units": 23553,
                            "cost_basis_usd": 0,
                        },
                    ],
                    "liabilities": [],
                },
                "tax_regimes": [
                    "california_prop13",
                    "san_francisco_secured_property_tax",
                    "federal_mortgage_interest",
                    "primary_residence_exclusion",
                ],
            }
            for scenario_id in scenario_ids
        ],
    }


def _assert_snake_case_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert _SNAKE_KEY.match(key), key
            _assert_snake_case_keys(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _assert_snake_case_keys(item)


def test_scenario_set_accepts_one_and_two_scenarios_with_typed_enums() -> None:
    scenario_set = ScenarioSet.model_validate(_scenario_set_body("sf_house", "vallejo_house"))

    assert [scenario.scenario_id for scenario in scenario_set.scenarios] == ["sf_house", "vallejo_house"]
    first = scenario_set.scenarios[0]
    assert first.actors[0].role is ActorRole.PRIMARY_OWNER
    assert first.initial_balance_sheet.accounts[0].account_type is AccountType.CHECKING
    assert first.initial_balance_sheet.assets[0].asset_type is AssetType.GENERIC_SP500_STOCK
    assert first.property_selection.property_id == "sf_ashton"
    assert first.property_selection.location_id == "san_francisco_ca"
    assert first.financing.financing_mode is FinancingMode.FIXED_30
    assert first.occupancy_plan.occupancy_mode is OccupancyMode.OWNER_LIVES_IN_PROPERTY
    assert first.rental_plan.rental_mode is RentalMode.RENT_ROOMS_WHILE_OWNER_LIVES_THERE
    assert first.policies[0].policy_type is PolicyType.CHECKING_FLOOR_SELL_PUBLIC_STOCK
    assert TaxRegime.FEDERAL_MORTGAGE_INTEREST in first.tax_regimes


@pytest.mark.parametrize("asset_type", ["cash", "real_estate", "deferred_tax_asset"])
def test_scenario_set_rejects_unsupported_initial_asset_variants(asset_type: str) -> None:
    body = _scenario_set_body("sf_house")
    body["scenarios"][0]["initial_balance_sheet"]["assets"] = [
        {"asset_id": "unsupported_asset", "asset_type": asset_type, "owner_actor_id": "owner", "value_usd": 100_000}
    ]

    with pytest.raises(ValidationError):
        ScenarioSet.model_validate(body)


@pytest.mark.parametrize("liability_type", ["mortgage", "tax_liability", "actor_equity_claim"])
def test_scenario_set_rejects_initial_liabilities(liability_type: str) -> None:
    body = _scenario_set_body("sf_house")
    body["scenarios"][0]["initial_balance_sheet"]["liabilities"] = [
        {
            "liability_id": "unsupported_liability",
            "liability_type": liability_type,
            "owner_actor_id": "owner",
            "balance_usd": 100_000,
        }
    ]

    with pytest.raises(ValidationError):
        ScenarioSet.model_validate(body)


def test_scenario_set_accepts_typed_scenario_economic_assumptions() -> None:
    body = _scenario_set_body("sf_house")
    body["scenarios"][0]["tax_profile"] = {
        "filing_status": "married_filing_jointly",
        "annual_ordinary_income_usd": 250_000,
    }
    body["scenarios"][0]["transaction_costs"] = {"closing_cost_buy_pct": 2.5, "closing_cost_sell_pct": 6.5}
    body["scenarios"][0]["property_assumptions"] = {
        "insurance_annual_usd": 1800,
        "maintenance_pct": 1,
        "depreciable_basis_pct": 80,
    }

    scenario_set = ScenarioSet.model_validate(body)

    scenario = scenario_set.scenarios[0]
    assert scenario.tax_profile.filing_status.value == "married_filing_jointly"
    assert scenario.tax_profile.annual_ordinary_income_usd == 250_000
    assert scenario.transaction_costs.closing_cost_sell_pct == 6.5
    assert scenario.property_assumptions.insurance_annual_usd == 1800


def test_market_request_requires_seed() -> None:
    market_request = MarketRequest(seed=7)

    assert market_request.seed == 7

    with pytest.raises(ValidationError, match="seed"):
        MarketRequest.model_validate({})


def test_policy_config_uses_discriminated_rules_and_enums() -> None:
    body = _scenario_set_body("sf_house")
    body["scenarios"][0]["policies"] = [
        {
            "policy_id": "private_equity_sale",
            "policy_type": "private_equity_sale",
            "actor_id": "owner",
            "proceeds_destination": "generic_sp500_stock",
            "sale_rule": {"sale_rule_type": "fixed_amount_on_opportunity", "amount_usd": 50_000},
        }
    ]

    scenario = ScenarioSet.model_validate(body).scenarios[0]

    private_equity_policy = scenario.policies[0]
    assert isinstance(private_equity_policy, PrivateEquitySalePolicy)
    assert private_equity_policy.policy_type is PolicyType.PRIVATE_EQUITY_SALE
    assert private_equity_policy.proceeds_destination is PrivateEquitySaleProceedsDestination.GENERIC_SP500_STOCK
    assert isinstance(private_equity_policy.sale_rule, FixedAmountPrivateEquitySaleRule)
    assert private_equity_policy.sale_rule.amount_usd == 50_000


@pytest.mark.parametrize("policy_type", ["liquidity_reserve", "portfolio_target_rebalance", "manual_event_schedule"])
def test_scenario_set_rejects_inert_policy_types(policy_type: str) -> None:
    body = _scenario_set_body("sf_house")
    body["scenarios"][0]["policies"] = [
        {"policy_id": "unsupported_policy", "policy_type": policy_type, "actor_id": "owner"}
    ]

    with pytest.raises(ValidationError):
        ScenarioSet.model_validate(body)


def test_policy_config_accepts_private_equity_liquid_net_worth_floor_rule() -> None:
    body = _scenario_set_body("sf_house")
    body["scenarios"][0]["policies"] = [
        {
            "policy_id": "private_equity_liquid_floor_sale",
            "policy_type": "private_equity_sale",
            "actor_id": "owner",
            "proceeds_destination": "generic_sp500_stock",
            "sale_rule": {
                "sale_rule_type": "liquid_net_worth_floor",
                "min_liquid_net_worth_usd": 250_000,
                "sale_amount_usd": 50_000,
            },
        }
    ]

    policy = ScenarioSet.model_validate(body).scenarios[0].policies[0]

    assert isinstance(policy, PrivateEquitySalePolicy)
    assert isinstance(policy.sale_rule, LiquidNetWorthFloorPrivateEquitySaleRule)
    assert policy.sale_rule.min_liquid_net_worth_usd == 250_000
    assert policy.sale_rule.sale_amount_usd == 50_000


def test_scenario_set_model_dump_keeps_backend_keys_snake_case() -> None:
    scenario_set = ScenarioSet.model_validate(_scenario_set_body("sf_house"))

    dumped = scenario_set.model_dump(mode="json")
    _assert_snake_case_keys(dumped)
    assert "scenario_set_id" in dumped
    assert "scenarioSetId" not in dumped


def test_scenario_result_serialization_has_no_projection_compatibility_field() -> None:
    result = ScenarioResult(
        scenario_id="sf_house",
        scenario_label="Sf House",
        summary=ScenarioAcceptedSummary(enabled=True, property_id="sf_ashton", location_id=LocationId.SAN_FRANCISCO_CA),
    )

    dumped = result.model_dump(mode="json", exclude_none=True)
    assert "projection" not in dumped
    assert "rollout_status_summary" not in dumped


def test_rollout_status_summary_serializes_counts_by_existing_status_type() -> None:
    summary = RolloutStatusSummary.from_statuses(
        (
            RolloutStatus(rollout_index=0, status=RolloutStatusType.ACTIVE, min_cash_usd=1_000),
            RolloutStatus(rollout_index=1, status=RolloutStatusType.CASH_NEGATIVE, min_cash_usd=-1),
            RolloutStatus(rollout_index=2, status=RolloutStatusType.ACTIVE, min_cash_usd=500),
        )
    )

    assert summary.total_rollout_count == 3
    assert summary.counts_by_status == {RolloutStatusType.ACTIVE: 2, RolloutStatusType.CASH_NEGATIVE: 1}
    assert summary.model_dump(mode="json") == {
        "total_rollout_count": 3,
        "counts_by_status": {"active": 2, "cash_negative": 1},
    }


def test_scenario_set_rejects_wrong_casing() -> None:
    body = _scenario_set_body("sf_house")
    body["scenarioSetId"] = body.pop("scenario_set_id")

    with pytest.raises(ValidationError):
        ScenarioSet.model_validate(body)

    body = _scenario_set_body("sf_house")
    body["scenarios"][0]["scenarioId"] = body["scenarios"][0].pop("scenario_id")

    with pytest.raises(ValidationError):
        ScenarioSet.model_validate(body)


def test_scenario_set_rejects_legacy_enum_values() -> None:
    body = _scenario_set_body("sf_house")
    body["scenarios"][0]["rental_plan"]["rental_mode"] = "rental_after_3"

    with pytest.raises(ValidationError):
        ScenarioSet.model_validate(body)

    body = _scenario_set_body("sf_house")
    body["scenarios"][0]["occupancy_plan"]["occupancy_mode"] = "live_in"

    with pytest.raises(ValidationError):
        ScenarioSet.model_validate(body)

    body = _scenario_set_body("sf_house")
    body["scenarios"][0]["policies"][0]["policy_type"] = "checking_floor_sp500"

    with pytest.raises(ValidationError):
        ScenarioSet.model_validate(body)

    body = _scenario_set_body("sf_house")
    body["scenarios"][0]["policies"] = [
        {
            "policy_id": "liquidity_reserve",
            "policy_type": "liquidity_reserve",
            "actor_id": "owner",
            "mode": "projected_deficits",
            "min_reserve_usd": 10_000,
            "forward_months": 12,
        }
    ]

    with pytest.raises(ValidationError):
        ScenarioSet.model_validate(body)


def test_scenario_set_rejects_duplicate_scenario_ids() -> None:
    body = _scenario_set_body("same_id", "other_id")
    body["scenarios"][1] = copy.deepcopy(body["scenarios"][0])

    with pytest.raises(ValidationError, match="scenario ids must be unique"):
        ScenarioSet.model_validate(body)


if __name__ == "__main__":
    pytest_bazel.main()
