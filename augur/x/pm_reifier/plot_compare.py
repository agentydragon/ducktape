"""Small-multiples calibration comparison: one mini PIT histogram per kernel, same 2024-06 window.

Each panel is one model's POOLED PIT histogram (n=100) in its own color, with the uniform-calibrated
reference line and the block-bootstrap 95% CI for mean PIT (H0=0.5) and tail-escape (H0=0.20). Grouped
side-by-side columns get too noisy at this many models, so we use small multiples instead. The joint
kernels are scored on their SAMPLES (samples = Q); the percentile kernel is a marginal-only diagnostic.

Writes results/compare.png and results/compare_stats.json.
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
BLOCK = 4  # moving-block bootstrap block length (months)
REPS = 4000
TAIL_NULL = 0.2
# (label, results filename, pits-key in per_step, color)
RUNS = [
    ("enumeration", "backtest.json", "pits", "#1f5fbf"),
    ("iid reframe", "backtest_iid.json", "pits", "#17a2b8"),
    ("joint (gridded)", "backtest_joint.json", "pits_samp", "#b07fd0"),
    ("joint (sharp) — deployable", "backtest_joint_sharp.json", "pits_samp", "#6a1b9a"),
    ("percentile (marginal diag)", "backtest_percentile.json", "pits", "#2e8b57"),
    ("state-space baseline", "backtest_statespace.json", "pits", "#e8820c"),
]
random.seed(0)


def tail_rate(vals: list[float]) -> float:
    return sum(1 for u in vals if u <= 0.1 or u >= 0.9) / len(vals)


def block_bootstrap_ci(month_pits: list[list[float]], statfn) -> tuple[float, float]:
    nblocks = math.ceil(len(month_pits) / BLOCK)
    out = []
    for _ in range(REPS):
        sampled: list[list[float]] = []
        for _ in range(nblocks):
            start = random.randint(0, len(month_pits) - BLOCK)
            sampled += month_pits[start : start + BLOCK]
        pooled = [p for mm in sampled[: len(month_pits)] for p in mm]
        out.append(statfn(pooled))
    out.sort()
    return out[int(0.025 * REPS)], out[int(0.975 * REPS)]


def score(filename: str, pits_key: str) -> dict | None:
    path = RESULTS / filename
    if not path.is_file():
        return None
    steps = sorted(json.loads(path.read_text())["per_step"], key=lambda r: r["month"])
    month_pits = [list(r[pits_key].values()) for r in steps if r.get(pits_key)]
    pooled = [p for mm in month_pits for p in mm]
    mean_lo, mean_hi = block_bootstrap_ci(month_pits, lambda v: sum(v) / len(v))
    tail_lo, tail_hi = block_bootstrap_ci(month_pits, tail_rate)
    return {
        "pooled": pooled,
        "mean_pit": float(np.mean(pooled)),
        "mean_ci": [mean_lo, mean_hi],
        "mean_excludes_half": not (mean_lo <= 0.5 <= mean_hi),
        "tail_rate": tail_rate(pooled),
        "tail_ci": [tail_lo, tail_hi],
        "tail_excludes_null": not (tail_lo <= TAIL_NULL <= tail_hi),
    }


def main() -> None:
    scored = [(label, score(fn, key), color) for label, fn, key, color in RUNS]
    scored = [(label, s, color) for label, s, color in scored if s is not None]
    n = len(scored)
    ncols = 3
    nrows = math.ceil(n / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(5.2 * ncols, 3.5 * nrows), squeeze=False)

    for ax, (label, run, color) in zip(axes.flat, scored, strict=False):
        vals = run["pooled"]
        ax.hist(vals, bins=BINS, range=(0, 1), color=color, edgecolor="white", linewidth=0.6)
        ax.axhline(len(vals) / BINS, ls="--", color="crimson", lw=1.2)  # uniform = calibrated
        # shade the two tail deciles (escape region)
        ax.axvspan(0, 0.1, color="grey", alpha=0.08)
        ax.axvspan(0.9, 1.0, color="grey", alpha=0.08)
        m_lo, m_hi = run["mean_ci"]
        t_lo, t_hi = run["tail_ci"]
        bias = "under" if run["mean_pit"] > 0.5 else "over"
        ax.set_title(
            f"{label}\n"
            f"mean PIT {run['mean_pit']:.2f} [{m_lo:.2f},{m_hi:.2f}] {'✗' if run['mean_excludes_half'] else '✓'}{bias}   "
            f"tail {run['tail_rate']:.0%} [{t_lo:.0%},{t_hi:.0%}] {'✗' if run['tail_excludes_null'] else '✓'}",
            fontsize=9,
        )
        ax.set_xlim(0, 1)
        ax.set_xlabel("PIT (realized in predictive CDF)", fontsize=8)
        ax.tick_params(labelsize=8)

    for ax in axes.flat[n:]:
        ax.axis("off")

    fig.suptitle(
        "LLM-as-Q calibration — pooled PIT per kernel (glm-4.5, 2024-06 anchor, 100 PITs each)\n"
        "flat = calibrated · ✓ = 95% CI includes the calibrated null · grey = tail-escape deciles · "
        "tail<20% over-wide, >20% thin",
        fontsize=11,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    out = RESULTS / "compare.png"
    fig.savefig(out, dpi=120)

    stats_out = {label: {k: v for k, v in run.items() if k != "pooled"} for label, run, _ in scored}
    (RESULTS / "compare_stats.json").write_text(json.dumps(stats_out, indent=2) + "\n")
    print(f"wrote {out}")
    for label, run, _ in scored:
        print(f"  {label:32} mean PIT {run['mean_pit']:.2f}  tail {run['tail_rate']:.0%}")


if __name__ == "__main__":
    main()
