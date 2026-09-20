"""Flux Kustomizations for the cluster/k8s/seaweedfs slice."""

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

from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
    flux_kustomization_depends_on_many,
)


def seaweedfs_cluster(
    chart: Chart,
    seaweedfs_operator: Kustomization,
    seaweedfs_secrets: Kustomization,
    seaweedfs_filer_db: Kustomization,
    local_path_provisioner: Kustomization,
) -> Kustomization:
    name = "seaweedfs-cluster"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=flux_kustomization_depends_on_many(
                seaweedfs_operator,
                seaweedfs_secrets,
                # Filer is configured with postgres2 backend.
                seaweedfs_filer_db,
                local_path_provisioner,
            ),
            wait=False,
            timeout="5m",
        ),
    )


def seaweedfs_filer_db(chart: Chart, seaweedfs_namespace: Kustomization, cnpg: Kustomization) -> Kustomization:
    name = "seaweedfs-filer-db"
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
            path="./",
            prune=True,
            wait=True,
            # Required to apply seaweedfs-filer-db-ssd-creds.sops.yaml (the filer DB app creds
            # CNPG syncs onto the -ssd seaweedfs role); without it Flux applies the ciphertext.
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=flux_kustomization_depends_on_many(seaweedfs_namespace, cnpg),
        ),
    )


def seaweedfs_drivefs_artifacts_bucket(chart: Chart, seaweedfs_cluster: Kustomization) -> Kustomization:
    name = "seaweedfs-drivefs-artifacts-bucket"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
            wait=True,
            timeout="5m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="Bucket",
                    name="drivefs-artifacts",
                    namespace="seaweedfs",
                )
            ],
        ),
    )


def seaweedfs_external_credentials(
    chart: Chart, seaweedfs_secrets: Kustomization, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "seaweedfs-external-credentials"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=flux_kustomization_depends_on_many(seaweedfs_secrets, seaweedfs_cluster),
            wait=True,
            timeout="5m",
        ),
        description="Externally managed SeaweedFS S3 credential source Secrets and grants.",
    )


def seaweedfs_forgejo_bucket(chart: Chart, seaweedfs_cluster: Kustomization) -> Kustomization:
    name = "seaweedfs-forgejo-bucket"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
            # The Bucket and S3Credentials moved to the Forgejo Kustomization and were
            # live-verified there. Keep this small Kustomization for the cluster-global
            # S3Identity that the namespaced S3Credentials references.
            wait=True,
            timeout="5m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="S3Identity", name="forgejo", namespace="seaweedfs"
                )
            ],
        ),
    )


def seaweedfs_loom_gym_bucket(chart: Chart, seaweedfs_cluster: Kustomization) -> Kustomization:
    name = "seaweedfs-loom-gym-bucket"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
            wait=True,
            timeout="5m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="loom-gym", namespace="seaweedfs"
                )
            ],
        ),
    )


def seaweedfs_monitoring(
    chart: Chart, seaweedfs_cluster: Kustomization, monitoring_crds: Kustomization
) -> Kustomization:
    name = "seaweedfs-monitoring"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=flux_kustomization_depends_on_many(
                seaweedfs_cluster,
                # PrometheusRule
                monitoring_crds,
            ),
        ),
    )


def seaweedfs_namespace(chart: Chart) -> Kustomization:
    name = "seaweedfs-namespace"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
        ),
    )


def seaweedfs_operator(chart: Chart, seaweedfs_namespace: Kustomization) -> Kustomization:
    name = "seaweedfs-operator"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            retry_interval="1m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[flux_kustomization_depends_on(seaweedfs_namespace)],
            wait=True,
            timeout="5m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="seaweedfs-operator",
                    namespace="seaweedfs",
                )
            ],
        ),
    )


def seaweedfs_pr_visuals_bucket(chart: Chart, seaweedfs_cluster: Kustomization) -> Kustomization:
    name = "seaweedfs-pr-visuals-bucket"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="1h",
            retry_interval="1m",
            timeout="5m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="pr-visuals", namespace="flux-system"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="pr-visuals-writer",
                    namespace="flux-system",
                ),
            ],
        ),
    )


def seaweedfs_public_coder_agent_backups_bucket(chart: Chart, seaweedfs_cluster: Kustomization) -> Kustomization:
    name = "seaweedfs-public-coder-agent-backups-bucket"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
            wait=True,
            timeout="5m",
        ),
    )


def seaweedfs_public_s3(
    chart: Chart,
    seaweedfs_external_credentials: Kustomization,
    seaweedfs_drivefs_artifacts_bucket: Kustomization,
    vm_images_publisher: Kustomization,
    seaweedfs_secrets: Kustomization,
    seaweedfs_cluster: Kustomization,
    gateway: Kustomization,
) -> Kustomization:
    name = "seaweedfs-public-s3"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./",
            prune=True,
            # Gate on Bucket CRs managed in this repo that the public identities target.
            # Claude and DriveFS identities authenticate through native IAM; static
            # gateway configuration now contains only the credential-free anonymous read.
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=flux_kustomization_depends_on_many(
                seaweedfs_external_credentials,
                seaweedfs_drivefs_artifacts_bucket,
                vm_images_publisher,
                seaweedfs_secrets,
                seaweedfs_cluster,
                gateway,
            ),
            wait=True,
            timeout="5m",
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="public-s3", namespace="seaweedfs"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Identity",
                    name="claude-reader",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Identity",
                    name="drivefs-artifacts-writer",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Identity",
                    name="drivefs-artifacts-reader",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="claude-reader",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="drivefs-artifacts-writer",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="drivefs-artifacts-reader",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Policy",
                    name="claude-reader-buckets",
                    namespace="seaweedfs",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3PolicyBinding",
                    name="claude-reader-buckets",
                    namespace="seaweedfs",
                ),
            ],
        ),
    )


def seaweedfs_registry_cache_bucket(chart: Chart, seaweedfs_cluster: Kustomization) -> Kustomization:
    name = "seaweedfs-registry-cache-bucket"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
            wait=True,
            timeout="5m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="registry-cache", namespace="oci-cache"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="registry-cache",
                    namespace="oci-cache",
                ),
            ],
        ),
    )


def seaweedfs_secrets(
    chart: Chart, seaweedfs_namespace: Kustomization, external_secrets_operator: Kustomization
) -> Kustomization:
    name = "seaweedfs-secrets"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            path="./",
            prune=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=flux_kustomization_depends_on_many(
                seaweedfs_namespace,
                # ExternalSecret + SecretStore CRDs + ESO controller
                external_secrets_operator,
            ),
        ),
    )
