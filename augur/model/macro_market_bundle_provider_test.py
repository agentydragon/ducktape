"""Shape-contract tests for the generic macro market-bundle provider.

Parametrised across every label in the registry, so every shipped macro
model is shape-checked against the MarketBundle contract automatically.
The model-internal correctness tests live next to each model in
`markets/models/*_test.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import pytest_bazel
from numpy.testing import assert_allclose
from pydantic import ValidationError

from augur.core.scenario_set import MarketRequest
from augur.model.macro_market_bundle_provider import MacroMarketBundleProvider
from augur.model.markets.registry import LABELS

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "market_config.example.json"


@pytest.fixture(params=LABELS)
def provider(request: pytest.FixtureRequest) -> MacroMarketBundleProvider:
    return MacroMarketBundleProvider.for_label(
        request.param, config_path=CONFIG_PATH, current_private_equity_price_usd=100.0
    )


def test_metadata_populated(provider: MacroMarketBundleProvider) -> None:
    assert provider.label in LABELS
    assert isinstance(provider.latest_observations, dict)


def test_provider_rejects_unknown_market_config_fields(tmp_path: Path) -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config["unused_knob"] = True
    config_path = tmp_path / "market_config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValidationError, match="unused_knob"):
        MacroMarketBundleProvider.for_label(LABELS[0], config_path=config_path, current_private_equity_price_usd=100.0)


def _request(provider: MacroMarketBundleProvider, *, rollout_count: int = 3, horizon_months: int = 24) -> MarketRequest:
    return MarketRequest(
        market_model_id=provider.label, rollout_count=rollout_count, horizon_months=horizon_months, seed=42
    )


def _sample(provider: MacroMarketBundleProvider, *, rollout_count: int = 3, horizon_months: int = 24):
    request = _request(provider, rollout_count=rollout_count, horizon_months=horizon_months)
    return provider.sample_market_bundle(
        rollout_count=rollout_count, horizon_months=horizon_months, seed=request.seed, market_request=request
    )


def test_sample_market_bundle_shape(provider: MacroMarketBundleProvider) -> None:
    n_rollouts = 3
    horizon_months = 24
    bundle = _sample(provider, rollout_count=n_rollouts, horizon_months=horizon_months)
    expected_shape = (n_rollouts, horizon_months + 1)

    assert bundle.rollout_count == n_rollouts
    assert bundle.horizon_months == horizon_months
    assert bundle.metadata.seed == 42
    assert bundle.metadata.model_card_id == "augur-market-model-card:2026-05-15"
    assert bundle.metadata.model_card is not None
    assert bundle.metadata.model_card.model_card_id == bundle.metadata.model_card_id
    assert bundle.metadata.model_version_id == bundle.metadata.market_model_version_id
    assert bundle.metadata.market_model_version_id.startswith("model_version:")
    assert bundle.metadata.evidence_set_id.startswith("evidence_set:")
    assert bundle.metadata.evidence_set.evidence_set_id == bundle.metadata.evidence_set_id
    assert bundle.metadata.evidence_set.factor_ids == bundle.metadata.risk_factor_ids
    assert bundle.metadata.evidence_set.latest_observation_ids == bundle.metadata.evidence_latest_observation_ids
    assert bundle.metadata.calibration_artifact_id.startswith("calibration_artifact:")
    assert bundle.metadata.calibration_run.calibration_run_id.startswith("calibration_run:")
    assert bundle.metadata.calibration_artifact.calibration_run_id == bundle.metadata.calibration_run.calibration_run_id
    assert bundle.metadata.risk_factor_set_id.startswith("risk_factor_set:")
    assert {"sp500", "rent", "inflation"} <= set(bundle.metadata.risk_factor_ids)
    assert bundle.metadata.validation_report_id == "validation_report:augur-market-models:not_available:2026-05-15"
    assert bundle.metadata.validation_report is not None
    assert bundle.metadata.validation_report.evidence_set_id == bundle.metadata.evidence_set_id
    assert bundle.metadata.known_limitation_ids == (
        "evidence-set-id-unversioned",
        "calibration-artifact-id-unversioned",
        "validation-report-not-decision-grade",
        "constant-mortgage-rate-path",
        "private-equity-marks-flat-fixture",
    )
    assert (
        tuple(limitation.known_limitation_id for limitation in bundle.metadata.known_limitations)
        == bundle.metadata.known_limitation_ids
    )
    assert bundle.metadata.scenario_generator_run.scenario_generator_run_id.startswith("scenario_generator_run:")
    assert bundle.metadata.scenario_generator_run.evidence_set_id == bundle.metadata.evidence_set_id
    assert bundle.metadata.exogenous_path_set.path_set_id == bundle.metadata.path_set_id
    assert bundle.metadata.source_metadata["market_provider_label"] == provider.label
    assert "market_provider_seed" not in bundle.metadata.source_metadata
    assert "market_provider_horizon_months" not in bundle.metadata.source_metadata
    # current_private_equity_price_usd is a typed metadata field, not an entry in source_metadata
    assert "current_private_equity_price_usd" not in bundle.metadata.source_metadata
    assert bundle.metadata.current_private_equity_price_usd == 100.0
    np.testing.assert_array_equal(bundle.month_index, np.arange(horizon_months + 1, dtype="int64"))
    for key in ("inflation_multipliers", "generic_sp500_multipliers", "mortgage_30y_rate_pct"):
        values = getattr(bundle, key)
        assert values.shape == expected_shape, key
        assert np.all(np.isfinite(values)), key
    pe_values = bundle.private_equity_value_multiplier(None)
    assert pe_values.shape == expected_shape
    assert np.all(np.isfinite(pe_values))
    for key in ("inflation_multipliers", "generic_sp500_multipliers"):
        values = getattr(bundle, key)
        assert_allclose(values[:, 0], 1.0)
        assert np.all(values > 0), key
    assert_allclose(pe_values[:, 0], 1.0)
    assert np.all(pe_values > 0)
    expected_locations = {"default", "san_francisco_ca", "vallejo_ca", "mare_island_vallejo_ca"}
    assert set(bundle.home_value_multipliers_by_location) == expected_locations
    assert set(bundle.rent_multipliers_by_location) == expected_locations
    assert_allclose(
        bundle.home_value_multipliers_by_location["san_francisco_ca"],
        bundle.home_value_multipliers_by_location["default"],
    )
    assert_allclose(
        bundle.rent_multipliers_by_location["san_francisco_ca"], bundle.rent_multipliers_by_location["default"]
    )


def test_mortgage_path_constant(provider: MacroMarketBundleProvider) -> None:
    bundle = _sample(provider, rollout_count=1, horizon_months=24)
    arr = bundle.mortgage_30y_rate_pct[0]
    assert_allclose(arr, arr[0])
    assert arr[0] > 0.0


def test_private_equity_paths_flat_with_yearly_tenders(provider: MacroMarketBundleProvider) -> None:
    bundle = _sample(provider, rollout_count=1, horizon_months=24)
    assert_allclose(bundle.private_equity_value_multiplier(None), 1.0)
    mask = bundle.private_equity_sale_opportunity_mask_for(None)
    assert not mask[:, 0].any()
    assert mask[:, 12].all()
    assert mask[:, 24].all()


def test_seed_determinism(provider: MacroMarketBundleProvider) -> None:
    request = _request(provider, rollout_count=2, horizon_months=24)
    a = provider.sample_market_bundle(rollout_count=2, horizon_months=24, seed=11, market_request=request)
    b = provider.sample_market_bundle(rollout_count=2, horizon_months=24, seed=11, market_request=request)
    assert_allclose(a.generic_sp500_multipliers, b.generic_sp500_multipliers)


if __name__ == "__main__":
    pytest_bazel.main()
