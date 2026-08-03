"""Independent-per-series exogenous provider configured from YAML.

The provider enumerates every external series the simulator may request, grouped
by magisterium (asset-price `sp500`/`crypto`; property-value `home_value`; index
`inflation`/`rent`), each mapped to a scalar level model (Constant / Deterministic
/ GBM). Private-equity marks are a separate `private_equity_marks` map keyed by
issuer id — they are not level series and travel via the typed PE bundle /
metadata, not the `levels` frame. There is no prefix dispatch: config keys are
already typed, so the level-vs-PE split is structural rather than parsed.

The model is the only source of price for any series it covers (including the
per-issuer current PE price), exposed both as the month-0 level and as the
`private_equity_prices_usd` metadata dict on the sampled bundle.
"""

from __future__ import annotations

from typing import Literal, assert_never

import jax.numpy as jnp
import numpy as np
from numpyro import distributions as dist
from pydantic import Field

from finance.augur.model.deterministic import Constant, Deterministic
from finance.augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle
from finance.augur.model.gbm import GeometricBrownian
from finance.augur.model.level_series_groups import LevelSeriesMagisteria
from finance.augur.model.path_models.scenarios import HistoricalSeries
from finance.augur.model.series import IssuerId, LevelSeriesKey
from finance.augur.model.series_model import ScalarSeriesSpec, sample_independent_levels


class IndependentProviderConfig(LevelSeriesMagisteria[ScalarSeriesSpec]):
    """YAML provider that enumerates every level series and PE mark explicitly.

    Level series are the magisterium sub-groups inherited from
    `LevelSeriesMagisteria` (`asset_prices` = `sp500`/`crypto`; `property_values` =
    `home_value`; `index_series` = `inflation`/`rent`). `private_equity_marks` carries
    per-issuer mark specs separately — PE marks are not level series. `extra="forbid"`
    (from `FrozenModel`) rejects stray keys, including legacy `"crypto:btc"`-style wire ids.
    """

    type: Literal["independent"] = "independent"
    private_equity_marks: dict[IssuerId, ScalarSeriesSpec] = Field(default_factory=dict)

    def realize_model(self) -> IndependentModel:
        # Pass the magisterium sub-groups through structurally (no flatten/re-expand), dropping
        # the config-only `type` sibling; PE marks travel separately as `pe_marks`.
        return IndependentModel(
            asset_prices=self.asset_prices,
            property_values=self.property_values,
            index_series=self.index_series,
            pe_marks=dict(self.private_equity_marks),
        )


class IndependentModel(LevelSeriesMagisteria[ScalarSeriesSpec]):
    """Runtime exogenous model built from an `IndependentProviderConfig`.

    Implements `Sampler` (the runtime sampling contract) and `Scorable` (the
    metric battery contract). No `Fittable` — params are YAML-set, not fit.

    Holds the level-series specs as the magisterium sub-groups inherited from
    `LevelSeriesMagisteria` (`asset_prices`/`property_values`/`index_series`) — the same
    magisterium-separated shape as the config and the sampled bundle, so nothing is flattened
    to an opaque key/value map. `pe_marks` is keyed by typed `IssuerId`. The level-vs-PE split
    is structural (it came from typed config), so this model never parses a prefix.
    """

    label: str = "independent"
    pe_marks: dict[IssuerId, ScalarSeriesSpec] = Field(default_factory=dict)

    @property
    def factor_names(self) -> tuple[LevelSeriesKey, ...]:
        """The typed level-series keys this provider models, in iteration order.

        PE marks are not included — they are not level series and have no GBM marginal.
        """

        return tuple(self._level_specs_by_level_key())

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        return frozenset(self._level_specs_by_level_key())

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        # PE marks live in `pe_marks` but are scalar mark generators only — this provider
        # doesn't synthesize a full PrivateEquityBundle, so it advertises no PE issuers as
        # bundle-emittable. PE-bundle emission is a CompositeModel + PE-provider job.
        return frozenset()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        # PE marks travel via the typed PE bundle / metadata, never a level magisterium
        # — the inherited magisterium groups carry only level series.
        frames = sample_independent_levels(self, request)
        return SampledExogenousBundle(
            **frames.as_bundle_kwargs(),
            metadata={"model_id": self.label, "private_equity_prices_usd": self._private_equity_prices_usd()},
        )

    def predictive(self, historical: HistoricalSeries, t: int, *, horizon: int = 1) -> dist.Distribution | None:
        """Joint predictive over the cumulative `horizon`-step log-return at
        origin t for the factor list `historical.factor_names`.

        Under per-series independence (the whole point of this provider) the
        joint is a `MultivariateNormal` with diagonal covariance. The
        marginal for factor i is `N(horizon · μ_i, horizon · σ_i²)` — h
        independent N(μ, σ²) increments cumulate to N(hμ, hσ²).

        Returns `None` if any factor in `historical.factor_names` isn't
        backed by a GBM scalar in the provider config — Constant /
        Deterministic factors have zero predictive variance and the
        density is degenerate.
        """
        if horizon < 1:
            raise ValueError(f"horizon must be >= 1; got {horizon}")
        n_steps = historical.levels.shape[0] - 1
        if t + horizon > n_steps:
            return None

        # `historical.factor_names` are typed LevelSeriesKeys; match them directly against
        # the typed level-spec map (a series the provider doesn't model isn't present → None).
        spec_by_factor = self._level_specs_by_level_key()
        mus: list[float] = []
        sigmas: list[float] = []
        for factor in historical.factor_names:
            spec = spec_by_factor.get(factor)
            if not isinstance(spec, GeometricBrownian):
                return None
            mus.append(float(spec.monthly_log_return_mu) * horizon)
            sigmas.append(float(spec.monthly_log_return_sigma) * np.sqrt(horizon))
        mean_arr = jnp.asarray(np.asarray(mus, dtype=np.float32))
        sd_arr = jnp.asarray(np.asarray(sigmas, dtype=np.float32))
        cov_arr = jnp.diag(sd_arr**2)
        return dist.MultivariateNormal(mean_arr, covariance_matrix=cov_arr)

    def _level_specs_by_level_key(self) -> dict[LevelSeriesKey, ScalarSeriesSpec]:
        """The provider's level specs keyed by their typed `LevelSeriesKey`.

        The magisterium projections (each keyed within its own magisterium) union
        into one `LevelSeriesKey`-keyed map for `factor_names` / `predictive`.
        """

        # The explicit `dict[LevelSeriesKey, ...]` annotation gives the `dict(...)` its key-type
        # context, so the narrow per-magisterium keys widen cleanly (dict keys are invariant,
        # so an unannotated `{**a, **b}` would not unify them into `LevelSeriesKey`).
        specs: dict[LevelSeriesKey, ScalarSeriesSpec] = dict(
            (
                *self.asset_prices.by_asset_price_key().items(),
                *self.property_values.by_property_value_key().items(),
                *self.index_series.by_index_series_key().items(),
            )
        )
        return specs

    def _private_equity_prices_usd(self) -> dict[str, float]:
        return {str(issuer_id): _month_zero_level(spec) for issuer_id, spec in self.pe_marks.items()}


def _month_zero_level(spec: ScalarSeriesSpec) -> float:
    if isinstance(spec, Constant):
        return float(spec.value)
    if isinstance(spec, Deterministic):
        return float(spec.levels[0])
    if isinstance(spec, GeometricBrownian):
        return float(spec.initial_value)
    assert_never(spec)
