"""The fuzzer's structural tier at CI budget: one fresh shape, and one compile, per case.

Wholly compilation-bound, and the compiled executables would otherwise all stay resident for
the life of the process, so the caches go between cases. `value_fuzz_test.py` is the other
tier, in its own target: there the cache is the point, and mixing the two in one process
would make each pay for the other.
"""

import jax
import pytest_bazel

from finance.augur.rust.differential import campaign
from finance.augur.rust.differential.generator import random_shape

STRUCTURAL_SEEDS = range(10)


def test_engines_agree_across_structural_draws() -> None:
    for seed in STRUCTURAL_SEEDS:
        report = campaign.run([campaign.Case(shape=random_shape(seed), value_seed=seed)])
        assert (report.compared, report.unrepresentable) == (1, 0)
        jax.clear_caches()


if __name__ == "__main__":
    pytest_bazel.main()
