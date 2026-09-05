"""Generate a canonical fixture and run the optimized Rust benchmark."""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from python.runfiles import runfiles

from finance.augur.rust.benchmark.fixture import write_fixture


def _binary() -> Path:
    resolver = runfiles.Create()
    if resolver is None:
        raise RuntimeError("Bazel runfiles are unavailable")
    path = resolver.Rlocation("_main/finance/augur/rust/simulator_bench")
    if path is None:
        raise RuntimeError("simulator_bench is absent from runfiles")
    return Path(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rollouts", type=int, default=None)
    parser.add_argument("--horizon-months", type=int, default=60)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--output-mode",
        choices=("dense", "compact"),
        default="dense",
        help="dense retains monthly state and compatibility events; compact retains terminal summaries",
    )
    args = parser.parse_args()
    rollout_count = args.rollouts if args.rollouts is not None else (500 if args.output_mode == "dense" else 20_000)

    with tempfile.TemporaryDirectory() as directory:
        fixture = Path(directory) / "fixture.json"
        write_fixture(fixture, rollout_count=rollout_count, horizon_months=args.horizon_months)
        completed = subprocess.run(
            [_binary(), fixture, str(args.repeats), args.output_mode], check=True, capture_output=True, text=True
        )
        report: dict[str, Any] = json.loads(completed.stdout)
        report.update(
            {
                "fixture_bytes": fixture.stat().st_size,
                "logical_cpu_count": os.cpu_count(),
                "rayon_num_threads": os.environ.get("RAYON_NUM_THREADS"),
                "peak_child_rss_kb": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
            }
        )
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
