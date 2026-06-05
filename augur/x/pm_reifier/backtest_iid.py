"""Calibration backtest of the IID-reframed enumeration kernel (augur/x, throwaway).

Same teacher-forced one-step setup / window / series as backtest.py, but the proposal is kernel_iid
(reframed "draw N i.i.d. samples from your predictive", optional thinking). Scored with kernel.pit
exactly like the original enumeration run, so results/backtest_iid*.json overlay the enumeration,
percentile, and state-space runs in plot_compare.py.

Set IID_THINKING=1 to enable model thinking (writes backtest_iid_thinking.json; else backtest_iid.json).
Run: PYTHONPATH=. python3 augur/x/pm_reifier/backtest_iid.py
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import kernel
import kernel_iid
from backtest import MODEL, N_HIST, N_OPTIONS, T0, build_series, jsd_to_uniform, m_index, m_label
from run_spike import CODING, RESULTS, quota

ENDPOINT = CODING
TEMP = 1.0
CONCURRENCY = 4
THINKING = os.environ.get("IID_THINKING") == "1"


def step(args: tuple[int, list[tuple[str, dict[str, float]]], dict[str, float]]) -> dict:
    target_idx, history, realized = args
    next_label = m_label(target_idx)
    options, usage = kernel_iid.sample_step(
        ENDPOINT, MODEL, history, next_label, N_OPTIONS, TEMP, f"iid_{next_label}", thinking=THINKING
    )
    pits = {}
    if len(options) >= 8:
        for s in kernel.SERIES:
            if s in realized:
                p = kernel.pit(options, realized[s], s)
                if p is not None:
                    pits[s] = p
    return {"month": next_label, "n_options": len(options), "pits": pits, "tokens": usage.get("total_tokens", 0)}


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    q0 = quota()
    print(f"quota before: weekly={q0.get('weekly_7d_pct')}%  model={MODEL} anchor={T0} iid thinking={THINKING}")
    vals = build_series()
    common = sorted(set.intersection(*(set(v) for v in vals.values())), key=m_index)
    tasks = []
    for tgt in common:
        ti = m_index(tgt)
        if ti <= m_index(T0):
            continue
        hist_months = [m for m in common if m_index(m) < ti][-N_HIST:]
        realized = {s: vals[s][tgt] for s in kernel.SERIES if tgt in vals[s]}
        if len(hist_months) == N_HIST and realized:
            history = [(m, {s: vals[s][m] for s in kernel.SERIES}) for m in hist_months]
            tasks.append((ti, history, realized))
    print(f"window {m_label(tasks[0][0])}..{m_label(tasks[-1][0])}  ({len(tasks)} steps)")

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        steps = sorted(ex.map(step, tasks), key=lambda r: r["month"])

    by_series: dict[str, list[float]] = {s: [] for s in kernel.SERIES}
    for r in steps:
        for s, p in r["pits"].items():
            by_series[s].append(p)
    pooled = [p for ps in by_series.values() for p in ps]
    n_opts = [r["n_options"] for r in steps]
    summary = {
        "model": MODEL,
        "kernel": "iid_thinking" if THINKING else "iid",
        "anchor": T0,
        "steps": len(steps),
        "tokens": sum(r["tokens"] for r in steps),
        "jsd_pooled": jsd_to_uniform(pooled),
        "n_by_series": {s: len(ps) for s, ps in by_series.items()},
        "mean_n_options": sum(n_opts) / len(n_opts) if n_opts else 0,
        "per_step": steps,
    }
    out = RESULTS / ("backtest_iid_thinking.json" if THINKING else "backtest_iid.json")
    out.write_text(json.dumps(summary, indent=2) + "\n")
    q1 = quota()
    tail = sum(1 for u in pooled if u <= 0.1 or u >= 0.9) / len(pooled) if pooled else float("nan")
    print(f"\n=== {MODEL} iid backtest (thinking={THINKING}): {len(steps)} steps, {len(pooled)} PITs ===")
    print(f"  mean n_options/step: {summary['mean_n_options']:.1f}")
    print(f"  mean PIT pooled: {sum(pooled) / len(pooled):.3f}  (0.5 = calibrated median)")
    print(f"  tail-escape (PIT<=.1 or >=.9): {tail:.0%}  (0.20 = calibrated)")
    print(f"  JSD-to-uniform pooled: {summary['jsd_pooled']:.3f} bits")
    print(f"  tokens={summary['tokens']}  weekly quota {q0.get('weekly_7d_pct')}% -> {q1.get('weekly_7d_pct')}%")


if __name__ == "__main__":
    main()
