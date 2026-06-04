"""Backend-parametrized tests for the GBM sampler.

Each test runs against both the NumPy reference and the JAX implementation (via `use_backend`) and
asserts the same invariants. The two backends produce different *realized* values (different RNG
algorithms), so these assert properties — reproducibility, per-seed independence, deterministic
anchors, and the configured moments — rather than specific samples.
"""

from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel

from augur.model.gbm import GeometricBrownian
from augur.model.sim_backend import SimBackend

# The `backend` fixture (parametrized over both sim backends) lives in conftest.py.


def test_shape_and_month_zero_is_initial_value(backend: SimBackend) -> None:
    gbm = GeometricBrownian(initial_value=200.0, monthly_log_return_mu=0.01, monthly_log_return_sigma=0.02)
    levels = gbm.sample_levels(rollout_seeds=(11, 12, 13), horizon_months=4)
    assert levels.shape == (3, 5)
    np.testing.assert_array_equal(levels[:, 0], 200.0)


def test_deterministic_when_sigma_zero(backend: SimBackend) -> None:
    # With no volatility both backends collapse to the analytic path initial * exp(cumsum(mu)).
    gbm = GeometricBrownian(initial_value=100.0, monthly_log_return_mu=0.01, monthly_log_return_sigma=0.0)
    levels = gbm.sample_levels(rollout_seeds=(1, 2, 3), horizon_months=3)
    expected = 100.0 * np.exp(np.cumsum(np.full(3, 0.01)))
    for row in range(3):
        np.testing.assert_allclose(levels[row, 1:], expected, rtol=1e-4)


def test_seed_reproducible(backend: SimBackend) -> None:
    gbm = GeometricBrownian(initial_value=50.0, monthly_log_return_mu=0.0, monthly_log_return_sigma=0.1)
    first = gbm.sample_levels(rollout_seeds=(7, 8, 9), horizon_months=6)
    second = gbm.sample_levels(rollout_seeds=(7, 8, 9), horizon_months=6)
    np.testing.assert_array_equal(first, second)


def test_each_trajectory_is_independent_of_the_batch(backend: SimBackend) -> None:
    # The whole point of per-seed seeding: seed 8's trajectory must be identical whether sampled
    # alongside other seeds or alone. (This is what a single batch-seeded draw would break.)
    gbm = GeometricBrownian(initial_value=50.0, monthly_log_return_mu=0.0, monthly_log_return_sigma=0.1)
    in_batch = gbm.sample_levels(rollout_seeds=(7, 8, 9), horizon_months=6)
    alone = gbm.sample_levels(rollout_seeds=(8,), horizon_months=6)
    np.testing.assert_array_equal(in_batch[1], alone[0])


def test_distinct_seeds_give_distinct_paths(backend: SimBackend) -> None:
    gbm = GeometricBrownian(initial_value=50.0, monthly_log_return_mu=0.0, monthly_log_return_sigma=0.1)
    levels = gbm.sample_levels(rollout_seeds=(7, 8), horizon_months=6)
    assert not np.allclose(levels[0, 1:], levels[1, 1:])


def test_log_return_moments(backend: SimBackend) -> None:
    mu, sigma = 0.003, 0.05
    gbm = GeometricBrownian(initial_value=100.0, monthly_log_return_mu=mu, monthly_log_return_sigma=sigma)
    levels = gbm.sample_levels(rollout_seeds=tuple(range(4000)), horizon_months=12)
    log_returns = np.diff(np.log(levels), axis=1)
    assert log_returns.mean() == pytest.approx(mu, abs=2e-3)
    assert log_returns.std() == pytest.approx(sigma, abs=2e-3)


if __name__ == "__main__":
    pytest_bazel.main()
