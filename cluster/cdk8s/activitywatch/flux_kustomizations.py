"""Flux Kustomizations for the cluster/k8s/activitywatch slice."""

from __future__ import annotations

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def activitywatch(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    local_path_provisioner: Kustomization,
) -> Kustomization:
    name = "activitywatch"
    return flux_kustomization(
        chart,
        name,
        # Revived 2026-08-26: the central aw-server is fed by the repo-owned
        # instance-to-instance importer (@ducktape_activitywatch//importer) over a
        # bearer-gated write route, replacing aw-sync. The cluster Syncthing receiver,
        # importer cronjob, and desktop Syncthing transport are gone.
        artifact,
        timeout="5m",
        decryption=SOPS_DECRYPTION,
        # Only local-path-proxmox (activitywatch-data) is used now that Syncthing and its
        # seaweedfs sync-inbox are gone -- so no seaweedfs-csi dependency, which otherwise
        # blocks the revive whenever seaweedfs-csi is degraded.
        depends_on=flux_kustomization_depends_on_many(external_secrets_config, forgejo_images, local_path_provisioner),
    )
