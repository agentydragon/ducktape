"""Compare fixed-real and bounded annual spending on identical materialized paths.

This is an executable-policy experiment, not a published-study reproduction. Market
history and instrument construction come from the existing Trinity experiment.
The tax-free portfolio uses Python-authored sales-only funding at the common
post-cashflow decision boundary.
"""

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from finance.augur.model.series import SecurityKey
from finance.augur.rust.invocation import write_prepared_input
from finance.augur.sim.backend import compile_run
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.quantiles import currency_quantiles
from finance.augur.study.trinity.replay import HORIZON_MONTHS, build_scenario, sample_replay
from finance.augur.x.bounded_spending.python_policy import BatchPolicy, Parameters, SpendingPolicy, consumption, run
from finance.augur.x.bounded_spending.stress_paths import sample


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
    allocation = scenario.target_allocation_policies[0]
    targets = {}
    for sleeve in allocation.sleeves:
        if not isinstance(sleeve.asset, SecurityKey):
            raise ValueError("bounded spending declares public-security sleeves")
        targets[allocation.source_account_ids[0], str(sleeve.asset.symbol)] = sleeve.weight
    scenario = scenario.model_copy(update={"scheduled_obligations": [], "target_allocation_policies": []})
    prepared = compile_run(
        scenario, rollout_count=rollout_count, external_series=external_series, jurisdictions={}, locations={}
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "policies.json").write_text(
        json.dumps(
            {
                "implementation": "python_policy.py:BatchPolicy + SpendingPolicy",
                "rate_bps": rate_bps,
                "consumption_component": "annual_consumption",
                "trace_rollouts": trace_rollouts,
                "review": "after scheduled cashflows and due-claim assembly",
                "funding": "overweight-first sales, then ordered due claims, then consumption; no purchases",
                "targets": [
                    {"account_id": account, "asset_id": asset, "weight": weight}
                    for (account, asset), weight in targets.items()
                ],
                "fixed_real": {"max_cut_bps": 0, "max_raise_bps": 0},
                "bounded": {"max_cut_bps": max_cut_bps, "max_raise_bps": max_raise_bps},
            },
            indent=2,
        )
    )
    input_path = output_dir / "execution-input.json"
    write_prepared_input(prepared, input_path)
    input_json = input_path.read_text()
    for name, cut, raise_ in (("fixed_real", 0, 0), ("bounded", max_cut_bps, max_raise_bps)):
        summary_path = output_dir / f"{name}.json"
        parameters = Parameters(rate_bps, cut, raise_)
        summary = run(
            input_json, SpendingPolicy(BatchPolicy(parameters, rollout_count), targets), list(range(rollout_count))
        )
        summary_path.write_text(json.dumps(summary))
        for rollout_id in trace_rollouts:
            replay = run(
                input_json,
                SpendingPolicy(BatchPolicy(parameters, rollout_count), targets),
                [rollout_id],
                capture="forensic",
            )
            summary_path.with_suffix(f".trace-{rollout_id}.json").write_text(json.dumps(replay["rollouts"][0]))
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
    requested, paid = consumption(summary)
    months = []
    for month in range(horizon_months):
        observed = [path for path, values in enumerate(requested) if month < len(values)]
        known = [path for path in observed if requested[path][month] is not None]
        months.append(
            {
                "event_month": month,
                "observed_path_count": len(observed),
                "consumption_observed_path_count": len(known),
                "consumption_requested": currency_quantiles(
                    np.asarray([requested[path][month] for path in known], dtype=np.int64), percentiles
                )
                if known
                else None,
                "consumption_paid": currency_quantiles(
                    np.asarray([paid[path][month] for path in known], dtype=np.int64), percentiles
                )
                if known
                else None,
            }
        )
    output_path.write_text(
        json.dumps(
            {
                "component": "annual_consumption",
                "currency_code": currency_code,
                "currency_quantum": currency_quantum,
                "amount_basis": "nominal currency quanta",
                "conditioning": "known component amounts on observed paths, including attempted consumption in a failure month",
                "percentiles": percentiles,
                "failed_month": [
                    row["summary"]["ending_mark_month"] if row["stop"] is not None else None
                    for row in summary["rollouts"]
                ],
                "months": months,
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--evidence-dir", type=Path)
    source.add_argument(
        "--synthetic", action="store_true", help="Three stipulated equity/CPI paths; use equity-share 1."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--equity-share", type=float, required=True)
    parser.add_argument("--rate-bps", type=int, required=True)
    parser.add_argument("--max-cut-bps", type=int, required=True)
    parser.add_argument("--max-raise-bps", type=int, required=True)
    parser.add_argument("--trace-rollout", type=int, action="append", default=[])
    args = parser.parse_args()
    if args.synthetic:
        if args.equity_share != 1:
            raise ValueError("synthetic paths declare equity only; use equity-share 1")
        paths = sample(rollout_count=3, horizon_months=HORIZON_MONTHS)
        count = 3
        provenance: dict[str, Any] = {"sampler": "three stipulated equity/CPI stress cases; not probability samples"}
    else:
        replay = sample_replay(args.evidence_dir)
        paths, count = replay.external_series, replay.window_count
        provenance = {
            "sampler": "Trinity historical overlapping monthly windows",
            "record_start": replay.record_start.isoformat(),
            "record_end": replay.record_end.isoformat(),
            "window_starts": [month.isoformat() for month in replay.window_starts],
        }
    compare(
        external_series=paths,
        rollout_count=count,
        equity_share=args.equity_share,
        rate_bps=args.rate_bps,
        max_cut_bps=args.max_cut_bps,
        max_raise_bps=args.max_raise_bps,
        output_dir=args.output_dir,
        trace_rollouts=tuple(args.trace_rollout),
    )
    (args.output_dir / "paths.json").write_text(json.dumps(provenance, indent=2))
    print(f"Saved {count} paired compact outcomes to {args.output_dir}; paths are not independent probability draws.")


if __name__ == "__main__":
    main()
