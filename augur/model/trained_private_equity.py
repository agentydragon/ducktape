"""Runtime sampler for compact trained private-equity price models."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import Field

from augur.dates import months_between
from augur.model.exogenous import SERIES_LEVELS_SCHEMA, ExogenousSamplingRequest, SampledExogenousBundle
from augur.model.private_equity_protocol import (
    neutral_private_equity_issuer_bundle,
    observed_private_equity_mark_matrix,
)
from augur.model.schemas import FrozenModel
from augur.model.series_model import derive_stream_rollout_seeds


class TrainedPrivateEquityScalePrior(FrozenModel):
    """Issuer-scale prior used to make extreme paths mean-revert softly.

    `current_market_cap_usd` is the inferred issuer-scale value at the artifact's
    current mark. `soft_cap_market_cap_usd` is not a hard maximum: above it, the
    sampler subtracts an increasing drift penalty while still allowing shocks to
    produce upside tail paths.
    """

    current_market_cap_usd: float = Field(gt=0)
    soft_cap_market_cap_usd: float = Field(gt=0)
    monthly_log_drift_penalty: float = Field(ge=0)


class TrainedPrivateEquityModelArtifact(FrozenModel):
    """Compact JSON artifact written by the offline PE trainer.

    The artifact stores fitted parameters only. Runtime sampling happens from
    these parameters on each Augur request; no pre-sampled trajectories are
    embedded in production config.
    """

    schema_version: Literal[1] = 1
    issuer_id: str = Field(min_length=1)
    as_of_date: date
    current_mark_usd: float = Field(gt=0)
    monthly_log_return_mu: float
    monthly_log_return_sigma: float = Field(gt=0)
    student_t_nu: float = Field(default=5.0, gt=2)
    tender_interval_months_median: float = Field(gt=0)
    tender_interval_log_sigma: float = Field(gt=0)
    tender_price_log_discount_mu: float = 0.0
    tender_price_log_discount_sigma: float = Field(default=0.08, ge=0)
    last_tender_observed_at: date | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    scale_prior: TrainedPrivateEquityScalePrior


class TrainedPrivateEquityProviderConfig(FrozenModel):
    """Runtime provider config for a compact trained private-equity model."""

    type: Literal["trained_private_equity"] = "trained_private_equity"
    trained_model_path: Path

    def realize_model(self) -> TrainedPrivateEquityModel:
        return TrainedPrivateEquityModel.from_path(self.trained_model_path)


class TrainedPrivateEquityModel(FrozenModel):
    label: str = "trained_private_equity"
    artifact: TrainedPrivateEquityModelArtifact

    @classmethod
    def from_path(cls, path: Path) -> TrainedPrivateEquityModel:
        try:
            artifact = TrainedPrivateEquityModelArtifact.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as error:
            raise ValueError(f"failed to load trained private-equity model {path}: {error}") from error
        return cls(artifact=artifact)

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        issuer = self.artifact.issuer_id
        rollout_count = request.rollout_count
        horizon_months = request.horizon_months
        if rollout_count == 0:
            levels = np.empty((0, horizon_months + 1), dtype=np.float64)
            events = np.empty((0, horizon_months + 1), dtype=np.bool_)
        else:
            level_seeds = derive_stream_rollout_seeds(request.rollout_seeds, stream_id=f"{issuer}:pe_level")
            event_seeds = derive_stream_rollout_seeds(request.rollout_seeds, stream_id=f"{issuer}:pe_event")
            levels = _sample_levels(self.artifact, rollout_seeds=level_seeds, horizon_months=horizon_months)
            events = _sample_tender_events(self.artifact, rollout_seeds=event_seeds, horizon_months=horizon_months)
            _apply_event_price_noise(self.artifact, levels=levels, events=events, rollout_seeds=event_seeds)
            levels = observed_private_equity_mark_matrix(levels, events)

        return SampledExogenousBundle(
            levels=SERIES_LEVELS_SCHEMA.to_frame(),
            private_equity=neutral_private_equity_issuer_bundle(
                issuer,
                observed_mark=levels,
                tender_events=events,
                rollout_count=rollout_count,
                horizon_months=horizon_months,
            ),
            metadata={
                "model_id": self.label,
                "private_equity_model_schema_version": self.artifact.schema_version,
                "private_equity_issuers": (issuer,),
                "private_equity_prices_usd": {issuer: self.artifact.current_mark_usd},
            },
        )


def _sample_levels(
    artifact: TrainedPrivateEquityModelArtifact, *, rollout_seeds: tuple[int, ...], horizon_months: int
) -> np.ndarray:
    levels = np.empty((len(rollout_seeds), horizon_months + 1), dtype=np.float64)
    for rollout_idx, seed in enumerate(rollout_seeds):
        rng = np.random.default_rng(seed)
        log_path = np.empty(horizon_months + 1, dtype=np.float64)
        log_path[0] = math.log(artifact.current_mark_usd)
        if horizon_months:
            shocks = rng.standard_t(df=artifact.student_t_nu, size=horizon_months) * artifact.monthly_log_return_sigma
            for month_idx, shock in enumerate(shocks, start=1):
                penalty = private_equity_soft_cap_penalty(
                    log_price=log_path[month_idx - 1], log_current_price=log_path[0], scale_prior=artifact.scale_prior
                )
                log_path[month_idx] = log_path[month_idx - 1] + artifact.monthly_log_return_mu + shock - penalty
        try:
            with np.errstate(over="raise", invalid="raise"):
                level_path = np.exp(log_path)
        except FloatingPointError as error:
            raise ValueError(
                f"trained private-equity model for issuer {artifact.issuer_id!r} produced non-finite prices"
            ) from error
        if not np.all(np.isfinite(level_path)) or np.any(level_path <= 0.0):
            raise ValueError(f"trained private-equity model for issuer {artifact.issuer_id!r} produced invalid prices")
        levels[rollout_idx, :] = level_path
    return levels


def _sample_tender_events(
    artifact: TrainedPrivateEquityModelArtifact, *, rollout_seeds: tuple[int, ...], horizon_months: int
) -> np.ndarray:
    events = np.zeros((len(rollout_seeds), horizon_months + 1), dtype=np.bool_)
    elapsed_since_last_tender = (
        max(months_between(artifact.last_tender_observed_at, artifact.as_of_date), 0.0)
        if artifact.last_tender_observed_at is not None
        else 0.0
    )
    for rollout_idx, seed in enumerate(rollout_seeds):
        rng = np.random.default_rng(seed)
        cursor_month = -elapsed_since_last_tender
        while True:
            interval = float(
                rng.lognormal(
                    mean=math.log(artifact.tender_interval_months_median), sigma=artifact.tender_interval_log_sigma
                )
            )
            cursor_month += max(interval, 1.0)
            month_index = round(cursor_month)
            if month_index > horizon_months:
                break
            if month_index >= 1:
                events[rollout_idx, month_index] = True
    return events


def _apply_event_price_noise(
    artifact: TrainedPrivateEquityModelArtifact,
    *,
    levels: np.ndarray,
    events: np.ndarray,
    rollout_seeds: tuple[int, ...],
) -> None:
    if artifact.tender_price_log_discount_sigma == 0 and artifact.tender_price_log_discount_mu == 0:
        return
    for rollout_idx, seed in enumerate(rollout_seeds):
        rng = np.random.default_rng(seed ^ 0x5EED)
        event_months = np.flatnonzero(events[rollout_idx])
        if event_months.size == 0:
            continue
        discounts = rng.normal(
            loc=artifact.tender_price_log_discount_mu,
            scale=artifact.tender_price_log_discount_sigma,
            size=event_months.size,
        )
        levels[rollout_idx, event_months] *= np.exp(discounts)


def private_equity_soft_cap_penalty(
    *, log_price: float, log_current_price: float, scale_prior: TrainedPrivateEquityScalePrior
) -> float:
    """Return a drift penalty for paths above the configured market-cap soft cap."""

    if scale_prior.monthly_log_drift_penalty == 0:
        return 0.0
    log_market_cap = math.log(scale_prior.current_market_cap_usd) + log_price - log_current_price
    over_soft_cap = max(0.0, log_market_cap - math.log(scale_prior.soft_cap_market_cap_usd))
    return scale_prior.monthly_log_drift_penalty * over_soft_cap
