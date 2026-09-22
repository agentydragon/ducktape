"""Flux Kustomizations for the cluster/k8s/authentik slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def authentik(chart: Chart, cnpg: Kustomization, monitoring_crds: Kustomization) -> Kustomization:
    name = "authentik"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m0s",
            path="./cluster/k8s/authentik",
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
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
            decryption=SOPS_DECRYPTION,
            depends_on=flux_kustomization_depends_on_many(cnpg, monitoring_crds),
        ),
    )


def authentik_db_backups(chart: Chart, cnpg: Kustomization, seaweedfs_cluster: Kustomization) -> Kustomization:
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
            depends_on=flux_kustomization_depends_on_many(cnpg, seaweedfs_cluster),
        ),
        description="Creates the Authentik CNPG backup schedule and its SeaweedFS storage.",
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
