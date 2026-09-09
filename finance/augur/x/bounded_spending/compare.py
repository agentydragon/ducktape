"""Compare fixed-real and bounded annual spending on identical materialized paths.

This is an executable-policy experiment, not a published-study reproduction. Market
history and instrument construction come from the existing Trinity experiment; its
tax-free portfolio and cashflow-only, sales-only funding policy are retained.
"""

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from finance.augur.rust.invocation import invoke, write_prepared_input
from finance.augur.sim.backend import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.quantiles import currency_quantiles
from finance.augur.study.trinity.replay import build_scenario, sample_replay
from util.bazel.runfiles import get_required_path, own_repo_rlocation


def compare(
    *,
    external_series: ExternalSeriesContext,
    rollout_count: int,
    equity_share: float,
    rate_bps: int,
    max_cut_bps: int,
    max_raise_bps: int,
    output_dir: Path,
    trace_rollouts: tuple[int, ...] = (),
) -> None:
    """Compile shared paths; retain compact consumption and opt-in original-path traces."""
    if not 0 <= equity_share <= 1:
        raise ValueError("equity_share must be finite and in [0, 1]")
    if not 0 < rate_bps <= 10_000 or not 0 <= max_cut_bps <= 10_000 or not 0 <= max_raise_bps <= 10_000:
        raise ValueError("rate must be in (0, 10000] bps; cut and raise in [0, 10000] bps")
    if any(rollout < 0 or rollout >= rollout_count for rollout in trace_rollouts):
        raise ValueError("trace rollout must identify an original path in the population")
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
                "consumption_component": "annual_consumption",
                "trace_rollouts": trace_rollouts,
                "fixed_real": {"max_cut_bps": 0, "max_raise_bps": 0},
                "bounded": {"max_cut_bps": max_cut_bps, "max_raise_bps": max_raise_bps},
            },
            indent=2,
        )
    )
    input_path = output_dir / "execution-input.json"
    write_prepared_input(run, input_path)
    binary = get_required_path(own_repo_rlocation("finance/augur/x/bounded_spending/runner"))
    for name, cut, raise_ in (("fixed_real", 0, 0), ("bounded", max_cut_bps, max_raise_bps)):
        summary_path = output_dir / f"{name}.json"
        summary = invoke(
            binary=binary,
            input_path=input_path,
            output_path=summary_path,
            arguments=[str(rate_bps), str(cut), str(raise_), *map(str, trace_rollouts)],
        )
        _write_consumption_distribution(
            summary,
            output_dir / f"{name}.consumption.json",
            horizon_months=scenario.horizon_months,
            currency_code=scenario.currency.code,
            currency_quantum=str(scenario.currency.quantum),
        )


def _write_consumption_distribution(
    summary: dict[str, Any], output_path: Path, *, horizon_months: int, currency_code: str, currency_quantum: str
) -> None:
    """Describe the empirical component distribution among paths observed that month."""
    percentiles = (5.0, 50.0, 95.0)
    requested = [np.asarray(path, dtype=np.int64) for path in summary["consumption_requested"]]
    paid = [np.asarray(path, dtype=np.int64) for path in summary["consumption_paid"]]
    months = []
    for month in range(horizon_months):
        observed = [path for path, values in enumerate(requested) if month < len(values)]
        months.append(
            {
                "event_month": month,
                "observed_path_count": len(observed),
                "consumption_requested": currency_quantiles(
                    np.asarray([requested[path][month] for path in observed], dtype=np.int64), percentiles
                )
                if observed
                else None,
                "consumption_paid": currency_quantiles(
                    np.asarray([paid[path][month] for path in observed], dtype=np.int64), percentiles
                )
                if observed
                else None,
            }
        )
    output_path.write_text(
        json.dumps(
            {
                "component": summary["component"],
                "currency_code": currency_code,
                "currency_quantum": currency_quantum,
                "amount_basis": "nominal currency quanta",
                "conditioning": "only paths observed in the event month, including their failure month",
                "percentiles": percentiles,
                "failed_month": summary["product_metrics"]["failed_month"],
                "months": months,
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--equity-share", type=float, required=True)
    parser.add_argument("--rate-bps", type=int, required=True)
    parser.add_argument("--max-cut-bps", type=int, required=True)
    parser.add_argument("--max-raise-bps", type=int, required=True)
    parser.add_argument("--trace-rollout", type=int, action="append", default=[])
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
        trace_rollouts=tuple(args.trace_rollout),
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
        f"Saved {replay.window_count} paired compact outcomes to {args.output_dir}; overlapping windows are not independent draws."
    )


if __name__ == "__main__":
    main()
