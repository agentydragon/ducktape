"""Plot the rolling-origin multi-horizon calibration backtest (reads results/backtest_rolling.json).

Calibration vs horizon: mean PIT and tail-escape as a function of months-ahead (do under-prediction
and thin tails compound as the free-running cone advances?), plus per-horizon PIT histograms. Bands
are origin-block bootstrap 95% CIs. Self-contained; run in the uv env:
  uv run --no-project --python 3.12 --with matplotlib --with numpy python augur/x/pm_reifier/plot_backtest_rolling.py
"""

from __future__ import annotations

import json
import pathlib
import random

import matplotlib
import matplotlib.pyplot as plt

matplotlib.use("Agg")
random.seed(0)

HERE = pathlib.Path(__file__).parent
BT = json.loads((HERE / "results" / "backtest_rolling.json").read_text())
RECS = BT["records"]
ORIGINS = BT["origins"]
H = BT["H"]


def boot_ci(recs: list[dict], statfn, reps: int = 3000) -> tuple[float, float, float]:
    """Origin-block bootstrap: resample whole origins (keeps within-origin dependence)."""
    by_o = {o: [r for r in recs if r["origin"] == o] for o in ORIGINS}
    vals = []
    for _ in range(reps):
        pool = [r for _ in ORIGINS for r in by_o[random.choice(ORIGINS)]]
        if pool:
            vals.append(statfn([r["pit"] for r in pool]))
    vals.sort()
    point = statfn([r["pit"] for r in recs]) if recs else float("nan")
    return point, vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals))]


def mean(ps):
    return sum(ps) / len(ps)


def tail(ps):
    return sum(1 for u in ps if u <= 0.1 or u >= 0.9) / len(ps)


def main() -> None:
    horizons = list(range(1, H + 1))
    rec_h = {h: [r for r in RECS if r["h"] == h] for h in horizons}

    fig, axes = plt.subplots(2, 3, figsize=(17, 9))

    for ax, statfn, null, title in [
        (axes[0, 0], mean, 0.5, "mean PIT vs horizon  (>0.5 = under-prediction)"),
        (axes[0, 1], tail, 0.2, "tail-escape vs horizon  (>0.2 = thin-tailed)"),
    ]:
        pts, los, his = [], [], []
        for h in horizons:
            p, lo, hi = boot_ci(rec_h[h], statfn)
            pts.append(p)
            los.append(lo)
            his.append(hi)
        ax.plot(horizons, pts, marker="o", color="navy")
        ax.fill_between(horizons, los, his, alpha=0.2, color="navy", label="95% origin-block CI")
        ax.axhline(null, ls="--", color="crimson", lw=1.2, label=f"calibrated = {null}")
        ax.set_xlabel("horizon (months ahead)")
        ax.set_title(title, fontsize=11)
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)

    # n-per-horizon
    axes[0, 2].bar(horizons, [len(rec_h[h]) for h in horizons], color="gray")
    axes[0, 2].set_title("ensemble PITs scored per horizon", fontsize=11)
    axes[0, 2].set_xlabel("horizon (months ahead)")

    # per-horizon PIT histograms at a few horizons
    for ax, h in zip(axes[1], [1, H // 2, H], strict=True):
        ps = [r["pit"] for r in rec_h[h]]
        ax.hist(ps, bins=10, range=(0, 1), color="steelblue", edgecolor="white")
        ax.axhline(len(ps) / 10, ls="--", color="crimson", lw=1.2)
        ax.set_title(f"PIT histogram, horizon {h}  (n={len(ps)})", fontsize=10)
        ax.set_xlabel("PIT (rank in free-running ensemble)")

    fig.suptitle(
        f"glm-4.5 rolling-origin calibration ({len(ORIGINS)} origins x {BT['M']} rollouts, free-running) — "
        "does miscalibration compound with horizon?",
        fontsize=13,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    out = HERE / "results" / "backtest_rolling.png"
    fig.savefig(out, dpi=110)
    print(f"wrote {out}")
    print("h  meanPIT  tail%   n")
    for h in horizons:
        ps = [r["pit"] for r in rec_h[h]]
        if ps:
            print(f"{h:>2}  {mean(ps):.2f}    {tail(ps) * 100:>4.0f}%  {len(ps)}")


if __name__ == "__main__":
    main()
