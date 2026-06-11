"""Structured-model calibration baseline on the SAME windows as the LLM kernel backtest (augur/x, throwaway).

The state-space model (`augur/model/state_space.py`) is a joint monthly LOG-return Gaussian: it fits
`monthly_log_return_mu` + `monthly_log_return_cov` and rolls forward as `level = exp(cumsum(returns))`.
Its one-step marginal predictive for series s is therefore `N(mu_s, sigma_s**2)` on the log-return, and the
analytic PIT of a realized next value is `Phi((log(realized/last) - mu_s) / sigma_s)`. (Block-shrinkage and
the PSD repair touch only the off-diagonal covariance — the per-series marginal used for PIT is the plain
trailing-window Gaussian.) No API, no LLM, no jax: pure local compute.

To be apples-to-apples with `backtest.py`, we score the structured model on the IDENTICAL teacher-forced
windows: each step it sees the same trailing N_HIST real levels the LLM kernel saw, and predicts the same
next month. Same scorecard (`plot_compare.py`) then overlays the two PIT distributions.

Run:  PYTHONPATH=. python3 augur/x/pm_reifier/backtest_statespace.py   (writes results/backtest_statespace.json)
Plot: uv run --no-project --python 3.12 --with matplotlib --with scipy --with numpy python augur/x/pm_reifier/plot_compare.py
"""

from __future__ import annotations

import itertools
import json
import math
import os
import pathlib
import statistics
from typing import TypedDict

from finance.augur.x.pm_reifier.evidence_series import monthly_levels_by_wire

SERIES = ["inflation", "sp500", "crypto:BTC", "home_value:sf_ca", "rent:sf_ca"]
T0 = "2024-06"  # same anchor as backtest.py (glm-4.5's leakage-probed cutoff)
N_HIST = 24  # months of history shown each step (== the LLM kernel's window)
# Under `bazel run`, __file__ is in the runfiles tree; BUILD_WORKING_DIRECTORY points back at the
# repo root so results land in the source tree. Fall back to __file__ for a plain `python3` run.
_ROOT = pathlib.Path(os.environ.get("BUILD_WORKING_DIRECTORY", pathlib.Path(__file__).parent.parent.parent.parent))
RESULTS = _ROOT / "augur" / "x" / "pm_reifier" / "results"


class StepPitSummary(TypedDict):
    month: str
    pits: dict[str, float]


def m_index(ym: str) -> int:
    y, m = map(int, ym.split("-"))
    return y * 12 + (m - 1)


def m_label(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _phi(z: float) -> float:
    """Standard-normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def gaussian_pit(history_levels: list[float], realized: float) -> float | None:
    """One-step log-return Gaussian PIT: fit N(mu, sigma) on the window's log-returns, score the realized step."""
    rets = [math.log(b / a) for a, b in itertools.pairwise(history_levels) if a > 0 and b > 0]
    if len(rets) < 3:
        return None
    mu = statistics.fmean(rets)
    sigma = statistics.stdev(rets)  # sample std (ddof=1) — the trailing-window MLE-ish scale
    if sigma <= 0:
        return None
    return _phi((math.log(realized / history_levels[-1]) - mu) / sigma)


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    vals = monthly_levels_by_wire()
    common = sorted(set.intersection(*(set(v) for v in vals.values())), key=m_index)  # months with all 5 series
    steps: list[StepPitSummary] = []
    for tgt in common:
        ti = m_index(tgt)
        if ti <= m_index(T0):
            continue
        hist_months = [m for m in common if m_index(m) < ti][-N_HIST:]
        if len(hist_months) != N_HIST:
            continue
        pits = {}
        for s in SERIES:
            if tgt in vals[s]:
                p = gaussian_pit([vals[s][m] for m in hist_months], vals[s][tgt])
                if p is not None:
                    pits[s] = p
        if pits:
            steps.append({"month": tgt, "pits": pits})

    steps.sort(key=lambda r: r["month"])
    by_series: dict[str, list[float]] = {s: [] for s in SERIES}
    for r in steps:
        for s, p in r["pits"].items():
            by_series[s].append(p)
    pooled = [p for ps in by_series.values() for p in ps]
    summary = {
        "model": "state_space (log-return Gaussian, trailing-window fit)",
        "anchor": T0,
        "steps": len(steps),
        "n_by_series": {s: len(ps) for s, ps in by_series.items()},
        "per_step": steps,
    }
    (RESULTS / "backtest_statespace.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"=== state-space baseline: {len(steps)} steps, {len(pooled)} scored PITs (anchor {T0}) ===")
    print(f"  window {steps[0]['month']}..{steps[-1]['month']}")
    print(f"  mean PIT pooled: {statistics.fmean(pooled):.3f}  (0.5 = calibrated median)")
    tail = sum(1 for u in pooled if u <= 0.1 or u >= 0.9) / len(pooled)
    print(f"  tail-escape: {tail:.0%} (0.20 = calibrated)")
    for s in SERIES:
        print(f"  {s:18} n={len(by_series[s]):3} meanPIT={statistics.fmean(by_series[s]):.3f}")


if __name__ == "__main__":
    main()
