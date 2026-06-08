"""Side-by-side calibration: LLM kernel (glm-4.5) vs the structured state-space model, SAME windows.

Reads results/backtest.json (LLM) and results/backtest_statespace.json (structured baseline) — both
teacher-forced one-step over the identical 2024-06-anchored window — and scores both through the same
scorecard: pooled PIT histograms, mean PIT, tail-escape, JSD-to-uniform, and the serial-dependence-robust
moving-block bootstrap 95% CIs (mean PIT H0=0.5, tail-escape H0=0.20). Writes results/compare.png and
results/compare_stats.json. This is the apples-to-apples "is the LLM better or worse than the mechanistic
model" verdict the README's next-step list asked for.

Run: uv run --no-project --python 3.12 --with matplotlib --with scipy --with numpy python augur/x/pm_reifier/plot_compare.py
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


def score(path: pathlib.Path) -> dict:
    bt = json.loads(path.read_text())
    steps = sorted(bt["per_step"], key=lambda r: r["month"])
    by_series = {s: [r["pits"][s] for r in steps if s in r["pits"]] for s in SERIES}
    pooled = [p for s in SERIES for p in by_series[s]]
    month_pits = [list(r["pits"].values()) for r in steps if r["pits"]]
    monthly_mean = [float(np.mean(m)) for m in month_pits]
    rho1 = float(np.corrcoef(monthly_mean[:-1], monthly_mean[1:])[0, 1]) if len(monthly_mean) > 2 else 0.0
    mean_lo, mean_hi = block_bootstrap_ci(month_pits, lambda v: sum(v) / len(v))
    tail_lo, tail_hi = block_bootstrap_ci(month_pits, tail_rate)
    mean_sig = not (mean_lo <= 0.5 <= mean_hi)
    tail_sig = not (tail_lo <= TAIL_NULL <= tail_hi)
    return {
        "model": bt["model"],
        "pooled": pooled,
        "by_series": by_series,
        "mean_pit": float(np.mean(pooled)),
        "mean_ci": [mean_lo, mean_hi],
        "mean_excludes_half": mean_sig,
        "tail_rate": tail_rate(pooled),
        "tail_ci": [tail_lo, tail_hi],
        "tail_excludes_null": tail_sig,
        "jsd": jsd_to_uniform(pooled),
        "rho1": rho1,
        "n_eff": (len(month_pits) * (1 - rho1) / (1 + rho1)) if rho1 > -1 else len(month_pits),
        "verdict": "MISCALIBRATED" if (mean_sig or tail_sig) else "not significant",
    }


def main() -> None:
    llm = score(RESULTS / "backtest.json")
    ss = score(RESULTS / "backtest_statespace.json")
    runs = [("LLM kernel (glm-4.5)", llm, "#1f5fbf"), ("state-space (log-ret Gaussian)", ss, "#e8820c")]
    fig, axes = plt.subplots(2, 4, figsize=(19, 8.5))
    edges = np.linspace(0, 1, BINS + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    w = 0.5 / BINS  # half a bin: two filled columns sit side by side within each PIT decile

    def grouped_bars(ax, vals_a: list[float], vals_b: list[float], expected: float, *, legend: bool = False) -> None:
        ca = np.histogram(vals_a, bins=edges)[0]
        cb = np.histogram(vals_b, bins=edges)[0]
        ax.bar(centers - w / 2, ca, width=w, color=runs[0][2], label=runs[0][0] if legend else None)
        ax.bar(centers + w / 2, cb, width=w, color=runs[1][2], label=runs[1][0] if legend else None)
        ax.axhline(expected, ls="--", color="crimson", lw=1, label="uniform (calibrated)" if legend else None)
        ax.set_xlim(0, 1)
        ax.set_xlabel("PIT")

    panels = [(axes[0, 0], SERIES[0]), (axes[0, 1], SERIES[1]), (axes[0, 2], SERIES[2]), (axes[0, 3], SERIES[3])]
    for ax, s in panels:
        grouped_bars(ax, llm["by_series"][s], ss["by_series"][s], 20 / BINS)
        ax.set_title(s, fontsize=10)
    grouped_bars(axes[1, 0], llm["by_series"]["rent:sf_ca"], ss["by_series"]["rent:sf_ca"], 20 / BINS)
    axes[1, 0].set_title("rent:sf_ca", fontsize=10)
    grouped_bars(axes[1, 1], llm["pooled"], ss["pooled"], 100 / BINS, legend=True)
    axes[1, 1].set_title("POOLED (n=100 each)", fontsize=10)
    axes[1, 1].legend(fontsize=7)

    for ax, (label, run, color) in zip([axes[1, 2], axes[1, 3]], runs, strict=True):
        ax.axis("off")
        m_lo, m_hi = run["mean_ci"]
        t_lo, t_hi = run["tail_ci"]
        txt = (
            f"{label}\n"
            f"  → {run['verdict']}\n\n"
            f"mean PIT  {run['mean_pit']:.2f}  [{m_lo:.2f}, {m_hi:.2f}]\n"
            f"  H0=0.50 {'EXCLUDED' if run['mean_excludes_half'] else 'included'}"
            f" ({'under' if run['mean_pit'] > 0.5 else 'over'}-predicts)\n\n"
            f"tail-esc  {run['tail_rate']:.2f}  [{t_lo:.2f}, {t_hi:.2f}]\n"
            f"  H0=0.20 {'EXCLUDED → thin-tailed' if run['tail_excludes_null'] else 'included'}\n\n"
            f"JSD-to-uniform  {run['jsd']:.3f} bits\n"
            f"autocorr rho1={run['rho1']:+.2f}  n_eff≈{run['n_eff']:.0f}"
        )
        ax.text(0.0, 0.98, txt, va="top", ha="left", family="monospace", fontsize=9.5, color=color)

    fig.suptitle(
        "Calibration: LLM kernel vs structured state-space model — same 2024-06 anchor, 20 one-step PITs/series",
        fontsize=13,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    out = RESULTS / "compare.png"
    fig.savefig(out, dpi=110)

    stats_out = {
        name: {k: v for k, v in run.items() if k not in ("pooled", "by_series")}
        for name, run, _ in [("llm", llm, None), ("state_space", ss, None)]
    }
    (RESULTS / "compare_stats.json").write_text(json.dumps(stats_out, indent=2) + "\n")
    print(f"wrote {out}")
    for name, run, _ in runs:
        print(
            f"  {name:32} mean PIT {run['mean_pit']:.2f} {run['mean_ci']}  "
            f"tail {run['tail_rate']:.2f} {run['tail_ci']}  JSD {run['jsd']:.3f}  → {run['verdict']}"
        )


if __name__ == "__main__":
    main()
