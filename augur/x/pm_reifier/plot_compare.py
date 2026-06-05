"""Three-way calibration comparison on the SAME 2024-06 window, 20 one-step PITs/series:

  - LLM kernel, ENUMERATION proposal (results/backtest.json) — N weighted joint options.
  - LLM kernel, PERCENTILE proposal (results/backtest_percentile.json) — elicited quantile function.
  - structured STATE-SPACE model (results/backtest_statespace.json) — log-return Gaussian.

All three are scored through the identical scorecard: pooled/per-series PIT histograms (grouped filled
bars), mean PIT, tail-escape, JSD-to-uniform, and the serial-dependence-robust moving-block bootstrap
95% CIs (mean PIT H0=0.5, tail-escape H0=0.20). Writes results/compare.png and results/compare_stats.json.

Run: uv run --no-project --python 3.12 --with matplotlib --with numpy python augur/x/pm_reifier/plot_compare.py
"""

from __future__ import annotations

import json
import math
import pathlib
import random

import matplotlib
import matplotlib.pyplot as plt
import numpy as np

matplotlib.use("Agg")

HERE = pathlib.Path(__file__).parent
RESULTS = HERE / "results"
SERIES = ["inflation", "sp500", "crypto:BTC", "home_value:sf_ca", "rent:sf_ca"]
BINS = 10
BLOCK = 4  # months per moving-block bootstrap block (keeps adjacent-month + cross-series dependence)
REPS = 4000
TAIL_NULL = 0.2
# (label, results filename, color)
RUNS = [
    ("LLM enumeration", "backtest.json", "#1f5fbf"),
    ("LLM percentile", "backtest_percentile.json", "#8e44ad"),
    ("state-space", "backtest_statespace.json", "#e8820c"),
]
random.seed(0)


def jsd_to_uniform(vals: list[float], bins: int = BINS) -> float | None:
    if len(vals) < bins:
        return None
    counts = [0.0] * bins
    for u in vals:
        counts[min(bins - 1, int(u * bins))] += 1
    n = sum(counts)
    p = [c / n for c in counts]
    q = 1.0 / bins
    m = [(pi + q) / 2 for pi in p]
    kl = lambda a, b: sum(ai * math.log2(ai / bi) for ai, bi in zip(a, b, strict=True) if ai > 0)  # noqa: E731
    return 0.5 * kl(p, m) + 0.5 * kl([q] * bins, m)


def tail_rate(vals: list[float]) -> float:
    return sum(1 for u in vals if u <= 0.1 or u >= 0.9) / len(vals)


def block_bootstrap_ci(month_pits: list[list[float]], statfn, reps: int = REPS) -> tuple[float, float]:
    nblocks = math.ceil(len(month_pits) / BLOCK)
    out = []
    for _ in range(reps):
        sampled: list[list[float]] = []
        for _ in range(nblocks):
            start = random.randint(0, len(month_pits) - BLOCK)
            sampled += month_pits[start : start + BLOCK]
        pooled = [p for mm in sampled[: len(month_pits)] for p in mm]
        out.append(statfn(pooled))
    out.sort()
    return out[int(0.025 * reps)], out[int(0.975 * reps)]


def score(filename: str) -> dict:
    bt = json.loads((RESULTS / filename).read_text())
    steps = sorted(bt["per_step"], key=lambda r: r["month"])
    by_series = {s: [r["pits"][s] for r in steps if s in r["pits"]] for s in SERIES}
    pooled = [p for s in SERIES for p in by_series[s]]
    month_pits = [list(r["pits"].values()) for r in steps if r["pits"]]
    mean_lo, mean_hi = block_bootstrap_ci(month_pits, lambda v: sum(v) / len(v))
    tail_lo, tail_hi = block_bootstrap_ci(month_pits, tail_rate)
    mean_sig = not (mean_lo <= 0.5 <= mean_hi)
    tail_sig = not (tail_lo <= TAIL_NULL <= tail_hi)
    return {
        "pooled": pooled,
        "by_series": by_series,
        "mean_pit": float(np.mean(pooled)),
        "mean_ci": [mean_lo, mean_hi],
        "mean_excludes_half": mean_sig,
        "tail_rate": tail_rate(pooled),
        "tail_ci": [tail_lo, tail_hi],
        "tail_excludes_null": tail_sig,
        "jsd": jsd_to_uniform(pooled),
        "p1_p99_escape_rate": bt.get("p1_p99_escape_rate"),
        "verdict": "MISCALIBRATED" if (mean_sig or tail_sig) else "not significant",
    }


def main() -> None:
    scored = [(label, score(fn), color) for label, fn, color in RUNS]
    edges = np.linspace(0, 1, BINS + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    n = len(scored)
    w = 0.8 / (BINS * n)  # n filled columns sit side by side within each PIT decile

    fig, axes = plt.subplots(2, 4, figsize=(20, 9))

    def grouped(ax, getvals, expected: float, *, legend: bool = False) -> None:
        for i, (label, run, color) in enumerate(scored):
            counts = np.histogram(getvals(run), bins=edges)[0]
            offset = (i - (n - 1) / 2) * w
            ax.bar(centers + offset, counts, width=w, color=color, label=label if legend else None)
        ax.axhline(expected, ls="--", color="crimson", lw=1, label="uniform (calibrated)" if legend else None)
        ax.set_xlim(0, 1)
        ax.set_xlabel("PIT")

    for ax, s in [(axes[0, 0], SERIES[0]), (axes[0, 1], SERIES[1]), (axes[0, 2], SERIES[2]), (axes[0, 3], SERIES[3])]:
        grouped(ax, lambda run, s=s: run["by_series"][s], 20 / BINS)
        ax.set_title(s, fontsize=10)
    grouped(axes[1, 0], lambda run: run["by_series"]["rent:sf_ca"], 20 / BINS)
    axes[1, 0].set_title("rent:sf_ca", fontsize=10)
    grouped(axes[1, 1], lambda run: run["pooled"], 100 / BINS, legend=True)
    axes[1, 1].set_title("POOLED (n=100 each)", fontsize=10)
    axes[1, 1].legend(fontsize=7)

    # stats table panel
    sx = axes[1, 2]
    sx.axis("off")
    lines = ["calibration scorecard (block-bootstrap 95% CI)\n"]
    for label, run, _color in scored:
        m_lo, m_hi = run["mean_ci"]
        t_lo, t_hi = run["tail_ci"]
        bias = "under" if run["mean_pit"] > 0.5 else "over"
        p1p99 = (
            f"  p1/p99 esc {run['p1_p99_escape_rate']:.0%} (H0 2%)\n" if run["p1_p99_escape_rate"] is not None else ""
        )
        lines.append(
            f"{label}  → {run['verdict']}\n"
            f"  mean PIT {run['mean_pit']:.2f} [{m_lo:.2f},{m_hi:.2f}]"
            f" {'EX' if run['mean_excludes_half'] else 'in'}({bias})\n"
            f"  tail-esc {run['tail_rate']:.2f} [{t_lo:.2f},{t_hi:.2f}]"
            f" {'EX' if run['tail_excludes_null'] else 'in'}\n"
            f"{p1p99}  JSD {run['jsd']:.3f} bits\n"
        )
    for i, (_label, _run, color) in enumerate(scored):
        sx.text(0.0, 0.98 - i * 0.32, lines[i + 1], va="top", ha="left", family="monospace", fontsize=8.5, color=color)

    nx = axes[1, 3]
    nx.axis("off")
    nx.text(
        0.0,
        0.98,
        "Reading the columns:\n"
        "EX = the 95% CI EXCLUDES the\n"
        "  calibrated null (significant miss).\n"
        "mean PIT > .5 = under-predicts;\n"
        "  < .5 = over-predicts.\n"
        "tail-esc > .20 = thin tails\n"
        "  (overconfident); < .20 = too wide.\n\n"
        "Naming the percentiles (p1..p99)\n"
        "removes the enumeration kernel's\n"
        "location bias AND thin tails; its\n"
        "stated p1/p99 are honest (~2%).",
        va="top",
        ha="left",
        family="monospace",
        fontsize=8.5,
    )

    fig.suptitle(
        "Calibration: LLM enumeration vs LLM percentile vs state-space — same 2024-06 anchor, 20 one-step PITs/series",
        fontsize=13,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    out = RESULTS / "compare.png"
    fig.savefig(out, dpi=110)

    stats_out = {label: {k: v for k, v in run.items() if k not in ("pooled", "by_series")} for label, run, _ in scored}
    (RESULTS / "compare_stats.json").write_text(json.dumps(stats_out, indent=2) + "\n")
    print(f"wrote {out}")
    for label, run, _ in scored:
        print(
            f"  {label:18} mean PIT {run['mean_pit']:.2f} {run['mean_ci']}  "
            f"tail {run['tail_rate']:.2f} {run['tail_ci']}  JSD {run['jsd']:.3f}  → {run['verdict']}"
        )


if __name__ == "__main__":
    main()
