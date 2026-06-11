"""Plot + statistically test the kernel calibration backtest.

Reads results/backtest.json (from backtest.py). Per-series PIT histograms, JSD-to-uniform over time,
and a stats panel with formal calibration tests:
  - KS and chi-square goodness-of-fit of the pooled PIT against Uniform[0,1] (p-values; assume i.i.d.).
  - n_eff from the monthly-PIT lag-1 autocorrelation (the i.i.d. p-values are anti-conservative; the
    block-bootstrap intervals below are the serial-dependence-robust ones).
  - Moving-block bootstrap (block = 4 months, preserving within-month cross-series and adjacent-month
    correlation) 95% CIs for mean PIT (H0 = 0.5, calibrated median) and tail-escape rate
    (H0 = 0.20, calibrated tail mass) — the robust "is it miscalibrated?" verdict.

Self-contained; run in the uv env:
  uv run --no-project --python 3.12 --with matplotlib --with scipy python augur/x/pm_reifier/plot_backtest.py
"""

from __future__ import annotations

import json
import math
import pathlib
import random

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

matplotlib.use("Agg")

HERE = pathlib.Path(__file__).parent
BT = json.loads((HERE / "results" / "backtest.json").read_text())
SERIES = ["inflation", "sp500", "crypto:BTC", "home_value:sf_ca", "rent:sf_ca"]
BINS = 10
WINDOW = 8
BLOCK = 4  # months per bootstrap block
REPS = 4000
TAIL_NULL = 0.2  # calibrated mass in the two end deciles (PIT ≤ .1 or ≥ .9)
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
    """Moving-block bootstrap CI: resample whole months in length-BLOCK blocks (keeps serial + cross-series dependence)."""
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


def main() -> None:
    steps = sorted(BT["per_step"], key=lambda r: r["month"])
    by_series = {s: [r["pits"][s] for r in steps if s in r["pits"]] for s in SERIES}
    pooled = [p for s in SERIES for p in by_series[s]]
    month_pits = [list(r["pits"].values()) for r in steps if r["pits"]]

    # --- formal tests ---
    ks_d, ks_p = stats.kstest(pooled, "uniform")
    obs = np.histogram(pooled, bins=BINS, range=(0, 1))[0]
    chi2, chi2_p = stats.chisquare(obs, [len(pooled) / BINS] * BINS)
    monthly_mean = [float(np.mean(m)) for m in month_pits]
    rho1 = float(np.corrcoef(monthly_mean[:-1], monthly_mean[1:])[0, 1]) if len(monthly_mean) > 2 else 0.0
    n_eff = len(month_pits) * (1 - rho1) / (1 + rho1) if rho1 > -1 else len(month_pits)
    mean_lo, mean_hi = block_bootstrap_ci(month_pits, lambda v: sum(v) / len(v))
    tail_lo, tail_hi = block_bootstrap_ci(month_pits, tail_rate)
    mean_sig = not (mean_lo <= 0.5 <= mean_hi)
    tail_sig = not (tail_lo <= TAIL_NULL <= tail_hi)
    verdict = "MISCALIBRATED" if (mean_sig or tail_sig) else "not significant"

    fig, axes = plt.subplots(2, 4, figsize=(18, 8))
    for ax, key in zip(axes.flat, [*SERIES, "POOLED"], strict=False):
        vals = pooled if key == "POOLED" else by_series[key]
        ax.hist(vals, bins=BINS, range=(0, 1), color="steelblue", edgecolor="white")
        ax.axhline(len(vals) / BINS, ls="--", color="crimson", lw=1.2, label="uniform (calibrated)")
        j = jsd_to_uniform(vals)
        ax.set_title(f"{key}  n={len(vals)}" + (f"  JSD={j:.3f}" if j is not None else ""), fontsize=10)
        ax.set_xlabel("PIT (weighted model-CDF at realized)")
        ax.legend(fontsize=7)

    ax = axes.flat[6]
    months = [r["month"] for r in steps]
    xs, ys = [], []
    for i in range(len(steps) - WINDOW + 1):
        wvals = [p for r in steps[i : i + WINDOW] for p in r["pits"].values()]
        j = jsd_to_uniform(wvals)
        if j is not None:
            xs.append(i + WINDOW // 2)
            ys.append(j)
    ax.plot(xs, ys, marker="o", color="darkorange")
    ax.set_xticks(range(0, len(months), 4))
    ax.set_xticklabels([months[i] for i in range(0, len(months), 4)], rotation=45, fontsize=7)
    ax.set_title(f"JSD-to-uniform over time ({WINDOW}-mo rolling)", fontsize=10)
    ax.set_ylabel("JSD (bits)")
    ax.grid(alpha=0.3)
    ax.set_ylim(bottom=0)

    sx = axes.flat[7]
    sx.axis("off")
    txt = (
        f"Is it miscalibrated?  →  {verdict}\n\n"
        f"Uniformity (i.i.d. null):\n"
        f"  KS    D={ks_d:.3f}  p={ks_p:.3g}\n"
        f"  chi2  x2={chi2:.1f}  p={chi2_p:.3g}\n"
        f"  (n={len(pooled)};  n_eff≈{n_eff:.0f} after\n"
        f"   autocorr rho1={rho1:+.2f} — i.i.d. p's\n"
        f"   are anti-conservative)\n\n"
        f"Block-bootstrap 95% CI (robust):\n"
        f"  mean PIT  {np.mean(pooled):.2f}  [{mean_lo:.2f}, {mean_hi:.2f}]\n"
        f"    H0=0.50 {'EXCLUDED → under-predicts' if mean_sig else 'included'}\n"
        f"  tail-esc  {tail_rate(pooled):.2f}  [{tail_lo:.2f}, {tail_hi:.2f}]\n"
        f"    H0=0.20 {'EXCLUDED → thin-tailed' if tail_sig else 'included'}"
    )
    sx.text(0.0, 0.98, txt, va="top", ha="left", family="monospace", fontsize=9.5)

    fig.suptitle(
        f"glm-4.5 kernel calibration backtest (anchor {BT['anchor']}, {BT['steps']} steps) — {verdict}", fontsize=13
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    out = HERE / "results" / "backtest.png"
    fig.savefig(out, dpi=110)
    stats_out = {
        "ks_d": ks_d,
        "ks_p": ks_p,
        "chi2": chi2,
        "chi2_p": chi2_p,
        "rho1": rho1,
        "n_eff": n_eff,
        "mean_pit": float(np.mean(pooled)),
        "mean_ci": [mean_lo, mean_hi],
        "mean_excludes_half": mean_sig,
        "tail_rate": tail_rate(pooled),
        "tail_ci": [tail_lo, tail_hi],
        "tail_excludes_null": tail_sig,
        "verdict": verdict,
    }
    (HERE / "results" / "backtest_stats.json").write_text(json.dumps(stats_out, indent=2) + "\n")
    print(f"wrote {out}\n{json.dumps(stats_out, indent=2)}")


if __name__ == "__main__":
    main()
