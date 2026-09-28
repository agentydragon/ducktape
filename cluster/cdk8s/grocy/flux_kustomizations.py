"""Flux Kustomizations for the cluster/k8s/grocy slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def grocy_sf(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, volsync: Kustomization, kyverno: Kustomization
) -> Kustomization:
    name = "grocy-sf"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            volsync,
            # Kyverno's failurePolicy: Fail webhooks admit the Deployment, Job and Namespace.
            kyverno,
        ),
    )


def grocy_mcp_sf(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_operator: Kustomization,
    valkey: Kustomization,
    monitoring_crds: Kustomization,
    kyverno: Kustomization,
) -> Kustomization:
    name = "grocy-mcp-sf"
    return flux_kustomization(
        chart,
        name,
        artifact,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            external_secrets_operator,
            valkey,
            # the ServiceMonitor/PodMonitor CRD
            monitoring_crds,
            # Kyverno's failurePolicy: Fail webhooks admit the Deployment and HTTPRoute.
            kyverno,
        ),
    )


def grocy_sf_user_perms(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, grocy_sf: Kustomization
) -> Kustomization:
    name = "grocy-sf-user-perms"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        # bootstrap-never-converges: the Job's retries (no TTL) run from apply and Flux never recreates it.
        depends_on=flux_kustomization_depends_on_many(grocy_sf),
    )


def grocy_vallejo(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, volsync: Kustomization, kyverno: Kustomization
) -> Kustomization:
    name = "grocy-vallejo"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            volsync,
            # Kyverno's failurePolicy: Fail webhooks admit the Deployment, Job and Namespace.
            kyverno,
        ),
    )


def grocy_mcp_vallejo(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_operator: Kustomization,
    valkey: Kustomization,
    monitoring_crds: Kustomization,
    kyverno: Kustomization,
) -> Kustomization:
    name = "grocy-mcp-vallejo"
    return flux_kustomization(
        chart,
        name,
        artifact,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(
            external_secrets_operator,
            valkey,
            # the ServiceMonitor/PodMonitor CRD
            monitoring_crds,
            # Kyverno's failurePolicy: Fail webhooks admit the Deployment and HTTPRoute.
            kyverno,
        ),
    )


def grocy_vallejo_user_perms(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, grocy_vallejo: Kustomization
) -> Kustomization:
    name = "grocy-vallejo-user-perms"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        # bootstrap-never-converges: the Job's retries (no TTL) run from apply and Flux never recreates it.
        depends_on=flux_kustomization_depends_on_many(grocy_vallejo),
    )
