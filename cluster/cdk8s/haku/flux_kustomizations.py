"""Flux Kustomizations for the cluster/k8s/haku slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many


def haku_kube_api_proxy_secret_prune(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, haku_console: Kustomization
) -> Kustomization:
    """Temporarily track the retained Haku proxy TLS Secret so Flux can prune it."""
    return flux_kustomization(
        chart,
        "haku-kube-api-proxy-secret-prune",
        artifact,
        timeout="5m",
        prune=True,
        deletion_policy=KustomizationSpecDeletionPolicy.DELETE,
        depends_on=flux_kustomization_depends_on_many(haku_console),
        description=(
            "Temporarily tracks the retired Haku Kubernetes API proxy's cert-manager TLS Secret with the Merge "
            "apply policy so its data is preserved until this Kustomization is removed."
        ),
    )


def haku_mailbox(
    chart: Chart,
    directory: RenderedDirectory,
    cnpg: Kustomization,
    cert_manager: Kustomization,
    external_secrets_operator: Kustomization,
    seaweedfs_operator: Kustomization,
) -> Kustomization:
    name = "haku-mailbox"
    return flux_kustomization(
        chart,
        name,
        directory,
        timeout="5m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(
            cnpg,
            # Certificate CRD + controller
            cert_manager,
            # ExternalSecret and ClusterExternalSecret CRDs and webhooks
            external_secrets_operator,
            seaweedfs_operator,
        ),
    )
