"""Train compact private-equity exogenous models from sparse JSONL observations."""

from __future__ import annotations

import argparse
import json
import math
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Literal

import numpy as np
import yaml
from pydantic import Field, model_validator

from finance.augur.dates import months_between
from finance.augur.model.schemas import StrictModel
from finance.augur.model.trained_private_equity import TrainedPrivateEquityModelArtifact, TrainedPrivateEquityScalePrior

ValuationKind = Literal["primary", "secondary", "admin", "implied"]
"""Annotation of what a valuation_observation represents.

- `primary`: a primary funding round (new shares issued; cash flows into the company).
  Must carry `cash_raised_usd > 0`. The mint-streams model reads these as discrete
  V-jump + share-jump events.
- `secondary`: an employee/investor tender or private secondary trade. No new shares,
  no cash to the company. Valuation is observed at the tender's implied per-share price.
- `admin`: a company-set or 409A-style administrative mark, lagged accounting estimate.
- `implied`: a valuation derived from a tender price + estimated share count, not an
  event observation. Used for synthetic test data and inferred mid-period valuations.
"""

# Stock-like forward priors for private companies whose observed marks are sparse
# tenders rather than continuous public trades. These defaults are deliberately
# generic; issuer-specific config can override them.
#
# Public data sources to use when revisiting these values:
# - Fama/French Data Library, industry portfolios and factor returns:
#   https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html
# - Damodaran US industry beta tables:
#   https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/Betas.html
# - CRSP would be better for survivorship-aware single-name return calibration,
#   but it is licensed rather than publicly downloadable:
#   https://www.crsp.org/research/data-access/
_DEFAULT_STOCK_LIKE_MONTHLY_LOG_RETURN_MU = 0.006
_DEFAULT_STOCK_LIKE_MONTHLY_LOG_RETURN_SIGMA = 0.12
_DEFAULT_STOCK_LIKE_MU_WEIGHT_MONTHS = 240.0
_DEFAULT_STOCK_LIKE_SIGMA_WEIGHT_RETURNS = 60.0

# Macro scale priors use world GDP as a soft economic-capacity reference. This
# is a drift penalty on implausibly huge issuer-scale paths, not a price clip.
# World Bank GDP current USD:
# https://data.worldbank.org/indicator/NY.GDP.MKTP.CD
_DEFAULT_WORLD_GDP_SOFT_CAP_FRACTION = 0.05
_DEFAULT_WORLD_GDP_MONTHLY_LOG_DRIFT_PENALTY = 0.08


class PriceObservation(StrictModel):
    type: Literal["price_observation"]
    issuer_id: str = Field(min_length=1)
    observed_at: date
    kind: Literal["tender_price", "ppu_mark"]
    price_usd_per_share: float = Field(gt=0)
    uncertainty_log_sigma: float = Field(gt=0)
    source_id: str = Field(min_length=1)
    notes: str = ""


class ValuationObservation(StrictModel):
    type: Literal["valuation_observation"]
    issuer_id: str = Field(min_length=1)
    observed_at: date
    valuation_usd: float = Field(gt=0)
    uncertainty_log_sigma: float = Field(gt=0)
    valuation_kind: ValuationKind
    cash_raised_usd: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Cash injected into the company by this round. Required (>0) for `primary`; "
            "must be `None` for non-primary kinds. Used by the mint-streams sampler to read "
            "primary-round events directly from observations rather than inferring them "
            "from a smooth dilution random walk."
        ),
    )
    shares_outstanding_post_round: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Post-event share count, when known (e.g. from a recap information statement "
            "or SEC filing). The fitter can use this to pin per-event dilution exactly, "
            "rather than inferring it from `cash_raised_usd / V_pre`."
        ),
    )
    source_id: str = Field(min_length=1)
    notes: str = ""

    @model_validator(mode="after")
    def _validate_kind_cash(self) -> ValuationObservation:
        if self.valuation_kind == "primary":
            if self.cash_raised_usd is None or self.cash_raised_usd <= 0:
                raise ValueError("primary valuation_observation requires cash_raised_usd > 0")
        elif self.cash_raised_usd is not None:
            raise ValueError(
                f"cash_raised_usd is only valid when valuation_kind='primary' (got {self.valuation_kind!r})"
            )
        return self


PrivateEquityObservation = PriceObservation | ValuationObservation


class PrivateEquityTrainingPriors(StrictModel):
    macro_capacity_reference_usd: float = Field(gt=0)
    min_monthly_log_return_sigma: float = Field(default=0.03, gt=0)
    student_t_nu: float = Field(default=5.0, gt=2)
    tender_interval_months_median_prior: float = Field(default=12.0, gt=0)
    tender_interval_log_sigma: float = Field(default=0.35, gt=0)
    tender_price_log_discount_mu: float = 0.0
    tender_price_log_discount_sigma: float = Field(default=0.08, ge=0)
    stock_like_monthly_log_return_mu: float = _DEFAULT_STOCK_LIKE_MONTHLY_LOG_RETURN_MU
    stock_like_monthly_log_return_mu_weight_months: float = Field(default=_DEFAULT_STOCK_LIKE_MU_WEIGHT_MONTHS, ge=0)
    stock_like_monthly_log_return_sigma: float = Field(default=_DEFAULT_STOCK_LIKE_MONTHLY_LOG_RETURN_SIGMA, gt=0)
    stock_like_monthly_log_return_sigma_weight_returns: float = Field(
        default=_DEFAULT_STOCK_LIKE_SIGMA_WEIGHT_RETURNS, ge=0
    )
    valuation_pairing_max_months: float = Field(default=2.5, ge=0)
    macro_capacity_soft_fraction: float = Field(default=_DEFAULT_WORLD_GDP_SOFT_CAP_FRACTION, gt=0)
    macro_capacity_monthly_log_drift_penalty: float = Field(default=_DEFAULT_WORLD_GDP_MONTHLY_LOG_DRIFT_PENALTY, ge=0)


class PrivateEquityTrainingConfig(StrictModel):
    issuer_id: str = Field(min_length=1)
    observations_path: str
    out_model_path: str
    as_of_date: date | None = None
    priors: PrivateEquityTrainingPriors


def load_price_observations_jsonl(path: Path) -> list[PrivateEquityObservation]:
    observations: list[PrivateEquityObservation] = []
    with path.open("r", encoding="utf-8") as f:
        for line_number, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                observation_type = payload.get("type") if isinstance(payload, dict) else None
                observation: PrivateEquityObservation
                if observation_type == "price_observation":
                    observation = PriceObservation.model_validate(payload)
                elif observation_type == "valuation_observation":
                    observation = ValuationObservation.model_validate(payload)
                else:
                    raise ValueError(f"unsupported observation type {observation_type!r}")
            except Exception as error:
                raise ValueError(f"{path} line {line_number}: {error}") from error
            observations.append(observation)
    if not observations:
        raise ValueError(f"{path} contains no price observations")
    return observations


def load_training_config(path: Path) -> PrivateEquityTrainingConfig:
    return PrivateEquityTrainingConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def fit_private_equity_model(
    observations: list[PrivateEquityObservation], config: PrivateEquityTrainingConfig
) -> TrainedPrivateEquityModelArtifact:
    issuer_observations = sorted(
        [observation for observation in observations if observation.issuer_id == config.issuer_id],
        key=lambda observation: observation.observed_at,
    )
    if len(issuer_observations) != len(observations):
        issuers = sorted({observation.issuer_id for observation in observations})
        raise ValueError(f"training config issuer_id={config.issuer_id!r} but observations include issuers {issuers}")
    price_observations = [
        observation for observation in issuer_observations if isinstance(observation, PriceObservation)
    ]
    valuation_observations = [
        observation for observation in issuer_observations if isinstance(observation, ValuationObservation)
    ]
    if len(price_observations) < 2:
        raise ValueError("private-equity training needs at least two price observations")

    mark_observations = [observation for observation in price_observations if observation.kind == "ppu_mark"]
    if not mark_observations:
        raise ValueError("private-equity training needs at least one ppu_mark observation")
    current_mark = max(mark_observations, key=lambda observation: observation.observed_at)
    as_of_date = config.as_of_date or current_mark.observed_at
    if as_of_date < current_mark.observed_at:
        raise ValueError(
            f"as_of_date {as_of_date.isoformat()} cannot be before latest ppu_mark "
            f"{current_mark.observed_at.isoformat()}"
        )

    times = np.array([months_between(price_observations[0].observed_at, obs.observed_at) for obs in price_observations])
    log_prices = np.log(np.array([obs.price_usd_per_share for obs in price_observations], dtype=np.float64))
    obs_sigmas = np.array([obs.uncertainty_log_sigma for obs in price_observations], dtype=np.float64)
    empirical_monthly_mu = _weighted_slope(times, log_prices, obs_sigmas)
    empirical_monthly_sigma = _monthly_sigma(
        times, log_prices, empirical_monthly_mu, obs_sigmas, config.priors.min_monthly_log_return_sigma
    )
    observed_months = float(times[-1] - times[0])
    monthly_mu = _shrink_monthly_mu(empirical_monthly_mu, observed_months, config.priors)
    monthly_sigma = _shrink_monthly_sigma(empirical_monthly_sigma, max(len(price_observations) - 1, 1), config.priors)

    tender_observations = [observation for observation in price_observations if observation.kind == "tender_price"]
    tender_interval_median = _tender_interval_months_median(
        tender_observations, prior=config.priors.tender_interval_months_median_prior
    )
    last_tender = max((observation.observed_at for observation in tender_observations), default=None)
    scale_prior, scale_diagnostics = _estimate_scale_prior(
        price_observations, valuation_observations, current_mark=current_mark, priors=config.priors
    )

    return TrainedPrivateEquityModelArtifact(
        issuer_id=config.issuer_id,
        as_of_date=as_of_date,
        current_mark_usd=current_mark.price_usd_per_share,
        monthly_log_return_mu=monthly_mu,
        monthly_log_return_sigma=monthly_sigma,
        student_t_nu=config.priors.student_t_nu,
        tender_interval_months_median=tender_interval_median,
        tender_interval_log_sigma=config.priors.tender_interval_log_sigma,
        tender_price_log_discount_mu=config.priors.tender_price_log_discount_mu,
        tender_price_log_discount_sigma=config.priors.tender_price_log_discount_sigma,
        last_tender_observed_at=last_tender,
        scale_prior=scale_prior,
        provenance={
            "observation_count": len(issuer_observations),
            "price_observation_count": len(price_observations),
            "valuation_observation_count": len(valuation_observations),
            "tender_price_observation_count": len(tender_observations),
            "ppu_mark_observation_count": len(mark_observations),
            "source_ids": sorted({observation.source_id for observation in issuer_observations}),
            "empirical_monthly_log_return_mu": empirical_monthly_mu,
            "empirical_monthly_log_return_sigma": empirical_monthly_sigma,
            "stock_like_monthly_log_return_mu": config.priors.stock_like_monthly_log_return_mu,
            "stock_like_monthly_log_return_sigma": config.priors.stock_like_monthly_log_return_sigma,
            "scale_diagnostics": scale_diagnostics,
        },
    )


def train_from_config(config_path: Path) -> TrainedPrivateEquityModelArtifact:
    config = load_training_config(config_path)
    base_dir = config_path.parent
    observations_path = _resolve_path(config.observations_path, base_dir)
    out_model_path = _resolve_path(config.out_model_path, base_dir)
    artifact = fit_private_equity_model(load_price_observations_jsonl(observations_path), config)
    out_model_path.parent.mkdir(parents=True, exist_ok=True)
    out_model_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
    return artifact


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train a compact private-equity exogenous model.")
    parser.add_argument("--config", required=True, type=Path, help="Training YAML path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    artifact = train_from_config(args.config.resolve())
    print(f"trained private-equity model issuer={artifact.issuer_id} as_of={artifact.as_of_date.isoformat()}")
    print(f"current mark: ${artifact.current_mark_usd:,.2f}")
    return 0


def _resolve_path(path: str | Path, base_dir: Path) -> Path:
    value = Path(path)
    if value.is_absolute():
        return value
    return (base_dir / value).resolve()


def _weighted_slope(times_months: np.ndarray, log_prices: np.ndarray, obs_sigmas: np.ndarray) -> float:
    if np.any(np.diff(times_months) <= 0):
        raise ValueError("price observations must have distinct increasing observed_at dates")
    weights = 1.0 / np.square(obs_sigmas)
    design = np.column_stack([np.ones_like(times_months), times_months])
    weighted_design = design * np.sqrt(weights[:, None])
    weighted_y = log_prices * np.sqrt(weights)
    _, slope = np.linalg.lstsq(weighted_design, weighted_y, rcond=None)[0]
    return float(slope)


def _monthly_sigma(
    times_months: np.ndarray, log_prices: np.ndarray, monthly_mu: float, obs_sigmas: np.ndarray, floor: float
) -> float:
    durations = np.diff(times_months)
    returns = np.diff(log_prices)
    residuals = (returns - monthly_mu * durations) / np.sqrt(durations)
    empirical = float(np.std(residuals, ddof=1)) if len(residuals) > 1 else float(abs(residuals[0]))
    observation_noise_floor = float(np.median(obs_sigmas) / math.sqrt(max(float(np.median(durations)), 1.0)))
    return max(empirical, observation_noise_floor, floor)


def _shrink_monthly_mu(empirical_mu: float, observed_months: float, priors: PrivateEquityTrainingPriors) -> float:
    weight = priors.stock_like_monthly_log_return_mu_weight_months
    if weight == 0:
        return float(empirical_mu)
    return float(
        (empirical_mu * observed_months + priors.stock_like_monthly_log_return_mu * weight) / (observed_months + weight)
    )


def _shrink_monthly_sigma(
    empirical_sigma: float, observed_return_count: int, priors: PrivateEquityTrainingPriors
) -> float:
    weight = priors.stock_like_monthly_log_return_sigma_weight_returns
    if weight == 0:
        return float(empirical_sigma)
    variance = (empirical_sigma**2 * observed_return_count + priors.stock_like_monthly_log_return_sigma**2 * weight) / (
        observed_return_count + weight
    )
    return float(max(math.sqrt(variance), priors.min_monthly_log_return_sigma))


def _estimate_scale_prior(
    price_observations: list[PriceObservation],
    valuation_observations: list[ValuationObservation],
    *,
    current_mark: PriceObservation,
    priors: PrivateEquityTrainingPriors,
) -> tuple[TrainedPrivateEquityScalePrior, dict[str, float | int]]:
    if not valuation_observations:
        raise ValueError("private-equity training needs at least one valuation_observation for macro-scale prior")

    log_share_count_estimates: list[float] = []
    weights: list[float] = []
    max_gap = priors.valuation_pairing_max_months
    for valuation in valuation_observations:
        nearest = min(
            price_observations, key=lambda price: abs(months_between(price.observed_at, valuation.observed_at))
        )
        gap = abs(months_between(nearest.observed_at, valuation.observed_at))
        if gap > max_gap:
            continue
        log_share_count_estimates.append(math.log(valuation.valuation_usd) - math.log(nearest.price_usd_per_share))
        weights.append(1.0 / max(valuation.uncertainty_log_sigma**2 + nearest.uncertainty_log_sigma**2, 1e-9))

    if not log_share_count_estimates:
        raise ValueError(
            "private-equity training could not pair any valuation_observation with a price_observation "
            f"within {max_gap:g} months"
        )

    log_share_count = float(np.average(np.asarray(log_share_count_estimates), weights=np.asarray(weights)))
    current_market_cap_usd = current_mark.price_usd_per_share * math.exp(log_share_count)
    soft_cap_market_cap_usd = priors.macro_capacity_reference_usd * priors.macro_capacity_soft_fraction
    return (
        TrainedPrivateEquityScalePrior(
            current_market_cap_usd=current_market_cap_usd,
            soft_cap_market_cap_usd=soft_cap_market_cap_usd,
            monthly_log_drift_penalty=priors.macro_capacity_monthly_log_drift_penalty,
        ),
        {
            "valuation_pair_count": len(log_share_count_estimates),
            "current_market_cap_usd": current_market_cap_usd,
            "soft_cap_market_cap_usd": soft_cap_market_cap_usd,
            "macro_capacity_reference_usd": priors.macro_capacity_reference_usd,
        },
    )


def _tender_interval_months_median(observations: list[PriceObservation], *, prior: float) -> float:
    if len(observations) < 2:
        return float(prior)
    dates = [observation.observed_at for observation in sorted(observations, key=lambda obs: obs.observed_at)]
    intervals = np.array([months_between(start, end) for start, end in pairwise(dates)])
    intervals = intervals[intervals > 0]
    if intervals.size == 0:
        return float(prior)
    # Light shrinkage keeps sparse/ambiguous tender histories from overfitting one short gap.
    return float((np.median(intervals) * intervals.size + prior) / (intervals.size + 1))


if __name__ == "__main__":
    raise SystemExit(main())
