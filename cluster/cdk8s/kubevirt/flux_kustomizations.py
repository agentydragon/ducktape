"""Flux Kustomizations for the cluster/k8s/kubevirt slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
    flux_kustomization_depends_on_many,
)


def kubevirt(chart: Chart, kubevirt_operator: Kustomization) -> Kustomization:
    name = "kubevirt"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            wait=True,
            timeout="10m",
            depends_on=[flux_kustomization_depends_on(kubevirt_operator)],
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


def cdi_operator(chart: Chart) -> Kustomization:
    name = "cdi-operator"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./",
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


def cdi(chart: Chart, cdi_operator: Kustomization, local_path_provisioner: Kustomization) -> Kustomization:
    name = "cdi"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            wait=True,
            timeout="10m",
            depends_on=flux_kustomization_depends_on_many(cdi_operator, local_path_provisioner),
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


def kubevirt_operator(chart: Chart) -> Kustomization:
    name = "kubevirt-operator"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./",
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
