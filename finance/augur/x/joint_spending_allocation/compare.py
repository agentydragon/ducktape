"""A paired spending-flexibility × allocation sweep on stipulated taxable paths."""

import argparse
import json
from dataclasses import asdict
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np

from finance.augur.rust.invocation import write_prepared_input
from finance.augur.sim.quantiles import currency_quantiles
from finance.augur.x.bounded_spending.python_policy import Parameters, consumption, run
from finance.augur.x.joint_spending_allocation.policy import JointPolicy
from finance.augur.x.joint_spending_allocation.scenario import prepare, sample


def measurements(output: dict[str, Any], policy: JointPolicy) -> dict[str, Any]:
    requests, paid = consumption(output)
    paths = []
    for rollout, path_requests, path_paid in zip(output["rollouts"], requests, paid, strict=True):
        id_ = rollout["rollout_id"]
        summary = rollout["summary"]
        intentions = policy.intentions.get(id_, {})
        months = []
        for month, (requested, actual) in enumerate(zip(path_requests, path_paid, strict=True)):
            intent = intentions.get(month)
            months.append(
                {
                    "month": month,
                    "intended_consumption": intent.consumption if intent is not None else None,
                    "fixed_real_anchor": intent.fixed_real_anchor if intent is not None else None,
                    "cut_from_fixed_real_anchor": max(0, intent.fixed_real_anchor - intent.consumption)
                    if intent is not None
                    else None,
                    "consumption_requested": requested,
                    "consumption_paid": actual,
                    "consumption_shortfall": max(0, intent.consumption - actual)
                    if intent is not None and actual is not None
                    else None,
                }
            )
        assets = sum(row["values"][-1] for field in ("cash", "public_holdings") for row in summary[field])
        paths.append(
            {
                "rollout_id": id_,
                "stop": rollout["stop"],
                "ending_mark_month": summary["ending_mark_month"],
                "ending_assets": assets,
                "terminal_assets": assets if rollout["stop"] is None else None,
                "tax_assessed": sum(row["total_tax"] for row in summary["tax_accruals"]),
                "tax_paid": sum(row["amount_paid"] for row in summary["tax_payments"]),
                "unpaid_claims": summary["unpaid_claims"],
                "months": months,
            }
        )
    terminal = [path["terminal_assets"] for path in paths if path["terminal_assets"] is not None]
    return {
        "paths": paths,
        "completed_paths": len(terminal),
        "terminal_assets_percentiles": currency_quantiles(np.asarray(terminal, dtype=np.int64), (0.0, 50.0, 100.0))
        if terminal
        else None,
    }


def compare(output_dir: Path) -> None:
    horizon = 60
    prepared = prepare(sample(horizon_months=horizon), rollout_count=3, horizon_months=horizon, taxable=True)
    output_dir.mkdir(parents=True, exist_ok=False)
    input_path = output_dir / "execution-input.json"
    write_prepared_input(prepared, input_path)
    input_json = input_path.read_text()
    cells = []
    for rate, (flex_name, cut, raise_), (allocation_name, step) in product(
        (400, 800), (("fixed_real", 0, 0), ("bounded", 2000, 500)), (("constant", 0), ("glide", 5))
    ):
        name = f"r{rate}-{flex_name}-{allocation_name}"
        parameters = Parameters(rate, cut, raise_)
        policy = JointPolicy(parameters, rollout_count=3, annual_step=step)
        output = run(input_json, policy, [0, 1, 2])
        output["measurements"] = measurements(output, policy)
        (output_dir / f"{name}.json").write_text(json.dumps(output))
        replay_policy = JointPolicy(parameters, rollout_count=3, annual_step=step)
        replay = run(input_json, replay_policy, [2, 0], capture="forensic")
        replay["measurements"] = measurements(replay, replay_policy)
        (output_dir / f"{name}-traces.json").write_text(json.dumps(replay))
        cells.append({"name": name, "spending": asdict(parameters), "annual_allocation_step_percent": step})
        print(
            f"{name}: completed={output['measurements']['completed_paths']}/3; paired stipulated cases, not probability"
        )
    (output_dir / "experiment.json").write_text(
        json.dumps(
            {
                "sampler": "three stipulated joint price/CPI paths; no fitted evidence window or random draws",
                "path_ids": [0, 1, 2],
                "path_source": "scenario.py:sample; exact materialized series in execution-input.json",
                "products": "test-growth and test-steady are quoted public securities with no distributions, fees or default model; neither is a bond fund",
                "taxes": "synthetic flat 20% ordinary/ST and 10% LT schedule, zero deduction/loss ordinary offset; no claim to statutory US/state/foreign coverage",
                "tax_limitations": "no personalized filing inputs, NIIT/AMT/state/foreign coverage claim, liquidation tax or horizon-end settlement; final assessed/unpaid taxes remain separate",
                "initial_situation": "USD 10000 cash and 500 units of each security at USD 100, basis USD 80/unit acquired 24 months earlier; USD 1000 nominal committed bill each year",
                "horizon_months": horizon,
                "currency_code": "USD",
                "currency_quantum": "0.01",
                "amount_basis": "nominal integer currency quanta",
                "review": "one monthly decision after scheduled cashflows and due-claim assembly; annual consumption at 0,12,...",
                "actions": "reserve due claims plus intended consumption; shared cash-band/drift trade proposal, then due claims, then consumption; no retry or tax gross-up",
                "allocation": "initial 50/50; constant or annual growth target +5 percentage points capped at 70/30; cash band USD 0/10000, purchases allowed, zero drift tolerance in-band",
                "consumption": "bounded rule on current cash plus public holdings; CPI-indexed previous amount and annual cut/raise limits; fixed_real_anchor is a separate zero-flex reference from the same opening wealth",
                "missing_amounts": "unattempted consumption request is null; actual paid is zero from the completed action prefix; post-stop months are absent; policy intention is not an engine receipt",
                "terminal_measure": "cash plus marked public holdings, before unsettled taxes and unpaid claims; completed horizon only; stopped assets have their own mark month",
                "percentiles": [0, 50, 100],
                "distribution_scope": "empirical summaries of these three cases, conditional on completion; no probabilities, independent-sampling error bars or policy ranking",
                "cells": cells,
                "trace_rollouts": [2, 0],
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    compare(parser.parse_args().output_dir)


if __name__ == "__main__":
    main()
