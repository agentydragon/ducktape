"""Round-trip test: train a model offline, write the provider config (VECM embeds its
fitted state directly; state_space writes it to a separate blob), re-load via Pydantic +
`<Model>ProviderConfig.realize_model(...)`, and sample.

This is the public contract the augur server consumes at startup: read
`Config.models`, dispatch via the discriminated union, and
sample without re-fitting from source CSVs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from pydantic import TypeAdapter

from finance.augur.fit.data import load_evidence
from finance.augur.fit.evidence_data import load_exogenous_evidence
from finance.augur.model.exogenous import ExogenousSamplingRequest, level_series_request_channels
from finance.augur.model.series import HomeValueKey, InflationKey, LevelSeriesKey, RentKey
from finance.augur.x.models.provider_config import ProviderConfig
from finance.augur.x.models.state_space import StateSpaceModelArtifact, StateSpaceProviderConfig
from finance.augur.x.models.state_space_fit import fit_state_space_artifact
from finance.augur.x.models.train import main as train_main
from finance.augur.x.models.vecm import VecmProviderConfig
from finance.evidence import sources

_ADAPTER: TypeAdapter[ProviderConfig] = TypeAdapter(ProviderConfig)


@pytest.mark.parametrize("model_label", ["vecm"])
def test_train_then_load_and_sample(model_label: str, tmp_path: Path, synthetic_evidence_dir: Path) -> None:
    out_manifest = tmp_path / "exogenous_provider.yaml"
    train_main(["--model", model_label, "--out-provider-config", str(out_manifest)])

    assert out_manifest.exists()

    parsed = _ADAPTER.validate_python(yaml.safe_load(out_manifest.read_text(encoding="utf-8")))
    # Trainer only emits the active trained provider config; narrow away the
    # other discriminated-union variants so the trained-provider fields are
    # accessible below.
    assert isinstance(parsed, VecmProviderConfig)
    assert parsed.type == model_label
    # Fitted state is embedded directly in the manifest — no separate blob to point at.
    assert parsed.trained_state.factor_names
    assert parsed.trained_state.params
    assert set(parsed.latest_observations) == set(parsed.trained_state.factor_names)
    assert all(point.source_id and point.units for point in parsed.latest_observations.values())
    expected_hashes = _expected_hashes(synthetic_evidence_dir)
    assert parsed.public_evidence_revision is None
    assert parsed.public_evidence_source_sha256 == expected_hashes

    model = parsed.realize_model()
    assert model.public_evidence_revision is None
    assert model.public_evidence_source_sha256 == expected_hashes
    assert model.evidence_set_id
    changed_hashes = _changed_hashes(expected_hashes, synthetic_evidence_dir, tmp_path)
    changed_model = parsed.model_copy(update={"public_evidence_source_sha256": changed_hashes}).realize_model()
    assert changed_model.evidence_set_id != model.evidence_set_id
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

    assert str(sampled.provenance["model_version_id"]).startswith("model_version:")
    assert sampled.provenance["public_evidence_revision"] is None
    assert sampled.provenance["public_evidence_source_sha256"] == expected_hashes
    assert sampled.levels.series_keys() == set(required_level_series)
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
    artifact = json.loads(out_blob.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 3
    assert "filtered_log_state_mean" not in artifact
    assert "filtered_log_state_cov" not in artifact
    expected_hashes = _expected_hashes(synthetic_evidence_dir)
    assert artifact["public_evidence_revision"] is None
    assert artifact["public_evidence_source_sha256"] == expected_hashes

    parsed = _ADAPTER.validate_python(yaml.safe_load(out_manifest.read_text(encoding="utf-8")))
    assert isinstance(parsed, StateSpaceProviderConfig)
    assert parsed.type == model_label
    assert parsed.trained_artifact_path == out_blob
    assert parsed.conditioning.observations

    model = parsed.realize_model()
    assert model.artifact.public_evidence_revision is None
    assert model.artifact.public_evidence_source_sha256 == expected_hashes
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

    assert str(sampled.provenance["model_version_id"]).startswith("model_version:")
    assert sampled.provenance["public_evidence_revision"] is None
    assert sampled.provenance["public_evidence_source_sha256"] == expected_hashes
    assert sampled.provenance["source_manifest"]
    assert sampled.levels.series_keys() >= set(required_level_series)

    changed_hashes = _changed_hashes(expected_hashes, synthetic_evidence_dir, tmp_path)
    changed_artifact = StateSpaceModelArtifact.model_validate(artifact).model_copy(
        update={"public_evidence_source_sha256": changed_hashes}
    )
    changed_blob = tmp_path / "changed_evidence.json"
    changed_blob.write_text(changed_artifact.model_dump_json(), encoding="utf-8")
    changed_model = parsed.model_copy(update={"trained_artifact_path": changed_blob}).realize_model()
    assert changed_model.evidence_set_id != model.evidence_set_id


def _expected_hashes(synthetic_evidence_dir: Path) -> dict[str, str]:
    return {
        source.provenance_label: hashlib.sha256(
            (synthetic_evidence_dir / source.output_filename).read_bytes()
        ).hexdigest()
        for source in (
            sources.FRED_SP500,
            sources.YAHOO_SPY,
            sources.YAHOO_BTC,
            sources.YAHOO_ETH,
            sources.FRED_CPI,
            sources.FRED_SFXRSA,
            sources.FRED_FHFA_SF,
            sources.FRED_MORTGAGE30,
            sources.ZILLOW_ZHVI,
            sources.ZILLOW_ZORI,
        )
    }


def _changed_hashes(expected_hashes: dict[str, str], synthetic_evidence_dir: Path, tmp_path: Path) -> dict[str, str]:
    # A second byte representation of an existing public source gives the identity check a
    # real SHA-256 value, while proving even byte-only evidence changes alter the evidence id.
    changed_bytes = (synthetic_evidence_dir / sources.FRED_CPI.output_filename).read_bytes() + b"\n"
    changed_path = tmp_path / "changed_CPIAUCSL.csv"
    changed_path.write_bytes(changed_bytes)
    return {**expected_hashes, sources.FRED_CPI.provenance_label: hashlib.sha256(changed_path.read_bytes()).hexdigest()}


def test_state_space_training_uses_dated_one_month_returns_across_a_cpi_gap(
    tmp_path: Path, synthetic_evidence_dir: Path
) -> None:
    cpi_path = synthetic_evidence_dir / sources.FRED_CPI.output_filename
    lines = cpi_path.read_text().splitlines()
    cpi_path.write_text("\n".join(line for line in lines if not line.startswith("2025-10-")) + "\n")
    evidence = load_exogenous_evidence()
    september_index = evidence.monthly_return_months.index("2025-09")
    assert evidence.monthly_return_months[september_index + 1] == "2025-12"

    out_manifest = tmp_path / "exogenous_provider.yaml"
    out_blob = tmp_path / "trained_state_space.json"
    train_main(["--model", "state_space", "--out-provider-config", str(out_manifest), "--out-blob", str(out_blob)])

    artifact = json.loads(out_blob.read_text(encoding="utf-8"))
    assert artifact["public_evidence_source_sha256"] == _expected_hashes(synthetic_evidence_dir)
    observed_months = artifact["source_manifest"]["monthly_return_months"]["observed"]
    assert observed_months == list(evidence.monthly_return_months)
    inflation_index = evidence.series_names.index(InflationKey())
    expected_mu = evidence.monthly_log_returns[:, inflation_index].mean()
    assert artifact["monthly_log_return_mu"][InflationKey().wire_id] == pytest.approx(expected_mu)


def test_state_space_fit_rejects_history_from_different_evidence(synthetic_evidence_dir: Path) -> None:
    historical, evidence = load_evidence()
    different_returns = evidence.monthly_log_returns.copy()
    different_returns[0, 0] += 0.01
    inconsistent_evidence = replace(evidence, monthly_log_returns=different_returns)

    with pytest.raises(ValueError, match="HistoricalSeries returns do not match"):
        fit_state_space_artifact(inconsistent_evidence, historical=historical)


if __name__ == "__main__":
    pytest_bazel.main()
