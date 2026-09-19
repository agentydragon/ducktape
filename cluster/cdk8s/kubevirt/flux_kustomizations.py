"""Flux Kustomizations for the cluster/k8s/kubevirt slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def kubevirt() -> dict[str, object]:
    name = "kubevirt"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/kubevirt/app",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            wait=True,
            timeout="10m",
            depends_on=[KustomizationSpecDependsOn(name="kubevirt-operator", namespace="ducktape-flux")],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="virt-api", namespace="kubevirt"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="virt-controller", namespace="kubevirt"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="DaemonSet", name="virt-handler", namespace="kubevirt"
                ),
            ],
        ),
    )


def cdi_operator() -> dict[str, object]:
    name = "cdi-operator"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/kubevirt/cdi-operator",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="kubevirt-cdi-operator",
                namespace="ducktape-flux",
            ),
            wait=True,
            timeout="10m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="cdi-operator", namespace="cdi"
                )
            ],
        ),
    )


def cdi() -> dict[str, object]:
    name = "cdi"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/kubevirt/cdi",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            wait=True,
            timeout="10m",
            depends_on=[
                KustomizationSpecDependsOn(name="cdi-operator", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="cdi-apiserver", namespace="cdi"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="cdi-deployment", namespace="cdi"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="cdi-uploadproxy", namespace="cdi"
                ),
            ],
        ),
    )


def kubevirt_operator() -> dict[str, object]:
    name = "kubevirt-operator"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/kubevirt/operator",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            wait=True,
            timeout="10m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="virt-operator", namespace="kubevirt"
                )
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/kubevirt/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, kubevirt())
    path = root / "cluster/k8s/kubevirt/cdi-operator/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, cdi_operator())
    path = root / "cluster/k8s/kubevirt/cdi/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, cdi())
    path = root / "cluster/k8s/kubevirt/operator/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, kubevirt_operator())
