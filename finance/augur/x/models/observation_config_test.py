"""A fitted dated VECM state and its source anchors cross one strict typed boundary."""

from datetime import date

import numpy as np
import pytest
import pytest_bazel
import yaml
from pydantic import ValidationError

from finance.augur.model.conditioning import ExogenousObservedPoint, ObservationTreatment, ObservationUnits
from finance.augur.model.evidence import EvidenceMetadata
from finance.augur.model.exogenous import ExogenousSamplingRequest, level_series_request_channels
from finance.augur.model.path_models.scenarios import DatedHistoricalSeries
from finance.augur.model.series import SP500_KEY, InflationKey
from finance.augur.x.models.vecm import VecmConfig, VecmModel, VecmProviderConfig
from util.bazel.runfiles import get_required_path, own_repo_rlocation


@pytest.fixture(scope="module")
def configured() -> VecmProviderConfig:
    months = ("2024-01", "2024-02", "2024-03", "2024-05", "2024-06", "2024-07")
    levels = np.asarray(
        [[470.0, 310.0], [472.0, 310.6], [469.0, 311.0], [475.0, 312.2], [478.0, 312.4], [481.0, 313.1]],
        dtype=np.float64,
    )
    historical = DatedHistoricalSeries(series_names=(SP500_KEY, InflationKey()), levels=levels, months=months)
    model = VecmModel(config=VecmConfig(n_iters=5, seed=29))
    model.fit_dated(historical)

    latest_observations = {
        SP500_KEY.wire_id: ExogenousObservedPoint(
            value=float(levels[-1, 0]),
            units=ObservationUnits.USD_PER_UNIT,
            observed_at=date.fromisoformat(f"{months[-1]}-01"),
            source_id="test:spy-adjusted-close",
            treatment=ObservationTreatment.HARD_START,
        ),
        InflationKey().wire_id: ExogenousObservedPoint(
            value=float(levels[-1, 1]),
            units=ObservationUnits.INDEX_POINTS,
            observed_at=date.fromisoformat(f"{months[-1]}-01"),
            source_id="test:cpi-index",
            treatment=ObservationTreatment.HARD_START,
        ),
    }
    metadata = EvidenceMetadata(
        auxiliary_observations={
            "sp500_price": ExogenousObservedPoint(
                value=5_400.0,
                units=ObservationUnits.INDEX_POINTS,
                observed_at=date.fromisoformat(f"{months[-1]}-01"),
                source_id="test:fred-sp500-price",
                treatment=ObservationTreatment.INFORMATIVE,
            )
        }
    )
    return VecmProviderConfig(
        trained_state=model.to_trained_state(),
        latest_observations=latest_observations,
        evidence_metadata=metadata,
        current_mortgage30_rate_pct=6.23,
    )


def test_fitted_artifact_retains_dated_levels_anchors_and_units(configured: VecmProviderConfig) -> None:
    expected = {
        "security:SPY": (481.0, ObservationUnits.USD_PER_UNIT),
        "inflation": (313.1, ObservationUnits.INDEX_POINTS),
    }
    assert {factor: (point.value, point.units) for factor, point in configured.latest_observations.items()} == expected
    assert configured.trained_state.as_of_month == "2024-07"
    assert configured.trained_state.train_months == ("2024-01", "2024-02", "2024-03", "2024-05", "2024-06", "2024-07")
    assert configured.trained_state.calendar_gaps[0].missing_months == ("2024-04",)
    assert configured.trained_state.training_segments[0].used_transition_count == 2
    assert configured.trained_state.training_segments[1].used_transition_count == 2
    assert configured.evidence_metadata.auxiliary_observations["sp500_price"].units == ObservationUnits.INDEX_POINTS

    model = configured.realize_model()
    request = ExogenousSamplingRequest(
        rollout_seeds=(71, 99), horizon_months=2, **level_series_request_channels(frozenset(model.factor_names))
    )
    sampled = model.sample(request)
    restored = VecmProviderConfig.model_validate_json(configured.model_dump_json()).realize_model()
    replayed = restored.sample(request)
    for factor in model.factor_names:
        actual = sampled.level_matrix(factor, rollout_count=2, horizon_months=2)
        assert actual[:, 0].tolist() == [expected[factor.wire_id][0]] * 2
        np.testing.assert_array_equal(actual, replayed.level_matrix(factor, rollout_count=2, horizon_months=2))


@pytest.mark.parametrize("missing", ["units", "observed_at", "source_id"])
def test_observations_require_units_date_and_source(configured: VecmProviderConfig, missing: str) -> None:
    point = configured.latest_observations["inflation"].model_dump(mode="json")
    del point[missing]
    with pytest.raises(ValidationError, match=missing):
        ExogenousObservedPoint.model_validate(point)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True])
def test_observations_reject_nonfinite_or_boolean_values(configured: VecmProviderConfig, value: float) -> None:
    point = configured.latest_observations["inflation"].model_dump(mode="json")
    point["value"] = value
    with pytest.raises(ValidationError):
        ExogenousObservedPoint.model_validate(point)


def test_auxiliary_evidence_never_supplies_a_missing_anchor(configured: VecmProviderConfig) -> None:
    document = configured.model_dump(mode="json")
    del document["latest_observations"]["security:SPY"]
    # An actual FRED equity-index observation still exists as auxiliary evidence.
    assert document["evidence_metadata"]["auxiliary_observations"]["sp500_price"]
    with pytest.raises(ValidationError, match="observations must match the fitted factors exactly"):
        VecmProviderConfig.model_validate(document)


def test_source_named_and_bare_numeric_anchors_are_not_an_alternate_format(configured: VecmProviderConfig) -> None:
    document = configured.model_dump(mode="json")
    document["latest_observations"] = {"spy_adjusted_close_latest": 481.0}
    with pytest.raises(ValidationError):
        VecmProviderConfig.model_validate(document)


def test_legacy_checked_in_vecm_artifact_requires_refitting() -> None:
    path = get_required_path(own_repo_rlocation("finance/augur/x/models/calibrated/trained_vecm_provider.yaml"))
    document = yaml.safe_load(path.read_text())
    with pytest.raises(ValidationError) as error:
        VecmProviderConfig.model_validate(document)
    missing_fields = {item["loc"][-1] for item in error.value.errors() if item["type"] == "missing"}
    assert {"train_months", "as_of_month", "training_segments", "calendar_gaps"} <= missing_fields


if __name__ == "__main__":
    pytest_bazel.main()
