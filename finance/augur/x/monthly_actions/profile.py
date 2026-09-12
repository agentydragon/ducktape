"""Profile the actual action example's capture and JSON transfer; observations, not gates."""

import argparse
import cProfile
import hashlib
import json
import os
import pstats
import resource
from pathlib import Path

from finance.augur.x.monthly_actions.run import execute, situation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollouts", type=int, required=True)
    parser.add_argument("--horizon-months", type=int, required=True)
    parser.add_argument("--native-threads", type=int, required=True)
    parser.add_argument("--capture", choices=("summary", "dense", "forensic"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.native_threads <= 0:
        raise ValueError("native threads must be positive")
    os.environ["RAYON_NUM_THREADS"] = str(args.native_threads)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    output_path = args.output_dir / "outcomes.json"
    case = situation(args.rollouts, args.horizon_months)
    ids = range(args.rollouts)
    profiler = cProfile.Profile()
    output = profiler.runcall(execute, case, output_path, ids, args.capture)
    profiler.dump_stats(args.output_dir / "execution.prof")
    # Record high-water marks before verification/hash construction adds allocations.
    self_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    seconds = sum(entry.inlinetime for entry in profiler.getstats())
    observed_months = sum(rollout.summary.ending_book.month for rollout in output.rollouts)
    compact_hash = hashlib.sha256()
    for rollout in output.rollouts:
        compact_hash.update(rollout.model_dump_json(exclude={"trace"}).encode())
    report = {
        "rollouts": args.rollouts,
        "horizon_months": args.horizon_months,
        "observed_path_months": observed_months,
        "capture": args.capture,
        "profiled_seconds": seconds,
        "profiled_path_months_per_second": observed_months / seconds,
        "peak_self_rss_kib": self_rss,
        "output_bytes": output_path.stat().st_size,
        "compact_sha256": compact_hash.hexdigest(),
        "logical_cpu_count": os.cpu_count(),
        "rayon_num_threads": args.native_threads,
        "paths": "alternating fixed $100/$50 quotes; repeated paths, not probability samples",
        "financial_scope": "two-share liquidation, one $150 bill, synthetic 10% LTCG tax; poor-price paths stop month 0; surviving paths pay tax month 12; subsequent decisions are empty",
        "timing_scope": "cProfile-instrumented Python-owned action session; includes per-path world composition, monthly Python policy/binding/native work, terminal JSON decoding and output file writing; excludes price-path preparation and file creation, not isolated native evaluation",
        "memory_scope": "one process high-water mark includes path preparation, composed worlds, Python policy/observations/results and retained native state; no child executor, not isolated Rust heap",
    }
    (args.output_dir / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, sort_keys=True))
    pstats.Stats(profiler).strip_dirs().sort_stats(pstats.SortKey.CUMULATIVE).print_stats(25)


if __name__ == "__main__":
    main()
