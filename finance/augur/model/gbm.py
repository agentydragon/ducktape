"""Geometric Brownian scalar exogenous models."""

from __future__ import annotations

from typing import Literal

import jax
import jax.numpy as jnp
import numpy as np
from jax import random
from pydantic import BaseModel


class GeometricBrownian(BaseModel):
    """Fixture GBM-sampled level process for one external series.

    `initial_value` is the month-0 level. Later months apply `exp(N(mu, sigma))` to the
    previous month's level. Path identity is supplied by `ExogenousSamplingRequest` (the
    per-stream `rollout_seeds`) rather than model config — each rollout is an independent,
    reproducibly-seeded trajectory.

    The implementation is a vectorized JAX `vmap` over per-seed PRNG keys. It returns host NumPy
    arrays at the model boundary so downstream compiler/materialization code keeps a simple table
    interface.
    """

    kind: Literal["gbm"] = "gbm"
    initial_value: float
    monthly_log_return_mu: float = 0.0
    monthly_log_return_sigma: float = 0.0

    def sample_levels(self, *, rollout_seeds: tuple[int, ...], horizon_months: int) -> np.ndarray:
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

        keys = jax.vmap(key_for_seed)(jnp.asarray(seed_words))
        standard_normals = jax.vmap(lambda key: random.normal(key, (horizon_months,)))(keys)
        log_returns = self.monthly_log_return_mu + self.monthly_log_return_sigma * standard_normals
        levels = jnp.concatenate(
            [
                jnp.full((rollout_count, 1), self.initial_value),
                self.initial_value * jnp.exp(jnp.cumsum(log_returns, axis=1)),
            ],
            axis=1,
        )
        return np.asarray(levels, dtype=np.float64)
