"""Round-trip test: train the active VECM model offline, write the provider config +
blob, re-load via Pydantic + `<Model>ProviderConfig.realize_model(...)`,
and sample.

This is the public contract the augur server consumes at startup: read
`Config.models`, dispatch via the discriminated union, and
sample without re-fitting from source CSVs."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
import yaml
from pydantic import TypeAdapter

from finance.augur.fit.main import main as train_main
from finance.augur.model.exogenous import ExogenousSamplingRequest, level_keys_in_bundle, level_series_request_channels
from finance.augur.model.provider_config import ProviderConfig
from finance.augur.model.series import HomeValueKey, LevelSeriesKey, RentKey
from finance.augur.model.state_space import StateSpaceProviderConfig
from finance.augur.model.vecm import VecmProviderConfig

_ADAPTER: TypeAdapter[ProviderConfig] = TypeAdapter(ProviderConfig)


@pytest.mark.parametrize("model_label", ["vecm"])
def test_train_then_load_and_sample(model_label: str, tmp_path: Path, synthetic_evidence_dir: Path) -> None:
    out_manifest = tmp_path / "exogenous_provider.yaml"
    out_blob = tmp_path / (f"trained_{model_label}.npz" if model_label == "vecm" else f"trained_{model_label}.json")
    train_main(["--model", model_label, "--out-provider-config", str(out_manifest), "--out-blob", str(out_blob)])

    assert out_manifest.exists()
    assert out_blob.exists()

    parsed = _ADAPTER.validate_python(yaml.safe_load(out_manifest.read_text(encoding="utf-8")))
    # Trainer only emits the active trained provider config; narrow away the
    # other discriminated-union variants so the trained-provider fields are
    # accessible below.
    assert isinstance(parsed, VecmProviderConfig)
    assert parsed.type == model_label
    assert parsed.trained_blob == out_blob
    assert parsed.latest_observations  # non-empty; exact keys depend on the source-data schema

    model = parsed.realize_model()
    # Post-collapse the model's typed factors ARE the level keys; derive the home-value and
    # rent locations directly from them (there is no location_series_sources to consult).
    home_locations = sorted(factor.location_id for factor in model.factor_names if isinstance(factor, HomeValueKey))
    rent_locations = sorted(factor.location_id for factor in model.factor_names if isinstance(factor, RentKey))
    required_level_series: frozenset[LevelSeriesKey] = frozenset(
        {HomeValueKey(location_id=loc) for loc in home_locations} | {RentKey(location_id=loc) for loc in rent_locations}
    )
    sampled = model.sample(
        ExogenousSamplingRequest(
            rollout_seeds=(7, 8), horizon_months=12, **level_series_request_channels(required_level_series)
        )
    )

    assert str(sampled.metadata["model_version_id"]).startswith("model_version:")
    assert level_keys_in_bundle(sampled) == set(required_level_series)
    for home_loc in home_locations:
        assert sampled.level_matrix(HomeValueKey(location_id=home_loc), rollout_count=2, horizon_months=12).shape == (
            2,
            13,
        )
    for rent_loc in rent_locations:
        assert sampled.level_matrix(RentKey(location_id=rent_loc), rollout_count=2, horizon_months=12).shape == (2, 13)


@pytest.mark.parametrize("model_label", ["state_space"])
def test_train_state_space_then_load_and_sample(model_label: str, tmp_path: Path, synthetic_evidence_dir: Path) -> None:
    out_manifest = tmp_path / "exogenous_provider.yaml"
    out_blob = tmp_path / f"trained_{model_label}.json"
    train_main(["--model", model_label, "--out-provider-config", str(out_manifest), "--out-blob", str(out_blob)])

    assert out_manifest.exists()
    assert out_blob.exists()

    parsed = _ADAPTER.validate_python(yaml.safe_load(out_manifest.read_text(encoding="utf-8")))
    assert isinstance(parsed, StateSpaceProviderConfig)
    assert parsed.type == model_label
    assert parsed.trained_artifact_path == out_blob
    assert parsed.conditioning.observations

    model = parsed.realize_model()
    home_locations = sorted(factor.location_id for factor in model.factor_names if isinstance(factor, HomeValueKey))
    rent_locations = sorted(factor.location_id for factor in model.factor_names if isinstance(factor, RentKey))
    required_level_series: frozenset[LevelSeriesKey] = frozenset(
        {HomeValueKey(location_id=loc) for loc in home_locations} | {RentKey(location_id=loc) for loc in rent_locations}
    )
    sampled = model.sample(
        ExogenousSamplingRequest(
            rollout_seeds=(7, 8), horizon_months=12, **level_series_request_channels(required_level_series)
        )
    )

    assert str(sampled.metadata["model_version_id"]).startswith("model_version:")
    assert sampled.metadata["source_manifest"]
    assert level_keys_in_bundle(sampled) >= set(required_level_series)


if __name__ == "__main__":
    pytest_bazel.main()
