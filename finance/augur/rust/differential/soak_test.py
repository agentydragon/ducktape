"""The differential fuzzer run wide, off the default CI path.

Same generator, same oracle and the same seed ranges as the two CI tiers, extended: a soak
failure at a seed inside CI's range reproduces there directly, and one past it reproduces by
widening the range in `value_fuzz_test.py` or `structural_fuzz_test.py`.

    bbr test //finance/augur/rust/differential:soak_test

Kept off CI because its cost is compilation rather than simulation — the structural tier pays
one XLA compile per case, and JAX holds every compiled executable for the life of the
process. Both tiers therefore drop the caches between batches: within a batch the cache is
the whole point, but carrying one batch's executables into the next only grows the resident
set.

A batch is one campaign, so a divergence inside it stops the batches after it. That is the
right trade at this scale: the batch still reports every distinct channel it saw, and
spending the remaining hours collecting more copies of a finding already in hand is not what
the runner is for.
"""

from itertools import batched

import jax
import pytest_bazel

from finance.augur.rust.differential import campaign
from finance.augur.rust.differential.generator import VALUE_TIER_SHAPES, random_shape

VALUE_SEEDS = range(600)
STRUCTURAL_SEEDS = range(60)
SHAPES_PER_BATCH = 10


def test_engines_agree_across_many_value_draws() -> None:
    for shape in VALUE_TIER_SHAPES:
        campaign.run(campaign.Trial(shape=shape, value_seed=seed) for seed in VALUE_SEEDS)
        jax.clear_caches()


def test_engines_agree_across_many_structural_draws() -> None:
    for seeds in batched(STRUCTURAL_SEEDS, SHAPES_PER_BATCH, strict=False):
        campaign.run(campaign.Trial(shape=random_shape(seed), value_seed=seed) for seed in seeds)
        jax.clear_caches()


if __name__ == "__main__":
    pytest_bazel.main()
