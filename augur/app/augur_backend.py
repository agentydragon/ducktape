"""Generic augur backend: builds the bootstrap payload and runs scenario
sets through the vectorized engine. User-specific data is read from the
`AugurConfig` passed at construction time."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from augur.app.catalog import build_bootstrap_payload
from augur.app.config import AugurConfig
from augur.core.api import simulate_set
from augur.core.bootstrap import Property
from augur.core.local_regulation import scenario_with_location_tax_defaults
from augur.core.market_bundle import HorizonBoundMarketBundleProvider, MarketBundleProvider
from augur.core.scenario_engine import MONTHS_PER_YEAR
from augur.core.scenario_set import Scenario, ScenarioSet, ScenarioSetRunResponse
from augur.core.schemas import ScenarioKnobs


@dataclass(frozen=True)
class AugurBackendRuntimeConfig:
    market_bundle_provider: MarketBundleProvider
    default_rollout_samples: int
    max_rollout_samples: int


class AugurBackend:
    def __init__(self, *, augur_config: AugurConfig, runtime_config: AugurBackendRuntimeConfig) -> None:
        self.runtime_config = runtime_config
        self.market_bundle_provider = runtime_config.market_bundle_provider
        self._bootstrap = build_bootstrap_payload(augur_config)
        self._property_by_id: dict[str, Property] = {
            property_.id: property_ for property_ in self._bootstrap.properties
        }
        self._location_by_id = {location.id: location for location in self._bootstrap.locations}
        self.default_knobs = self._default_knobs_for_provider(self._bootstrap.default_knobs)
        self.default_property_id = self._bootstrap.default_property_id
        self.default_actor_policy = self._bootstrap.default_actor_policy

    def bootstrap_payload(self):
        return self._bootstrap.model_copy(
            update={
                "default_property_id": self.default_property_id,
                "default_actor_policy": self.default_actor_policy,
                "default_knobs": self.default_knobs,
                "default_rollout_samples": self.runtime_config.default_rollout_samples,
            }
        )

    def run_scenario_set_for_request_body(self, body: dict[str, Any]) -> ScenarioSetRunResponse:
        scenario_set = ScenarioSet.model_validate(body)
        self._validate_scenario_set_property_references(scenario_set)
        scenario_set = self._scenario_set_with_catalog_defaults(scenario_set)
        return simulate_set(scenario_set, market_provider=self.market_bundle_provider).to_response()

    def _default_knobs_for_provider(self, knobs: ScenarioKnobs) -> ScenarioKnobs:
        if not isinstance(self.market_bundle_provider, HorizonBoundMarketBundleProvider):
            return knobs
        max_hold_years = max(1, self.market_bundle_provider.horizon_months // MONTHS_PER_YEAR)
        return knobs.model_copy(update={"hold_years": min(knobs.hold_years, max_hold_years)})

    def _validate_scenario_set_property_references(self, scenario_set: ScenarioSet) -> None:
        for scenario in scenario_set.scenarios:
            property_id = scenario.property_selection.property_id
            if property_id is None:
                continue
            property_ = self._property_by_id[property_id]
            requested_location = scenario.property_selection.location_id
            if requested_location is not None and str(requested_location) != property_.location_id:
                raise ValueError(
                    "scenario property/location mismatch: "
                    f"{scenario.scenario_id} uses {property_id!r} with {requested_location!r}"
                )

    def _scenario_set_with_catalog_defaults(self, scenario_set: ScenarioSet) -> ScenarioSet:
        scenarios: list[Scenario] = []
        for scenario in scenario_set.scenarios:
            property_id = scenario.property_selection.property_id
            if property_id is None:
                scenarios.append(scenario)
                continue
            property_ = self._property_by_id[property_id]
            location = self._location_by_id[property_.location_id]
            with_catalog_property = scenario.model_copy(
                update={
                    "property_selection": scenario.property_selection.model_copy(
                        update={
                            "location_id": scenario.property_selection.location_id or property_.location_id,
                            "purchase_price_usd": scenario.property_selection.purchase_price_usd
                            if scenario.property_selection.purchase_price_usd is not None
                            else property_.price_usd,
                        }
                    )
                }
            )
            scenarios.append(scenario_with_location_tax_defaults(with_catalog_property, location.local_regulation))
        return scenario_set.model_copy(update={"scenarios": tuple(scenarios)})
