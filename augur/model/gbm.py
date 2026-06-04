"""Geometric Brownian scalar exogenous models."""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import BaseModel

from augur.model.sim_backend import SimBackend, current_backend


class GeometricBrownian(BaseModel):
    """Fixture GBM-sampled level process for one external series.

    `initial_value` is the month-0 level. Later months apply `exp(N(mu, sigma))` to the
    previous month's level. Path identity is supplied by `ExogenousSamplingRequest` (the
    per-stream `rollout_seeds`) rather than model config — each rollout is an independent,
    reproducibly-seeded trajectory.

    Two interchangeable implementations exist behind `sim_backend.current_backend()`: the NumPy
    reference (`_sample_levels_numpy`, a per-rollout `default_rng(seed)` loop) and the JAX path
    (`_sample_levels_jax`, a vectorized `vmap` over per-seed PRNG keys). They produce different
    realized values (different RNG algorithms) but the same invariants: per-seed reproducibility,
    per-seed independence, month-0 == `initial_value`, and the configured log-return moments.
    """

    kind: Literal["gbm"] = "gbm"
    initial_value: float
    monthly_log_return_mu: float = 0.0
    monthly_log_return_sigma: float = 0.0

    def sample_levels(self, *, rollout_seeds: tuple[int, ...], horizon_months: int) -> np.ndarray:
        if current_backend() is SimBackend.JAX:
            return self._sample_levels_jax(rollout_seeds, horizon_months)
        return self._sample_levels_numpy(rollout_seeds, horizon_months)

    def _sample_levels_numpy(self, rollout_seeds: tuple[int, ...], horizon_months: int) -> np.ndarray:
        rollout_count = len(rollout_seeds)
        levels = np.empty((rollout_count, horizon_months + 1), dtype=np.float64)
        levels[:, 0] = self.initial_value
        for rollout_index, seed in enumerate(rollout_seeds):
            rng = np.random.default_rng(seed)
            log_returns = rng.normal(
                loc=self.monthly_log_return_mu, scale=self.monthly_log_return_sigma, size=horizon_months
            )
            levels[rollout_index, 1:] = self.initial_value * np.exp(np.cumsum(log_returns))
        return levels

    def _sample_levels_jax(self, rollout_seeds: tuple[int, ...], horizon_months: int) -> np.ndarray:
        # Lazy import: keep `jax` (a heavy import) off the default NumPy-backend path.
        import jax
        from jax import random

        rollout_count = len(rollout_seeds)
        # One independent PRNG key per rollout, folded from that rollout's (arbitrary-precision,
        # already per-stream) seed in 32-bit words. This is the "vector of R independent seeded
        # states": trajectory i depends only on `rollout_seeds[i]`, never on the batch.
        seed_words = np.array(
            [[(int(seed) >> shift) & 0xFFFFFFFF for shift in (96, 64, 32, 0)] for seed in rollout_seeds],
            dtype=np.uint32,
        )

        def key_for_seed(words: jax.Array) -> jax.Array:
            key = random.PRNGKey(0)
            for word_index in range(words.shape[0]):
                key = random.fold_in(key, words[word_index])
            return key

        keys = jax.vmap(key_for_seed)(jax.numpy.asarray(seed_words))
        standard_normals = jax.vmap(lambda key: random.normal(key, (horizon_months,)))(keys)
        log_returns = self.monthly_log_return_mu + self.monthly_log_return_sigma * standard_normals
        levels = jax.numpy.concatenate(
            [
                jax.numpy.full((rollout_count, 1), self.initial_value),
                self.initial_value * jax.numpy.exp(jax.numpy.cumsum(log_returns, axis=1)),
            ],
            axis=1,
        )
        return np.asarray(levels, dtype=np.float64)
