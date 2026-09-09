"""Compare fixed-real and bounded annual spending on identical materialized paths.

This is an executable-policy experiment, not a published-study reproduction. Market
history and instrument construction come from the existing Trinity experiment; its
tax-free portfolio and cashflow-only, sales-only funding policy are retained.
"""

import argparse
import json
import subprocess
from pathlib import Path

from python.runfiles import runfiles

from finance.augur.sim.backend import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.study.trinity.replay import build_scenario, sample_replay


def compare(
    *,
    external_series: ExternalSeriesContext,
    rollout_count: int,
    equity_share: float,
    rate_bps: int,
    max_cut_bps: int,
    max_raise_bps: int,
    output_dir: Path,
) -> None:
    """Compile once; run two functions on those exact paths and retain full timelines."""
    if not 0 <= equity_share <= 1:
        raise ValueError("equity_share must be finite and in [0, 1]")
    if not 0 < rate_bps <= 10_000 or not 0 <= max_cut_bps <= 10_000 or not 0 <= max_raise_bps <= 10_000:
        raise ValueError("rate must be in (0, 10000] bps; cut and raise in [0, 10000] bps")
    scenario = build_scenario(equity_share=equity_share, withdrawal_rate=rate_bps / 10_000)
    # The executable function supplies consumption; leave all other financial mechanics.
    scenario = scenario.model_copy(update={"scheduled_obligations": []})
    run = compile_run(
        scenario, rollout_count=rollout_count, external_series=external_series, jurisdictions={}, locations={}
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "policies.json").write_text(
        json.dumps(
            {
                "implementation": "policy.rs:annual_spending",
                "rate_bps": rate_bps,
                "fixed_real": {"max_cut_bps": 0, "max_raise_bps": 0},
                "bounded": {"max_cut_bps": max_cut_bps, "max_raise_bps": max_raise_bps},
            },
            indent=2,
        )
    )
    input_path = output_dir / "execution-input.json"
    input_path.write_text(json.dumps(run.execution_input))
    resolver = runfiles.Create()
    if resolver is None:
        raise RuntimeError("Bazel runfiles are unavailable")
    binary = resolver.Rlocation("_main/finance/augur/x/bounded_spending/runner")
    if binary is None:
        raise RuntimeError("bounded-spending runner is absent from runfiles")
    for name, cut, raise_ in (("fixed_real", 0, 0), ("bounded", max_cut_bps, max_raise_bps)):
        subprocess.run(
            [binary, str(input_path), str(output_dir / f"{name}.json"), str(rate_bps), str(cut), str(raise_)],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--equity-share", type=float, required=True)
    parser.add_argument("--rate-bps", type=int, required=True)
    parser.add_argument("--max-cut-bps", type=int, required=True)
    parser.add_argument("--max-raise-bps", type=int, required=True)
    args = parser.parse_args()
    replay = sample_replay(args.evidence_dir)
    compare(
        external_series=replay.external_series,
        rollout_count=replay.window_count,
        equity_share=args.equity_share,
        rate_bps=args.rate_bps,
        max_cut_bps=args.max_cut_bps,
        max_raise_bps=args.max_raise_bps,
        output_dir=args.output_dir,
    )
    (args.output_dir / "paths.json").write_text(
        json.dumps(
            {
                "sampler": "Trinity historical overlapping monthly windows",
                "record_start": replay.record_start.isoformat(),
                "record_end": replay.record_end.isoformat(),
                "window_starts": [month.isoformat() for month in replay.window_starts],
            },
            indent=2,
        )
    )
    print(
        f"Saved {replay.window_count} paired timelines to {args.output_dir}; overlapping windows are not independent draws."
    )


if __name__ == "__main__":
    main()
