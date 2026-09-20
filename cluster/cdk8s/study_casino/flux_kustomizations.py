"""Flux Kustomizations for the cluster/k8s/study-casino slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def study_casino(
    chart: Chart,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    gateway: Kustomization,
    study_casino_namespace: Kustomization,
    study_casino_db: Kustomization,
    claude_rbac: Kustomization,
) -> Kustomization:
    name = "study-casino"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="study-casino-app",
                namespace="ducktape-flux",
            ),
            path="./cluster/k8s/study-casino/app",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="study-casino", namespace="study-casino"
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config, forgejo_images, gateway, study_casino_namespace, study_casino_db, claude_rbac
            ),
        ),
    )


def study_casino_db(
    chart: Chart,
    cnpg: Kustomization,
    study_casino_namespace: Kustomization,
    local_path_provisioner: Kustomization,
    reflector: Kustomization,
) -> Kustomization:
    name = "study-casino-db"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/study-casino/db",
            prune=False,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            depends_on=flux_kustomization_depends_on_many(
                cnpg, study_casino_namespace, local_path_provisioner, reflector
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
        ),
    )


def study_casino_namespace(chart: Chart) -> Kustomization:
    name = "study-casino-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="1m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/study-casino/namespace",
            prune=False,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
        ),
    )
