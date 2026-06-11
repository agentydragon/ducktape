"""Aggregate agent-eval baseline runs into numbers (with cluster-bootstrap CIs) and plots.

Reads two Inspect log dirs (no-archive vs archive) of the market panel, extracts
per-task log loss / Brier from the gym scorer, and compares them against the
market crowd baseline (`prob_at_as_of`). All means carry a 95% percentile
cluster-bootstrap CI clustered by `as_of` (tasks sharing an era are correlated).

    bazelisk run //loom/gym:analyze_baselines_bin -- \
        --run no-archive=/tmp/baseline/noarch --run archive=/tmp/baseline/arch \
        --out /tmp/baseline/plots
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import matplotlib.pyplot as plt
from inspect_ai.log import list_eval_logs, read_eval_log

from loom.gym.market_seed_tasks import MARKET_SEED_RECORDS
from loom.gym.scoring import cluster_bootstrap_ci

plt.switch_backend("Agg")  # headless rendering

_EPS = 1e-6


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    log_loss: float  # nan if no parseable submission
    brier: float
    p: float | None  # submitted probability, if any


def read_run(log_dir: str) -> dict[str, TaskResult]:
    results: dict[str, TaskResult] = {}
    for info in list_eval_logs(log_dir):
        log = read_eval_log(info)
        for sample in log.samples or []:
            score = (sample.scores or {}).get("gym_proper_loss")
            if score is None:
                continue
            meta = score.metadata or {}
            value = float(score.value) if isinstance(score.value, (int, float)) else float("nan")
            p = None
            with contextlib.suppress(json.JSONDecodeError, KeyError, ValueError, TypeError):
                p = float(json.loads(str(score.answer))["p"])
            results[str(sample.id)] = TaskResult(
                task_id=str(sample.id), log_loss=value, brier=float(meta.get("brier", float("nan"))), p=p
            )
    return results


def crowd_baseline() -> dict[str, TaskResult]:
    out: dict[str, TaskResult] = {}
    for r in MARKET_SEED_RECORDS:
        p_yes = r.prob_at_as_of
        realized = p_yes if r.resolved_yes else 1.0 - p_yes
        out[r.task_id] = TaskResult(
            task_id=r.task_id,
            log_loss=-math.log(max(realized, _EPS)),
            brier=(p_yes - float(r.resolved_yes)) ** 2,
            p=p_yes,
        )
    return out


def as_of_clusters() -> dict[str, date]:
    return {r.task_id: r.as_of for r in MARKET_SEED_RECORDS}


def aggregate(
    results: dict[str, TaskResult], cluster_by_id: dict[str, date], metric: str
) -> tuple[float, tuple, int, int]:
    """Pooled mean of `metric` over submitted tasks + 95% cluster-bootstrap CI; (mean, ci, n_submitted, n_total)."""
    by_cluster: dict[date, list[float]] = {}
    n_total = 0
    for task_id, cluster in cluster_by_id.items():
        if task_id not in results:
            continue
        n_total += 1
        value = getattr(results[task_id], metric)
        if math.isnan(value):
            continue
        by_cluster.setdefault(cluster, []).append(value)
    values = [v for vs in by_cluster.values() for v in vs]
    mean = sum(values) / len(values) if values else float("nan")
    ci = cluster_bootstrap_ci(list(by_cluster.values())) or (float("nan"), float("nan"))
    return mean, ci, len(values), n_total


def paired_delta(
    a: dict[str, TaskResult], b: dict[str, TaskResult], cluster_by_id: dict[str, date], metric: str
) -> tuple[float, tuple, int]:
    """Mean per-task delta (b - a; negative = b better) + 95% cluster-bootstrap CI.

    Over tasks both submitted. Absolute loss is dominated by task/era difficulty
    that both contestants share; differencing within each task cancels it, so the
    same clusters resolve a much smaller effect with a far tighter CI than two
    absolute means."""
    by_cluster: dict[date, list[float]] = {}
    for task_id, cluster in cluster_by_id.items():
        if task_id not in a or task_id not in b:
            continue
        av, bv = getattr(a[task_id], metric), getattr(b[task_id], metric)
        if math.isnan(av) or math.isnan(bv):
            continue
        by_cluster.setdefault(cluster, []).append(bv - av)
    values = [v for vs in by_cluster.values() for v in vs]
    if not values:
        return float("nan"), (float("nan"), float("nan")), 0
    mean = sum(values) / len(values)
    ci = cluster_bootstrap_ci(list(by_cluster.values())) or (float("nan"), float("nan"))
    return mean, ci, len(values)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, help="label=log_dir (repeatable).")
    parser.add_argument("--out", type=Path, required=True, help="Directory for plots.")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    cluster_by_id = as_of_clusters()
    runs: dict[str, dict[str, TaskResult]] = {}
    for spec in args.run:
        label, _, log_dir = spec.partition("=")
        runs[label] = read_run(log_dir)
    runs["market crowd"] = crowd_baseline()

    print(f"\n{'contestant':<16} {'n':>7} {'log loss (95% CI)':>26} {'Brier (95% CI)':>26}")
    print("-" * 78)
    stats: dict[str, tuple[float, tuple]] = {}
    for label, results in runs.items():
        ll_mean, ll_ci, n_sub, n_tot = aggregate(results, cluster_by_id, "log_loss")
        br_mean, br_ci, _, _ = aggregate(results, cluster_by_id, "brier")
        stats[label] = (ll_mean, ll_ci)
        n = f"{n_sub}/{n_tot}"
        print(
            f"{label:<16} {n:>7} {ll_mean:>9.3f} [{ll_ci[0]:.3f}, {ll_ci[1]:.3f}]"
            f"   {br_mean:>7.3f} [{br_ci[0]:.3f}, {br_ci[1]:.3f}]"
        )
    print()

    # Paired per-task deltas — the powerful comparison (cancels shared difficulty).
    agent_arms = [k for k in runs if k != "market crowd"]
    pairs: list[tuple[str, str]] = []
    if "no-archive" in runs and "archive" in runs:
        pairs.append(("no-archive", "archive"))
    pairs += [("market crowd", arm) for arm in agent_arms]
    if pairs:
        print("paired per-task deltas (b - a; negative = b better):")
        for a_label, b_label in pairs:
            for metric, name in (("log_loss", "log loss"), ("brier", "Brier")):
                delta, ci, count = paired_delta(runs[a_label], runs[b_label], cluster_by_id, metric)
                print(f"  {b_label} - {a_label:<13} {name:<9} {delta:+.3f} [{ci[0]:+.3f}, {ci[1]:+.3f}]  (n={count})")
        print()

    # Plot 1: mean log loss with cluster-bootstrap CI whiskers.
    labels = list(stats)
    means = [stats[k][0] for k in labels]
    lo = [stats[k][0] - stats[k][1][0] for k in labels]
    hi = [stats[k][1][1] - stats[k][0] for k in labels]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar(labels, means, yerr=[lo, hi], capsize=6, color=["#4c72b0", "#dd8452", "#999999"][: len(labels)])
    ax.set_ylabel("mean log loss (lower is better)")
    ax.set_title("glm-4.5 on 33 resolved Manifold markets\n95% cluster-bootstrap CI (clustered by as_of)")
    fig.tight_layout()
    fig.savefig(args.out / "log_loss.png", dpi=130)

    # Plot 2: submitted probability vs market probability, per arm.
    crowd_p = {r.task_id: r.prob_at_as_of for r in MARKET_SEED_RECORDS}
    yes = {r.task_id: r.resolved_yes for r in MARKET_SEED_RECORDS}
    fig2, ax2 = plt.subplots(figsize=(5.5, 5.5))
    ax2.plot([0, 1], [0, 1], color="#cccccc", lw=1, zorder=0)
    for label, color in zip(agent_arms, ["#4c72b0", "#dd8452"], strict=False):
        points = [(crowd_p[t], res.p, yes[t]) for t, res in runs[label].items() if res.p is not None and t in crowd_p]
        for resolved, marker, suffix in ((True, "^", "YES"), (False, "v", "NO")):
            xs = [x for x, _, y in points if y == resolved]
            ys = [yv for _, yv, y in points if y == resolved]
            ax2.scatter(xs, ys, color=color, marker=marker, alpha=0.8, label=f"{label} (resolved {suffix})")
    ax2.set_xlabel("market probability at as_of")
    ax2.set_ylabel("glm-4.5 submitted probability")
    ax2.set_title("Agent vs crowd (▲ YES, ▼ NO; diagonal = agrees with market)")
    ax2.legend(fontsize=7)
    fig2.tight_layout()
    fig2.savefig(args.out / "agent_vs_market.png", dpi=130)
    print(f"plots: {args.out}/log_loss.png  {args.out}/agent_vs_market.png")


if __name__ == "__main__":
    main()
