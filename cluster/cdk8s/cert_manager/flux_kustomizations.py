"""Flux Kustomizations for the cluster/k8s/cert-manager slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def cert_manager() -> dict[str, object]:
    name = "cert-manager"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/cert-manager/app",
            prune=True,
            wait=True,
            # Health check ensures cert-manager pods are ready before dependents try to create Certificates
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="cert-manager",
                    namespace="cert-manager",
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="cert-manager", namespace="cert-manager"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="cert-manager-webhook", namespace="cert-manager"
                ),
            ],
            post_build=KustomizationSpecPostBuild(
                substitute_from=[
                    KustomizationSpecPostBuildSubstituteFrom(
                        kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                    )
                ]
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="cert-manager-issuer-config", namespace="ducktape-flux"),
                # Produces the namespace-local ConfigMap that postBuild reads.
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # the ServiceMonitor/PodMonitor CRD
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def cert_manager_environment() -> dict[str, object]:
    name = "cert-manager-environment"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/cert-manager/environment",
            prune=True,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            post_build=KustomizationSpecPostBuild(
                substitute_from=[
                    KustomizationSpecPostBuildSubstituteFrom(
                        kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                    )
                ]
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="cert-manager", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-trust", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-issuer-config", namespace="ducktape-flux"),
            ],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="cert-manager.io/v1", kind="ClusterIssuer", name="letsencrypt-prod", namespace=""
                ),
                KustomizationSpecHealthChecks(
                    api_version="cert-manager.io/v1", kind="ClusterIssuer", name="letsencrypt-staging", namespace=""
                ),
            ],
        ),
    )


def cert_manager_issuer_config() -> dict[str, object]:
    name = "cert-manager-issuer-config"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/cert-manager/issuer-config",
            prune=True,
            wait=True,
        ),
    )


def cert_manager_trust() -> dict[str, object]:
    name = "cert-manager-trust"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/cert-manager/trust",
            prune=True,
            wait=True,
            # Health check ensures trust-manager is ready before ClusterIssuers depend on it
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="trust-manager",
                    namespace="cert-manager",
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="cert-manager", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="kyverno",  # Kyverno VWC must be operational before creating resources
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/cert-manager/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, cert_manager())
    path = root / "cluster/k8s/cert-manager/environment/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, cert_manager_environment())
    path = root / "cluster/k8s/cert-manager/issuer-config/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, cert_manager_issuer_config())
    path = root / "cluster/k8s/cert-manager/trust/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, cert_manager_trust())
