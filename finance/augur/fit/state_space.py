"""Fit inputs for the block-shrunk state-space exogenous provider."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from finance.augur.dates import months_between
from finance.augur.fit.evidence_data import ExogenousEvidence
from finance.augur.fit.private_equity import (
    PriceObservation,
    fit_private_equity_model,
    load_price_observations_jsonl,
    load_training_config,
)
from finance.augur.model.asset_key import PrivateEquityAssetKey
from finance.augur.model.conditioning import (
    ExogenousConditioningContext,
    ExogenousObservedPoint,
    ObservationTreatment,
    ObservationUnits,
)
from finance.augur.model.path_models.scenarios import HistoricalSeries
from finance.augur.model.series import SP500_KEY, IssuerId
from finance.augur.model.state_space import (
    StateSpaceAdditionalFactor,
    StateSpaceModel,
    StateSpaceModelArtifact,
    StateSpacePrivateEquityEventPrior,
)

# Weak prior for the monthly private-equity/SP500 return correlation. The value
# is intentionally generic rather than company-specific: 0.35 is a middle of the
# "positive but noisy market exposure" range implied by public software/high-growth
# industry beta tables such as Damodaran's industry betas, converted into a
# conservative correlation anchor instead of a hard beta. The pseudo-count keeps
# sparse tender histories from pinning the covariance from one or two intervals.
_PRIVATE_EQUITY_SP500_CORR_PRIOR = 0.35
_PRIVATE_EQUITY_SP500_CORR_PRIOR_WEIGHT = 2.0


@dataclass(frozen=True)
class FittedPrivateEquityStateSpaceFactor:
    factor: StateSpaceAdditionalFactor
    coupling_diagnostics: dict[str, float | int]


def fit_state_space_artifact(
    historical: HistoricalSeries, evidence: ExogenousEvidence, *, private_equity_config_paths: tuple[Path, ...] = ()
) -> tuple[StateSpaceModelArtifact, ExogenousConditioningContext]:
    private_factors = tuple(_fit_private_equity_factor(path, historical) for path in private_equity_config_paths)
    artifact = StateSpaceModel.fit(
        historical,
        latest_observations={key.wire_id: point for key, point in evidence.latest_observations.items()},
        source_manifest=_source_manifest(evidence, private_factors),
        prior_manifest=_prior_manifest(evidence, private_factors),
        additional_factors=tuple(factor.factor for factor in private_factors),
    )
    conditioning = _conditioning_from_evidence(evidence, private_factors)
    return artifact, conditioning


def _conditioning_from_evidence(
    evidence: ExogenousEvidence, private_factors: tuple[FittedPrivateEquityStateSpaceFactor, ...]
) -> ExogenousConditioningContext:
    observations: dict[str, tuple[ExogenousObservedPoint, ...]] = {
        key.wire_id: (evidence.latest_observations[key],) for key in evidence.series_names
    }
    for fitted in private_factors:
        observations[fitted.factor.factor_name] = (fitted.factor.observation,)
    start_at = max(point.observed_at for points in observations.values() for point in points)
    return ExogenousConditioningContext(start_at=start_at, observations=observations)


def _fit_private_equity_factor(config_path: Path, historical: HistoricalSeries) -> FittedPrivateEquityStateSpaceFactor:
    config = load_training_config(config_path)
    observations_path = _resolve_path(config.observations_path, config_path.parent)
    observations = load_price_observations_jsonl(observations_path)
    artifact = fit_private_equity_model(observations, config)
    issuer_observations = sorted(
        [observation for observation in observations if observation.issuer_id == config.issuer_id],
        key=lambda observation: observation.observed_at,
    )
    price_observations = [
        observation for observation in issuer_observations if isinstance(observation, PriceObservation)
    ]
    mark = max((obs for obs in price_observations if obs.kind == "ppu_mark"), key=lambda obs: obs.observed_at)
    coupling = _estimate_sp500_coupling(price_observations, historical)
    sp500_sigma = _monthly_sigma(historical, SP500_KEY.wire_id)
    covariance_with_sp500 = coupling["rho_to_sp500"] * artifact.monthly_log_return_sigma * sp500_sigma
    factor = StateSpaceAdditionalFactor(
        factor_name=PrivateEquityAssetKey(issuer_id=IssuerId(config.issuer_id)).wire_id,
        observation=ExogenousObservedPoint(
            value=artifact.current_mark_usd,
            units=ObservationUnits.USD_PER_UNIT,
            observed_at=artifact.as_of_date,
            source_id=f"private:{mark.source_id}",
            treatment=ObservationTreatment.NOISY_MARK,
            log_sigma=mark.uncertainty_log_sigma,
            notes=mark.notes,
        ),
        monthly_log_return_mu=artifact.monthly_log_return_mu,
        monthly_log_return_sigma=artifact.monthly_log_return_sigma,
        covariance_with_factors={SP500_KEY.wire_id: covariance_with_sp500},
        source_ids=tuple(sorted({obs.source_id for obs in issuer_observations})),
        private_equity_issuer_id=config.issuer_id,
        private_equity_event_prior=StateSpacePrivateEquityEventPrior(
            tender_interval_months_median=artifact.tender_interval_months_median,
            tender_interval_log_sigma=artifact.tender_interval_log_sigma,
            last_tender_observed_at=artifact.last_tender_observed_at,
        ),
        private_equity_scale_prior=artifact.scale_prior,
    )
    return FittedPrivateEquityStateSpaceFactor(factor=factor, coupling_diagnostics=coupling)


def _estimate_sp500_coupling(
    observations: list[PriceObservation], historical: HistoricalSeries
) -> dict[str, float | int]:
    rows: list[tuple[float, float]] = []
    for start, end in pairwise(sorted(observations, key=lambda obs: obs.observed_at)):
        duration = months_between(start.observed_at, end.observed_at)
        if duration <= 0:
            continue
        sp500_return = _historical_cumulative_return(historical, SP500_KEY.wire_id, start.observed_at, end.observed_at)
        if sp500_return is None:
            continue
        pe_return = math.log(end.price_usd_per_share / start.price_usd_per_share)
        rows.append((sp500_return / duration, pe_return / duration))

    if len(rows) >= 2:
        points = np.asarray(rows, dtype=np.float64)
        sp500 = points[:, 0]
        pe = points[:, 1]
        sp500_var = float(np.var(sp500, ddof=1))
        if sp500_var > 0:
            beta_hat = float(np.cov(sp500, pe, ddof=1)[0, 1] / sp500_var)
            rho_hat = float(np.corrcoef(sp500, pe)[0, 1])
            if not math.isfinite(rho_hat):
                rho_hat = _PRIVATE_EQUITY_SP500_CORR_PRIOR
        else:
            beta_hat = 0.0
            rho_hat = _PRIVATE_EQUITY_SP500_CORR_PRIOR
    else:
        beta_hat = 0.0
        rho_hat = _PRIVATE_EQUITY_SP500_CORR_PRIOR

    n = len(rows)
    rho = (rho_hat * n + _PRIVATE_EQUITY_SP500_CORR_PRIOR * _PRIVATE_EQUITY_SP500_CORR_PRIOR_WEIGHT) / (
        n + _PRIVATE_EQUITY_SP500_CORR_PRIOR_WEIGHT
    )
    return {"interval_count": n, "beta_to_sp500": beta_hat, "rho_to_sp500": rho}


def _historical_cumulative_return(
    historical: HistoricalSeries, factor_name: str, start: date, end: date
) -> float | None:
    try:
        factor_idx = [factor.wire_id for factor in historical.series_names].index(factor_name)
    except ValueError:
        return None
    periods = [pd.Period(month, freq="M") for month in historical.months]
    start_period = pd.Period(start, freq="M")
    end_period = pd.Period(end, freq="M")
    log_levels = np.log(historical.levels[:, factor_idx])
    total = 0.0
    matched = False
    for idx in range(1, len(periods)):
        if start_period < periods[idx] <= end_period:
            total += float(log_levels[idx] - log_levels[idx - 1])
            matched = True
    return total if matched else None


def _monthly_sigma(historical: HistoricalSeries, factor_name: str) -> float:
    factor_idx = [factor.wire_id for factor in historical.series_names].index(factor_name)
    returns = np.diff(np.log(historical.levels[:, factor_idx]))
    return float(np.std(returns, ddof=1))


def _source_manifest(
    evidence: ExogenousEvidence, private_factors: tuple[FittedPrivateEquityStateSpaceFactor, ...]
) -> dict[str, Any]:
    return {
        "public_factor_names": tuple(key.wire_id for key in evidence.series_names),
        "monthly_return_months": {
            "first": evidence.monthly_return_months[0],
            "last": evidence.monthly_return_months[-1],
            "count": len(evidence.monthly_return_months),
        },
        "evidence_metadata": evidence.metadata.model_dump(mode="json"),
        "private_equity": {
            fitted.factor.factor_name: {
                "source_ids": fitted.factor.source_ids,
                "coupling_diagnostics": fitted.coupling_diagnostics,
                "has_scale_prior": fitted.factor.private_equity_scale_prior is not None,
            }
            for fitted in private_factors
        },
    }


def _prior_manifest(
    evidence: ExogenousEvidence, private_factors: tuple[FittedPrivateEquityStateSpaceFactor, ...]
) -> dict[str, Any]:
    return {
        "kind": "empirical_block_shrunk_state_space_v1",
        "covariance": {
            "crypto_cross_block_correlation": 0.0,
            "non_crypto_offdiag_shrinkage": 0.5,
            "private_equity_couples_to": SP500_KEY.wire_id,
        },
        "series_path_prior_calibration": {
            name: point.model_dump(mode="json")
            for name, point in evidence.metadata.series_path_prior_calibration.items()
        },
        "private_equity_sp500_correlation_prior": {
            "rho": _PRIVATE_EQUITY_SP500_CORR_PRIOR,
            "interval_weight": _PRIVATE_EQUITY_SP500_CORR_PRIOR_WEIGHT,
            "applies_to": [fitted.factor.factor_name for fitted in private_factors],
        },
    }


def _resolve_path(path: str | Path, base_dir: Path) -> Path:
    value = Path(path)
    if value.is_absolute():
        return value
    return (base_dir / value).resolve()
