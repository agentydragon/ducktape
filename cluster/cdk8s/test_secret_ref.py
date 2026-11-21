"""Typed Secret references embedded in Helm's untyped values retain Kubernetes field names."""

import pytest_bazel
from cdk8s import Chart, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.secret_ref import SecretRef

_KEY = SecretRef(namespace="test-namespace", name="test-credentials").key("token")
_ENV = {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"key": "token", "name": "test-credentials"}}}


def test_helm_values_env_map(scratch_chart: Chart) -> None:
    helm_release(
        scratch_chart,
        "test-release",
        "test-namespace",
        repository=https_helm_repository(
            scratch_chart, "test-charts", "test-namespace", url="https://charts.test.invalid"
        ),
        chart="test-chart",
        version="1.0.0",
        interval="1h",
        values={"env": {"TOKEN": {"valueFrom": _KEY.value_from()}}},
    )
    (_, release) = Cdk8sTesting.synth(scratch_chart)
    assert release["spec"]["values"]["env"] == {"TOKEN": {"valueFrom": _ENV["valueFrom"]}}


if __name__ == "__main__":
    pytest_bazel.main()
