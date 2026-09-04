"""Vector Error Correction Model (VECM) as a NumPyro generative model.

  Δr_t = α (β' x_{t-1} + μ) + ε_t,  ε_t ~ N(0, Σ)

where x_t is the F-vector of log-levels. Identification: β[0] = 1 fixed,
β[1:] free. We always use coint_rank = 1; for the configured factors a
single cointegrating relationship is what binds rent and CPI to a shared
trend rather than letting them drift apart over a 30-year horizon.

The generative function `_vecm_generative(log_levels)` is the single source
of truth used by:

- `VecmModel.fit(historical)` — SVI / MAP optimisation over the model's
  free parameters with the obs site bound to the observed differences.
  Weakly-informative priors approximate maximum-likelihood — augur tracks
  fit quality via held-out predictive density on the metric battery, not
  by parameter equivalence with the previous statsmodels implementation.
- `VecmModel.predictive(historical, t, horizon=h)` — returns the joint
  predictive `MultivariateNormal` over the cumulative h-step log-return,
  closed form at h=1, MC-fitted Gaussian at h>1.
- `VecmModel.sample(request)` — rolls the recurrence forward stochastically
  per rollout, converts log-level paths to multipliers, routes each typed
  level key onto the factor that *is* its key (factor identity == LevelSeriesKey),
  scales by the deployment's `latest_observations`, and returns a
  `SampledExogenousBundle`.

VecmModel implements Sampler + Fittable + Scorable from one class — the
trainable thing and the runtime sampler are the same object, parameterised
by `params` once `fit` (or `from_trained_state`) has run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal

import jax
import jax.numpy as jnp
import numpy as np
import numpyro
import numpyro.distributions as dist
from numpyro.infer import SVI, Trace_ELBO
from numpyro.infer.autoguide import AutoDelta
from numpyro.optim import Adam
from pydantic import Field

from finance.augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle, assemble_level_frames
from finance.augur.model.path_models.scenarios import HistoricalSeries
from finance.augur.model.provenance import stable_identity_digest
from finance.augur.model.schemas import FrozenModel
from finance.augur.model.series import (
    SP500_SYMBOL,
    HomeValueKey,
    InflationKey,
    IssuerId,
    LevelSeriesKey,
    RentKey,
    SecurityKey,
    SecuritySymbol,
    parse_level_series_key,
)

_SECURITY_ANCHOR_OBSERVATIONS: Mapping[SecuritySymbol, tuple[str, str | None]] = {
    SP500_SYMBOL: ("spy_adjusted_close_latest", "sp500_price_latest")
}

# Sample count for h>1 MC predictive density.
_MC_HORIZON_SAMPLES = 5000

# NumPyro inference defaults — informative priors + Adam SVI. The previous
# "weakly informative" Normal(0, 100) scales on α, β let AutoDelta walk to
# a degenerate MAP where |α|, |β| ≈ 30-50 and the cointegrating residual
# β·x_t + μ ran to ≈ 16 on held-out months. Prediction E[Δr] = α·(β·x+μ)
# then exploded to log-returns of magnitude ~10² while observed Δr ≈ 10⁻²,
# tanking held-out log-density by ~5e5 nats/month. VECM is only weakly
# identified without a rank-restriction (Johansen's eigendecomposition does
# that explicitly; we don't, so we ridge-regularise instead). At
# α ~ N(0, 0.1), β_tail ~ N(0, 0.5), 20k iters @ lr=0.005, held-out
# per_month lands at ~+15.85 nats (baseline statsmodels was +17.79) and
# multi_step h=12 at ~+0.88 (baseline -5.50; the NumPyro fit is actually
# better at long horizons — the regularisation curbs the spurious mean
# reversion the old MLE picked up).
_DEFAULT_FIT_ITERS = 20000
_DEFAULT_LEARNING_RATE = 0.005
_ALPHA_PRIOR_SCALE = 0.1
_BETA_TAIL_PRIOR_SCALE = 0.5
_CONST_COINT_PRIOR_SCALE = 5.0
# Cholesky parameterisation lives on the log scale. A typical monthly
# log-return residual has σ in the 0.005-0.05 range, so log σ ∈ [-5.3, -3.0].
# Prior Normal(-4, 2) covers σ ∈ [≈0.002, ≈0.135] within ±1 sd; not biasing,
# and well-conditioned: SVI gradients on log_diag carry orders-of-magnitude
# rescaling through `exp(...)` instead of getting flattened by a saturating
# softplus. The previous `chol_raw=Normal(0,1)` + softplus init started at
# σ ≈ 0.7 (~70× too large) and softplus saturated as `chol_raw` decreased,
# trapping SVI at the prior mode and tanking held-out log-density.
#
# Off-diagonal terms are relative row loadings, not raw log-return standard
# deviations. This keeps a low-volatility target factor such as CPI from
# inheriting 20-50% monthly residual volatility merely because it is correlated
# with an earlier high-volatility factor like crypto.
_LOG_DIAG_PRIOR_MEAN = -4.0
_LOG_DIAG_PRIOR_STD = 2.0
_OFFDIAG_PRIOR_STD = 0.1


def _vecm_generative(log_levels: jnp.ndarray) -> None:
    """VECM(k=1, r=1) joint distribution on Δr given x_{t-1}.

    Inputs are the observed log-levels `(n_obs, n_factors)`. The function
    declares sample sites for the model parameters with weak priors and an
    obs site for Δr conditioned on the inferred parameters. Used for
    fitting, scoring, and sampling — see module docstring.

    The residual covariance Σ = chol · cholᵀ is parameterised by `log_diag`
    (length n_factors) and a flat vector of strictly-lower-triangular
    off-diagonals; the log-scale diagonal sidesteps the softplus-saturation
    pathology that broke the earlier parameterisation.
    """
    _n_obs, n_factors = log_levels.shape
    # β identification: β[0] = 1 fixed, β[1:] free (Johansen normalisation).
    beta_tail = numpyro.sample("beta_tail", dist.Normal(jnp.zeros(n_factors - 1), _BETA_TAIL_PRIOR_SCALE).to_event(1))
    beta = jnp.concatenate([jnp.ones(1), beta_tail])
    alpha = numpyro.sample("alpha", dist.Normal(jnp.zeros(n_factors), _ALPHA_PRIOR_SCALE).to_event(1))
    const_coint = numpyro.sample("const_coint", dist.Normal(0.0, _CONST_COINT_PRIOR_SCALE))
    log_diag = numpyro.sample(
        "log_diag", dist.Normal(jnp.full((n_factors,), _LOG_DIAG_PRIOR_MEAN), _LOG_DIAG_PRIOR_STD).to_event(1)
    )
    n_offdiag = n_factors * (n_factors - 1) // 2
    offdiag_flat = numpyro.sample("offdiag_flat", dist.Normal(jnp.zeros(n_offdiag), _OFFDIAG_PRIOR_STD).to_event(1))
    chol = _build_cholesky(jnp.asarray(log_diag), jnp.asarray(offdiag_flat), n_factors)
    # Recurrence: Δr_t = α (β' x_{t-1} + μ) + ε_t
    x_prev = log_levels[:-1]
    diff = jnp.diff(log_levels, axis=0)
    coint_arr = jnp.matmul(jnp.asarray(x_prev), beta) + const_coint
    mu_step = jnp.einsum("t,f->tf", coint_arr, alpha)
    numpyro.sample("obs", dist.MultivariateNormal(mu_step, scale_tril=chol), obs=diff)


def _build_cholesky(log_diag: jnp.ndarray, offdiag_flat: jnp.ndarray, n_factors: int) -> jnp.ndarray:
    """Build the n_factors × n_factors lower-triangular Cholesky factor.

    Diagonal = `exp(log_diag)`; strictly-lower-triangular entries come from
    `offdiag_flat` in row-major order (so the first n_factors-1 entries are
    row 1, next n_factors-2 are row 2, etc. — whatever
    `jnp.tril_indices(n_factors, k=-1)` returns).
    """
    row_idx, col_idx = jnp.tril_indices(n_factors, k=-1)
    diag = jnp.exp(log_diag)
    chol = jnp.diag(diag)
    return chol.at[row_idx, col_idx].set(offdiag_flat * diag[row_idx])


def _build_cholesky_np(log_diag: np.ndarray, offdiag_flat: np.ndarray, n_factors: int) -> np.ndarray:
    row_idx, col_idx = np.tril_indices(n_factors, k=-1)
    diag = np.exp(log_diag)
    chol = np.diag(diag)
    chol[row_idx, col_idx] = offdiag_flat * diag[row_idx]
    return chol


@partial(jax.jit, static_argnames=("horizon",))
def _roll_log_level_paths(
    beta: jnp.ndarray,
    alpha: jnp.ndarray,
    const_coint: jnp.ndarray,
    chol: jnp.ndarray,
    x0: jnp.ndarray,
    keys: jnp.ndarray,
    horizon: int,
) -> jnp.ndarray:
    """Stochastically roll the VECM recurrence `horizon` steps from `x0` for
    each PRNG key in `keys`. Returns paths shape `(n_rollouts, horizon+1, F)`
    with `paths[:, 0, :] = x0`.

    Module-level + JIT-compiled by design: the previous implementation defined
    `step` / `roll` inside the calling method, so every call produced a fresh
    Python closure and JAX retraced + recompiled the lax.scan body. With
    hundreds of rolling-origin scoring calls per metric run, that recompilation
    cost dominated runtime by orders of magnitude.
    """
    n_factors = x0.shape[0]

    def step(x_prev: jnp.ndarray, key: jnp.ndarray) -> tuple[jnp.ndarray, jnp.ndarray]:
        eps_std = jax.random.normal(key, shape=(n_factors,))
        eps = chol @ eps_std
        coint = beta @ x_prev + const_coint
        x_next = x_prev + coint * alpha + eps
        return x_next, x_next

    def roll_one(key: jnp.ndarray) -> jnp.ndarray:
        inner_keys = jax.random.split(key, horizon)
        _, forward = jax.lax.scan(step, x0, inner_keys)
        return jnp.concatenate([x0[None, :], forward], axis=0)

    return jax.vmap(roll_one)(keys)


@dataclass(frozen=True)
class VecmConfig:
    """Fit hyperparameters. coint_rank=1 is what binds the factors via a
    single long-run relationship; coint_rank>1 not currently supported."""

    n_iters: int = _DEFAULT_FIT_ITERS
    learning_rate: float = _DEFAULT_LEARNING_RATE
    seed: int = 0


@dataclass
class VecmModel:
    """VECM joint exogenous model — Sampler, Fittable, Scorable.

    Holds three groups of state:

    1. Fit results (factor_names, n_factors, params, train_log_levels):
       populated by `fit(historical)` or `from_trained_state(...)`. Define the
       statistical model.
    2. Deployment-layer config (latest_observations): set by
       `VecmProviderConfig.realize_model` from YAML. Defines how
       multipliers scale to absolute levels.
    3. Provenance ids (model_version_id, evidence_set_id,
       calibration_artifact_id): set after fit or load via
       `_compute_provenance`. Surface as bundle metadata.
    """

    label: str = "vecm"
    config: VecmConfig = field(default_factory=VecmConfig)

    # Fit results.
    factor_names: tuple[LevelSeriesKey, ...] = ()
    n_factors: int = 0
    params: dict[str, np.ndarray] = field(default_factory=dict)
    train_log_levels: np.ndarray = field(default_factory=lambda: np.zeros((0, 0)))

    # Deployment-layer config.
    latest_observations: dict[str, Any] = field(default_factory=dict)

    # Provenance.
    model_version_id: str = ""
    evidence_set_id: str = ""
    calibration_artifact_id: str = ""

    # ──────────────────────── Fittable ────────────────────────

    def fit(self, historical: HistoricalSeries) -> None:
        """SVI / MAP estimation of the VECM parameters from observed log-levels.

        AutoDelta MAP with informative Normal priors on α, β, μ (see module
        constants). The priors act as ridge regularisation that keeps the
        weakly-identified cointegration vector from collapsing to a
        degenerate (huge-magnitude) MAP — see header notes for the per-month
        log-density these defaults produce vs the statsmodels baseline.
        """
        log_levels = np.log(historical.levels)
        if log_levels.shape[0] < 3:
            raise ValueError("VECM needs at least 3 observations to fit")

        rng = jax.random.PRNGKey(self.config.seed)
        guide = AutoDelta(_vecm_generative)
        svi = SVI(_vecm_generative, guide, Adam(self.config.learning_rate), Trace_ELBO())
        result = svi.run(rng, self.config.n_iters, jnp.asarray(log_levels), progress_bar=False)
        self.params = {k: np.asarray(v) for k, v in result.params.items()}
        self.factor_names = tuple(historical.series_names)
        self.n_factors = int(log_levels.shape[1])
        self.train_log_levels = log_levels.copy()

    # ──────────────────────── Scorable ────────────────────────

    def predictive(self, historical: HistoricalSeries, t: int, *, horizon: int = 1) -> dist.Distribution | None:
        """Joint predictive distribution over the cumulative `horizon`-step
        log-return at origin t.

        h=1: closed-form `MultivariateNormal(μ_t, Σ)` from the fitted recurrence.
        h>1: Gaussian fit to a Monte-Carlo unroll of `_MC_HORIZON_SAMPLES` paths.
        Returns None when the requested origin doesn't have h observations
        available (so the scorer marks the row Unscored).
        """
        if not self.params:
            raise RuntimeError("VecmModel has no fitted parameters; call fit() or from_trained_state() first")
        if horizon < 1:
            raise ValueError(f"horizon must be >= 1; got {horizon}")
        log_levels = np.log(historical.levels)
        n_steps = log_levels.shape[0] - 1
        if t + horizon > n_steps:
            return None
        if horizon == 1:
            mu = self._predict_mean_np(log_levels[t])
            cov = self._cov_np()
            return dist.MultivariateNormal(
                jnp.asarray(mu, dtype=jnp.float32), covariance_matrix=jnp.asarray(cov, dtype=jnp.float32)
            )
        # MC h-step
        return self._mc_horizon_predictive(log_levels[: t + 1], horizon=horizon, origin=t)

    # ──────────────────────── Sampler ────────────────────────

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        return frozenset(self.factor_names)

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        return frozenset()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        """Roll the VECM forward for each rollout seed, convert log-level paths to
        multipliers, scale by deployment `latest_observations`, and emit a
        `SampledExogenousBundle`. Each factor's identity is its typed `LevelSeriesKey`;
        a level series is sampled from the factor that *is* its key."""
        rollout_count = request.rollout_count
        horizon_months = request.horizon_months
        if rollout_count == 0:
            multipliers = np.empty((0, horizon_months + 1, self.n_factors), dtype="float64")
        else:
            multipliers = self._simulate_multipliers(rollout_seeds=request.rollout_seeds, horizon_months=horizon_months)
        path_by_factor: dict[LevelSeriesKey, np.ndarray] = {
            factor: multipliers[:, :, factor_index] for factor_index, factor in enumerate(self.factor_names)
        }

        frames = assemble_level_frames(
            (
                (key, self._level_series(key, path_by_factor=path_by_factor))
                for key in sorted(request.required_level_series, key=lambda key: key.wire_id)
            ),
            rollout_count=rollout_count,
            horizon_months=horizon_months,
        )
        return SampledExogenousBundle(
            levels=frames,
            model_id=self.label,
            provenance={
                "model_version_id": self.model_version_id,
                "scenario_generator_id": "vecm_numpyro",
                "scenario_generator_version_id": "vecm_numpyro:v1",
                "evidence_set_id": self.evidence_set_id,
                "calibration_artifact_id": self.calibration_artifact_id,
                "notes": ("sampled by VecmModel (NumPyro)",),
                "exogenous_provider_label": self.label,
            },
        )

    # ──────────────────────── Persistence ────────────────────────

    def to_trained_state(self) -> VecmTrainedState:
        """Snapshot post-fit state into the embeddable form written directly into the
        provider config YAML: the SVI/MAP params, factor names, and the training
        log-level history needed to roll forward at sample time."""
        return VecmTrainedState(
            # Wire-id factor identity, decoded back to typed LevelSeriesKeys by
            # `from_trained_state` at the single `parse_level_series_key` boundary.
            factor_names=tuple(factor.wire_id for factor in self.factor_names),
            train_log_levels=tuple(tuple(float(value) for value in row) for row in self.train_log_levels.tolist()),
            params={name: _yaml_value(array) for name, array in self.params.items()},
        )

    @classmethod
    def from_trained_state(
        cls,
        trained_state: VecmTrainedState,
        *,
        latest_observations: Mapping[str, Any],
        evidence_source_id: str,
        config: VecmConfig | None = None,
    ) -> VecmModel:
        """Rebuild post-fit state from `to_trained_state(...)`'s output, attach
        deployment-layer config from the runtime YAML, and compute provenance ids."""
        factor_names = tuple(parse_level_series_key(name) for name in trained_state.factor_names)
        model = cls(
            config=config or VecmConfig(),
            factor_names=factor_names,
            n_factors=len(factor_names),
            params={name: np.asarray(value, dtype=np.float32) for name, value in trained_state.params.items()},
            train_log_levels=np.asarray(trained_state.train_log_levels, dtype=np.float64),
            latest_observations=dict(latest_observations),
        )
        model._compute_provenance(evidence_source_id)
        return model

    # ──────────────────────── Internal: forecast ────────────────────────

    def _packed_params(self) -> tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray, jnp.ndarray]:
        """Pack the fitted params into (beta, alpha, const_coint, L) jnp arrays.

        Returned tuple is the argument set consumed by `_roll_log_level_paths`.
        """
        beta_tail = jnp.asarray(self.params["beta_tail_auto_loc"])
        alpha = jnp.asarray(self.params["alpha_auto_loc"])
        const_coint = jnp.asarray(self.params["const_coint_auto_loc"])
        chol = _build_cholesky(
            jnp.asarray(self.params["log_diag_auto_loc"]),
            jnp.asarray(self.params["offdiag_flat_auto_loc"]),
            self.n_factors,
        )
        beta = jnp.concatenate([jnp.ones(1), beta_tail])
        return beta, alpha, const_coint, chol

    def _simulate_multipliers(self, *, rollout_seeds: tuple[int, ...], horizon_months: int) -> np.ndarray:
        """Stochastically roll the VECM recurrence `horizon_months` steps for
        each seed; return multipliers shape `(n_rollouts, horizon_months+1, F)`
        normalised so multipliers[:, 0, :] = 1.0."""
        beta, alpha, const_coint, chol = self._packed_params()
        x0 = jnp.asarray(self.train_log_levels[-1])
        rngs = jnp.stack([jax.random.PRNGKey(int(seed)) for seed in rollout_seeds])
        log_level_paths = _roll_log_level_paths(beta, alpha, const_coint, chol, x0, rngs, horizon_months)
        multipliers = jnp.exp(log_level_paths - log_level_paths[:, :1, :])
        return np.asarray(multipliers)

    def _mc_horizon_predictive(
        self, log_levels_history: np.ndarray, *, horizon: int, origin: int
    ) -> dist.MultivariateNormal:
        """Roll forward `horizon` steps from `log_levels_history[-1]` using
        the fitted params; fit a multivariate Gaussian to the
        `_MC_HORIZON_SAMPLES` × F matrix of cumulative log-returns."""
        beta, alpha, const_coint, chol = self._packed_params()
        x0 = jnp.asarray(log_levels_history[-1])
        base_key = jax.random.PRNGKey(int(origin) * 1009 + horizon)
        keys = jax.random.split(base_key, _MC_HORIZON_SAMPLES)
        paths = _roll_log_level_paths(beta, alpha, const_coint, chol, x0, keys, horizon)
        # paths shape (n_samples, horizon+1, F); cumulative h-step log-return = paths[-1] - paths[0]
        samples = paths[:, -1, :] - paths[:, 0, :]
        samples_np = np.asarray(samples)
        mean = samples_np.mean(axis=0)
        diff = samples_np - mean
        cov = (diff.T @ diff) / (samples_np.shape[0] - 1)
        cov = (cov + cov.T) / 2 + np.eye(self.n_factors) * 1e-12
        return dist.MultivariateNormal(
            jnp.asarray(mean, dtype=jnp.float32), covariance_matrix=jnp.asarray(cov, dtype=jnp.float32)
        )

    def _predict_mean_np(self, x_prev: np.ndarray) -> np.ndarray:
        """E[Δr_{t+1} | x_t] from fitted params, in numpy."""
        beta_tail = self.params["beta_tail_auto_loc"]
        alpha = self.params["alpha_auto_loc"]
        const_coint = float(self.params["const_coint_auto_loc"])
        beta = np.concatenate([np.ones(1), beta_tail])
        coint = float(beta @ x_prev) + const_coint
        return np.asarray(coint * alpha)

    def _cov_np(self) -> np.ndarray:
        chol = _build_cholesky_np(
            self.params["log_diag_auto_loc"], self.params["offdiag_flat_auto_loc"], self.n_factors
        )
        return np.asarray(chol @ chol.T)

    # ──────────────────────── Internal: bundle dispatch ────────────────────────

    def _level_series(self, key: LevelSeriesKey, *, path_by_factor: dict[LevelSeriesKey, np.ndarray]) -> np.ndarray:
        # Post-collapse, a level series is sampled from the factor that *is* its key
        # (factor identity == the typed key); there is no source-name indirection.
        try:
            multiplier = path_by_factor[key]
        except KeyError as error:
            raise ValueError(f"VECM fit blob has no factor {key.wire_id!r}") from error
        return self._latest_factor_value(key) * multiplier

    def _latest_factor_value(self, key: LevelSeriesKey) -> float:
        # `latest_observations` is keyed by the factor's wire id (the deployment YAML form).
        direct = self.latest_observations.get(key.wire_id)
        if isinstance(direct, (int, float)):
            return float(direct)
        match key:
            case InflationKey():
                return self._latest_observation_value("cpi_latest")
            case SecurityKey(symbol=symbol):
                # A security's anchor is its own latest close. SPY is the one symbol whose
                # evidence arrives under two names (the Yahoo adjusted close, with the FRED
                # index level as fallback), so it gets an explicit row rather than the
                # `{symbol}_close_latest` convention.
                primary, fallback = _SECURITY_ANCHOR_OBSERVATIONS.get(symbol, (f"{symbol}_close_latest", None))
                return self._latest_observation_value(primary, fallback_key=fallback)
            case HomeValueKey() | RentKey():
                for obs_key in (
                    "zillow_home_value_latest_by_factor",
                    "zillow_rent_latest_by_factor",
                    "case_shiller_home_value_latest_by_factor",
                ):
                    by_factor = self.latest_observations.get(obs_key)
                    if isinstance(by_factor, dict) and key.wire_id in by_factor:
                        return _observation_value(by_factor[key.wire_id], f"{obs_key}[{key.wire_id!r}]")
                if isinstance(key, RentKey) and key.location_id == "san_francisco_ca":
                    # FRED-only degraded evidence anchors SF rent under this legacy CPI key.
                    return self._latest_observation_value("sf_rent_cpi_latest")
        raise ValueError(f"VECM config latest_observations has no usable latest value for factor {key.wire_id!r}")

    def _latest_observation_value(self, key: str, *, fallback_key: str | None = None) -> float:
        if key in self.latest_observations:
            return _observation_value(self.latest_observations[key], key)
        if fallback_key is not None and fallback_key in self.latest_observations:
            return _observation_value(self.latest_observations[fallback_key], fallback_key)
        expected = key if fallback_key is None else f"{key!r} or {fallback_key!r}"
        raise ValueError(f"VECM config latest_observations has no {expected}")

    def _compute_provenance(self, evidence_source_id: str) -> None:
        self.model_version_id = "model_version:" + stable_identity_digest(
            {"label": self.label, "class": type(self).__qualname__}
        )
        self.evidence_set_id = "evidence_set:" + stable_identity_digest(
            {
                "evidence_source_id": evidence_source_id,
                "factor_names": [factor.wire_id for factor in self.factor_names],
                "latest_observations": dict(self.latest_observations),
            }
        )
        self.calibration_artifact_id = "calibration_artifact:" + stable_identity_digest(
            {"model_id": self.label, "model_version_id": self.model_version_id, "evidence_set_id": self.evidence_set_id}
        )


def _observation_value(observation: Any, key: str) -> float:
    if isinstance(observation, (int, float)):
        return float(observation)
    if isinstance(observation, dict) and isinstance(observation.get("value"), (int, float)):
        return float(observation["value"])
    raise TypeError(f"VECM latest_observations {key} must be a number or object with numeric 'value'")


def _yaml_value(array: np.ndarray) -> float | tuple[float, ...]:
    """A fitted param as its natural YAML shape: a bare scalar for a 0-d array (e.g.
    `const_coint_auto_loc`), a tuple for anything with rank >= 1."""
    return float(array) if array.ndim == 0 else tuple(float(value) for value in array.tolist())


class VecmTrainedState(FrozenModel):
    """The fitted VECM state, embedded directly in the provider config YAML rather than a
    separate blob: SVI/MAP `params` (keyed by NumPyro's `<site>_auto_loc` param names — a
    generic map because the site set is `_vecm_generative`'s to define, not this schema's to
    duplicate), factor identity, and the training log-level history `VecmModel` needs to
    anchor sampling at the last observed month. Written by `VecmModel.to_trained_state()`."""

    factor_names: tuple[str, ...]
    train_log_levels: tuple[tuple[float, ...], ...]
    params: dict[str, float | tuple[float, ...]]


class VecmProviderConfig(FrozenModel):
    """Pre-trained VECM provider config, written whole by `bb run //finance/augur/fit:train
    -- --model vecm ...`. The model is loaded at server startup; no fitting happens on the
    request path."""

    type: Literal["vecm"] = "vecm"
    trained_state: VecmTrainedState
    latest_observations: dict[str, Any] = Field(
        description="Latest observed series state at the start of the simulation horizon (factor → value)."
    )
    current_mortgage30_rate_pct: float

    def realize_model(self) -> VecmModel:
        evidence_source_id = "vecm_trained_state:" + stable_identity_digest({"trained_state": self.trained_state})
        return VecmModel.from_trained_state(
            self.trained_state, latest_observations=self.latest_observations, evidence_source_id=evidence_source_id
        )
