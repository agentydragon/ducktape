"""The fuzzer's value tier at CI budget: many draws over a few fixed shapes.

A value draw moves only what the compiled program takes as a traced input, so every case of
one shape reuses that shape's XLA compile and the target's cost is dominated by simulation
rather than compilation. `structural_fuzz_test.py` is the other tier, in its own target so
the two compile concurrently and neither process holds the other's executables.

`soak_test.py` runs both tiers over wider seed ranges, off the default CI path.
"""

import pytest_bazel

from finance.augur.rust.differential import campaign
from finance.augur.rust.differential.generator import VALUE_TIER_SHAPES

VALUE_SEEDS = range(80)


def test_engines_agree_across_value_draws() -> None:
    report = campaign.run(
        campaign.Trial(shape=shape, value_seed=seed) for shape in VALUE_TIER_SHAPES for seed in VALUE_SEEDS
    )
    # Nothing unrepresentable: a shape the Rust fixture will not take runs cases that prove
    # nothing, and the count would still look like coverage.
    assert (report.compared, report.unrepresentable) == (len(VALUE_TIER_SHAPES) * len(VALUE_SEEDS), 0)


if __name__ == "__main__":
    pytest_bazel.main()
