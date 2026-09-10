"""Measure the feature-rich workload through its Python-controlled configured runner."""

import argparse
import hashlib
import json
import os
import resource
import statistics
from dataclasses import asdict, dataclass
from enum import StrEnum
from timeit import Timer

from finance.augur.benchmark.scenario import feature_rich_case
from finance.augur.sim.configured import simulate_dense_json, simulate_summaries_json
from finance.augur.sim.prepared import CompiledRun


class OutputMode(StrEnum):
    DENSE = "dense"
    COMPACT = "compact"


@dataclass(frozen=True)
class BenchmarkReport:
    output_mode: OutputMode
    rollout_count: int
    horizon_months: int
    cold_seconds: float
    warm_seconds: list[float]
    median_seconds: float
    output_bytes: int
    output_sha256: str
    peak_self_rss_kib: int
    logical_cpu_count: int | None


def benchmark(run: CompiledRun, *, output_mode: OutputMode, repeats: int) -> BenchmarkReport:
    if repeats < 1:
        raise ValueError("repeats must be positive")
    simulate = simulate_dense_json if output_mode == OutputMode.DENSE else simulate_summaries_json
    output = ""

    def run_once() -> None:
        nonlocal output
        output = ""  # Do not retain the previous population while executing its replacement.
        output = simulate(run)

    timer = Timer(run_once)
    cold = timer.timeit(number=1)
    warm = timer.repeat(repeat=repeats, number=1)
    memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    encoded = output.encode()
    return BenchmarkReport(
        output_mode=output_mode,
        rollout_count=run.rollout_count,
        horizon_months=run.scenario.horizon_months,
        cold_seconds=cold,
        warm_seconds=warm,
        median_seconds=statistics.median(warm),
        output_bytes=len(encoded),
        output_sha256=hashlib.sha256(encoded).hexdigest(),
        peak_self_rss_kib=memory,
        logical_cpu_count=os.cpu_count(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollouts", type=int, required=True)
    parser.add_argument("--horizon-months", type=int, default=60)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output-mode", type=OutputMode, choices=list(OutputMode), required=True)
    args = parser.parse_args()
    if args.rollouts < 1:
        raise ValueError("rollouts must be positive")
    case = feature_rich_case(rollout_count=args.rollouts, horizon_months=args.horizon_months)
    print(
        json.dumps(
            asdict(benchmark(case.compiled_run, output_mode=args.output_mode, repeats=args.repeats)), sort_keys=True
        )
    )


if __name__ == "__main__":
    main()
