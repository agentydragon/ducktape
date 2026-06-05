"""Plot the kernel calibration backtest: per-series PIT histograms + JSD-to-uniform over time.

Reads results/backtest.json (written by backtest.py). Self-contained (no repo imports) so it runs in
the matplotlib uv env:
  uv run --no-project --python 3.12 --with matplotlib python augur/x/pm_reifier/plot_backtest.py
"""

from __future__ import annotations

import json
import math
import pathlib

import matplotlib
import matplotlib.pyplot as plt

matplotlib.use("Agg")

HERE = pathlib.Path(__file__).parent
BT = json.loads((HERE / "results" / "backtest.json").read_text())
SERIES = ["inflation", "sp500", "crypto:BTC", "home_value:sf_ca", "rent:sf_ca"]
BINS = 10
WINDOW = 8  # months per rolling JSD window


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


def main() -> None:
    steps = sorted(BT["per_step"], key=lambda r: r["month"])
    by_series = {s: [r["pits"][s] for r in steps if s in r["pits"]] for s in SERIES}
    pooled = [p for s in SERIES for p in by_series[s]]

    fig, axes = plt.subplots(2, 4, figsize=(18, 8))
    panels = [*SERIES, "POOLED"]
    for ax, key in zip(axes.flat, panels, strict=False):
        vals = pooled if key == "POOLED" else by_series[key]
        ax.hist(vals, bins=BINS, range=(0, 1), color="steelblue", edgecolor="white")
        ax.axhline(len(vals) / BINS, ls="--", color="crimson", lw=1.2, label="uniform (calibrated)")
        j = jsd_to_uniform(vals)
        ax.set_title(f"{key}  n={len(vals)}" + (f"  JSD={j:.3f}" if j is not None else ""), fontsize=10)
        ax.set_xlabel("PIT (weighted model-CDF at realized)")
        ax.legend(fontsize=7)

    # JSD-to-uniform over time: rolling WINDOW-month window of pooled PITs, plotted at window center.
    ax = axes.flat[6]
    months = [r["month"] for r in steps]
    xs, ys = [], []
    for i in range(len(steps) - WINDOW + 1):
        win = steps[i : i + WINDOW]
        wvals = [p for r in win for p in r["pits"].values()]
        j = jsd_to_uniform(wvals)
        if j is not None:
            xs.append(i + WINDOW // 2)
            ys.append(j)
    ax.plot(xs, ys, marker="o", color="darkorange")
    ax.set_xticks(range(0, len(months), 4))
    ax.set_xticklabels([months[i] for i in range(0, len(months), 4)], rotation=45, fontsize=7)
    ax.set_title(f"JSD-to-uniform over time ({WINDOW}-mo rolling, pooled)", fontsize=10)
    ax.set_ylabel("JSD (bits)")
    ax.grid(alpha=0.3)
    ax.set_ylim(bottom=0)

    axes.flat[7].axis("off")
    fig.suptitle(
        f"glm-4.5 kernel calibration backtest (anchor {BT['anchor']}, {BT['steps']} steps) — "
        "flat histogram = calibrated; U-shape = overconfident/thin-tailed",
        fontsize=13,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    out = HERE / "results" / "backtest.png"
    fig.savefig(out, dpi=110)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
