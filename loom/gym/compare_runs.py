"""Paired comparison of two eval runs over identical tasks.

Absolute losses are dominated by era difficulty, which all contestants share;
per-task **deltas** cancel it, so the same few anchor clusters can separate
much smaller effects. For every metric present in both runs, reports the mean
delta (B - A; negative = B better) with a 95% cluster-bootstrap CI, clustered
by `as_of`.

Usage:

    bazelisk run //loom/gym:compare_eval_runs -- a.json b.json --labels bare,bundled
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

from finance.evidence.checkout import ensure_checkout
from loom.gym.monthly_series import load_series
from loom.gym.scoring import cluster_bootstrap_ci
from loom.gym.series_tasks import all_tasks


def metric_deltas_by_cluster(
    metrics_a: dict[str, dict[str, float]], metrics_b: dict[str, dict[str, float]], cluster_by_id: dict[str, date]
) -> dict[str, dict[date, list[float]]]:
    """Metric name → cluster → per-task deltas (B - A), over tasks scored in both runs."""
    deltas: dict[str, dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))
    for task_id in metrics_a.keys() & metrics_b.keys():
        cluster = cluster_by_id[task_id]
        for name in metrics_a[task_id].keys() & metrics_b[task_id].keys():
            deltas[name][cluster].append(metrics_b[task_id][name] - metrics_a[task_id][name])
    return deltas


def _load_metrics(path: Path) -> dict[str, dict[str, float]]:
    return {result["task_id"]: result["metrics"] for result in json.loads(path.read_text())["results"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_a", type=Path)
    parser.add_argument("run_b", type=Path)
    parser.add_argument("--labels", default="A,B", help="Comma-separated labels for the two runs.")
    args = parser.parse_args()
    label_a, label_b = args.labels.split(",")
    metrics_a = _load_metrics(args.run_a)
    metrics_b = _load_metrics(args.run_b)
    cluster_by_id = {task.task_id: task.as_of for task in all_tasks(load_series(ensure_checkout()))}

    print(f"paired delta = {label_b} - {label_a} (negative = {label_b} better), 95% cluster-bootstrap CI")
    for name, clusters in sorted(metric_deltas_by_cluster(metrics_a, metrics_b, cluster_by_id).items()):
        values = [delta for cluster_deltas in clusters.values() for delta in cluster_deltas]
        mean_delta = sum(values) / len(values)
        ci = cluster_bootstrap_ci(tuple(clusters.values()))
        ci_text = f" [{ci[0]:+.4f},{ci[1]:+.4f}]" if ci is not None else ""
        separated = ci is not None and (ci[0] > 0 or ci[1] < 0)
        print(
            f"{name:18} n={len(values):3} clusters={len(clusters):2} delta={mean_delta:+.4f}{ci_text}"
            + ("  *" if separated else "")
        )


if __name__ == "__main__":
    main()
