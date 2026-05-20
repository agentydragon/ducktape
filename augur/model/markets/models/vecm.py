"""Vector Error Correction Model (VECM) on log-levels.

  Δr_t = α (β' x_{t-1} + μ) + Σ_{i=1..p-1} Γ_i Δr_{t-i} + ε_t,  ε_t ~ N(0, Σ)

where x_t is the F-vector of log-levels and `coint_rank` is the assumed
rank of the cointegration relationship. Equivalent to a VAR(p) on
log-levels with a long-run pull toward the cointegrating relationships
β' x + μ = 0; for the configured market factors this is what binds rent
and CPI to a shared trend rather than letting them drift apart over 30
years.

Fit via `statsmodels.tsa.vector_ar.vecm.VECM` once at training time; we
then extract α, β, Γ, the constant inside the relation, and the residual
covariance into typed attributes and drop the third-party fit object —
predict / simulate read these attributes directly. The predictive
density at month t is multivariate normal with mean from the fitted
recurrence and covariance from the fitted residual covariance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import Field
from statsmodels.tsa.vector_ar.vecm import VECM

from augur.frames import concat_frames
from augur.model.location_market_sources import LocationMarketSources, LocationMarketSourcesConfig
from augur.model.market_api import (
    MARKET_EVENTS_SCHEMA,
    MARKET_LEVELS_SCHEMA,
    MarketSamplingRequest,
    SampledMarketBundle,
    market_events_frame,
    market_levels_frame,
)
from augur.model.markets._density import gaussian_logpdf, gaussian_logpdf_from_samples
from augur.model.markets.scenarios import HistoricalSeries, Scenarios
from augur.model.provenance import stable_identity_digest
from augur.model.schemas import FrozenModel
from augur.model.series import (
    CRYPTO_SERIES_PREFIX,
    HOME_VALUE_SERIES_PREFIX,
    INFLATION_SERIES_ID,
    PRIVATE_EQUITY_SALE_EVENT_PREFIX,
    PRIVATE_EQUITY_SERIES_PREFIX,
    RENT_SERIES_PREFIX,
    SP500_SERIES_ID,
    series_suffix,
)

# Constant inside the cointegration relation. Other deterministic options
# ("co", "lo", "li", "n") change which constants/trends statsmodels populates
# on the fit result; `fit()` below assumes "ci" when it copies parameters out.
# Add another mode only after extending the parameter copy + `_predict_mean`.
_DETERMINISTIC: Literal["ci"] = "ci"
_TENDER_INTERVAL_MONTHS = 12
_MODEL_CARD_ID = "augur-market-model-card:2026-05-15"
_VALIDATION_REPORT_ID = "validation_report:augur-market-models:not_available:2026-05-15"
_KNOWN_LIMITATION_IDS = (
    "evidence-set-id-unversioned",
    "calibration-artifact-id-unversioned",
    "validation-report-not-decision-grade",
    "constant-mortgage-rate-path",
    "private-equity-marks-flat-fixture",
    "private-equity-paths-all-share-placeholder",
    "crypto-paths-all-share-placeholder",
)


@dataclass(frozen=True)
class VecmConfig:
    """Vecm hyperparameters fixed at construction. `k_ar_diff` is the lag
    order on Δlog-level terms; `coint_rank` is r in the rank-r
    cointegration assumption (1 binds the configured factors to a single
    long-run relationship)."""

    k_ar_diff: int = 1
    coint_rank: int = 1

    def __post_init__(self) -> None:
        if self.k_ar_diff < 0:
            raise ValueError(f"k_ar_diff must be >= 0; got {self.k_ar_diff}")
        if self.coint_rank < 1:
            raise ValueError(f"coint_rank must be >= 1; got {self.coint_rank}")


def _zeros2() -> np.ndarray:
    return np.zeros((0, 0))


def _zeros1() -> np.ndarray:
    return np.zeros((0,))


@dataclass
class VecmModel:
    label = "vecm"

    config: VecmConfig = field(default_factory=VecmConfig)

    # Parameters extracted from the statsmodels fit — empty until `fit()` runs.
    alpha: np.ndarray = field(default_factory=_zeros2)  # (F, r)
    beta: np.ndarray = field(default_factory=_zeros2)  # (F, r)
    gamma: np.ndarray = field(default_factory=_zeros2)  # (F, F * k_ar_diff)
    const_coint: np.ndarray = field(default_factory=_zeros1)  # (r,)
    inv_cov: np.ndarray = field(default_factory=_zeros2)  # (F, F)
    cov_chol: np.ndarray = field(default_factory=_zeros2)  # (F, F)
    cov_log_det: float = 0.0
    factor_names: tuple[str, ...] = ()
    n_factors: int = 0
    train_log_levels: np.ndarray = field(default_factory=_zeros2)  # for simulation seed

    def fit(self, historical: HistoricalSeries) -> None:
        log_levels = np.log(historical.levels)
        if log_levels.shape[0] < self.config.k_ar_diff + 3:
            raise ValueError("VECM needs more observations than k_ar_diff + 3")

        model = VECM(
            log_levels, k_ar_diff=self.config.k_ar_diff, coint_rank=self.config.coint_rank, deterministic=_DETERMINISTIC
        )
        fit = model.fit()

        residuals = np.asarray(fit.resid)
        n_obs, n_factors = residuals.shape
        cov = (residuals.T @ residuals) / max(1, n_obs - 1)
        cov = (cov + cov.T) / 2 + np.eye(n_factors) * 1e-12
        sign, log_det = np.linalg.slogdet(cov)
        if sign <= 0:
            raise ValueError("VECM residual covariance has non-positive determinant")

        # Extract typed parameter arrays from the statsmodels fit and drop the
        # third-party object — `_predict_mean` reads only these.
        self.alpha = np.asarray(fit.alpha)
        self.beta = np.asarray(fit.beta)
        self.gamma = np.asarray(fit.gamma)
        self.const_coint = np.asarray(fit.const_coint).reshape(-1)
        self.inv_cov = np.linalg.inv(cov)
        self.cov_chol = np.linalg.cholesky(cov)
        self.cov_log_det = float(log_det)
        self.factor_names = historical.factor_names
        self.n_factors = n_factors
        self.train_log_levels = log_levels.copy()

    def log_predictive_density(self, historical: HistoricalSeries, t: int) -> float | None:
        if t < self.config.k_ar_diff + 1:
            raise ValueError(f"VECM with k_ar_diff={self.config.k_ar_diff} needs t >= k_ar_diff + 1; got {t}")
        log_levels = np.log(historical.levels)
        # The fitted VECM recurrence predicts Δlog_level[t+1] from
        # log_level[t] and the previous k_ar_diff Δlog_levels:
        #   Δr_{t+1} = α @ (β' @ x_t + const_coint) + Γ_blocks @ stacked_Δr
        mu = self._predict_mean(log_levels, t)
        diff = log_levels[t + 1] - log_levels[t] - mu
        return gaussian_logpdf(diff=diff, inv_cov=self.inv_cov, log_det=self.cov_log_det)

    def log_predictive_marginals(self, historical: HistoricalSeries, t: int) -> dict[str, float] | None:
        if t < self.config.k_ar_diff + 1:
            raise ValueError(f"VECM with k_ar_diff={self.config.k_ar_diff} needs t >= k_ar_diff + 1; got {t}")
        log_levels = np.log(historical.levels)
        mu = self._predict_mean(log_levels, t)
        diff = log_levels[t + 1] - log_levels[t] - mu
        cov = np.linalg.inv(self.inv_cov)
        sd = np.sqrt(np.diag(cov))
        names = self.factor_names or tuple(f"f{i}" for i in range(diff.shape[0]))
        out: dict[str, float] = {}
        for k, name in enumerate(names):
            out[name] = float(-0.5 * (math.log(2 * math.pi) + 2 * math.log(sd[k]) + (diff[k] / sd[k]) ** 2))
        return out

    def log_predictive_density_at_horizon(self, historical: HistoricalSeries, t: int, h: int) -> float | None:
        if h < 1:
            raise ValueError(f"h must be >= 1; got {h}")
        if t < self.config.k_ar_diff + 1:
            return None
        log_returns_full = np.diff(np.log(historical.levels), axis=0)
        if t + h > log_returns_full.shape[0]:
            return None

        # Monte Carlo: roll the VECM recurrence forward from log_levels[:t+1].
        rng = np.random.default_rng(int(t) * 1009 + h)
        n_paths_mc = 5000
        n_factors = log_returns_full.shape[1]
        log_levels = np.log(historical.levels[: t + 1])

        history = log_levels[-(self.config.k_ar_diff + 2) :]
        log_levels_buf = np.broadcast_to(history, (n_paths_mc, history.shape[0], n_factors)).copy()

        innovations = rng.standard_normal((n_paths_mc, h, n_factors)) @ self.cov_chol.T
        cumulative_log_returns = np.zeros((n_paths_mc, n_factors))
        for step in range(h):
            for path_idx in range(n_paths_mc):
                tail = log_levels_buf[path_idx]
                t_local = tail.shape[0] - 1
                mu = self._predict_mean(tail, t_local)
                # Capture last log-level *before* mutating the buffer — tail is
                # a view, so the in-place update below would alias it to the
                # new next_level and zero out the diff.
                last_level = tail[-1].copy()
                next_level = last_level + mu + innovations[path_idx, step, :]
                log_levels_buf[path_idx] = np.concatenate([tail[1:], next_level[None, :]], axis=0)
                cumulative_log_returns[path_idx] += next_level - last_level

        observed_cumulative = log_returns_full[t : t + h].sum(axis=0)
        return gaussian_logpdf_from_samples(samples=cumulative_log_returns, observation=observed_cumulative)

    def _predict_mean(self, log_levels: np.ndarray, t: int) -> np.ndarray:
        """Predict E[Δlog_levels[t+1] | log_levels[:t+1]] under deterministic="ci"
        from the typed parameter arrays populated by `fit()`."""
        x = log_levels[t]
        beta_eff = self.beta[: x.shape[0]]
        coint_term = beta_eff.T @ x + self.const_coint
        mean = self.alpha @ coint_term

        if self.config.k_ar_diff > 0:
            diffs = [log_levels[t - i] - log_levels[t - i - 1] for i in range(self.config.k_ar_diff)]
            mean = mean + self.gamma @ np.concatenate(diffs)

        return np.asarray(mean)

    def save(self, descriptor: VecmMarketProviderConfig) -> None:
        """Persist post-fit state to the `.npz` archive named by the
        descriptor's `trained_blob` so the runtime can skip re-fitting at
        startup. Symmetric to `VecmModel.load(descriptor)`."""
        np.savez_compressed(
            descriptor.trained_blob,
            k_ar_diff=np.array(self.config.k_ar_diff),
            coint_rank=np.array(self.config.coint_rank),
            alpha=self.alpha,
            beta=self.beta,
            gamma=self.gamma,
            const_coint=self.const_coint,
            inv_cov=self.inv_cov,
            cov_chol=self.cov_chol,
            cov_log_det=np.array(self.cov_log_det),
            factor_names=np.array(self.factor_names, dtype=object),
            train_log_levels=self.train_log_levels,
        )

    @staticmethod
    def load(descriptor: VecmMarketProviderConfig) -> VecmModel:
        with np.load(descriptor.trained_blob, allow_pickle=True) as data:
            config = VecmConfig(k_ar_diff=int(data["k_ar_diff"]), coint_rank=int(data["coint_rank"]))
            factor_names = tuple(str(name) for name in data["factor_names"])
            model = VecmModel(config=config)
            model.alpha = np.asarray(data["alpha"])
            model.beta = np.asarray(data["beta"])
            model.gamma = np.asarray(data["gamma"])
            model.const_coint = np.asarray(data["const_coint"])
            model.inv_cov = np.asarray(data["inv_cov"])
            model.cov_chol = np.asarray(data["cov_chol"])
            model.cov_log_det = float(data["cov_log_det"])
            model.factor_names = factor_names
            model.n_factors = len(factor_names)
            model.train_log_levels = np.asarray(data["train_log_levels"])
        return model

    def simulate(self, n_paths: int, n_months: int, seed: int) -> Scenarios:
        rng = np.random.default_rng(seed)
        n_factors = self.n_factors
        train_log_levels = self.train_log_levels

        history = train_log_levels[-(self.config.k_ar_diff + 2) :]
        log_levels_buf = np.broadcast_to(history, (n_paths, history.shape[0], n_factors)).copy()

        out_log_levels = np.empty((n_paths, n_months + 1, n_factors), dtype="float64")
        out_log_levels[:, 0, :] = log_levels_buf[:, -1, :]

        innovations = rng.standard_normal((n_paths, n_months, n_factors)) @ self.cov_chol.T

        for step in range(n_months):
            for path_idx in range(n_paths):
                tail = log_levels_buf[path_idx]
                t_local = tail.shape[0] - 1
                mu = self._predict_mean(tail, t_local)
                next_level = tail[-1] + mu + innovations[path_idx, step, :]
                log_levels_buf[path_idx] = np.concatenate([tail[1:], next_level[None, :]], axis=0)
                out_log_levels[path_idx, step + 1, :] = next_level

        # Convert to multipliers normalized so multipliers[:, 0, :] = 1.0.
        multipliers = np.exp(out_log_levels - out_log_levels[:, :1, :])
        return Scenarios(
            factor_names=self.factor_names or tuple(f"f{i}" for i in range(n_factors)),
            multipliers=multipliers,
            seed=seed,
            label=self.label,
        )


@dataclass(frozen=True)
class VecmJointMarketModel:
    """Native sampled-bundle wrapper around a fitted `VecmModel`."""

    model: VecmModel
    latest_observations: dict[str, Any]
    current_private_equity_price_usd: float
    location_market_sources: LocationMarketSources
    label: str
    risk_factor_ids: tuple[str, ...]
    evidence_latest_observation_ids: tuple[str, ...]
    risk_factor_set_id: str
    market_model_version_id: str
    evidence_set_id: str
    calibration_artifact_id: str

    @classmethod
    def from_loaded_model(
        cls,
        model: VecmModel,
        *,
        latest_observations: dict[str, Any],
        current_private_equity_price_usd: float,
        location_market_sources: LocationMarketSources,
        evidence_source_id: str,
    ) -> VecmJointMarketModel:
        factor_names = tuple(model.factor_names)
        label = model.label
        risk_factor_set_id = "risk_factor_set:" + stable_identity_digest({"factor_names": factor_names})
        market_model_version_id = "model_version:" + stable_identity_digest(
            {"label": label, "class": type(model).__qualname__}
        )
        evidence_set_id = "evidence_set:" + stable_identity_digest(
            {
                "evidence_source_id": evidence_source_id,
                "factor_names": factor_names,
                "latest_observations": dict(latest_observations),
            }
        )
        calibration_artifact_id = "calibration_artifact:" + stable_identity_digest(
            {
                "market_model_id": label,
                "market_model_version_id": market_model_version_id,
                "evidence_set_id": evidence_set_id,
                "risk_factor_set_id": risk_factor_set_id,
            }
        )
        return cls(
            model=model,
            latest_observations=dict(latest_observations),
            current_private_equity_price_usd=float(current_private_equity_price_usd),
            location_market_sources=location_market_sources,
            label=label,
            risk_factor_ids=factor_names,
            evidence_latest_observation_ids=tuple(sorted(str(key) for key in latest_observations)),
            risk_factor_set_id=risk_factor_set_id,
            market_model_version_id=market_model_version_id,
            evidence_set_id=evidence_set_id,
            calibration_artifact_id=calibration_artifact_id,
        )

    def sample(self, request: MarketSamplingRequest) -> SampledMarketBundle:
        if request.rollout_count:
            multipliers = np.concatenate(
                [
                    self.model.simulate(n_paths=1, n_months=request.horizon_months, seed=seed).multipliers
                    for seed in request.rollout_seeds
                ],
                axis=0,
            )
        else:
            multipliers = np.empty((0, request.horizon_months + 1, self.model.n_factors), dtype="float64")
        factor_names = self.model.factor_names or tuple(f"f{i}" for i in range(self.model.n_factors))
        path_by_factor = {
            factor_name: multipliers[:, :, factor_index] for factor_index, factor_name in enumerate(factor_names)
        }
        shape = (request.rollout_count, request.horizon_months + 1)
        private_equity_events = np.zeros(shape, dtype=np.bool_)
        private_equity_events[:, _TENDER_INTERVAL_MONTHS : request.horizon_months + 1 : _TENDER_INTERVAL_MONTHS] = True

        level_blocks = [
            market_levels_frame(
                series_id,
                self._level_series(series_id, path_by_factor=path_by_factor, shape=shape),
                rollout_count=request.rollout_count,
                horizon_months=request.horizon_months,
            )
            for series_id in sorted(request.required_level_series)
        ]
        event_blocks = [
            market_events_frame(
                event_id,
                self._event_series(event_id, private_equity_events=private_equity_events),
                rollout_count=request.rollout_count,
                horizon_months=request.horizon_months,
            )
            for event_id in sorted(request.required_event_series)
        ]
        return SampledMarketBundle(
            levels=concat_frames(level_blocks, MARKET_LEVELS_SCHEMA),
            events=concat_frames(event_blocks, MARKET_EVENTS_SCHEMA),
            metadata={
                "model_card_id": _MODEL_CARD_ID,
                "model_version_id": self.market_model_version_id,
                "validation_report_id": _VALIDATION_REPORT_ID,
                "known_limitation_ids": _KNOWN_LIMITATION_IDS,
                "market_model_version_id": self.market_model_version_id,
                "scenario_generator_id": "vecm_joint_market_model",
                "scenario_generator_version_id": "vecm_joint_market_model:v1",
                "evidence_set_id": self.evidence_set_id,
                "calibration_artifact_id": self.calibration_artifact_id,
                "risk_factor_set_id": self.risk_factor_set_id,
                "risk_factor_ids": self.risk_factor_ids,
                "evidence_latest_observation_ids": self.evidence_latest_observation_ids,
                "current_private_equity_price_usd": self.current_private_equity_price_usd,
                "event_stream_ids": ("private_equity_sale_opportunity_event",),
                "notes": ("sampled by VecmJointMarketModel",),
                "market_provider_label": self.label,
            },
        )

    def _level_series(
        self, series_id: str, *, path_by_factor: dict[str, np.ndarray], shape: tuple[int, int]
    ) -> np.ndarray:
        if series_id == INFLATION_SERIES_ID:
            return self._factor_level(INFLATION_SERIES_ID, path_by_factor=path_by_factor)
        if series_id == SP500_SERIES_ID:
            return self._factor_level(SP500_SERIES_ID, path_by_factor=path_by_factor)
        if location_id := series_suffix(series_id, HOME_VALUE_SERIES_PREFIX):
            return self._factor_level(self._location_factor("home_value", location_id), path_by_factor=path_by_factor)
        if location_id := series_suffix(series_id, RENT_SERIES_PREFIX):
            return self._factor_level(self._location_factor("rent", location_id), path_by_factor=path_by_factor)
        if series_suffix(series_id, PRIVATE_EQUITY_SERIES_PREFIX) is not None:
            return np.full(shape, self.current_private_equity_price_usd or 1.0, dtype="float64")
        if series_suffix(series_id, CRYPTO_SERIES_PREFIX) is not None:
            return np.ones(shape, dtype="float64")
        raise ValueError(f"VECM market model cannot sample level series {series_id!r}")

    def _event_series(self, event_id: str, *, private_equity_events: np.ndarray) -> np.ndarray:
        if series_suffix(event_id, PRIVATE_EQUITY_SALE_EVENT_PREFIX) is not None:
            return private_equity_events
        raise ValueError(f"VECM market model cannot sample event series {event_id!r}")

    def _location_factor(self, kind: Literal["home_value", "rent"], location_id: str) -> str:
        source_by_location = (
            self.location_market_sources.home_value if kind == "home_value" else self.location_market_sources.rent
        )
        try:
            return source_by_location[location_id]
        except KeyError as error:
            raise ValueError(f"location_market_sources.{kind} has no entry for {location_id!r}") from error

    def _factor_level(self, factor_name: str, *, path_by_factor: dict[str, np.ndarray]) -> np.ndarray:
        try:
            multiplier = path_by_factor[factor_name]
        except KeyError as error:
            raise ValueError(f"VECM trained blob has no factor {factor_name!r}") from error
        return self._latest_factor_value(factor_name) * multiplier

    def _latest_factor_value(self, factor_name: str) -> float:
        direct = self.latest_observations.get(factor_name)
        if isinstance(direct, (int, float)):
            return float(direct)

        if factor_name == "sp500":
            return self._latest_observation_value("spy_adjusted_close_latest", fallback_key="sp500_price_latest")
        if factor_name == "rent":
            return self._latest_observation_value("sf_rent_cpi_latest")
        if factor_name == "inflation":
            return self._latest_observation_value("cpi_latest")

        for key in ("zillow_home_value_latest_by_factor", "case_shiller_home_value_latest_by_factor"):
            by_factor = self.latest_observations.get(key)
            if isinstance(by_factor, dict) and factor_name in by_factor:
                return _observation_value(by_factor[factor_name], f"{key}[{factor_name!r}]")

        raise ValueError(f"VECM config latest_observations has no usable latest value for factor {factor_name!r}")

    def _latest_observation_value(self, key: str, *, fallback_key: str | None = None) -> float:
        if key in self.latest_observations:
            return _observation_value(self.latest_observations[key], key)
        if fallback_key is not None and fallback_key in self.latest_observations:
            return _observation_value(self.latest_observations[fallback_key], fallback_key)
        expected = key if fallback_key is None else f"{key!r} or {fallback_key!r}"
        raise ValueError(f"VECM config latest_observations has no {expected}")


def _observation_value(observation: Any, key: str) -> float:
    if isinstance(observation, (int, float)):
        return float(observation)
    if isinstance(observation, dict) and isinstance(observation.get("value"), (int, float)):
        return float(observation["value"])
    raise TypeError(f"VECM latest_observations {key} must be a number or object with numeric 'value'")


class VecmMarketProviderConfig(FrozenModel):
    """Pre-trained VECM provider config — points at the trained-state blob
    written by `bb run //augur/fit:train`. The model is loaded at server
    startup; no fitting happens on the request path."""

    type: Literal["vecm"] = "vecm"
    trained_blob: Path = Field(description="Absolute path to the .npz produced by VecmModel.save(descriptor).")
    latest_observations: dict[str, Any] = Field(
        description="Latest observed market state at the start of the simulation horizon (factor → value)."
    )
    current_mortgage30_rate_pct: float
    location_market_sources: LocationMarketSourcesConfig

    def realize_model(self, *, current_private_equity_price_usd: float) -> VecmJointMarketModel:
        model = VecmModel.load(self)
        return VecmJointMarketModel.from_loaded_model(
            model,
            latest_observations=self.latest_observations,
            current_private_equity_price_usd=current_private_equity_price_usd,
            location_market_sources=LocationMarketSources.from_config(self.location_market_sources),
            evidence_source_id=str(self.trained_blob),
        )
