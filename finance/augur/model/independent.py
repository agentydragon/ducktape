"""Independent-per-series exogenous provider configured from YAML.

The provider enumerates every external series the simulator may request, grouped
by role (asset-price `sp500`/`crypto`; property-value `home_value`; index
`inflation`/`rent`), each mapped to a scalar level model (Constant / Deterministic
/ GBM). It samples no private-equity marks; its PE bundle is empty. There is no
prefix dispatch: config keys are already typed.
"""

from __future__ import annotations

from typing import Literal

import jax.numpy as jnp
import numpy as np
from numpyro import distributions as dist

from finance.augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle
from finance.augur.model.gbm import GeometricBrownian
from finance.augur.model.level_series_groups import LevelSeriesGroups
from finance.augur.model.path_models.scenarios import HistoricalSeries
from finance.augur.model.series import IssuerId, LevelSeriesKey
from finance.augur.model.series_model import ScalarSeriesSpec, sample_independent_levels


class IndependentProviderConfig(LevelSeriesGroups[ScalarSeriesSpec]):
    """YAML provider that enumerates every level series explicitly.

    Level series are the role sub-groups inherited from
    `LevelSeriesGroups` (`asset_prices` = `sp500`/`crypto`; `property_values` =
    `home_value`; `index_series` = `inflation`/`rent`). `extra="forbid"`
    (from `FrozenModel`) rejects stray keys, including legacy `"security:btc"`-style wire ids.
    """

    type: Literal["independent"] = "independent"

    def realize_model(self) -> IndependentModel:
        # Pass the role sub-groups through structurally (no flatten/re-expand), dropping
        # the config-only `type` sibling.
        return IndependentModel(
            asset_prices=self.asset_prices,
            security_distributions=self.security_distributions,
            property_values=self.property_values,
            index_series=self.index_series,
        )


class IndependentModel(LevelSeriesGroups[ScalarSeriesSpec]):
    """Runtime exogenous model built from an `IndependentProviderConfig`.

    Implements `Sampler` (the runtime sampling contract) and `Scorable` (the
    metric battery contract). No `Fittable` — params are YAML-set, not fit.

    Holds the level-series specs as the role sub-groups inherited from
    `LevelSeriesGroups` (`asset_prices`/`property_values`/`index_series`) — the same
    role-separated shape as the config and the sampled bundle, so nothing is flattened
    to an opaque key/value map.
    """

    label: str = "independent"

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        return frozenset(self._level_specs_by_level_key())

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        # PE-bundle emission is a CompositeModel + PE-provider job.
        return frozenset()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        # The inherited role groups carry only level series. This provider does not
        # synthesize the typed PE protocol bundle.
        frames = sample_independent_levels(self, request)
        return SampledExogenousBundle(levels=frames, model_id=self.label)

    def predictive(self, historical: HistoricalSeries, t: int, *, horizon: int = 1) -> dist.Distribution | None:
        """Joint predictive over the cumulative `horizon`-step log-return at origin t, for the
        series `historical.series_names` names.

        There is no factor basis here and no covariance to speak of: under per-series
        independence — the whole point of this provider — the joint is a `MultivariateNormal`
        with DIAGONAL covariance, and the marginal for series i is `N(horizon · μ_i,
        horizon · σ_i²)`, since h independent N(μ, σ²) increments cumulate to N(hμ, hσ²).

        Returns `None` if any requested series isn't backed by a GBM scalar — a Constant or
        Deterministic series has zero predictive variance and the density is degenerate.
        """
        if horizon < 1:
            raise ValueError(f"horizon must be >= 1; got {horizon}")
        n_steps = historical.levels.shape[0] - 1
        if t + horizon > n_steps:
            return None

        # `historical.series_names` are typed LevelSeriesKeys; match them directly against
        # the typed level-spec map (a series the provider doesn't model isn't present → None).
        spec_by_series = self._level_specs_by_level_key()
        mus: list[float] = []
        sigmas: list[float] = []
        for series in historical.series_names:
            spec = spec_by_series.get(series)
            if not isinstance(spec, GeometricBrownian):
                return None
            mus.append(float(spec.monthly_log_return_mu) * horizon)
            sigmas.append(float(spec.monthly_log_return_sigma) * np.sqrt(horizon))
        mean_arr = jnp.asarray(np.asarray(mus, dtype=np.float32))
        sd_arr = jnp.asarray(np.asarray(sigmas, dtype=np.float32))
        cov_arr = jnp.diag(sd_arr**2)
        return dist.MultivariateNormal(mean_arr, covariance_matrix=cov_arr)

    def _level_specs_by_level_key(self) -> dict[LevelSeriesKey, ScalarSeriesSpec]:
        """The provider's level specs keyed by their typed `LevelSeriesKey` — the role
        projections unioned into one map, for `emittable_level_keys` and `predictive`."""

        return self.by_level_key()
