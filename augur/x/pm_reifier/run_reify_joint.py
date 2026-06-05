"""Forward reify path using the sharp joint kernel (augur/x, throwaway).

Wires the deployable kernel (kernel_joint, sharp = density-weighted i.i.d. joint samples) into the
actual reify operation: roll a cloud of forward trajectories step-by-step from real recent history,
then max-ent reweight that cloud to illustrative prediction-market prices and report whether the
effective sample size survives (ESS) — the question the whole spike exists to answer, now with the
kernel we settled on.

One trajectory = a chain of single-month draws: at each step ask the kernel for N joint samples of the
next cross-section (sharp mode), draw ONE (samples are equally weighted), append to a rolling N_HIST
window, advance. R independent chains → the base-measure cloud Q. Markets are evaluated on the realized
horizon-month levels; prices are ILLUSTRATIVE (not live).

Run: PYTHONPATH=. python3 augur/x/pm_reifier/run_reify_joint.py   (writes results/reify_joint.json)
"""

from __future__ import annotations

import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor, wait

import kernel_joint
from backtest import MODEL, N_HIST, build_series, m_index, m_label
from kernel import SERIES
from run_spike import CODING, RESULTS, quota, reweight

ENDPOINT = CODING
HORIZON = 12  # months rolled forward per trajectory
ROLLOUTS = 20  # independent chains → the cloud
N_OPTIONS = 24  # joint samples requested per step (we draw one)
TEMP = 1.0
CONCURRENCY = 10  # run most chains at once so none starve at the tail
WALL_BUDGET_S = 1500  # evaluate whatever finished within this; a slow straggler can't waste the run

# Illustrative markets on the horizon-month level vs the "now" anchor: (series, +pct threshold, crowd price).
MARKETS = [
    ("sp500", 0.08, 0.55),
    ("sp500", 0.20, 0.25),
    ("crypto:BTC", 0.50, 0.40),
    ("crypto:BTC", 1.50, 0.12),
    ("inflation", 0.03, 0.60),
    ("home_value:sf_ca", 0.05, 0.45),
    ("rent:sf_ca", 0.04, 0.55),
]


def rollout(args: tuple[int, str, list[tuple[str, dict[str, float]]]]) -> dict:
    """One forward chain: returns {"path": {series: [HORIZON levels]}, "tokens": int}."""
    idx, now, seed_history = args
    rng = random.Random(1000 + idx)
    history = list(seed_history)
    now_idx = m_index(now)
    path: dict[str, list[float]] = {s: [] for s in SERIES}
    tokens = 0
    for h in range(1, HORIZON + 1):
        label = m_label(now_idx + h)
        _percentiles, samples, usage = kernel_joint.sample_step(
            ENDPOINT, MODEL, history, label, N_OPTIONS, TEMP, f"reify_r{idx:02d}_m{h:02d}", sharp=True
        )
        tokens += usage.get("total_tokens", 0)
        if not samples:
            break
        draw = rng.choice(samples)["values"]
        for s in SERIES:
            path[s].append(draw[s])
        history = [*history[1:], (label, draw)]  # slide the N_HIST window forward
    return {"path": path, "tokens": tokens}


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    q0 = quota()
    print(f"quota before: weekly={q0.get('weekly_7d_pct')}%  model={MODEL}  sharp-joint reify")
    vals = build_series()
    common = sorted(set.intersection(*(set(v) for v in vals.values())), key=m_index)
    now = common[-1]
    seed_months = common[-N_HIST:]
    seed_history = [(m, {s: vals[s][m] for s in SERIES}) for m in seed_months]
    anchor = {s: vals[s][now] for s in SERIES}
    print(f"anchor (now={now}): " + "  ".join(f"{s}={anchor[s]:g}" for s in SERIES))
    print(f"rolling {ROLLOUTS} chains x {HORIZON} months (concurrency {CONCURRENCY})")

    tasks = [(i, now, seed_history) for i in range(ROLLOUTS)]
    ex = ThreadPoolExecutor(max_workers=CONCURRENCY)
    futures = [ex.submit(rollout, t) for t in tasks]
    done, not_done = wait(futures, timeout=WALL_BUDGET_S)
    results = [f.result() for f in done if not f.exception()]
    ex.shutdown(wait=False, cancel_futures=True)
    if not_done:
        print(f"  ({len(not_done)} chains unfinished at the wall budget; evaluating the {len(results)} that completed)")

    # Keep only full-length trajectories; evaluate markets on the final horizon-month level.
    paths = [r["path"] for r in results if all(len(r["path"][s]) == HORIZON for s in SERIES)]
    n = len(paths)
    g = [[int(p[s][-1] >= anchor[s] * (1 + pct)) for s, pct, _ in MARKETS] for p in paths]
    targets = [price for _, _, price in MARKETS]
    raw = [sum(row[m] for row in g) / n for m in range(len(MARKETS))] if n else []
    _w, post, ess = reweight(g, targets) if n else ([], [], 0.0)

    market_rows = [
        {
            "market": f"{s}>=+{pct:.0%}@m{HORIZON}",
            "price": price,
            "raw": round(raw[m], 3),
            "reweighted": round(post[m], 3),
        }
        for m, (s, pct, price) in enumerate(MARKETS)
    ]
    final_levels = {s: sorted(round(p[s][-1], 2) for p in paths) for s in SERIES}  # horizon-month spread per series
    summary = {
        "model": MODEL,
        "kernel": "joint_sharp",
        "now": now,
        "horizon_months": HORIZON,
        "rollouts": ROLLOUTS,
        "valid": n,
        "ess": ess,
        "ess_frac": ess / n if n else 0.0,
        "tokens": sum(r["tokens"] for r in results),
        "markets": market_rows,
        "final_levels": final_levels,
    }
    (RESULTS / "reify_joint.json").write_text(json.dumps(summary, indent=2) + "\n")
    q1 = quota()
    print(
        f"\n=== sharp-joint forward reify: {n}/{ROLLOUTS} full trajectories, ESS {ess:.1f} ({summary['ess_frac']:.0%}) ==="
    )
    print(f"  {'market':28} price   raw  ->  reweighted")
    for r in market_rows:
        print(f"  {r['market']:28} {r['price']:.2f}  {r['raw']:.2f}  ->  {r['reweighted']:.2f}")
    print(f"  tokens={summary['tokens']}  weekly quota {q0.get('weekly_7d_pct')}% -> {q1.get('weekly_7d_pct')}%")
    sys.stdout.flush()
    if not_done:
        os._exit(0)  # abandon still-running chains (non-daemon executor threads would otherwise block exit)


if __name__ == "__main__":
    main()
