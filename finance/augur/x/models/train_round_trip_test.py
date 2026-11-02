"""Round-trip test: train a model offline, write the provider config (VECM embeds its
fitted state directly; state_space writes it to a separate blob), re-load via Pydantic +
`<Model>ProviderConfig.realize_model(...)`, and sample.

This is the public contract the augur server consumes at startup: read
`Config.models`, dispatch via the discriminated union, and
sample without re-fitting from source CSVs."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from pydantic import TypeAdapter

from finance.augur.fit.data import load_evidence, load_evidence_segments
from finance.augur.fit.evidence_data import load_exogenous_evidence
from finance.augur.fit.metrics import ScoredHeldOutResult, held_out_predictive_score
from finance.augur.model.conditioning import ObservationUnits
from finance.augur.model.exogenous import ExogenousSamplingRequest, level_series_request_channels
from finance.augur.model.series import HomeValueKey, InflationKey, LevelSeriesKey, RentKey
from finance.augur.x.models.provider_config import ProviderConfig
from finance.augur.x.models.state_space import StateSpaceProviderConfig
from finance.augur.x.models.state_space_fit import fit_state_space_artifact
from finance.augur.x.models.train import main as train_main
from finance.augur.x.models.vecm import VecmConfig, VecmModel, VecmProviderConfig
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

    assert str(sampled.provenance["model_version_id"]).startswith("model_version:")
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

    assert str(sampled.provenance["model_version_id"]).startswith("model_version:")
    assert sampled.provenance["source_manifest"]
    assert sampled.levels.series_keys() >= set(required_level_series)


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
    observed_months = artifact["source_manifest"]["monthly_return_months"]["observed"]
    assert observed_months == list(evidence.monthly_return_months)
    inflation_index = evidence.series_names.index(InflationKey())
    expected_mu = evidence.monthly_log_returns[:, inflation_index].mean()
    assert artifact["monthly_log_return_mu"][InflationKey().wire_id] == pytest.approx(expected_mu)


def test_gapped_evidence_trains_and_scores_vecm_from_real_dated_levels(
    tmp_path: Path, synthetic_evidence_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cpi_path = synthetic_evidence_dir / sources.FRED_CPI.output_filename
    lines = cpi_path.read_text().splitlines()
    cpi_path.write_text("\n".join(line for line in lines if not line.startswith("2025-10-")) + "\n")
    historical, evidence = load_evidence_segments()
    assert historical.gaps[0].missing_months == ("2025-10",)

    transitions = historical.transition_pairs
    train_end = round(len(transitions) * 0.9)
    model = VecmModel(config=VecmConfig(n_iters=5, seed=13))
    model.fit_dated(historical.prefix_through(transitions[train_end - 1][1]))
    scored = held_out_predictive_score(model, historical, train_fraction=0.9)

    assert isinstance(scored, ScoredHeldOutResult)
    assert scored.calendar_coverage is not None
    assert scored.calendar_coverage.gaps[0][2] == ("2025-10",)
    assert scored.held_out_count == len(transitions) - train_end
    assert scored.joint_log_density_per_month == pytest.approx(scored.joint_log_density_total / scored.held_out_count)

    out_manifest = tmp_path / "gapped_vecm_provider.yaml"
    monkeypatch.setattr("finance.augur.x.models.train.VecmConfig", lambda: VecmConfig(n_iters=5, seed=13))
    train_main(["--model", "vecm", "--out-provider-config", str(out_manifest)])
    parsed = _ADAPTER.validate_python(yaml.safe_load(out_manifest.read_text(encoding="utf-8")))
    assert isinstance(parsed, VecmProviderConfig)
    state = parsed.trained_state
    assert state.calendar_gaps[0].missing_months == ("2025-10",)
    assert sum(segment.used_transition_count for segment in state.training_segments) == len(transitions)
    assert state.train_months == historical.months
    assert state.as_of_month == historical.months[-1]
    assert {point.observed_at.strftime("%Y-%m") for point in parsed.latest_observations.values()} == {
        historical.months[-1]
    }
    assert parsed.latest_observations["security:SPY"].units == ObservationUnits.USD_PER_UNIT
    assert parsed.latest_observations["security:SPY"].source_id == f"public:{sources.YAHOO_SPY.provenance_label}"
    # FRED's SP500 price index remains auxiliary; it is not substituted for SPY adjusted close.
    assert evidence.metadata.auxiliary_observations["sp500_price"].source_id == (
        f"public:{sources.FRED_SP500.provenance_label}"
    )


def test_state_space_fit_rejects_history_from_different_evidence(synthetic_evidence_dir: Path) -> None:
    historical, evidence = load_evidence()
    different_levels = evidence.monthly_level_values.copy()
    different_levels[0, 0] += 0.01
    inconsistent_evidence = replace(evidence, monthly_level_values=different_levels)

    with pytest.raises(ValueError, match="historical levels do not match"):
        fit_state_space_artifact(inconsistent_evidence, historical=historical)


def test_state_space_fit_rejects_returns_inconsistent_with_dated_levels(synthetic_evidence_dir: Path) -> None:
    _, evidence = load_evidence()
    different_returns = evidence.monthly_log_returns.copy()
    different_returns[0, 0] += 0.01
    inconsistent_evidence = replace(evidence, monthly_log_returns=different_returns)

    with pytest.raises(ValueError, match="monthly returns do not match"):
        fit_state_space_artifact(inconsistent_evidence)


if __name__ == "__main__":
    pytest_bazel.main()
