"""Flux Kustomizations for the cluster/k8s/forgejo-images slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def forgejo_images(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_config: Kustomization,
    forgejo: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
) -> Kustomization:
    name = "forgejo-images"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            source_ref=artifact_source_ref(artifact),
            decryption=SOPS_DECRYPTION,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="forgejo-images",
                    namespace="flux-system",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config,
                # Forgejo API must be up (provider target)
                forgejo,
                tofu_controller,
                tofu_state_db,
            ),
        ),
        description=(
            "ducktape-ci Forgejo registry tenant — shared credential (read by "
            "consumers, including flux-system, via per-namespace ExternalSecrets "
            "against kubernetes-forgejo-images-secret-store) + Terraform that "
            "provisions the Forgejo user."
        ),
    )
