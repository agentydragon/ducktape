"""Flux Kustomizations for the cluster/k8s/cert-manager slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifacts import artifact_source_ref, directory_artifact
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many

_CERT_MANAGER_DIR = "cluster/k8s/cert-manager/app"
_ENVIRONMENT_DIR = "cluster/k8s/cert-manager/environment"
_ISSUER_CONFIG_DIR = "cluster/k8s/cert-manager/issuer-config"


def cert_manager_artifact() -> ArtifactGeneratorSpecArtifacts:
    return directory_artifact("cert-manager", _CERT_MANAGER_DIR)


def cert_manager_environment_artifact() -> ArtifactGeneratorSpecArtifacts:
    return directory_artifact(
        "cert-manager-environment",
        _ENVIRONMENT_DIR,
        "cluster/k8s/cert-manager/config",
        "cluster/k8s/cert-manager/cluster-ca",
    )


def cert_manager_issuer_config_artifact() -> ArtifactGeneratorSpecArtifacts:
    return directory_artifact("cert-manager-issuer-config", _ISSUER_CONFIG_DIR)


def cert_manager(
    chart: Chart,
    *,
    artifact: ArtifactGeneratorSpecArtifacts,
    cert_manager_issuer_config: Kustomization,
    reflector: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        artifact.name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=f"./{_CERT_MANAGER_DIR}",
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
            depends_on=flux_kustomization_depends_on_many(
                cert_manager_issuer_config,
                # Produces the namespace-local ConfigMap that postBuild reads.
                reflector,
                # the ServiceMonitor/PodMonitor CRD
                monitoring_crds,
            ),
        ),
    )


def cert_manager_environment(
    chart: Chart,
    *,
    artifact: ArtifactGeneratorSpecArtifacts,
    cert_manager: Kustomization,
    cert_manager_trust: Kustomization,
    cert_manager_issuer_config: Kustomization,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        artifact.name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=f"./{_ENVIRONMENT_DIR}",
            prune=True,
            wait=True,
            post_build=KustomizationSpecPostBuild(
                substitute_from=[
                    KustomizationSpecPostBuildSubstituteFrom(
                        kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                    )
                ]
            ),
            depends_on=flux_kustomization_depends_on_many(
                cert_manager, cert_manager_trust, cert_manager_issuer_config, external_creds, external_secrets_config
            ),
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


def cert_manager_issuer_config(chart: Chart, *, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(
        chart,
        artifact.name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=f"./{_ISSUER_CONFIG_DIR}",
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
            depends_on=flux_kustomization_depends_on_many(
                cert_manager,
                # Kyverno VWC must be operational before creating resources
                kyverno,
            ),
        ),
    )
