"""Rolling-origin, multi-horizon free-running calibration backtest (augur/x, throwaway).

The one-step teacher-forced backtest (backtest.py) tests p(next | true history) but exhausts the
leakage-free window in ~20 correlated steps (n_eff~5). This tests the kernel the way we'd actually
use it — applied autoregressively — and resolves calibration BY HORIZON:

  for each origin o (>= the model's cutoff): run M FREE-RUNNING rollouts forward H months (the kernel
  draws its own next step and conditions on it), forming an M-member ensemble at each horizon h; score
  the realized value at o+h as its rank within that ensemble. Under-prediction and thin tails should
  COMPOUND with h (the cone drifts and stays too narrow) — that is what the per-horizon PIT shows.

Reuses the shared kernel + the date-ranged data from backtest.py. Run: python3 backtest_rolling.py
"""

from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor

import kernel
from backtest import build_series, m_index, m_label
from run_spike import CODING, RESULTS, quota

MODEL = "glm-4.5"
ENDPOINT = CODING
ORIGINS = ["2024-06", "2024-09", "2024-12", "2025-03", "2025-05"]  # all >= the ~2024-06 cutoff
M = 12  # free-running rollouts per origin (the ensemble)
H = 10  # horizons (months ahead) per rollout
N_HIST = 24
N_OPTIONS = 20
TEMP = 1.0
CONCURRENCY = 8


def chain(args: tuple[str, int, list[tuple[str, dict[str, float]]]]) -> dict:
    """One free-running rollout from an origin: draw the next step, append it, repeat for H months."""
    origin, r, real_hist = args
    rng = random.Random(hash((origin, r)) & 0xFFFFFFFF)
    history = list(real_hist)
    path: dict[int, dict[str, float]] = {}
    for h in range(1, H + 1):
        next_label = m_label(m_index(origin) + h)
        options, _ = kernel.sample_step(ENDPOINT, MODEL, history, next_label, N_OPTIONS, TEMP, f"roll_{origin}_{r}_{h}")
        if len(options) < 8:  # one retry, else truncate the chain here
            options, _ = kernel.sample_step(
                ENDPOINT, MODEL, history, next_label, N_OPTIONS, TEMP, f"roll_{origin}_{r}_{h}b"
            )
        if len(options) < 8:
            break
        drawn = kernel.draw(options, rng.random())["values"]
        path[h] = drawn
        history = [*history, (next_label, drawn)][-N_HIST:]
    return {"origin": origin, "r": r, "path": path}


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    q0 = quota()
    print(f"quota before: weekly={q0.get('weekly_7d_pct')}%  model={MODEL}")
    vals = build_series()
    common = sorted(set.intersection(*(set(v) for v in vals.values())), key=m_index)
    last = m_index(common[-1])

    tasks = []
    realized: dict[str, dict[int, dict[str, float]]] = {}
    for o in ORIGINS:
        hist_months = [m for m in common if m_index(m) < m_index(o)][-N_HIST:]
        real_hist = [(m, {s: vals[s][m] for s in kernel.SERIES}) for m in hist_months]
        realized[o] = {
            h: {s: vals[s][m_label(m_index(o) + h)] for s in kernel.SERIES if m_label(m_index(o) + h) in vals[s]}
            for h in range(1, H + 1)
            if m_index(o) + h <= last
        }
        tasks += [(o, r, real_hist) for r in range(M)]
    print(f"{len(ORIGINS)} origins x {M} rollouts x {H} horizons = {len(tasks)} chains")

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        chains = list(ex.map(chain, tasks))

    # ensemble PIT per (origin, horizon, series): rank of realized within the M drawn values
    pit_by_h: dict[int, list[float]] = {h: [] for h in range(1, H + 1)}
    records = []
    by_origin: dict[str, list[dict]] = {o: [] for o in ORIGINS}
    for c in chains:
        by_origin[c["origin"]].append(c)
    for o in ORIGINS:
        for h in range(1, H + 1):
            ens = {s: [c["path"][h][s] for c in by_origin[o] if h in c["path"]] for s in kernel.SERIES}
            for s in kernel.SERIES:
                rz = realized[o].get(h, {}).get(s)
                if rz is None or len(ens[s]) < 6:
                    continue
                pit = sum(1 for v in ens[s] if v <= rz) / len(ens[s])
                pit_by_h[h].append(pit)
                records.append({"origin": o, "h": h, "series": s, "pit": pit, "m": len(ens[s])})

    def stat(ps: list[float]) -> dict:
        n = len(ps)
        return {
            "n": n,
            "mean": sum(ps) / n if n else None,
            "tail": sum(1 for u in ps if u <= 0.1 or u >= 0.9) / n if n else None,
        }

    summary = {
        "model": MODEL,
        "origins": ORIGINS,
        "M": M,
        "H": H,
        "by_horizon": {h: stat(pit_by_h[h]) for h in range(1, H + 1)},
        "records": records,
    }
    (RESULTS / "backtest_rolling.json").write_text(json.dumps(summary, indent=2) + "\n")
    q1 = quota()
    print(f"\n=== {MODEL} rolling-origin backtest: {len(records)} ensemble PITs ===")
    print(f"  {'h':>2} {'n':>4} {'meanPIT':>8} {'tail%':>6}")
    for h in range(1, H + 1):
        s = summary["by_horizon"][h]
        if s["mean"] is not None:
            print(f"  {h:>2} {s['n']:>4} {s['mean']:>8.2f} {s['tail'] * 100:>5.0f}%")
    print(f"  weekly quota {q0.get('weekly_7d_pct')}% -> {q1.get('weekly_7d_pct')}%")


if __name__ == "__main__":
    main()
