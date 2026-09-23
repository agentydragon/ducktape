"""Flux Kustomizations for the cluster/k8s/atuin slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def atuin(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cert_manager_issuer_config: Kustomization,
    cnpg: Kustomization,
) -> Kustomization:
    name = "atuin"
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
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            post_build=KustomizationSpecPostBuild(
                substitute_from=[
                    KustomizationSpecPostBuildSubstituteFrom(
                        kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                    )
                ]
            ),
            depends_on=flux_kustomization_depends_on_many(cert_manager_issuer_config, cnpg),
        ),
    )


def atuin_user_provisioner(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, atuin: Kustomization, user_agentydragon: Kustomization
) -> Kustomization:
    name = "atuin-user-provisioner"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            depends_on=flux_kustomization_depends_on_many(atuin, user_agentydragon),
        ),
    )
