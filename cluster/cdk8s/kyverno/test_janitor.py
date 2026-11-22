"""Tests for the janitor NamespacedDeletingPolicy: only objects older than 7 days are reaped."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Chart

from cluster.cdk8s.kyverno.apply import apply_policy
from cluster.cdk8s.kyverno.apply_inputs import policy
from cluster.cdk8s.kyverno.janitor import ApiResource, janitor

_NAMESPACE = "janitor-test"


@pytest.fixture(scope="module")
def janitor_policy(scratch_app_factory: Callable[[], App]) -> Path:
    app = scratch_app_factory()
    chart = Chart(app, "janitor-test")
    janitor(
        chart,
        "janitor",
        name="test-janitor",
        namespace=_NAMESPACE,
        schedule="* * * * *",
        resources=[ApiResource(group="", version="v1", resource="pods")],
    )
    return policy(chart, Path(app.outdir))


def _pod(directory: Path, *, age: timedelta) -> Path:
    path = directory / "pod.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "janitor-test-pod",
                    "namespace": _NAMESPACE,
                    "creationTimestamp": (datetime.now(UTC) - age).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                "spec": {"containers": [{"name": "app", "image": "busybox:latest"}]},
            }
        )
    )
    return path


class TestJanitor:
    def test_object_older_than_seven_days_is_reaped(self, janitor_policy: Path, tmp_path: Path):
        result = apply_policy(janitor_policy, _pod(tmp_path, age=timedelta(days=8)))
        assert result.passed == 1, result.stdout

    def test_younger_object_is_kept(self, janitor_policy: Path, tmp_path: Path):
        result = apply_policy(janitor_policy, _pod(tmp_path, age=timedelta(days=6)))
        assert result.ok, result.stdout
        assert result.passed == 0, result.stdout


if __name__ == "__main__":
    pytest_bazel.main()
