"""Flux Kustomizations for the cluster/k8s/haku slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import (
    CERT_MANAGER_ISSUER_SUBSTITUTION,
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)


def haku_mailbox(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cnpg: Kustomization,
    cert_manager: Kustomization,
    external_secrets_operator: Kustomization,
    cert_manager_issuer_config: Kustomization,
) -> Kustomization:
    name = "haku-mailbox"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(
            cnpg,
            # Certificate CRD + controller
            cert_manager,
            # ExternalSecret and ClusterExternalSecret CRDs and webhooks
            external_secrets_operator,
            # ${LETSENCRYPT_ISSUER}
            cert_manager_issuer_config,
        ),
        post_build=CERT_MANAGER_ISSUER_SUBSTITUTION,  # ${LETSENCRYPT_ISSUER}
    )
