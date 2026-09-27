"""`Kustomization` renders the required fields under their own names and leaves optional
`KustomizationSpec` fields unset on `None`, so Flux's own defaults apply."""

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from cluster.cdk8s.providers.flux.flux_kustomization import Kustomization

_SOURCE_REF = KustomizationSpecSourceRef(kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="test-repo")


def test_required_fields_render_under_their_own_names() -> None:
    chart = Cdk8sTesting.chart()
    Kustomization(
        chart,
        "kustomization",
        name="test-app",
        namespace="test-namespace",
        source_ref=_SOURCE_REF,
        interval="10m",
        prune=True,
    )
    (kustomization,) = Cdk8sTesting.synth(chart)
    assert kustomization["metadata"]["name"] == "test-app"
    assert kustomization["metadata"]["namespace"] == "test-namespace"
    assert kustomization["spec"]["sourceRef"] == {"kind": "GitRepository", "name": "test-repo"}
    assert kustomization["spec"]["interval"] == "10m"
    assert kustomization["spec"]["prune"] is True
    assert "wait" not in kustomization["spec"]
    assert "path" not in kustomization["spec"]


def test_optional_fields_render_when_given() -> None:
    chart = Cdk8sTesting.chart()
    Kustomization(
        chart,
        "kustomization",
        name="test-app",
        namespace="test-namespace",
        source_ref=_SOURCE_REF,
        interval="10m",
        prune=True,
        path="./test-app",
        wait=True,
        timeout="2m",
    )
    (kustomization,) = Cdk8sTesting.synth(chart)
    assert kustomization["spec"]["path"] == "./test-app"
    assert kustomization["spec"]["wait"] is True
    assert kustomization["spec"]["timeout"] == "2m"


if __name__ == "__main__":
    pytest_bazel.main()
