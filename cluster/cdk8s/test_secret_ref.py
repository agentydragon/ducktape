"""A `SecretKey` reads the same secretKeyRef in every env dialect: a raw `k8s.EnvVar`, a cdk8s-plus
`EnvValue`, and a Helm values map, where the typed `k8s.EnvVarSource` is serialized through jsii's
`any` and must still come out wire-shaped (the executable record of that library behavior)."""

import pytest
import pytest_bazel
from cdk8s import ApiObjectMetadata, Chart, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from cdk8s_plus_34 import Deployment, k8s

from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.secret_ref import SecretRef

_KEY = SecretRef(namespace="test-namespace", name="test-credentials").key("token")
_ENV = {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"key": "token", "name": "test-credentials"}}}


@pytest.fixture
def chart() -> Chart:
    return Cdk8sTesting.chart()


def test_raw_env_var(chart: Chart) -> None:
    k8s.KubePod(
        chart,
        "pod",
        metadata=k8s.ObjectMeta(name="test-pod"),
        spec=k8s.PodSpec(
            containers=[k8s.Container(name="app", image="app.test.invalid/app", env=[_KEY.env_var("TOKEN")])]
        ),
    )
    [pod] = Cdk8sTesting.synth(chart)
    assert pod["spec"]["containers"][0]["env"] == [_ENV]


def test_cdk8s_plus_env_value(chart: Chart) -> None:
    deployment = Deployment(chart, "deployment", metadata=ApiObjectMetadata(name="test-app"))
    deployment.add_container(
        image="app.test.invalid/app", env_variables={"TOKEN": _KEY.env_value(chart, "credentials")}
    )
    [rendered] = Cdk8sTesting.synth(chart)
    assert rendered["spec"]["template"]["spec"]["containers"][0]["env"] == [_ENV]


def test_helm_values_env_map(chart: Chart) -> None:
    helm_release(
        chart,
        "test-release",
        "test-namespace",
        repository=https_helm_repository(chart, "test-charts", "test-namespace", url="https://charts.test.invalid"),
        chart="test-chart",
        version="1.0.0",
        interval="1h",
        values={"env": {"TOKEN": {"valueFrom": _KEY.value_from()}}},
    )
    (_, release) = Cdk8sTesting.synth(chart)
    assert release["spec"]["values"]["env"] == {"TOKEN": {"valueFrom": _ENV["valueFrom"]}}


if __name__ == "__main__":
    pytest_bazel.main()
