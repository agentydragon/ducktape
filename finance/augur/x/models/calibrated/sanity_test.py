"""Every checked-in fit artifact describes a plausible economy, or is quarantined.

Sampling without error proves nothing about a fit, and nothing downstream inspects a central
path. So each artifact beside this file is realized as the sampler it ships as, and checked for
in-sample one-step residuals on the scale the model claims for its innovations (where the
artifact embeds the sample it was fitted on) and for a 12-month central path inside the US
historical record.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import pytest_bazel
import yaml
from numpyro import distributions as dist
from pydantic import TypeAdapter

from finance.augur.fit.model import Scorable
from finance.augur.model.equity import EquitySpec
from finance.augur.model.exogenous import (
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    Sampler,
    level_series_request_channels,
)
from finance.augur.model.path_models.scenarios import HistoricalSeries, historical_log_returns
from finance.augur.model.series import SP500_KEY, SP500_SYMBOL, InflationKey, LevelSeriesKey
from finance.augur.x.models.structural_macro import (
    EquityProcess,
    FittedEquityMean,
    StructuralMacroFittedDefaults,
    StructuralMacroProviderConfig,
)
from finance.augur.x.models.vecm import VecmProviderConfig

# Artifacts that fail the checks below, with what they measured. A quarantined artifact stays only
# for the typed boundaries it still exercises and is never to be used as a model. The test requires
# it to keep failing, so a refit that passes has to delete its entry.
QUARANTINED = {
    "trained_vecm_provider.yaml": (
        "12-month median CPI x1.69 and SPY x0.71; in-sample CPI residual RMS 5.1 modelled sds"
        " (one-step mean +1.6%/month vs +0.29% observed, sd 3.2% vs 0.69%)"
    )
}

ARTIFACT_DIR = Path(__file__).parent
ARTIFACTS = sorted(path.name for path in ARTIFACT_DIR.glob("*.yaml"))

HORIZON_MONTHS = 12
ROLLOUT_COUNT = 1000

# Bands on each series' median 12-month multiplier, generous enough to catch only an absurd
# central path:
# - CPI: every 12-month US CPI change since 1913 lies inside; the extremes are +23.7% to June
#   1920 and -15.8% to June 1921 (BLS).
# - Broad equity: a central path is a drift and one year of equity is mostly noise, so the
#   envelope is decade-annualised. No rolling decade since 1926 compounded below about -5%/yr
#   (to 1939) or above about +21%/yr (to 1959); widened to round numbers.
CENTRAL_PATH_BANDS: dict[LevelSeriesKey, tuple[float, float]] = {InflationKey(): (0.84, 1.24), SP500_KEY: (0.90, 1.25)}

# Per-series RMS of in-sample one-step residuals in modelled innovation sds. A correctly
# specified model gives 1, with a sampling sd near 1/sqrt(2n) (0.07 at n = 98); a factor of two
# either way flags only a modelled scale that is off by a multiple.
RESIDUAL_SCALE_BAND = (0.5, 2.0)


def _load(name: str) -> VecmProviderConfig | StructuralMacroFittedDefaults:
    # A kind of fit outside this union fails to parse, so it cannot be checked in without saying
    # how to realize it below.
    adapter: TypeAdapter[VecmProviderConfig | StructuralMacroFittedDefaults] = TypeAdapter(
        VecmProviderConfig | StructuralMacroFittedDefaults
    )
    return adapter.validate_python(yaml.safe_load((ARTIFACT_DIR / name).read_text(encoding="utf-8")))


def _assert_residual_scale(model: Scorable, fitted_on: HistoricalSeries) -> None:
    standardized = []
    for origin, observed in enumerate(historical_log_returns(fitted_on)):
        predictive = model.predictive(fitted_on, origin)
        if not isinstance(predictive, dist.MultivariateNormal):
            raise TypeError(f"no closed-form marginals for {predictive=}")
        standardized.append(
            (observed - np.asarray(predictive.mean)) / np.sqrt(np.diag(np.asarray(predictive.covariance_matrix)))
        )
    scales = {
        key.wire_id: float(scale)
        for key, scale in zip(fitted_on.series_names, np.sqrt(np.mean(np.square(standardized), axis=0)), strict=True)
    }
    lower, upper = RESIDUAL_SCALE_BAND
    assert all(lower <= scale <= upper for scale in scales.values()), (
        f"in-sample residual RMS in modelled sds: {scales}"
    )


def _median_multiplier(sampled: SampledExogenousBundle, key: LevelSeriesKey) -> float:
    levels = sampled.level_matrix(key, rollout_count=ROLLOUT_COUNT, horizon_months=HORIZON_MONTHS)
    return float(np.median(levels[:, -1] / levels[:, 0]))


def _assert_central_path_plausible(model: Sampler) -> None:
    """The median rollout is the model's central path (for the log-linear Gaussian models here,
    exactly its deterministic skeleton), taken through `sample` so the check covers the sampling
    code that ships rather than a re-derivation of each model's recurrence."""

    sampled = model.sample(
        ExogenousSamplingRequest(
            horizon_months=HORIZON_MONTHS,
            rollout_seeds=tuple(range(ROLLOUT_COUNT)),
            **level_series_request_channels(CENTRAL_PATH_BANDS.keys()),
        )
    )
    medians = {key.wire_id: _median_multiplier(sampled, key) for key in CENTRAL_PATH_BANDS}
    assert all(lower <= medians[key.wire_id] <= upper for key, (lower, upper) in CENTRAL_PATH_BANDS.items()), (
        f"median 12-month multipliers: {medians}"
    )


def _check_vecm(artifact: VecmProviderConfig) -> None:
    model = artifact.realize_model()
    levels = np.exp(model.train_log_levels)
    _assert_residual_scale(
        model,
        # The artifact keeps no month labels, and `predictive` reads only the levels.
        HistoricalSeries(
            series_names=model.factor_names, levels=levels, months=tuple(str(month) for month in range(len(levels)))
        ),
    )
    _assert_central_path_plausible(model)


def _check_structural_macro(artifact: StructuralMacroFittedDefaults) -> None:
    # No residual-scale check: the fit's sample is the augur-evidence checkout, which tests cannot
    # read, and every innovation scale it carries is its own residuals' sample moment
    # (`macro_var.py`, `equity_fit.py`), so the check would pass by construction.
    _assert_central_path_plausible(
        StructuralMacroProviderConfig(
            macro_state=artifact.macro_state,
            equity=EquityProcess(
                # The fit names no instrument; its equity is bound to `SP500_KEY`, where every
                # artifact's broad market is read.
                instrument=EquitySpec(symbol=SP500_SYMBOL, initial_price_usd=100.0),
                mean=FittedEquityMean(monthly_log_return_mu=artifact.equity_monthly_log_return_mu),
                monthly_log_return_sigma=artifact.equity_monthly_log_return_sigma,
            ),
        ).realize_model()
    )


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(name, marks=pytest.mark.xfail(raises=AssertionError, reason=QUARANTINED[name], strict=True))
        if name in QUARANTINED
        else name
        for name in ARTIFACTS
    ],
)
def test_artifact_describes_a_plausible_economy(name: str) -> None:
    artifact = _load(name)
    if isinstance(artifact, VecmProviderConfig):
        _check_vecm(artifact)
    else:
        _check_structural_macro(artifact)


def test_quarantine_names_checked_in_artifacts() -> None:
    # Also the anti-vacuity guard: an empty glob leaves the test above no cases, which pytest
    # reports as a skip and Bazel as a pass.
    assert ARTIFACTS
    assert QUARANTINED.keys() <= set(ARTIFACTS)


if __name__ == "__main__":
    pytest_bazel.main()
