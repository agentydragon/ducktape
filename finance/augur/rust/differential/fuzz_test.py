"""Randomized differential fuzzing of the two engines, at CI budget.

Two tiers, because JAX bakes the plan structure into the compiled program. The value tier
holds a handful of shapes fixed and moves only what the compiled program takes as traced
inputs, so many cases share one XLA compile. The structural tier draws a new shape per case
and pays a compile for each, so it runs few.

The counts here are what fits a `large` target. `soak_test.py` runs the same campaigns over
wider seed ranges, off the default CI path.
"""

import pytest_bazel

from finance.augur.rust.differential import campaign
from finance.augur.rust.differential.generator import VALUE_TIER_SHAPES, random_shape

VALUE_SEEDS = range(200)
STRUCTURAL_SEEDS = range(10)


def test_engines_agree_across_value_draws() -> None:
    report = campaign.run(
        campaign.Case(shape=shape, value_seed=seed) for shape in VALUE_TIER_SHAPES for seed in VALUE_SEEDS
    )
    # Nothing unrepresentable: a shape the legacy surface will not take runs cases that prove
    # nothing, and the count would still look like coverage.
    assert (report.compared, report.unrepresentable) == (len(VALUE_TIER_SHAPES) * len(VALUE_SEEDS), 0)


def test_engines_agree_across_structural_draws() -> None:
    report = campaign.run(campaign.Case(shape=random_shape(seed), value_seed=seed) for seed in STRUCTURAL_SEEDS)
    assert report.shapes == len(STRUCTURAL_SEEDS)


if __name__ == "__main__":
    pytest_bazel.main()
