"""Smoke test for the cdk8s-generated Flux Kustomization bindings (cdk8s_import.bzl).

Confirms the generated jsii package actually loads and synthesizes under Bazel, and
that its `KustomizationSpecSourceRefKind` enum accepts the "ExternalArtifact" value
cluster/cdk8s/flux_constructs.py currently emits as a plain string.
"""

from pathlib import Path

import pytest_bazel
from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)


def test_kustomization_synthesizes_with_external_artifact_source(tmp_path: Path) -> None:
    app = App(outdir=str(tmp_path))
    chart = Chart(app, "test", disable_resource_name_hashes=True)
    Kustomization(
        chart,
        "kustomization",
        spec=KustomizationSpec(
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="litellm"
            ),
            path="./cluster/k8s/litellm/app",
            interval="10m",
            prune=True,
        ),
    )
    app.synth()
    (manifest_path,) = tmp_path.glob("*.k8s.yaml")
    manifest = manifest_path.read_text()

    assert "kind: Kustomization" in manifest
    assert "sourceRef" in manifest
    assert "ExternalArtifact" in manifest


if __name__ == "__main__":
    pytest_bazel.main()
