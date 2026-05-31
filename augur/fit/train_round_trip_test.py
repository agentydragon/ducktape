"""Round-trip test: train the active VECM model offline, write the provider config +
blob, re-load via Pydantic + `<Model>ExogenousProviderConfig.realize_model(...)`,
and sample.

This is the public contract the augur server consumes at startup: read
`Config.exogenous_provider`, dispatch via the discriminated union, and
sample without re-fitting from source CSVs."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
import yaml
from pydantic import TypeAdapter

from augur.fit.main import main as train_main
from augur.model.exogenous import ExogenousSamplingRequest
from augur.model.exogenous_provider_config import ExogenousProviderConfig
from augur.model.series import HomeValueKey, LevelSeriesKey, LocationId, RentKey
from augur.model.state_space import StateSpaceExogenousProviderConfig
from augur.model.vecm import VecmExogenousProviderConfig

_ADAPTER: TypeAdapter[ExogenousProviderConfig] = TypeAdapter(ExogenousProviderConfig)


@pytest.mark.parametrize("model_label", ["vecm"])
def test_train_then_load_and_sample(model_label: str, tmp_path: Path) -> None:
    out_manifest = tmp_path / "exogenous_provider.yaml"
    out_blob = tmp_path / (f"trained_{model_label}.npz" if model_label == "vecm" else f"trained_{model_label}.json")
    train_main(["--model", model_label, "--out-provider-config", str(out_manifest), "--out-blob", str(out_blob)])

    assert out_manifest.exists()
    assert out_blob.exists()

    parsed = _ADAPTER.validate_python(yaml.safe_load(out_manifest.read_text(encoding="utf-8")))
    # Trainer only emits the active trained provider config; narrow away the
    # other discriminated-union variants so the trained-provider fields are
    # accessible below.
    assert isinstance(parsed, VecmExogenousProviderConfig)
    assert parsed.type == model_label
    assert parsed.trained_blob == out_blob
    assert parsed.latest_observations  # non-empty; exact keys depend on the source-data schema

    model = parsed.realize_model()
    locations = sorted(parsed.location_series_sources.home_value)
    required_level_series: frozenset[LevelSeriesKey] = frozenset(
        {
            key
            for location in locations
            for key in (HomeValueKey(location_id=LocationId(location)), RentKey(location_id=LocationId(location)))
        }
    )
    sampled = model.sample(
        ExogenousSamplingRequest(rollout_seeds=(7, 8), horizon_months=12, required_level_series=required_level_series)
    )

    assert str(sampled.metadata["model_version_id"]).startswith("model_version:")
    assert {row["series_id"] for row in sampled.levels.select("series_id").unique().iter_rows(named=True)} == {
        key.wire_id for key in required_level_series
    }
    for location in locations:
        assert sampled.level_matrix(
            HomeValueKey(location_id=LocationId(location)), rollout_count=2, horizon_months=12
        ).shape == (2, 13)
        assert sampled.level_matrix(
            RentKey(location_id=LocationId(location)), rollout_count=2, horizon_months=12
        ).shape == (2, 13)


@pytest.mark.parametrize("model_label", ["state_space"])
def test_train_state_space_then_load_and_sample(model_label: str, tmp_path: Path) -> None:
    out_manifest = tmp_path / "exogenous_provider.yaml"
    out_blob = tmp_path / f"trained_{model_label}.json"
    train_main(["--model", model_label, "--out-provider-config", str(out_manifest), "--out-blob", str(out_blob)])

    assert out_manifest.exists()
    assert out_blob.exists()

    parsed = _ADAPTER.validate_python(yaml.safe_load(out_manifest.read_text(encoding="utf-8")))
    assert isinstance(parsed, StateSpaceExogenousProviderConfig)
    assert parsed.type == model_label
    assert parsed.trained_artifact_path == out_blob
    assert parsed.conditioning.observations

    model = parsed.realize_model()
    locations = sorted(parsed.location_series_sources.home_value)
    required_level_series: frozenset[LevelSeriesKey] = frozenset(
        {
            key
            for location in locations
            for key in (HomeValueKey(location_id=LocationId(location)), RentKey(location_id=LocationId(location)))
        }
    )
    sampled = model.sample(
        ExogenousSamplingRequest(rollout_seeds=(7, 8), horizon_months=12, required_level_series=required_level_series)
    )

    assert str(sampled.metadata["model_version_id"]).startswith("model_version:")
    assert sampled.metadata["source_manifest"]
    assert {row["series_id"] for row in sampled.levels.select("series_id").unique().iter_rows(named=True)} >= {
        key.wire_id for key in required_level_series
    }


if __name__ == "__main__":
    pytest_bazel.main()
