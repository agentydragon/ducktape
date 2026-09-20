"""Flux Kustomizations for the cluster/k8s/cert-manager slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecHealthChecks,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def cert_manager(
    chart: Chart, cert_manager_issuer_config: Kustomization, reflector: Kustomization, monitoring_crds: Kustomization
) -> Kustomization:
    name = "cert-manager"
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
                flux_kustomization_depends_on(cert_manager_issuer_config),
                # Produces the namespace-local ConfigMap that postBuild reads.
                flux_kustomization_depends_on(reflector),
                # the ServiceMonitor/PodMonitor CRD
                flux_kustomization_depends_on(monitoring_crds),
            ],
        ),
    )


def cert_manager_environment(
    chart: Chart,
    cert_manager: Kustomization,
    cert_manager_trust: Kustomization,
    cert_manager_issuer_config: Kustomization,
) -> Kustomization:
    name = "cert-manager-environment"
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
                flux_kustomization_depends_on(cert_manager),
                flux_kustomization_depends_on(cert_manager_trust),
                flux_kustomization_depends_on(cert_manager_issuer_config),
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


def cert_manager_issuer_config(chart: Chart) -> Kustomization:
    name = "cert-manager-issuer-config"
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
            path="./cluster/k8s/cert-manager/issuer-config",
            prune=True,
            wait=True,
        ),
    )


def cert_manager_trust(chart: Chart, cert_manager: Kustomization, kyverno: Kustomization) -> Kustomization:
    name = "cert-manager-trust"
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
                flux_kustomization_depends_on(cert_manager),
                # Kyverno VWC must be operational before creating resources
                flux_kustomization_depends_on(kyverno),
            ],
        ),
    )
