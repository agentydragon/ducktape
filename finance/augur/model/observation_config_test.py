"""Actual fitted artifact and source observations cross one strict typed boundary."""

from datetime import date

import numpy as np
import pytest
import pytest_bazel
import yaml
from pydantic import ValidationError

from finance.augur.model.conditioning import ExogenousObservedPoint, ObservationUnits
from finance.augur.model.exogenous import ExogenousSamplingRequest, level_series_request_channels
from finance.augur.model.vecm import VecmProviderConfig
from util.bazel.runfiles import get_required_path, own_repo_rlocation


@pytest.fixture
def configured() -> VecmProviderConfig:
    path = get_required_path(own_repo_rlocation("finance/augur/fit/calibrated/trained_vecm_provider.yaml"))
    return VecmProviderConfig.model_validate(yaml.safe_load(path.read_text()))


def test_checked_in_artifact_retains_actual_anchors_and_units(configured: VecmProviderConfig) -> None:
    expected = {
        "security:SPY": (711.5800170898438, ObservationUnits.USD_PER_UNIT),
        "security:btc": (76803.421875, ObservationUnits.USD_PER_UNIT),
        "security:eth": (2094.830078125, ObservationUnits.USD_PER_UNIT),
        "home_value:san_francisco_ca": (1356661.6050197445, ObservationUnits.USD),
        "home_value:vallejo_ca": (520969.8257389618, ObservationUnits.USD),
        # This historical artifact used rent CPI, not the current loader's dollar ZORI.
        "rent:san_francisco_ca": (528.147, ObservationUnits.INDEX_POINTS),
        "inflation": (330.293, ObservationUnits.INDEX_POINTS),
    }
    assert {factor: (point.value, point.units) for factor, point in configured.latest_observations.items()} == expected
    rent = configured.latest_observations["rent:san_francisco_ca"]
    assert rent.observed_at == date(2026, 3, 1)
    assert rent.source_id == "public:../../data/fred_sf_rent_cpi.csv"
    assert configured.evidence_metadata.adjusted_close_return_count == 399
    assert len(configured.evidence_metadata.return_sources) == 4
    assert configured.evidence_metadata.auxiliary_observations["mortgage30"].value == 6.23

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
    document["latest_observations"] = {"spy_adjusted_close_latest": 711.5800170898438}
    with pytest.raises(ValidationError):
        VecmProviderConfig.model_validate(document)


if __name__ == "__main__":
    pytest_bazel.main()
