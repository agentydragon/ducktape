"""Flux Kustomizations for the cluster/k8s/authentik slice."""

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


def authentik(
    chart: Chart,
    authentik_namespace: Kustomization,
    authentik_db: Kustomization,
    cert_manager: Kustomization,
    gateway: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "authentik"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m0s",
            path="./cluster/k8s/authentik/app",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="10m0s",
            wait=True,
            # Health checks ensure Authentik is fully operational before dependents start
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="authentik", namespace="authentik"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="authentik-server", namespace="authentik"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="authentik-worker", namespace="authentik"
                ),
            ],
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=flux_kustomization_depends_on_many(
                authentik_namespace,
                # Wait for CNPG cluster ready and authentik-db-app secret to exist
                authentik_db,
                # Wait for cert-manager for TLS certificates
                cert_manager,
                # Wait for Gateway API for external access
                gateway,
                # the ServiceMonitor/PodMonitor CRD
                monitoring_crds,
            ),
        ),
    )


def authentik_db_backups(
    chart: Chart,
    authentik_db: Kustomization,
    cnpg: Kustomization,
    seaweedfs_cluster: Kustomization,
    authentik_namespace: Kustomization,
) -> Kustomization:
    name = "authentik-db-backups"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="15m",
            path="./cluster/k8s/authentik/db-backups",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="Bucket",
                    name="authentik-db-backups",
                    namespace="authentik",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Identity",
                    name="authentik-db-backups",
                    namespace="authentik",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="authentik-db-backups",
                    namespace="authentik",
                ),
                KustomizationSpecHealthChecks(
                    api_version="barmancloud.cnpg.io/v1",
                    kind="ObjectStore",
                    name="authentik-db-ovh",
                    namespace="authentik",
                ),
            ],
            depends_on=flux_kustomization_depends_on_many(authentik_db, cnpg, seaweedfs_cluster, authentik_namespace),
        ),
        description="Creates the Authentik CNPG backup schedule and its SeaweedFS storage.",
    )


def authentik_db(
    chart: Chart, cnpg: Kustomization, authentik_namespace: Kustomization, local_path_provisioner: Kustomization
) -> Kustomization:
    name = "authentik-db"
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
            path="./cluster/k8s/authentik/db",
            prune=False,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            depends_on=flux_kustomization_depends_on_many(cnpg, authentik_namespace, local_path_provisioner),
        ),
    )


def authentik_namespace(chart: Chart) -> Kustomization:
    name = "authentik-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="1h",
            path="./cluster/k8s/authentik/namespace",
            prune=False,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="1m",
        ),
    )


def authentik_proxy_routes(chart: Chart, gateway: Kustomization, authentik: Kustomization) -> Kustomization:
    name = "authentik-proxy-routes"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/authentik/proxy-routes",
            prune=False,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            depends_on=flux_kustomization_depends_on_many(gateway, authentik),
        ),
    )


def sso_providers_tf(
    chart: Chart, tofu_controller: Kustomization, tofu_state_db: Kustomization, authentik: Kustomization
) -> Kustomization:
    name = "sso-providers-tf"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/authentik/sso-providers-tf",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="sso-providers",
                    namespace="flux-system",
                )
            ],
            timeout="10m",
            depends_on=flux_kustomization_depends_on_many(tofu_controller, tofu_state_db, authentik),
        ),
    )
