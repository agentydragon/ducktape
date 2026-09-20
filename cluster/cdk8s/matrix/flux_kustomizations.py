"""Flux Kustomizations for the cluster/k8s/matrix slice."""

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

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def matrix(
    chart: Chart,
    matrix_namespace: Kustomization,
    matrix_db: Kustomization,
    sso_providers_tf: Kustomization,
    reflector: Kustomization,
    gateway: Kustomization,
    local_path_provisioner: Kustomization,
) -> Kustomization:
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
            path="./cluster/k8s/matrix/app",
            prune=True,
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
            depends_on=flux_kustomization_depends_on_many(
                matrix_namespace,
                # externalPostgresql reads the CNPG-generated matrix-db-app secret
                matrix_db,
                # writes matrix-oidc-config into the authentik namespace
                sso_providers_tf,
                # mirrors matrix-oidc-config into the matrix namespace
                reflector,
                gateway,
                # local-path-proxmox media store PVC
                local_path_provisioner,
            ),
        ),
    )


def matrix_db(
    chart: Chart, matrix_namespace: Kustomization, cnpg: Kustomization, local_path_provisioner: Kustomization
) -> Kustomization:
    name = "matrix-db"
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
            path="./cluster/k8s/matrix/db",
            prune=True,
            wait=True,
            depends_on=flux_kustomization_depends_on_many(matrix_namespace, cnpg, local_path_provisioner),
        ),
    )


def matrix_namespace(chart: Chart) -> Kustomization:
    name = "matrix-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="1h",
            path="./cluster/k8s/matrix/namespace",
            prune=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="1m",
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
            path="./cluster/k8s/matrix/user-provisioner",
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
