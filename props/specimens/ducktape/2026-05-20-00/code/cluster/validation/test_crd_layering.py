"""Unit tests for CRD layering validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
import yaml

from cluster.validation.crd_layering import CrdLayeringViolationError, check_crd_layering
from cluster.validation.k8s import parse_k8s_resources
from cluster.validation.kustomize import KustomizeBuildResult
from util.bazel.runfiles import get_required_path

_TESTDATA = get_required_path("_main/cluster/validation/testdata/crd_layering")


def _build_result(kustomization_path: Path, build_output_file: Path) -> KustomizeBuildResult:
    yaml_output = build_output_file.read_text()
    return KustomizeBuildResult(
        kustomization_path=kustomization_path, resources=parse_k8s_resources(yaml.safe_load_all(yaml_output))
    )


@pytest.mark.parametrize(
    ("kustomization_path", "testdata_name"),
    [
        (Path("/k8s/test-app/kustomization.yaml"), "helmrelease_only"),
        (Path("/k8s/test-app/kustomization.yaml"), "crd_only"),
        (Path("/k8s/external-secrets-operator/kustomization.yaml"), "operator_with_crd"),
        (Path("/k8s/cert-manager-config/base/kustomization.yaml"), "cert_manager_nested"),
        (Path("/k8s/vault/config/base/kustomization.yaml"), "vault_nested"),
        (Path("/k8s/test-app/overlays/production/kustomization.yaml"), "mixed_helmrelease_and_crd"),
    ],
    ids=["helmrelease_only", "crd_only", "operator_with_crd", "cert_manager_nested", "vault_nested", "overlay_skipped"],
)
def test_valid_cases(kustomization_path: Path, testdata_name: str) -> None:
    check_crd_layering(_build_result(kustomization_path, _TESTDATA / f"{testdata_name}.yaml"))


def test_detects_crd_layering_violation() -> None:
    build_output = _TESTDATA / "mixed_helmrelease_and_crd.yaml"
    with pytest.raises(CrdLayeringViolationError, match="ExternalSecret"):
        check_crd_layering(_build_result(Path("/k8s/test-app/kustomization.yaml"), build_output))


if __name__ == "__main__":
    pytest_bazel.main()
