"""Flux Kustomizations for the cluster/k8s/matrix slice."""

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


def matrix() -> dict[str, object]:
    name = "matrix"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="matrix-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="matrix-db"  # externalPostgresql reads the CNPG-generated matrix-db-app secret
                ),
                KustomizationSpecDependsOn(
                    name="sso-providers-tf",  # writes matrix-oidc-config into the authentik namespace
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="reflector"  # mirrors matrix-oidc-config into the matrix namespace
                ),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="local-path-provisioner",  # local-path-proxmox media store PVC
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def matrix_db() -> dict[str, object]:
    name = "matrix-db"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="matrix-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )


def matrix_namespace() -> dict[str, object]:
    name = "matrix-namespace"
    return flux_kustomization(
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


def matrix_user_provisioner() -> dict[str, object]:
    name = "matrix-user-provisioner"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="matrix"  # Synapse is deployed and healthy; also carries the registration shared secret, admin and bot passwords
                ),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/matrix/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, matrix())
    path = root / "cluster/k8s/matrix/db/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, matrix_db())
    path = root / "cluster/k8s/matrix/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, matrix_namespace())
    path = root / "cluster/k8s/matrix/user-provisioner/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, matrix_user_provisioner())
