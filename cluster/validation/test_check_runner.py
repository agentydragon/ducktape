"""Tests for check_runner serialization utilities."""

from __future__ import annotations

import json
from pathlib import Path

import pytest_bazel

from cluster.validation.k8s import K8sResource
from cluster.validation.kustomize import KustomizeBuildResult


class TestKustomizeResultsSerialization:
    """Tests for round-trip serialization of KustomizeBuildResult."""

    def test_round_trip_successful_result(self, tmp_path: Path) -> None:
        """Successful build result survives JSON round-trip."""
        original = KustomizeBuildResult(
            kustomization_path=Path("/workspace/cluster/k8s/test-app/kustomization.yaml"),
            resources=[
                K8sResource(kind="HelmRelease", apiVersion="helm.toolkit.fluxcd.io/v2"),
                K8sResource(kind="ConfigMap", apiVersion="v1"),
            ],
        )

        # Serialize with relative path (like kustomize_build_all does)
        data = original.model_dump(mode="json")
        data["kustomization_path"] = "cluster/k8s/test-app/kustomization.yaml"

        json_path = tmp_path / "results.json"
        json_path.write_text(json.dumps([data]))

        # Deserialize (manually, since load_kustomize_results needs workspace)
        loaded = json.loads(json_path.read_text())
        result = KustomizeBuildResult.model_validate(loaded[0])
        assert len(result.resources) == 2
        assert result.resources[0].kind == "HelmRelease"
        assert result.resources[1].kind == "ConfigMap"

    def test_round_trip_helmrelease_chart_version(self) -> None:
        """HelmRelease chart version is preserved through serialization."""
        original = KustomizeBuildResult(
            kustomization_path=Path("cluster/k8s/app/kustomization.yaml"),
            success=True,
            resources=[
                K8sResource(
                    kind="HelmRelease",
                    apiVersion="helm.toolkit.fluxcd.io/v2",
                    spec={"chart": {"spec": {"version": "1.2.3"}}},
                )
            ],
        )

        data = original.model_dump(mode="json")
        result = KustomizeBuildResult.model_validate(data)
        assert result.resources[0].chart_version == "1.2.3"


if __name__ == "__main__":
    pytest_bazel.main()
