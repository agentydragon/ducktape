"""Flux Kustomizations for the cluster/k8s/seaweedfs slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def seaweedfs_cluster() -> dict[str, object]:
    name = "seaweedfs-cluster"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            path="./cluster/k8s/seaweedfs/cluster",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="seaweedfs-operator", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-secrets", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="seaweedfs-filer-db",  # Filer is configured with postgres2 backend.
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
            wait=False,
            timeout="5m",
        ),
    )


def seaweedfs_filer_db() -> dict[str, object]:
    name = "seaweedfs-filer-db"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/seaweedfs/db",
            prune=True,
            wait=True,
            # Required to apply seaweedfs-filer-db-ssd-creds.sops.yaml (the filer DB app creds
            # CNPG syncs onto the -ssd seaweedfs role); without it Flux applies the ciphertext.
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="seaweedfs-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
            ],
        ),
    )


def seaweedfs_drivefs_artifacts_bucket() -> dict[str, object]:
    name = "seaweedfs-drivefs-artifacts-bucket"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/seaweedfs/drivefs-artifacts-bucket",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux")],
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


def seaweedfs_external_credentials() -> dict[str, object]:
    name = "seaweedfs-external-credentials"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/seaweedfs/external-credentials",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="seaweedfs-secrets", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
            ],
            wait=True,
            timeout="5m",
        ),
        description="Externally managed SeaweedFS S3 credential source Secrets and grants.",
    )


def seaweedfs_forgejo_bucket() -> dict[str, object]:
    name = "seaweedfs-forgejo-bucket"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/seaweedfs/forgejo-bucket",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux")],
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


def seaweedfs_langfuse_bucket() -> dict[str, object]:
    name = "seaweedfs-langfuse-bucket"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/seaweedfs/langfuse-bucket",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux")],
            # The Bucket and S3Credentials moved to the Langfuse Kustomization and were
            # live-verified there. Keep this small Kustomization for the cluster-global
            # S3Identity that the namespaced S3Credentials references.
            wait=True,
            timeout="5m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="S3Identity", name="langfuse", namespace="seaweedfs"
                )
            ],
        ),
    )


def seaweedfs_loom_gym_bucket() -> dict[str, object]:
    name = "seaweedfs-loom-gym-bucket"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/seaweedfs/loom-gym-bucket",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux")],
            wait=True,
            timeout="5m",
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="loom-gym", namespace="seaweedfs"
                )
            ],
        ),
    )


def seaweedfs_monitoring() -> dict[str, object]:
    name = "seaweedfs-monitoring"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            path="./cluster/k8s/seaweedfs/monitoring",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # PrometheusRule
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def seaweedfs_namespace() -> dict[str, object]:
    name = "seaweedfs-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            path="./cluster/k8s/seaweedfs/namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
        ),
    )


def seaweedfs_operator() -> dict[str, object]:
    name = "seaweedfs-operator"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/seaweedfs/operator",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[KustomizationSpecDependsOn(name="seaweedfs-namespace", namespace="ducktape-flux")],
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


def seaweedfs_pr_visuals_bucket() -> dict[str, object]:
    name = "seaweedfs-pr-visuals-bucket"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="1h",
            retry_interval="1m",
            timeout="5m",
            path="./cluster/k8s/seaweedfs/pr-visuals-bucket",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux")],
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


def seaweedfs_public_coder_agent_backups_bucket() -> dict[str, object]:
    name = "seaweedfs-public-coder-agent-backups-bucket"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/seaweedfs/public-coder-agent-backups-bucket",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux")],
            wait=True,
            timeout="5m",
        ),
    )


def seaweedfs_public_s3() -> dict[str, object]:
    name = "seaweedfs-public-s3"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/seaweedfs/public-s3",
            prune=True,
            # Gate on Bucket CRs managed in this repo that the public identities target.
            # Claude and DriveFS identities authenticate through native IAM; static
            # gateway configuration now contains only the credential-free anonymous read.
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="seaweedfs-external-credentials", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-drivefs-artifacts-bucket", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="vm-images-publisher", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-secrets", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
            ],
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


def seaweedfs_registry_cache_bucket() -> dict[str, object]:
    name = "seaweedfs-registry-cache-bucket"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/seaweedfs/registry-cache-bucket",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux")],
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


def seaweedfs_secrets() -> dict[str, object]:
    name = "seaweedfs-secrets"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            suspend=False,
            interval="10m",
            path="./cluster/k8s/seaweedfs/secrets",
            prune=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="seaweedfs-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="external-secrets-operator",  # ExternalSecret + SecretStore CRDs + ESO controller
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/seaweedfs/cluster/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_cluster())
    path = root / "cluster/k8s/seaweedfs/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_filer_db())
    path = root / "cluster/k8s/seaweedfs/drivefs-artifacts-bucket/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_drivefs_artifacts_bucket())
    path = root / "cluster/k8s/seaweedfs/external-credentials/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_external_credentials())
    path = root / "cluster/k8s/seaweedfs/forgejo-bucket/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_forgejo_bucket())
    path = root / "cluster/k8s/seaweedfs/langfuse-bucket/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_langfuse_bucket())
    path = root / "cluster/k8s/seaweedfs/loom-gym-bucket/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_loom_gym_bucket())
    path = root / "cluster/k8s/seaweedfs/monitoring/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_monitoring())
    path = root / "cluster/k8s/seaweedfs/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_namespace())
    path = root / "cluster/k8s/seaweedfs/operator/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_operator())
    path = root / "cluster/k8s/seaweedfs/pr-visuals-bucket/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_pr_visuals_bucket())
    path = root / "cluster/k8s/seaweedfs/public-coder-agent-backups-bucket/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_public_coder_agent_backups_bucket())
    path = root / "cluster/k8s/seaweedfs/public-s3/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_public_s3())
    path = root / "cluster/k8s/seaweedfs/registry-cache-bucket/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_registry_cache_bucket())
    path = root / "cluster/k8s/seaweedfs/secrets/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, seaweedfs_secrets())
