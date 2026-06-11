"""Fit inputs for the block-shrunk state-space exogenous provider."""

from __future__ import annotations

import math
from collections.abc import Mapping
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
from finance.augur.model.conditioning import ExogenousConditioningContext, ExogenousObservedPoint, ObservationTreatment
from finance.augur.model.path_models.scenarios import HistoricalSeries
from finance.augur.model.series import IssuerId
from finance.augur.model.state_space import (
    StateSpaceAdditionalFactor,
    StateSpaceModel,
    StateSpaceModelArtifact,
    StateSpacePrivateEquityEventPrior,
)
from finance.augur.product.asset_key import PrivateEquityAssetKey

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
    conditioning_point: ExogenousObservedPoint
    coupling_diagnostics: dict[str, float | int]


def fit_state_space_artifact(
    historical: HistoricalSeries, evidence: ExogenousEvidence, *, private_equity_config_paths: tuple[Path, ...] = ()
) -> tuple[StateSpaceModelArtifact, ExogenousConditioningContext]:
    private_factors = tuple(_fit_private_equity_factor(path, historical) for path in private_equity_config_paths)
    artifact = StateSpaceModel.fit(
        historical,
        latest_level_by_factor=_latest_level_by_factor(evidence),
        source_manifest=_source_manifest(evidence, private_factors),
        prior_manifest=_prior_manifest(evidence, private_factors),
        additional_factors=tuple(factor.factor for factor in private_factors),
    )
    conditioning = _conditioning_from_evidence(evidence, private_factors)
    return artifact, conditioning


def _latest_level_by_factor(evidence: ExogenousEvidence) -> dict[str, float]:
    return {
        factor: _latest_factor_observation(evidence.latest_observations, factor).value
        for factor in evidence.factor_names
    }


def _conditioning_from_evidence(
    evidence: ExogenousEvidence, private_factors: tuple[FittedPrivateEquityStateSpaceFactor, ...]
) -> ExogenousConditioningContext:
    observations: dict[str, tuple[ExogenousObservedPoint, ...]] = {
        factor: (_latest_factor_observation(evidence.latest_observations, factor),) for factor in evidence.factor_names
    }
    for fitted in private_factors:
        observations[fitted.factor.factor_name] = (fitted.conditioning_point,)
    start_at = max(point.observed_at for points in observations.values() for point in points)
    return ExogenousConditioningContext(start_at=start_at, observations=observations)


def _latest_factor_observation(latest: Mapping[str, Any], factor: str) -> ExogenousObservedPoint:
    if factor == "sp500":
        return _point_from_latest(latest["spy_adjusted_close_latest"], source_prefix="public")
    if factor == "inflation":
        return _point_from_latest(latest["cpi_latest"], source_prefix="public")
    if factor == "crypto:btc":
        return _point_from_latest(latest["btc_close_latest"], source_prefix="public")
    if factor == "crypto:eth":
        return _point_from_latest(latest["eth_close_latest"], source_prefix="public")

    home_by_factor = latest.get("zillow_home_value_latest_by_factor")
    if isinstance(home_by_factor, Mapping) and factor in home_by_factor:
        return _point_from_latest(home_by_factor[factor], source_prefix="public")
    rent_by_factor = latest.get("zillow_rent_latest_by_factor")
    if isinstance(rent_by_factor, Mapping) and factor in rent_by_factor:
        return _point_from_latest(rent_by_factor[factor], source_prefix="public")
    case_shiller_by_factor = latest.get("case_shiller_home_value_latest_by_factor")
    if isinstance(case_shiller_by_factor, Mapping) and factor in case_shiller_by_factor:
        return _point_from_latest(case_shiller_by_factor[factor], source_prefix="public")
    raise ValueError(f"no latest observation can anchor state-space factor {factor!r}")


def _point_from_latest(raw: Any, *, source_prefix: str) -> ExogenousObservedPoint:
    if not isinstance(raw, Mapping):
        raise TypeError(f"latest observation must be a mapping, got {type(raw).__name__}")
    value = raw.get("value")
    source = raw.get("source")
    observed_at = raw.get("date")
    if not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"latest observation has invalid value {value!r}")
    if not isinstance(source, str) or not source:
        raise ValueError("latest observation has no source")
    return ExogenousObservedPoint(
        value=float(value),
        observed_at=_parse_observed_date(observed_at),
        source_id=f"{source_prefix}:{source}",
        treatment=ObservationTreatment.HARD_START,
    )


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
    sp500_sigma = _monthly_sigma(historical, "sp500")
    covariance_with_sp500 = coupling["rho_to_sp500"] * artifact.monthly_log_return_sigma * sp500_sigma
    factor = StateSpaceAdditionalFactor(
        factor_name=PrivateEquityAssetKey(issuer_id=IssuerId(config.issuer_id)).wire_id,
        latest_level=artifact.current_mark_usd,
        monthly_log_return_mu=artifact.monthly_log_return_mu,
        monthly_log_return_sigma=artifact.monthly_log_return_sigma,
        covariance_with_factors={"sp500": covariance_with_sp500},
        source_ids=tuple(sorted({obs.source_id for obs in issuer_observations})),
        private_equity_issuer_id=config.issuer_id,
        private_equity_event_prior=StateSpacePrivateEquityEventPrior(
            tender_interval_months_median=artifact.tender_interval_months_median,
            tender_interval_log_sigma=artifact.tender_interval_log_sigma,
            last_tender_observed_at=artifact.last_tender_observed_at,
        ),
        private_equity_scale_prior=artifact.scale_prior,
    )
    point = ExogenousObservedPoint(
        value=artifact.current_mark_usd,
        observed_at=artifact.as_of_date,
        source_id=f"private:{mark.source_id}",
        treatment=ObservationTreatment.NOISY_MARK,
        log_sigma=mark.uncertainty_log_sigma,
        notes=mark.notes,
    )
    return FittedPrivateEquityStateSpaceFactor(factor=factor, conditioning_point=point, coupling_diagnostics=coupling)


def _estimate_sp500_coupling(
    observations: list[PriceObservation], historical: HistoricalSeries
) -> dict[str, float | int]:
    rows: list[tuple[float, float]] = []
    for start, end in pairwise(sorted(observations, key=lambda obs: obs.observed_at)):
        duration = months_between(start.observed_at, end.observed_at)
        if duration <= 0:
            continue
        sp500_return = _historical_cumulative_return(historical, "sp500", start.observed_at, end.observed_at)
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
        factor_idx = [factor.wire_id for factor in historical.factor_names].index(factor_name)
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
    factor_idx = [factor.wire_id for factor in historical.factor_names].index(factor_name)
    returns = np.diff(np.log(historical.levels[:, factor_idx]))
    return float(np.std(returns, ddof=1))


def _source_manifest(
    evidence: ExogenousEvidence, private_factors: tuple[FittedPrivateEquityStateSpaceFactor, ...]
) -> dict[str, Any]:
    return {
        "public_factor_names": evidence.factor_names,
        "monthly_return_months": {
            "first": evidence.monthly_return_months[0],
            "last": evidence.monthly_return_months[-1],
            "count": len(evidence.monthly_return_months),
        },
        "latest_observations": evidence.latest_observations,
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
            "private_equity_couples_to": "sp500",
        },
        "series_path_prior_calibration": evidence.latest_observations.get("series_path_prior_calibration", {}),
        "private_equity_sp500_correlation_prior": {
            "rho": _PRIVATE_EQUITY_SP500_CORR_PRIOR,
            "interval_weight": _PRIVATE_EQUITY_SP500_CORR_PRIOR_WEIGHT,
            "applies_to": [fitted.factor.factor_name for fitted in private_factors],
        },
    }


def _parse_observed_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TypeError(f"observed date must be a string or date, got {type(value).__name__}")
    if len(value) == 7:
        return pd.Period(value, freq="M").to_timestamp().date()
    return date.fromisoformat(value)


def _resolve_path(path: str | Path, base_dir: Path) -> Path:
    value = Path(path)
    if value.is_absolute():
        return value
    return (base_dir / value).resolve()
