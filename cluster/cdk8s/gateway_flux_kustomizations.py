"""Flux Kustomizations for the cluster/k8s/gateway slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def gateway(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cert_manager: Kustomization,
    kyverno: Kustomization,
    cert_manager_issuer_config: Kustomization,
) -> Kustomization:
    name = "gateway"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            depends_on=flux_kustomization_depends_on_many(cert_manager, kyverno, cert_manager_issuer_config),
            post_build=KustomizationSpecPostBuild(
                substitute_from=[
                    KustomizationSpecPostBuildSubstituteFrom(
                        kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                    )
                ]
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="gateway.networking.k8s.io/v1",
                    kind="Gateway",
                    name="cluster-gateway",
                    namespace="gateway-system",
                )
            ],
        ),
    )
