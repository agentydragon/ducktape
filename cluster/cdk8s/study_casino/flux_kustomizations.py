"""Flux Kustomizations for the cluster/k8s/study-casino slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


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
            depends_on=[
                flux_kustomization_depends_on(external_secrets_config),
                flux_kustomization_depends_on(forgejo_images),
                flux_kustomization_depends_on(gateway),
                flux_kustomization_depends_on(study_casino_namespace),
                flux_kustomization_depends_on(study_casino_db),
                flux_kustomization_depends_on(claude_rbac),
            ],
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
            prune=True,
            wait=True,
            depends_on=[
                flux_kustomization_depends_on(cnpg),
                flux_kustomization_depends_on(study_casino_namespace),
                flux_kustomization_depends_on(local_path_provisioner),
                flux_kustomization_depends_on(reflector),
            ],
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
            wait=True,
        ),
    )
