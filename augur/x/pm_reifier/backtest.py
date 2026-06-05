"""Calibration backtest of the LLM kernel on resolved history (augur/x, throwaway).

Anchor at a known-cutoff model's cutoff (glm-4.5 self-reports ~2024-06; leakage-probed: it does not
know the 2024-2026 OpenAI rounds or end-2025 BTC). Teacher-force a one-step walk over the resolved
window, feeding the model REAL history each step (so steps are independent and run in parallel), and
score the realized next value as a weighted PIT within the kernel's N options. Outputs per-series PIT
histograms (flat = calibrated; U = overconfident/thin-tailed) and JSD-to-uniform over time.

Run: python3 augur/x/pm_reifier/backtest.py   (writes results/backtest.json)
Plot: uv run --no-project --python 3.12 --with matplotlib python augur/x/pm_reifier/plot_backtest.py
"""

from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor

import kernel
from fetch_real_history import FRED, NORMALIZE, YAHOO, fred_monthly, yahoo_monthly
from run_spike import CODING, RESULTS, quota

MODEL = "glm-4.5"  # self-reported cutoff ~2024-06 (leakage-probed clean for 2024-2026)
ENDPOINT = CODING
T0 = "2024-06"  # anchor = the model's cutoff; everything after is leakage-free ground truth
N_HIST = 24  # months of history shown each step
N_OPTIONS = 24  # weighted joint options requested per step
TEMP = 1.0
CONCURRENCY = 4
PIT_BINS = 10


def m_index(ym: str) -> int:
    y, m = map(int, ym.split("-"))
    return y * 12 + (m - 1)


def m_label(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def build_series() -> dict[str, dict[str, float]]:
    """wire -> {YYYY-MM: value}; index series rebased to 100 at T0, sp500/BTC kept as levels."""
    raw: dict[str, dict[str, float]] = {}
    for wire, sym in YAHOO.items():
        raw[wire] = dict(yahoo_monthly(sym))
    for wire, sid in FRED.items():
        raw[wire] = dict(fred_monthly(sid))
    out: dict[str, dict[str, float]] = {}
    for wire, series in raw.items():
        if wire in NORMALIZE:
            base = series[T0]  # must exist; raises otherwise (T0 predates all our series)
            out[wire] = {m: v / base * 100.0 for m, v in series.items()}
        else:
            out[wire] = dict(series)
    return out


def step(args: tuple[int, list[tuple[str, dict[str, float]]], dict[str, float]]) -> dict:
    """One teacher-forced step: history -> kernel options -> realized PITs. args = (target_idx, history, realized)."""
    target_idx, history, realized = args
    next_label = m_label(target_idx)
    options, usage = kernel.sample_step(ENDPOINT, MODEL, history, next_label, N_OPTIONS, TEMP, f"bt_{next_label}")
    pits = {}
    if len(options) >= 8:
        for s in kernel.SERIES:
            if s in realized:
                p = kernel.pit(options, realized[s], s)
                if p is not None:
                    pits[s] = p
    return {"month": next_label, "n_options": len(options), "pits": pits, "tokens": usage.get("total_tokens", 0)}


def jsd_to_uniform(pit_values: list[float], bins: int = PIT_BINS) -> float | None:
    """Jensen-Shannon divergence (bits) between the PIT histogram and Uniform[0,1]; 0 = calibrated."""
    if len(pit_values) < bins:
        return None
    counts = [0.0] * bins
    for u in pit_values:
        counts[min(bins - 1, int(u * bins))] += 1
    n = sum(counts)
    p = [c / n for c in counts]
    q = 1.0 / bins
    m = [(pi + q) / 2 for pi in p]

    def _kl(a: list[float], b: list[float]) -> float:
        return sum(ai * math.log2(ai / bi) for ai, bi in zip(a, b, strict=True) if ai > 0)

    return 0.5 * _kl(p, m) + 0.5 * _kl([q] * bins, m)


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    q0 = quota()
    print(f"quota before: weekly={q0.get('weekly_7d_pct')}%  model={MODEL} anchor={T0}")
    vals = build_series()
    common = sorted(set.intersection(*(set(v) for v in vals.values())), key=m_index)  # months with all 5 series
    # walk targets: each month strictly after T0 that has >=1 realized series and >=N_HIST common history before it
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
    summary = {
        "model": MODEL,
        "anchor": T0,
        "steps": len(steps),
        "tokens": sum(r["tokens"] for r in steps),
        "jsd_pooled": jsd_to_uniform(pooled),
        "jsd_by_series": {s: jsd_to_uniform(ps) for s, ps in by_series.items()},
        "n_by_series": {s: len(ps) for s, ps in by_series.items()},
        "per_step": steps,
    }
    (RESULTS / "backtest.json").write_text(json.dumps(summary, indent=2) + "\n")
    q1 = quota()
    print(f"\n=== {MODEL} backtest: {len(steps)} steps, {len(pooled)} scored PITs ===")
    print(f"  JSD-to-uniform pooled: {summary['jsd_pooled']:.3f} bits  (0 = calibrated)")
    for s in kernel.SERIES:
        j = summary["jsd_by_series"][s]
        print(
            f"  {s:18} n={len(by_series[s]):3} JSD={j:.3f}"
            if j is not None
            else f"  {s:18} n={len(by_series[s]):3} JSD=NA"
        )
    print(f"  tokens={summary['tokens']}  weekly quota {q0.get('weekly_7d_pct')}% -> {q1.get('weekly_7d_pct')}%")


if __name__ == "__main__":
    main()
