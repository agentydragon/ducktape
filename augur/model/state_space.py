"""Trained block-shrunk state-space exogenous provider.

The v1 state-space provider is intentionally compact: training estimates a
joint monthly log-return distribution with block-structured covariance
shrinkage, persists the filtered latest latent state, and runtime conditioning
updates that state from grouped observations before sampling forward.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import jax.numpy as jnp
import numpy as np
from numpyro import distributions as dist
from pydantic import Field, model_validator

from augur.dates import months_between
from augur.frames import concat_frames
from augur.model.conditioning import ExogenousConditioningContext, ObservationTreatment, latest_observations_by_series
from augur.model.exogenous import (
    SERIES_LEVELS_SCHEMA,
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    series_levels_frame,
    validate_sample_satisfies_request,
)
from augur.model.location_series_sources import LocationSeriesSources, LocationSeriesSourcesConfig
from augur.model.path_models.scenarios import HistoricalSeries, historical_log_returns
from augur.model.private_equity_bundle import PrivateEquityBundle
from augur.model.private_equity_protocol import (
    neutral_private_equity_issuer_bundle,
    observed_private_equity_mark_matrix,
)
from augur.model.provenance import stable_identity_digest
from augur.model.schemas import FrozenModel
from augur.model.series import (
    CryptoKey,
    HomeValueKey,
    IssuerId,
    LevelSeriesKey,
    LocationId,
    RentKey,
    SP500Key,
    parse_level_series_key,
    try_parse_level_series_key,
)
from augur.model.series_model import derive_stream_rollout_seeds
from augur.model.trained_private_equity import TrainedPrivateEquityScalePrior, private_equity_soft_cap_penalty
from augur.product.asset_key import PrivateEquityAssetKey, parse_asset_key

_MIN_MONTHLY_VARIANCE = 1e-8
_OFF_BLOCK_SHRINKAGE = 0.0
_ON_BLOCK_SHRINKAGE = 0.5


class StateSpacePrivateEquityEventPrior(FrozenModel):
    tender_interval_months_median: float = Field(gt=0)
    tender_interval_log_sigma: float = Field(gt=0)
    last_tender_observed_at: date | None = None


def _classify_factor(factor: str) -> LevelSeriesKey | IssuerId:
    """Classify a `factor_names` entry as either a `LevelSeriesKey` (non-PE) or the
    `IssuerId` of a private-equity mark factor.

    The artifact's on-disk JSON keeps `factor_names` as wire-id strings; this
    helper is the single boundary that turns each wire-id back into a typed key.
    Raises `ValueError` for unrecognized wire ids.
    """

    if (level_key := try_parse_level_series_key(factor)) is not None:
        return level_key
    asset_key = parse_asset_key(factor)
    if not isinstance(asset_key, PrivateEquityAssetKey):
        raise ValueError(f"state-space factor {factor!r} is neither a level series nor a PE mark")
    return asset_key.issuer_id


class StateSpaceModelArtifact(FrozenModel):
    schema_version: Literal[1] = 1
    factor_names: tuple[str, ...] = Field(min_length=1)
    trained_through_month: str = Field(min_length=7, max_length=7)
    latest_level_by_factor: dict[str, float] = Field(min_length=1)
    monthly_log_return_mu: dict[str, float] = Field(min_length=1)
    monthly_log_return_cov: tuple[tuple[float, ...], ...]
    filtered_log_state_mean: dict[str, float] = Field(min_length=1)
    filtered_log_state_cov: tuple[tuple[float, ...], ...]
    private_equity_event_priors: dict[str, StateSpacePrivateEquityEventPrior] = Field(default_factory=dict)
    private_equity_scale_priors: dict[str, TrainedPrivateEquityScalePrior] = Field(default_factory=dict)
    source_manifest: dict[str, Any] = Field(default_factory=dict)
    prior_manifest: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_shapes(self) -> StateSpaceModelArtifact:
        factors = set(self.factor_names)
        missing_levels = factors - set(self.latest_level_by_factor)
        missing_mu = factors - set(self.monthly_log_return_mu)
        missing_state = factors - set(self.filtered_log_state_mean)
        if missing_levels:
            raise ValueError(f"latest_level_by_factor missing factors {sorted(missing_levels)}")
        if missing_mu:
            raise ValueError(f"monthly_log_return_mu missing factors {sorted(missing_mu)}")
        if missing_state:
            raise ValueError(f"filtered_log_state_mean missing factors {sorted(missing_state)}")
        n = len(self.factor_names)
        _require_square_matrix(self.monthly_log_return_cov, n, "monthly_log_return_cov")
        _require_square_matrix(self.filtered_log_state_cov, n, "filtered_log_state_cov")
        if any(self.latest_level_by_factor[factor] <= 0 for factor in self.factor_names):
            raise ValueError("latest_level_by_factor values must be positive")
        private_equity_issuers = {str(issuer) for issuer in self.private_equity_factor_issuers}
        missing_scale_priors = private_equity_issuers - set(self.private_equity_scale_priors)
        if missing_scale_priors:
            raise ValueError(
                "private_equity_scale_priors missing issuer(s) "
                f"{sorted(missing_scale_priors)}; macro-scale private-equity prior is required"
            )
        missing_event_priors = private_equity_issuers - set(self.private_equity_event_priors)
        if missing_event_priors:
            raise ValueError(
                "private_equity_event_priors missing issuer(s) "
                f"{sorted(missing_event_priors)}; private-equity tender event series is required"
            )
        return self

    @cached_property
    def factor_classifications(self) -> tuple[LevelSeriesKey | IssuerId, ...]:
        """Typed classification of every `factor_names` entry. Parsed once."""

        return tuple(_classify_factor(factor) for factor in self.factor_names)

    @cached_property
    def level_factors(self) -> tuple[LevelSeriesKey, ...]:
        """Non-PE level-series factors in `factor_names` order."""

        return tuple(item for item in self.factor_classifications if not isinstance(item, str))

    @cached_property
    def private_equity_factor_issuers(self) -> tuple[IssuerId, ...]:
        """PE issuer ids in `factor_names` order."""

        return tuple(IssuerId(item) for item in self.factor_classifications if isinstance(item, str))


@dataclass(frozen=True)
class StateSpaceAdditionalFactor:
    factor_name: str
    latest_level: float
    monthly_log_return_mu: float
    monthly_log_return_sigma: float
    covariance_with_factors: Mapping[str, float] = field(default_factory=dict)
    source_ids: tuple[str, ...] = ()
    private_equity_issuer_id: str | None = None
    private_equity_event_prior: StateSpacePrivateEquityEventPrior | None = None
    private_equity_scale_prior: TrainedPrivateEquityScalePrior | None = None


class StateSpaceExogenousProviderConfig(FrozenModel):
    type: Literal["state_space"] = "state_space"
    trained_artifact_path: Path
    conditioning: ExogenousConditioningContext
    current_mortgage30_rate_pct: float
    location_series_sources: LocationSeriesSourcesConfig

    def realize_model(self) -> StateSpaceModel:
        return StateSpaceModel.from_path(
            self.trained_artifact_path,
            conditioning=self.conditioning,
            location_series_sources=LocationSeriesSources.from_config(self.location_series_sources),
            evidence_source_id=str(self.trained_artifact_path),
        )


@dataclass
class StateSpaceModel:
    artifact: StateSpaceModelArtifact
    conditioning: ExogenousConditioningContext
    location_series_sources: LocationSeriesSources
    label: str = "state_space"
    model_version_id: str = ""
    evidence_set_id: str = ""
    calibration_artifact_id: str = ""

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        conditioning: ExogenousConditioningContext,
        location_series_sources: LocationSeriesSources,
        evidence_source_id: str,
    ) -> StateSpaceModel:
        try:
            artifact = StateSpaceModelArtifact.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as error:
            raise ValueError(f"failed to load trained state-space model {path}: {error}") from error
        model = cls(artifact=artifact, conditioning=conditioning, location_series_sources=location_series_sources)
        model._compute_provenance(evidence_source_id)
        return model

    @classmethod
    def fit(
        cls,
        historical: HistoricalSeries,
        *,
        latest_level_by_factor: Mapping[str, float],
        source_manifest: Mapping[str, Any],
        prior_manifest: Mapping[str, Any],
        additional_factors: tuple[StateSpaceAdditionalFactor, ...] = (),
    ) -> StateSpaceModelArtifact:
        base_factor_names = tuple(historical.factor_names)
        returns = historical_log_returns(historical)
        if returns.shape[0] < 3:
            raise ValueError("state-space training needs at least three monthly return rows")
        mean = returns.mean(axis=0)
        cov = np.cov(returns, rowvar=False)
        if cov.ndim == 0:
            cov = np.asarray([[float(cov)]], dtype=np.float64)
        cov = _regularize_covariance(base_factor_names, np.asarray(cov, dtype=np.float64))

        factor_names = list(base_factor_names)
        latest_levels = {factor: float(latest_level_by_factor[factor]) for factor in base_factor_names}
        mean_by_factor = {factor: float(mean[idx]) for idx, factor in enumerate(base_factor_names)}
        event_priors: dict[str, StateSpacePrivateEquityEventPrior] = {}
        scale_priors: dict[str, TrainedPrivateEquityScalePrior] = {}
        covariance = cov

        for extra in additional_factors:
            if extra.factor_name in factor_names:
                raise ValueError(f"additional factor duplicates fitted factor {extra.factor_name!r}")
            if extra.latest_level <= 0:
                raise ValueError(f"additional factor {extra.factor_name!r} latest_level must be positive")
            factor_names.append(extra.factor_name)
            latest_levels[extra.factor_name] = float(extra.latest_level)
            mean_by_factor[extra.factor_name] = float(extra.monthly_log_return_mu)
            covariance = _append_factor_covariance(
                tuple(factor_names[:-1]),
                covariance,
                sigma=float(extra.monthly_log_return_sigma),
                covariance_with_factors=extra.covariance_with_factors,
            )
            if extra.private_equity_issuer_id is not None and extra.private_equity_event_prior is not None:
                event_priors[extra.private_equity_issuer_id] = extra.private_equity_event_prior
            if extra.private_equity_issuer_id is not None:
                if extra.private_equity_event_prior is None:
                    raise ValueError(
                        f"private-equity factor {extra.private_equity_issuer_id!r} missing tender event prior"
                    )
                if extra.private_equity_scale_prior is None:
                    raise ValueError(
                        f"private-equity factor {extra.private_equity_issuer_id!r} missing macro-scale prior"
                    )
                scale_priors[extra.private_equity_issuer_id] = extra.private_equity_scale_prior

        covariance = _nearest_positive_semidefinite(covariance)
        filtered_cov = np.diag(np.maximum(np.diag(covariance), _MIN_MONTHLY_VARIANCE))
        trained_through_month = historical.months[-1]
        merged_source_manifest = {
            **dict(source_manifest),
            "additional_factors": {extra.factor_name: {"source_ids": extra.source_ids} for extra in additional_factors},
        }
        return StateSpaceModelArtifact(
            factor_names=tuple(factor_names),
            trained_through_month=trained_through_month,
            latest_level_by_factor=latest_levels,
            monthly_log_return_mu=mean_by_factor,
            monthly_log_return_cov=_matrix_to_tuple(covariance),
            filtered_log_state_mean={factor: math.log(latest_levels[factor]) for factor in factor_names},
            filtered_log_state_cov=_matrix_to_tuple(filtered_cov),
            private_equity_event_priors=event_priors,
            private_equity_scale_priors=scale_priors,
            source_manifest=merged_source_manifest,
            prior_manifest=dict(prior_manifest),
        )

    @property
    def factor_names(self) -> tuple[str, ...]:
        return self.artifact.factor_names

    def save(self, path: Path) -> None:
        path.write_text(self.artifact.model_dump_json(indent=2), encoding="utf-8")

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        rollout_count = request.rollout_count
        horizon_months = request.horizon_months
        factor_names = self.artifact.factor_names
        if rollout_count == 0:
            factor_levels = np.empty((0, horizon_months + 1, len(factor_names)), dtype=np.float64)
        else:
            factor_levels = self._sample_factor_levels(request)

        path_by_factor = {
            factor_name: factor_levels[:, :, factor_index] for factor_index, factor_name in enumerate(factor_names)
        }
        event_by_issuer = {
            issuer_id: self._private_equity_event_series(issuer_id, request)
            for issuer_id in sorted(self.artifact.private_equity_event_priors)
        }
        level_blocks = []
        observed_mark_by_issuer: dict[str, np.ndarray] = {}
        pe_issuer_by_wire_id = {
            PrivateEquityAssetKey(issuer_id=issuer_id).wire_id: issuer_id
            for issuer_id in self.artifact.private_equity_factor_issuers
        }
        for series_id, factor_name in sorted(self._series_factor_map().items()):
            if factor_name not in path_by_factor:
                continue
            levels = path_by_factor[factor_name]
            if (private_equity_issuer := pe_issuer_by_wire_id.get(series_id)) is not None:
                # PE marks live in the canonical PrivateEquityBundle below; the legacy
                # `levels` frame only carries non-PE series now.
                if str(private_equity_issuer) in event_by_issuer:
                    observed_mark_by_issuer[str(private_equity_issuer)] = observed_private_equity_mark_matrix(
                        levels, event_by_issuer[str(private_equity_issuer)]
                    )
                continue
            level_blocks.append(
                series_levels_frame(
                    parse_level_series_key(series_id),
                    levels,
                    rollout_count=rollout_count,
                    horizon_months=horizon_months,
                )
            )
        private_equity_parts = [
            neutral_private_equity_issuer_bundle(
                issuer_id,
                observed_mark=observed_mark_by_issuer[issuer_id],
                tender_events=tender_events,
                rollout_count=rollout_count,
                horizon_months=horizon_months,
            )
            for issuer_id, tender_events in event_by_issuer.items()
            if issuer_id in observed_mark_by_issuer
        ]
        private_equity = (
            PrivateEquityBundle.combine(private_equity_parts) if private_equity_parts else PrivateEquityBundle.empty()
        )
        sampled = SampledExogenousBundle(
            levels=concat_frames(level_blocks, SERIES_LEVELS_SCHEMA),
            private_equity=private_equity,
            metadata={
                "model_version_id": self.model_version_id,
                "model_id": self.label,
                "scenario_generator_id": "state_space_numpy",
                "scenario_generator_version_id": "state_space_numpy:v1",
                "evidence_set_id": self.evidence_set_id,
                "calibration_artifact_id": self.calibration_artifact_id,
                "trained_through_month": self.artifact.trained_through_month,
                "conditioning_start_at": self.conditioning.start_at.isoformat(),
                "source_manifest": self.artifact.source_manifest,
                "prior_manifest": self.artifact.prior_manifest,
                "private_equity_prices_usd": self._private_equity_prices_usd(),
                "exogenous_provider_label": self.label,
            },
        )
        validate_sample_satisfies_request(request, sampled)
        return sampled

    def predictive(self, historical: HistoricalSeries, t: int, *, horizon: int = 1) -> dist.Distribution | None:
        if horizon < 1:
            raise ValueError(f"horizon must be >= 1; got {horizon}")
        n_steps = historical.levels.shape[0] - 1
        if t + horizon > n_steps:
            return None
        factor_index = {factor: idx for idx, factor in enumerate(self.artifact.factor_names)}
        try:
            indices = [factor_index[factor] for factor in historical.factor_names]
        except KeyError:
            return None
        mean = np.asarray([self.artifact.monthly_log_return_mu[factor] for factor in historical.factor_names])
        cov = np.asarray(self.artifact.monthly_log_return_cov, dtype=np.float64)[np.ix_(indices, indices)]
        return dist.MultivariateNormal(
            jnp.asarray(mean * horizon, dtype=jnp.float32),
            covariance_matrix=jnp.asarray(cov * horizon, dtype=jnp.float32),
        )

    def _sample_factor_levels(self, request: ExogenousSamplingRequest) -> np.ndarray:
        factor_names = self.artifact.factor_names
        x0 = np.asarray([math.log(level) for level in self._conditioned_start_levels().values()], dtype=np.float64)
        mean = np.asarray([self.artifact.monthly_log_return_mu[factor] for factor in factor_names], dtype=np.float64)
        cov = np.asarray(self.artifact.monthly_log_return_cov, dtype=np.float64)
        levels = np.empty((request.rollout_count, request.horizon_months + 1, len(factor_names)), dtype=np.float64)
        private_equity_scale_indexes = self._private_equity_scale_indexes()
        for rollout_idx, seed in enumerate(request.rollout_seeds):
            rng = np.random.default_rng(seed)
            log_path = np.empty((request.horizon_months + 1, len(factor_names)), dtype=np.float64)
            log_path[0, :] = x0
            if request.horizon_months:
                increments = rng.multivariate_normal(mean=mean, cov=cov, size=request.horizon_months)
                if private_equity_scale_indexes:
                    for month_idx, raw_increment in enumerate(increments, start=1):
                        increment = np.asarray(raw_increment, dtype=np.float64).copy()
                        for factor_index, scale_prior in private_equity_scale_indexes:
                            increment[factor_index] -= private_equity_soft_cap_penalty(
                                log_price=float(log_path[month_idx - 1, factor_index]),
                                log_current_price=float(x0[factor_index]),
                                scale_prior=scale_prior,
                            )
                        log_path[month_idx, :] = log_path[month_idx - 1, :] + increment
                else:
                    log_path[1:, :] = x0 + np.cumsum(increments, axis=0)
            try:
                with np.errstate(over="raise", invalid="raise"):
                    level_path = np.exp(log_path)
            except FloatingPointError as error:
                raise ValueError("state-space model produced non-finite levels") from error
            if not np.all(np.isfinite(level_path)) or np.any(level_path <= 0.0):
                raise ValueError("state-space model produced invalid levels")
            levels[rollout_idx, :, :] = level_path
        return levels

    def _conditioned_start_levels(self) -> dict[str, float]:
        factor_by_series = self._series_factor_map()
        levels = {factor: float(self.artifact.latest_level_by_factor[factor]) for factor in self.artifact.factor_names}
        conditioned_by_factor: dict[str, tuple[str, float]] = {}
        for series_id, point in latest_observations_by_series(self.conditioning).items():
            if point.treatment == ObservationTreatment.INFORMATIVE:
                continue
            factor = factor_by_series.get(series_id)
            if factor is None:
                continue
            previous = conditioned_by_factor.get(factor)
            if previous is not None and not math.isclose(previous[1], point.value, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(
                    f"conditioning observations {previous[0]!r} and {series_id!r} both target factor "
                    f"{factor!r} with different values"
                )
            conditioned_by_factor[factor] = (series_id, float(point.value))
        for factor, (_series_id, value) in conditioned_by_factor.items():
            levels[factor] = value
        return levels

    def _series_factor_map(self) -> dict[str, str]:
        factors = set(self.artifact.factor_names)
        mapping: dict[str, str] = {}
        for factor, classification in zip(
            self.artifact.factor_names, self.artifact.factor_classifications, strict=True
        ):
            # Both PE and level-series factors keep their wire-id form as the
            # series-id lookup key. Classification just ensures they round-trip
            # cleanly through the typed boundary.
            del classification  # presence in the classification is enough
            mapping[factor] = factor
        for location_id, factor in self.location_series_sources.home_value.items():
            if factor in factors:
                mapping[HomeValueKey(location_id=LocationId(location_id)).wire_id] = factor
        for location_id, factor in self.location_series_sources.rent.items():
            if factor in factors:
                mapping[RentKey(location_id=LocationId(location_id)).wire_id] = factor
        return mapping

    def _private_equity_event_series(self, issuer_id: str, request: ExogenousSamplingRequest) -> np.ndarray:
        prior = self.artifact.private_equity_event_priors[issuer_id]
        events = np.zeros((request.rollout_count, request.horizon_months + 1), dtype=np.bool_)
        if request.rollout_count == 0:
            return events
        event_seeds = derive_stream_rollout_seeds(request.rollout_seeds, stream_id=f"{issuer_id}:state_space_pe_event")
        elapsed_since_last_tender = (
            max(months_between(prior.last_tender_observed_at, self.conditioning.start_at), 0.0)
            if prior.last_tender_observed_at is not None
            else 0.0
        )
        for rollout_idx, seed in enumerate(event_seeds):
            rng = np.random.default_rng(seed)
            cursor_month = -elapsed_since_last_tender
            while True:
                interval = float(
                    rng.lognormal(
                        mean=math.log(prior.tender_interval_months_median), sigma=prior.tender_interval_log_sigma
                    )
                )
                cursor_month += max(interval, 1.0)
                month_index = round(cursor_month)
                if month_index > request.horizon_months:
                    break
                if month_index >= 1:
                    events[rollout_idx, month_index] = True
        return events

    def _private_equity_prices_usd(self) -> dict[str, float]:
        levels = self._conditioned_start_levels()
        return {
            str(issuer_id): levels[PrivateEquityAssetKey(issuer_id=issuer_id).wire_id]
            for issuer_id in self.artifact.private_equity_factor_issuers
        }

    def _private_equity_scale_indexes(self) -> tuple[tuple[int, TrainedPrivateEquityScalePrior], ...]:
        if not self.artifact.private_equity_scale_priors:
            return ()
        factor_index = {factor: idx for idx, factor in enumerate(self.artifact.factor_names)}
        indexes: list[tuple[int, TrainedPrivateEquityScalePrior]] = []
        for issuer_id, scale_prior in self.artifact.private_equity_scale_priors.items():
            factor_name = PrivateEquityAssetKey(issuer_id=IssuerId(issuer_id)).wire_id
            if factor_name in factor_index:
                indexes.append((factor_index[factor_name], scale_prior))
        return tuple(indexes)

    def _compute_provenance(self, evidence_source_id: str) -> None:
        self.model_version_id = "model_version:" + stable_identity_digest(
            {"label": self.label, "class": type(self).__qualname__, "schema_version": self.artifact.schema_version}
        )
        self.evidence_set_id = "evidence_set:" + stable_identity_digest(
            {"evidence_source_id": evidence_source_id, "conditioning": self.conditioning}
        )
        self.calibration_artifact_id = "calibration_artifact:" + stable_identity_digest(
            {"model_id": self.label, "model_version_id": self.model_version_id, "evidence_set_id": self.evidence_set_id}
        )


def _append_factor_covariance(
    existing_factor_names: tuple[str, ...],
    covariance: np.ndarray,
    *,
    sigma: float,
    covariance_with_factors: Mapping[str, float],
) -> np.ndarray:
    if sigma <= 0:
        raise ValueError("additional factor sigma must be positive")
    n = covariance.shape[0]
    out = np.zeros((n + 1, n + 1), dtype=np.float64)
    out[:n, :n] = covariance
    out[n, n] = sigma**2
    index = {factor: idx for idx, factor in enumerate(existing_factor_names)}
    for factor, cov_value in covariance_with_factors.items():
        if factor not in index:
            raise ValueError(f"additional factor covariance references unknown factor {factor!r}")
        idx = index[factor]
        out[idx, n] = out[n, idx] = float(cov_value)
    return out


def _regularize_covariance(factor_names: tuple[str, ...], covariance: np.ndarray) -> np.ndarray:
    covariance = np.asarray(covariance, dtype=np.float64)
    diag = np.maximum(np.diag(covariance), _MIN_MONTHLY_VARIANCE)
    out = np.diag(diag)
    for row in range(len(factor_names)):
        for col in range(row):
            shrinkage = (
                _ON_BLOCK_SHRINKAGE if _coupling_allowed(factor_names[row], factor_names[col]) else _OFF_BLOCK_SHRINKAGE
            )
            value = float(covariance[row, col]) * shrinkage
            out[row, col] = out[col, row] = value
    return _nearest_positive_semidefinite(out)


def _coupling_allowed(left: str, right: str) -> bool:
    left_classification = _classify_factor(left)
    right_classification = _classify_factor(right)
    left_is_pe = isinstance(left_classification, str)
    right_is_pe = isinstance(right_classification, str)
    if isinstance(left_classification, CryptoKey) or isinstance(right_classification, CryptoKey):
        return isinstance(left_classification, CryptoKey) and isinstance(right_classification, CryptoKey)
    if left_is_pe:
        return isinstance(right_classification, SP500Key)
    if right_is_pe:
        return isinstance(left_classification, SP500Key)
    return True


def _nearest_positive_semidefinite(matrix: np.ndarray) -> np.ndarray:
    matrix = (matrix + matrix.T) / 2.0
    eigvals, eigvecs = np.linalg.eigh(matrix)
    eigvals = np.maximum(eigvals, _MIN_MONTHLY_VARIANCE)
    repaired = (eigvecs * eigvals) @ eigvecs.T
    return np.asarray((repaired + repaired.T) / 2.0, dtype=np.float64)


def _matrix_to_tuple(matrix: np.ndarray) -> tuple[tuple[float, ...], ...]:
    return tuple(tuple(float(value) for value in row) for row in np.asarray(matrix, dtype=np.float64))


def _require_square_matrix(value: tuple[tuple[float, ...], ...], n: int, label: str) -> None:
    if len(value) != n or any(len(row) != n for row in value):
        raise ValueError(f"{label} must be {n}x{n}")


def write_state_space_artifact(path: Path, artifact: StateSpaceModelArtifact) -> None:
    path.write_text(json.dumps(artifact.model_dump(mode="json"), indent=2, sort_keys=True), encoding="utf-8")
