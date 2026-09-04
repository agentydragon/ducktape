"""The fuzzer's structural tier at CI budget: one fresh shape, and one compile, per case.

Wholly compilation-bound, where `value_fuzz_test.py` is bound by its cases. Separate targets
so the two compile concurrently and neither process is left holding the other tier's
executables, which JAX keeps for the life of a process.

One campaign over every shape rather than one per shape: a campaign reports every distinct
divergence it sees, and a per-shape loop would stop at the first shape that found one and
leave the rest unrun.
"""

import pytest_bazel

from finance.augur.rust.differential import campaign
from finance.augur.rust.differential.generator import random_shape

STRUCTURAL_SEEDS = range(10)


def test_engines_agree_across_structural_draws() -> None:
    report = campaign.run(campaign.Trial(shape=random_shape(seed), value_seed=seed) for seed in STRUCTURAL_SEEDS)
    assert (report.compared, report.unrepresentable) == (len(STRUCTURAL_SEEDS), 0)


if __name__ == "__main__":
    pytest_bazel.main()
