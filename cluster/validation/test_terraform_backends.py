"""Unit tests for tofu-controller backend validation."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.k8s import K8sResource, TerraformResource
from cluster.validation.kustomize import KustomizeBuildResult
from cluster.validation.terraform_backends import check_terraform_backends


def _cluster_with_resource(resource: K8sResource) -> ParsedCluster:
    return ParsedCluster(
        build_results=[KustomizeBuildResult(kustomization_path=Path("kustomization.yaml"), resources=[resource])]
    )


def test_pg_backend_passes() -> None:
    resource = TerraformResource.model_validate(
        {
            "apiVersion": "infra.contrib.fluxcd.io/v1alpha2",
            "kind": "Terraform",
            "metadata": {"name": "example", "namespace": "flux-system"},
            "spec": {"backendConfig": {"customConfiguration": 'backend "pg" {\n  schema_name = "example"\n}'}},
        }
    )

    assert check_terraform_backends(_cluster_with_resource(resource)) == []


def test_kubernetes_backend_fails() -> None:
    resource = TerraformResource.model_validate(
        {
            "apiVersion": "infra.contrib.fluxcd.io/v1alpha2",
            "kind": "Terraform",
            "metadata": {"name": "example", "namespace": "flux-system"},
            "spec": {
                "backendConfig": {"customConfiguration": 'backend "kubernetes" {\n  secret_suffix = "example"\n}'}
            },
        }
    )

    errors = check_terraform_backends(_cluster_with_resource(resource))

    assert errors == [
        'Terraform flux-system/example uses backend "kubernetes"; '
        'use backend "pg" for tofu-controller state so locks and state live outside etcd'
    ]


if __name__ == "__main__":
    pytest_bazel.main()
