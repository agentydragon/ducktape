"""Calibration backtest of the PERCENTILE kernel on resolved history (augur/x, throwaway).

Same teacher-forced one-step setup as backtest.py (anchor glm-4.5 at its leakage-probed 2024-06 cutoff,
same window/series), but the per-step proposal is the elicited quantile function (kernel_percentile)
instead of N weighted options. PIT is read by inverting the quantile function at the realized value.
Aligns 1:1 with results/backtest.json (LLM enumeration) and results/backtest_statespace.json so all
three overlay in plot_compare.py.

Run: PYTHONPATH=. python3 augur/x/pm_reifier/backtest_percentile.py   (writes results/backtest_percentile.json)
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import kernel_percentile
from backtest import MODEL, N_HIST, T0, build_series, jsd_to_uniform, m_index, m_label
from kernel import SERIES
from run_spike import CODING, RESULTS, quota

ENDPOINT = CODING
TEMP = 1.0
CONCURRENCY = 4


def step(args: tuple[int, list[tuple[str, dict[str, float]]], dict[str, float]]) -> dict:
    """One teacher-forced step: history -> elicited quantiles -> realized PITs + tail-commit diagnostics."""
    target_idx, history, realized = args
    next_label = m_label(target_idx)
    quantiles, usage = kernel_percentile.sample_step(ENDPOINT, MODEL, history, next_label, TEMP, f"pct_{next_label}")
    pits = {}
    escapes = {}  # series -> "below_p1" / "above_p99" / "inside": did realized exceed the stated 1/99 commitment?
    for s in SERIES:
        if s in quantiles and s in realized:
            qmap = quantiles[s]
            pits[s] = kernel_percentile.pit(qmap, realized[s])
            lo, hi = qmap[min(qmap)], qmap[max(qmap)]
            escapes[s] = "below_p1" if realized[s] < lo else "above_p99" if realized[s] > hi else "inside"
    return {
        "month": next_label,
        "n_series": len(pits),
        "pits": pits,
        "escapes": escapes,
        "tokens": usage.get("total_tokens", 0),
    }


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    q0 = quota()
    print(f"quota before: weekly={q0.get('weekly_7d_pct')}%  model={MODEL} anchor={T0} (percentile kernel)")
    vals = build_series()
    common = sorted(set.intersection(*(set(v) for v in vals.values())), key=m_index)
    tasks = []
    for tgt in common:
        ti = m_index(tgt)
        if ti <= m_index(T0):
            continue
        hist_months = [m for m in common if m_index(m) < ti][-N_HIST:]
        realized = {s: vals[s][tgt] for s in SERIES if tgt in vals[s]}
        if len(hist_months) == N_HIST and realized:
            history = [(m, {s: vals[s][m] for s in SERIES}) for m in hist_months]
            tasks.append((ti, history, realized))
    print(f"window {m_label(tasks[0][0])}..{m_label(tasks[-1][0])}  ({len(tasks)} steps)")

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        steps = sorted(ex.map(step, tasks), key=lambda r: r["month"])

    by_series: dict[str, list[float]] = {s: [] for s in SERIES}
    for r in steps:
        for s, p in r["pits"].items():
            by_series[s].append(p)
    pooled = [p for ps in by_series.values() for p in ps]
    # Direct tail-commitment diagnostic: how often did the realized value blow past the stated p1/p99?
    esc = [e for r in steps for e in r["escapes"].values()]
    escape_rate = sum(1 for e in esc if e != "inside") / len(esc) if esc else None
    summary = {
        "model": MODEL,
        "kernel": "percentile",
        "anchor": T0,
        "steps": len(steps),
        "tokens": sum(r["tokens"] for r in steps),
        "jsd_pooled": jsd_to_uniform(pooled),
        "jsd_by_series": {s: jsd_to_uniform(ps) for s, ps in by_series.items()},
        "n_by_series": {s: len(ps) for s, ps in by_series.items()},
        "p1_p99_escape_rate": escape_rate,  # H0 = 0.02 if the stated 98% interval were honest
        "per_step": steps,
    }
    (RESULTS / "backtest_percentile.json").write_text(json.dumps(summary, indent=2) + "\n")
    q1 = quota()
    tail = sum(1 for u in pooled if u <= 0.1 or u >= 0.9) / len(pooled) if pooled else float("nan")
    print(f"\n=== {MODEL} percentile backtest: {len(steps)} steps, {len(pooled)} scored PITs ===")
    print(f"  mean PIT pooled: {sum(pooled) / len(pooled):.3f}  (0.5 = calibrated median)")
    print(f"  tail-escape (PIT<=.1 or >=.9): {tail:.0%}  (0.20 = calibrated)")
    print(f"  realized beyond stated p1/p99: {escape_rate:.0%}  (0.02 = honest 98% interval)")
    print(f"  JSD-to-uniform pooled: {summary['jsd_pooled']:.3f} bits")
    print(f"  tokens={summary['tokens']}  weekly quota {q0.get('weekly_7d_pct')}% -> {q1.get('weekly_7d_pct')}%")


if __name__ == "__main__":
    main()
