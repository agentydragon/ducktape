"""Unit tests for CRD layering validation."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import pytest_bazel
import yaml

from cluster.validation.crd_layering import CrdLayeringViolationError, check_crd_layering
from cluster.validation.k8s import parse_k8s_resources
from cluster.validation.kustomize import KustomizeBuildResult


def _build_result(path: Path, yaml_output: str) -> KustomizeBuildResult:
    return KustomizeBuildResult(kustomization_path=path, resources=parse_k8s_resources(yaml.safe_load_all(yaml_output)))


_HELMRELEASE_ONLY = dedent("""
    apiVersion: helm.toolkit.fluxcd.io/v2
    kind: HelmRelease
    metadata:
      name: test-app
    spec:
      chart:
        spec:
          chart: test
""")

_CRD_ONLY = dedent("""
    apiVersion: external-secrets.io/v1beta1
    kind: ExternalSecret
    metadata:
      name: test-secret
    spec:
      secretStoreRef:
        name: vault-backend
""")

_MIXED_HELMRELEASE_AND_CRD = dedent("""
    apiVersion: helm.toolkit.fluxcd.io/v2
    kind: HelmRelease
    metadata:
      name: test-app
    spec:
      chart:
        spec:
          chart: test
    ---
    apiVersion: external-secrets.io/v1beta1
    kind: ExternalSecret
    metadata:
      name: test-secret
    spec:
      secretStoreRef:
        name: vault-backend
""")

_OPERATOR_WITH_CRD = dedent("""
    apiVersion: helm.toolkit.fluxcd.io/v2
    kind: HelmRelease
    metadata:
      name: external-secrets
    ---
    apiVersion: external-secrets.io/v1beta1
    kind: ClusterSecretStore
    metadata:
      name: vault
""")

_CERT_MANAGER_NESTED = dedent("""
    apiVersion: helm.toolkit.fluxcd.io/v2
    kind: HelmRelease
    metadata:
      name: cert-manager-webhook
    ---
    apiVersion: cert-manager.io/v1
    kind: ClusterIssuer
    metadata:
      name: letsencrypt-prod
""")

_VAULT_NESTED = dedent("""
    apiVersion: helm.toolkit.fluxcd.io/v2
    kind: HelmRelease
    metadata:
      name: vault-webhook
    ---
    apiVersion: vault.banzaicloud.com/v1alpha1
    kind: Vault
    metadata:
      name: vault
""")


@pytest.mark.parametrize(
    ("path", "yaml_output"),
    [
        (Path("/k8s/test-app/kustomization.yaml"), _HELMRELEASE_ONLY),
        (Path("/k8s/test-app/kustomization.yaml"), _CRD_ONLY),
        (Path("/k8s/external-secrets-operator/kustomization.yaml"), _OPERATOR_WITH_CRD),
        (Path("/k8s/cert-manager-config/base/kustomization.yaml"), _CERT_MANAGER_NESTED),
        (Path("/k8s/vault/config/base/kustomization.yaml"), _VAULT_NESTED),
        (Path("/k8s/test-app/overlays/production/kustomization.yaml"), _MIXED_HELMRELEASE_AND_CRD),
    ],
    ids=["helmrelease_only", "crd_only", "operator_with_crd", "cert_manager_nested", "vault_nested", "overlay_skipped"],
)
def test_valid_cases(path: Path, yaml_output: str) -> None:
    check_crd_layering(_build_result(path, yaml_output))


def test_detects_crd_layering_violation() -> None:
    with pytest.raises(CrdLayeringViolationError, match="ExternalSecret"):
        check_crd_layering(_build_result(Path("/k8s/test-app/kustomization.yaml"), _MIXED_HELMRELEASE_AND_CRD))


if __name__ == "__main__":
    pytest_bazel.main()
