"""Flux Kustomizations for the cluster/k8s/matrix slice."""

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


def matrix(chart: Chart, cnpg: Kustomization) -> Kustomization:
    name = "matrix"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="matrix-app", namespace="ducktape-flux"
            ),
            path="./",
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="matrix-synapse",
                    namespace="matrix",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(cnpg),
        ),
    )


def matrix_user_provisioner(
    chart: Chart, external_secrets_config: Kustomization, forgejo_images: Kustomization, matrix: Kustomization
) -> Kustomization:
    name = "matrix-user-provisioner"
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
            timeout="5m",
            # The job registers users against Synapse's admin API, so it must not start
            # until Synapse answers. Depending on the app Kustomization (which is
            # wait:true over the HelmRelease) gives that ordering; the health check states
            # it directly rather than relying on that transitively.
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="matrix-synapse",
                    namespace="matrix",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config,
                forgejo_images,
                # Synapse is deployed and healthy; also carries the registration shared secret, admin and bot passwords
                matrix,
            ),
        ),
    )
