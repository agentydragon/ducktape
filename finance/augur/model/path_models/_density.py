"""Shared density helpers used by multiple models."""

from __future__ import annotations

import math

import numpy as np


def gaussian_logpdf(*, diff: np.ndarray, inv_cov: np.ndarray, log_det: float) -> float:
    """Closed-form multivariate normal log-pdf at `diff = x - mean`, given
    pre-computed `inv_cov` and `log_det = log|cov|`. Avoids re-inverting on
    every evaluation in the model classes (var, vecm, dcc) that cache
    those at fit time."""
    n_factors = diff.shape[0]
    quad = float(diff @ inv_cov @ diff)
    return float(-0.5 * (n_factors * math.log(2 * math.pi) + log_det + quad))


def gaussian_logpdf_from_samples(*, samples: np.ndarray, observation: np.ndarray) -> float | None:
    """Fit a multivariate Gaussian to `samples` (shape (n, F)) and return
    log p(observation) under that Gaussian. Used for Monte-Carlo h-step
    predictive density when the model has no closed form. Returns None
    when the sample covariance is not positive definite."""
    if samples.ndim != 2:
        raise ValueError(f"samples must be 2-D (n, F); got shape {samples.shape}")
    n_samples, n_factors = samples.shape
    mean = samples.mean(axis=0)
    centered = samples - mean
    cov = centered.T @ centered / max(1, n_samples - 1)
    cov = (cov + cov.T) / 2 + np.eye(n_factors) * 1e-12
    sign, log_det = np.linalg.slogdet(cov)
    if sign <= 0 or not math.isfinite(log_det):
        return None
    inv_cov = np.linalg.inv(cov)
    diff = observation - mean
    quad = float(diff @ inv_cov @ diff)
    return float(-0.5 * (n_factors * math.log(2 * math.pi) + log_det + quad))
