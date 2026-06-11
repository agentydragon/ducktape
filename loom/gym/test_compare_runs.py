from __future__ import annotations

from datetime import date

import pytest
import pytest_bazel

from loom.gym.compare_runs import metric_deltas_by_cluster

CLUSTER_BY_ID = {"t1": date(2024, 7, 1), "t2": date(2024, 7, 1), "t3": date(2024, 10, 1)}


def test_deltas_pair_only_shared_tasks_and_metrics() -> None:
    metrics_a = {"t1": {"log_loss": 0.5}, "t2": {"log_loss": 0.7, "brier": 0.2}, "only-a": {"log_loss": 1.0}}
    metrics_b = {"t1": {"log_loss": 0.3}, "t2": {"log_loss": 0.6}, "t3": {"brier": 0.1}}
    deltas = metric_deltas_by_cluster(metrics_a, metrics_b, CLUSTER_BY_ID)
    # "only-a" is unpaired; t2's brier exists only in run A; t3 only in run B.
    assert set(deltas) == {"log_loss"}
    assert sorted(deltas["log_loss"][date(2024, 7, 1)]) == pytest.approx([-0.2, -0.1])


if __name__ == "__main__":
    pytest_bazel.main()
