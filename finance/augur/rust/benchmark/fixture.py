"""The feature-rich case, written out as the integer document the Rust simulator reads.

The scenario itself is not Rust's and lives in `//finance/augur/benchmark:scenario`. What is
Rust's is the encoding, and only the standalone benchmark binary needs it on disk: the
in-process bindings hand the same document across without a file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance.augur.benchmark.scenario import feature_rich_case
from finance.augur.rust.differential.fixture import fixture_for


def write_fixture(path: Path, *, rollout_count: int, horizon_months: int) -> None:
    with path.open("w") as file:
        json.dump(
            fixture_for(feature_rich_case(rollout_count=rollout_count, horizon_months=horizon_months)),
            file,
            separators=(",", ":"),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rollouts", type=int, default=2_000)
    parser.add_argument("--horizon-months", type=int, default=60)
    args = parser.parse_args()
    write_fixture(args.output, rollout_count=args.rollouts, horizon_months=args.horizon_months)


if __name__ == "__main__":
    main()
