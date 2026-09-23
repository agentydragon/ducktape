"""Flux Kustomizations for the cluster/k8s/haku slice."""

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
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
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
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path=artifact_path(artifact),
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            source_ref=artifact_source_ref(artifact),
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
            post_build=KustomizationSpecPostBuild(
                substitute_from=[
                    KustomizationSpecPostBuildSubstituteFrom(
                        kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                    )
                ]
            ),  # ${LETSENCRYPT_ISSUER}
        ),
    )


def haku_ui_image_webhook(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, haku_state: Kustomization
) -> Kustomization:
    name = "haku-ui-image-webhook"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            depends_on=[
                # haku-state provisions the forgejo-webhook-token Secret (the Receiver's secretRef)
                # and the Forgejo package webhook that targets this receiver.
                flux_kustomization_depends_on(haku_state)
            ],
        ),
    )
