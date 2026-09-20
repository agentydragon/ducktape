"""Flux Kustomizations for the cluster/k8s/atuin slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def atuin(
    chart: Chart,
    cert_manager_issuer_config: Kustomization,
    atuin_namespace: Kustomization,
    atuin_db: Kustomization,
    gateway: Kustomization,
    cert_manager_environment: Kustomization,
) -> Kustomization:
    name = "atuin"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/atuin/app",
            prune=True,
            wait=True,
            post_build=KustomizationSpecPostBuild(
                substitute_from=[
                    KustomizationSpecPostBuildSubstituteFrom(
                        kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                    )
                ]
            ),
            depends_on=flux_kustomization_depends_on_many(
                cert_manager_issuer_config, atuin_namespace, atuin_db, gateway, cert_manager_environment
            ),
        ),
    )


def atuin_db(
    chart: Chart, atuin_namespace: Kustomization, cnpg: Kustomization, local_path_provisioner: Kustomization
) -> Kustomization:
    name = "atuin-db"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/atuin/db",
            prune=False,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            depends_on=flux_kustomization_depends_on_many(atuin_namespace, cnpg, local_path_provisioner),
        ),
    )


def atuin_namespace(chart: Chart) -> Kustomization:
    name = "atuin-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/atuin/namespace",
            prune=False,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )


def atuin_user_provisioner(chart: Chart, atuin: Kustomization, user_agentydragon: Kustomization) -> Kustomization:
    name = "atuin-user-provisioner"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/atuin/user-provisioner",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=flux_kustomization_depends_on_many(atuin, user_agentydragon),
        ),
    )
