"""Tests for the janitor NamespacedDeletingPolicy: only objects older than 7 days are reaped."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from cdk8s import (
    App,
    Chart,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)

from cluster.cdk8s.kyverno.apply import apply_policy
from cluster.cdk8s.kyverno.janitor import ApiResource, janitor

_NAMESPACE = "janitor-test"


@pytest.fixture
def janitor_policy(scratch_app: App, tmp_path: Path) -> Path:
    chart = Chart(scratch_app, "janitor-test")
    janitor(
        chart,
        "janitor",
        name="test-janitor",
        namespace=_NAMESPACE,
        schedule="* * * * *",
        resources=[ApiResource(group="", version="v1", resource="pods")],
    )
    [document] = Cdk8sTesting.synth(chart)
    path = tmp_path / "janitor.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


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
        assert result.passed == 0, result.stdout


if __name__ == "__main__":
    pytest_bazel.main()
