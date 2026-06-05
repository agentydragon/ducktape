"""Calibration backtest of the joint (percentiles + samples) kernel (augur/x, throwaway).

Same teacher-forced window/series as backtest.py. Each step the model emits BOTH per-series percentiles
and N joint samples (kernel_joint). We score the realized value two ways and compare:

  - pit_pctl: inverse quantile function of the stated percentiles (kernel_percentile.pit).
  - pit_samp: empirical CDF of the joint sample cloud (kernel.pit).

Plus a consistency check: the sample inter-decile width vs the stated p10..p90 width per series. If the
samples hug the center despite wide stated percentiles, the spread ratio << 1 → the model states wide
but samples narrow (use percentiles for marginals, samples only for the copula). If ~1, the percentile
commitment successfully widened the joint samples.

Run: PYTHONPATH=. python3 augur/x/pm_reifier/backtest_joint.py   (writes results/backtest_joint.json)
"""

from __future__ import annotations

import json
import statistics
from concurrent.futures import ThreadPoolExecutor

import kernel
import kernel_joint
import kernel_percentile
from backtest import MODEL, N_HIST, N_OPTIONS, T0, build_series, jsd_to_uniform, m_index, m_label
from run_spike import CODING, RESULTS, quota

ENDPOINT = CODING
TEMP = 1.0
CONCURRENCY = 4


def _interdecile(values: list[float]) -> float:
    """p90 - p10 of a small sample (statistics.quantiles n=10 gives the 9 inner deciles)."""
    if len(values) < 10:
        return float("nan")
    deciles = statistics.quantiles(values, n=10)  # deciles[0]=p10 .. deciles[8]=p90
    return deciles[8] - deciles[0]


def step(args: tuple[int, list[tuple[str, dict[str, float]]], dict[str, float]]) -> dict:
    target_idx, history, realized = args
    next_label = m_label(target_idx)
    percentiles, samples, usage = kernel_joint.sample_step(
        ENDPOINT, MODEL, history, next_label, N_OPTIONS, TEMP, f"joint_{next_label}"
    )
    pit_pctl, pit_samp, spread_ratio = {}, {}, {}
    for s in kernel.SERIES:
        if s not in realized:
            continue
        if s in percentiles:
            pit_pctl[s] = kernel_percentile.pit(percentiles[s], realized[s])
            stated_idr = percentiles[s][0.9] - percentiles[s][0.1]  # stated p10..p90 width
            if samples and stated_idr > 0:
                samp_idr = _interdecile([o["values"][s] for o in samples])
                spread_ratio[s] = samp_idr / stated_idr
        if len(samples) >= 8:
            pit_samp[s] = kernel.pit(samples, realized[s], s)
    return {
        "month": next_label,
        "n_samples": len(samples),
        "n_percentiles": len(percentiles),
        "pits_pctl": pit_pctl,
        "pits_samp": pit_samp,
        "spread_ratio": spread_ratio,
        "tokens": usage.get("total_tokens", 0),
    }


def _summarize(steps: list[dict], key: str) -> dict:
    by_series: dict[str, list[float]] = {s: [] for s in kernel.SERIES}
    for r in steps:
        for s, p in r[key].items():
            by_series[s].append(p)
    pooled = [p for ps in by_series.values() for p in ps]
    tail = sum(1 for u in pooled if u <= 0.1 or u >= 0.9) / len(pooled) if pooled else float("nan")
    return {
        "mean_pit": sum(pooled) / len(pooled) if pooled else float("nan"),
        "tail_escape": tail,
        "jsd": jsd_to_uniform(pooled),
        "n": len(pooled),
    }


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    q0 = quota()
    print(f"quota before: weekly={q0.get('weekly_7d_pct')}%  model={MODEL} anchor={T0} (joint kernel)")
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

    ratios = [v for r in steps for v in r["spread_ratio"].values()]
    summary = {
        "model": MODEL,
        "kernel": "joint",
        "anchor": T0,
        "steps": len(steps),
        "tokens": sum(r["tokens"] for r in steps),
        "from_percentiles": _summarize(steps, "pits_pctl"),
        "from_samples": _summarize(steps, "pits_samp"),
        "mean_spread_ratio": sum(ratios) / len(ratios) if ratios else float("nan"),
        "per_step": steps,
    }
    (RESULTS / "backtest_joint.json").write_text(json.dumps(summary, indent=2) + "\n")
    q1 = quota()
    p, sm = summary["from_percentiles"], summary["from_samples"]
    print(f"\n=== {MODEL} joint backtest: {len(steps)} steps ===")
    print(f"  from PERCENTILES: mean PIT {p['mean_pit']:.3f}  tail-escape {p['tail_escape']:.0%}  JSD {p['jsd']:.3f}")
    print(
        f"  from SAMPLES:     mean PIT {sm['mean_pit']:.3f}  tail-escape {sm['tail_escape']:.0%}  JSD {sm['jsd']:.3f}"
    )
    print(
        f"  sample/stated inter-decile width ratio: {summary['mean_spread_ratio']:.2f}  (1.0 = samples as wide as stated)"
    )
    print(f"  tokens={summary['tokens']}  weekly quota {q0.get('weekly_7d_pct')}% -> {q1.get('weekly_7d_pct')}%")


if __name__ == "__main__":
    main()
