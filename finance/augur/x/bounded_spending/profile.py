"""Profile one concrete authoring/capture workload; timings are observations, not gates."""

import argparse
import cProfile
import hashlib
import json
import os
import pstats
import resource
from pathlib import Path
from typing import Any

from finance.augur.rust.invocation import invoke
from finance.augur.x.bounded_spending.python_policy import BatchPolicy, Parameters, ScalarAdapter, run
from finance.augur.x.bounded_spending.stress_paths import prepare
from util.bazel.runfiles import get_required_path, own_repo_rlocation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollouts", type=int, required=True)
    parser.add_argument("--horizon-months", type=int, required=True)
    parser.add_argument("--native-threads", type=int, required=True)
    parser.add_argument("--authoring", choices=("scalar", "batch", "native-cli"), required=True)
    parser.add_argument("--capture", choices=("summary", "forensic"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.rollouts <= 0 or args.horizon_months <= 0 or args.native_threads <= 0:
        raise ValueError("rollouts, horizon and native threads must be positive")
    # Set before the extension or native child initializes Rayon's global pool.
    os.environ["RAYON_NUM_THREADS"] = str(args.native_threads)
    if args.authoring == "native-cli" and args.capture != "summary":
        raise ValueError("native CLI control profiles compact output only")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    input_json = json.dumps(prepare(rollout_count=args.rollouts, horizon_months=args.horizon_months).execution_input)
    input_path = args.output_dir / "input.json"
    input_path.write_text(input_json)
    parameters = Parameters(400, 1000, 500)

    def execute() -> dict[str, Any]:
        if args.authoring == "native-cli":
            return invoke(
                binary=get_required_path(own_repo_rlocation("finance/augur/x/bounded_spending/runner")),
                input_path=input_path,
                output_path=args.output_dir / "native.json",
                arguments=["400", "1000", "500"],
            )
        ids = list(range(args.rollouts))
        policy = (
            ScalarAdapter(parameters, ids) if args.authoring == "scalar" else BatchPolicy(parameters, args.rollouts)
        )
        return run(input_json, policy, ids, forensic=args.capture == "forensic")

    profiler = cProfile.Profile()
    output = profiler.runcall(execute)
    profiler.dump_stats(args.output_dir / "execution.prof")
    stats = pstats.Stats(profiler)
    profiled_seconds = sum(entry.inlinetime for entry in profiler.getstats())
    observed_months = (
        sum(len(path) for path in output["consumption_paid"])
        if args.capture == "summary"
        else sum(len(path["months"]) - 1 for path in output["rollouts"])
    )
    report = {
        "authoring": args.authoring,
        "capture": args.capture,
        "rollouts": args.rollouts,
        "horizon_months": args.horizon_months,
        "observed_path_months": observed_months,
        "profiled_seconds": profiled_seconds,
        "profiled_path_months_per_second": observed_months / profiled_seconds,
        "peak_self_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "peak_child_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        "output_sha256": hashlib.sha256(json.dumps(output, sort_keys=True).encode()).hexdigest(),
        "logical_cpu_count": os.cpu_count(),
        "rayon_num_threads": os.environ.get("RAYON_NUM_THREADS"),
        "input_sha256": hashlib.sha256(input_json.encode()).hexdigest(),
        "input_bytes": len(input_json.encode()),
        "parameters": {"rate_bps": 400, "max_cut_bps": 1000, "max_raise_bps": 500},
        "paths": "three repeated stipulated equity price paths; 100 -> 200/50/130 at month 12; CPI 1 -> 1.25",
        "financial_scope": "tax-free equity-only, zero cash band, cashflow-only funding, purchases disabled",
        "timing_scope": "cProfile-instrumented policy/session construction through decoded final output; excludes path compilation/input-file creation",
        "native_cli_scope": "includes subprocess startup and output file I/O; not isolated native evaluator time",
        "memory_scope": "separate process high-water marks, including input preparation; not additive simultaneous peak",
    }
    (args.output_dir / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, sort_keys=True))
    stats.strip_dirs().sort_stats(pstats.SortKey.CUMULATIVE).print_stats(30)


if __name__ == "__main__":
    main()
