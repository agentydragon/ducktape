"""Generic macro market-bundle provider.

Wraps any `MarketModel` implementation from `augur.model.markets.models.*`
as a `MarketBundleProvider` for the scenario-set runtime. Composition keeps
each macro model focused on the macro process; private-equity sale opportunities,
mortgage rates, and location-specific path selection are runtime bundle concerns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from augur.core.market_bundle import MarketBundle, MarketBundleMetadata
from augur.core.provenance import stable_identity_digest
from augur.core.scenario_set import MarketRequest
from augur.model.location_market_sources import LocationMarketSources, build_location_market_maps
from augur.model.market_config import load_market_config
from augur.model.markets.data import load_evidence
from augur.model.markets.market_model import MarketModel
from augur.model.markets.registry import BY_LABEL

_TENDER_INTERVAL_MONTHS = 12
_MODEL_CARD_ID = "augur-market-model-card:2026-05-15"
_VALIDATION_REPORT_ID = "validation_report:augur-market-models:not_available:2026-05-15"
_KNOWN_LIMITATION_IDS = (
    "evidence-set-id-unversioned",
    "calibration-artifact-id-unversioned",
    "validation-report-not-decision-grade",
    "constant-mortgage-rate-path",
    "private-equity-marks-flat-fixture",
)


class MacroMarketBundleProvider:
    def __init__(
        self, market_model: MarketModel, config_path: Path, *, current_private_equity_price_usd: float
    ) -> None:
        self.config_path = Path(config_path).resolve()
        config = load_market_config(self.config_path)
        self.label: str = market_model.label

        historical, evidence = load_evidence(config, self.config_path.parent)
        self.latest_observations: dict[str, Any] = dict(evidence.latest_observations)
        self._risk_factor_set_id = "risk_factor_set:" + stable_identity_digest(
            {"factor_names": historical.factor_names}
        )
        self._market_model_version_id = "model_version:" + stable_identity_digest(
            {"label": self.label, "class": type(market_model).__qualname__}
        )
        self._evidence_set_id = "evidence_set:" + stable_identity_digest(
            {
                "config_file": self.config_path.name,
                "factor_names": historical.factor_names,
                "latest_observations": self.latest_observations,
            }
        )
        self._calibration_artifact_id = "calibration_artifact:" + stable_identity_digest(
            {
                "market_model_id": self.label,
                "market_model_version_id": self._market_model_version_id,
                "evidence_set_id": self._evidence_set_id,
                "risk_factor_set_id": self._risk_factor_set_id,
            }
        )
        self._current_mortgage30_rate_pct = float(evidence.current_mortgage30_rate_pct)
        self._current_private_equity_price_usd = float(current_private_equity_price_usd)
        self._risk_factor_ids = historical.factor_names
        self._evidence_latest_observation_ids = tuple(sorted(str(key) for key in self.latest_observations))
        self._factor_index = {name: idx for idx, name in enumerate(historical.factor_names)}
        self._location_market_sources = LocationMarketSources.from_config(config.location_market_sources)

        market_model.fit(historical)
        self._market_model = market_model

    @classmethod
    def for_label(
        cls, label: str, *, config_path: Path, current_private_equity_price_usd: float
    ) -> MacroMarketBundleProvider:
        return cls(
            BY_LABEL[label].build(), config_path, current_private_equity_price_usd=current_private_equity_price_usd
        )

    def sample_market_bundle(
        self, *, rollout_count: int, horizon_months: int, seed: int, market_request: MarketRequest
    ) -> MarketBundle:
        scenarios = self._market_model.simulate(n_paths=rollout_count, n_months=horizon_months, seed=seed)
        shape = (rollout_count, horizon_months + 1)
        path_by_factor: dict[str, np.ndarray] = {
            factor_name: scenarios.multipliers[:, :, factor_index]
            for factor_name, factor_index in self._factor_index.items()
        }
        home_value_paths_by_location, rent_paths_by_location = build_location_market_maps(
            path_by_factor=path_by_factor, sources=self._location_market_sources
        )
        home_value_paths_by_location = {"default": path_by_factor["home"], **home_value_paths_by_location}
        rent_paths_by_location = {"default": path_by_factor["rent"], **rent_paths_by_location}

        private_equity_events = np.zeros(shape, dtype=np.bool_)
        private_equity_events[:, _TENDER_INTERVAL_MONTHS : horizon_months + 1 : _TENDER_INTERVAL_MONTHS] = True

        return MarketBundle(
            month_index=np.arange(horizon_months + 1, dtype="int64"),
            inflation_multipliers=path_by_factor["inflation"],
            generic_sp500_multipliers=path_by_factor["sp500"],
            home_value_multipliers_by_location=home_value_paths_by_location,
            rent_multipliers_by_location=rent_paths_by_location,
            mortgage_30y_rate_pct=np.full(shape, self._current_mortgage30_rate_pct, dtype="float64"),
            private_equity_value_multipliers=np.ones(shape, dtype="float64"),
            private_equity_sale_opportunity_mask=private_equity_events,
            crypto_value_multipliers=np.ones(shape, dtype="float64"),
            metadata=MarketBundleMetadata(
                market_model_id=market_request.market_model_id,
                model_card_id=_MODEL_CARD_ID,
                model_version_id=self._market_model_version_id,
                validation_report_id=_VALIDATION_REPORT_ID,
                known_limitation_ids=_KNOWN_LIMITATION_IDS,
                market_model_version_id=self._market_model_version_id,
                scenario_generator_id="macro_market_bundle_provider",
                scenario_generator_version_id="macro_market_bundle_provider:v1",
                evidence_set_id=self._evidence_set_id,
                calibration_artifact_id=self._calibration_artifact_id,
                risk_factor_set_id=self._risk_factor_set_id,
                risk_factor_ids=self._risk_factor_ids,
                evidence_latest_observation_ids=self._evidence_latest_observation_ids,
                seed=seed,
                rollout_count=rollout_count,
                horizon_months=horizon_months,
                event_stream_ids=("private_equity_sale_opportunity_event",),
                notes=("sampled by MacroMarketBundleProvider",),
                source_metadata={
                    "market_provider_label": self.label,
                    "current_private_equity_price_usd": self._current_private_equity_price_usd,
                    "latest_observation_ids": list(self._evidence_latest_observation_ids),
                },
            ),
        )
