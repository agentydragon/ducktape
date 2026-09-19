"""Flux Kustomizations for the cluster/k8s/authentik slice."""

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


def authentik() -> dict[str, object]:
    name = "authentik"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="authentik-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="authentik-db",  # Wait for CNPG cluster ready and authentik-db-app secret to exist
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="cert-manager",  # Wait for cert-manager for TLS certificates
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="gateway",  # Wait for Gateway API for external access
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # the ServiceMonitor/PodMonitor CRD
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def authentik_db_backups() -> dict[str, object]:
    name = "authentik-db-backups"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="authentik-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik-namespace", namespace="ducktape-flux"),
            ],
        ),
        description="Creates the Authentik CNPG backup schedule and its SeaweedFS storage.",
    )


def authentik_db() -> dict[str, object]:
    name = "authentik-db"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/authentik/db",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )


def authentik_namespace() -> dict[str, object]:
    name = "authentik-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="1h",
            path="./cluster/k8s/authentik/namespace",
            prune=False,  # Don't delete namespace on kustomization removal
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="1m",
        ),
    )


def authentik_proxy_routes() -> dict[str, object]:
    name = "authentik-proxy-routes"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/authentik/proxy-routes",
            prune=True,
            depends_on=[
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik", namespace="ducktape-flux"),
            ],
        ),
    )


def sso_providers_tf() -> dict[str, object]:
    name = "sso-providers-tf"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik", namespace="ducktape-flux"),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/authentik/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, authentik())
    path = root / "cluster/k8s/authentik/db-backups/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, authentik_db_backups())
    path = root / "cluster/k8s/authentik/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, authentik_db())
    path = root / "cluster/k8s/authentik/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, authentik_namespace())
    path = root / "cluster/k8s/authentik/proxy-routes/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, authentik_proxy_routes())
    path = root / "cluster/k8s/authentik/sso-providers-tf/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, sso_providers_tf())
