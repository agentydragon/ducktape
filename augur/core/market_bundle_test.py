from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel
from numpy.testing import assert_allclose

from augur.core.market_bundle import MarketBundle, MarketBundleMetadata, SimpleMarketBundleProvider
from augur.core.scenario_set import MarketRequest


def test_simple_market_bundle_shapes_and_reproducibility() -> None:
    request = MarketRequest(market_model_id="simple_test", rollout_count=4, horizon_months=18, seed=123)
    provider = SimpleMarketBundleProvider()

    first = provider.sample_market_bundle(
        rollout_count=request.rollout_count,
        horizon_months=request.horizon_months,
        seed=request.seed,
        market_request=request,
    )
    second = provider.sample_market_bundle(
        rollout_count=request.rollout_count,
        horizon_months=request.horizon_months,
        seed=request.seed,
        market_request=request,
    )

    assert first.generic_sp500_multipliers.shape == (4, 19)
    assert first.private_equity_sale_opportunity_mask.shape == (4, 19)
    assert first.private_equity_sale_opportunity_mask.dtype == np.bool_
    np.testing.assert_array_equal(first.month_index, np.arange(19, dtype="int64"))
    assert_allclose(first.generic_sp500_multipliers, second.generic_sp500_multipliers)
    assert_allclose(first.inflation_multipliers[:, 0], 1.0)
    assert_allclose(first.private_equity_value_multipliers[:, 0], 1.0)
    assert first.metadata.path_set_id == second.metadata.path_set_id
    assert first.metadata.exogenous_path_ids == second.metadata.exogenous_path_ids


def test_market_bundle_provenance_fields_drive_path_identity() -> None:
    base = MarketBundleMetadata(
        market_model_id="model_a",
        market_model_version_id="model_version:1",
        scenario_generator_id="generator",
        scenario_generator_version_id="generator:1",
        evidence_set_id="evidence:1",
        calibration_artifact_id="calibration:1",
        seed=1,
        rollout_count=2,
        horizon_months=3,
        event_stream_ids=("private_equity_sale_opportunity_event",),
    )
    computed_metadata_fields = {
        "path_set_id",
        "exogenous_path_ids",
        "model_card",
        "validation_report",
        "known_limitations",
        "evidence_set",
        "calibration_run",
        "calibration_artifact",
        "scenario_generator_run",
        "exogenous_path_set",
    }
    same = MarketBundleMetadata.model_validate(base.model_dump(exclude=computed_metadata_fields))
    changed_seed = base.model_copy(update={"seed": 2})
    changed_model = base.model_copy(update={"market_model_version_id": "model_version:2"})
    changed_evidence = base.model_copy(update={"evidence_set_id": "evidence:2"})
    changed_calibration = base.model_copy(update={"calibration_artifact_id": "calibration:2"})

    assert base.path_set_id == same.path_set_id
    assert base.exogenous_path_ids == same.exogenous_path_ids
    assert changed_seed.path_set_id != base.path_set_id
    assert changed_model.path_set_id != base.path_set_id
    assert changed_evidence.path_set_id != base.path_set_id
    assert changed_calibration.path_set_id != base.path_set_id
    assert changed_evidence.exogenous_path_ids[0] != base.exogenous_path_ids[0]
    assert base.evidence_set.evidence_set_id == "evidence:1"
    assert base.calibration_run.evidence_set_id == "evidence:1"
    assert base.calibration_artifact.calibration_run_id == base.calibration_run.calibration_run_id
    assert base.scenario_generator_run.scenario_generator_run_id.startswith("scenario_generator_run:")
    assert base.exogenous_path_set.path_set_id == base.path_set_id
    assert base.exogenous_path_set.scenario_generator_run_id == base.scenario_generator_run.scenario_generator_run_id


def test_market_bundle_rejects_bad_shapes() -> None:
    metadata = MarketBundleMetadata(
        market_model_id="bad", seed=1, rollout_count=2, horizon_months=3, event_stream_ids=()
    )
    valid = np.ones((2, 4), dtype="float64")

    with pytest.raises(ValueError, match="generic_sp500_multipliers"):
        MarketBundle(
            month_index=np.arange(4, dtype="int64"),
            inflation_multipliers=valid,
            generic_sp500_multipliers=np.ones((2, 3), dtype="float64"),
            home_value_multipliers_by_location={"default": valid},
            rent_multipliers_by_location={"default": valid},
            mortgage_30y_rate_pct=np.full((2, 4), 6.5, dtype="float64"),
            private_equity_value_multipliers=valid,
            private_equity_sale_opportunity_mask=np.zeros((2, 4), dtype=np.bool_),
            crypto_value_multipliers=valid,
            metadata=metadata,
        )


if __name__ == "__main__":
    pytest_bazel.main()
