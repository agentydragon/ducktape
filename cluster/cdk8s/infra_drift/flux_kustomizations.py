"""Flux Kustomizations for the cluster/k8s/infra-drift slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def infra_drift(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, tofu_controller: Kustomization, tofu_state_db: Kustomization
) -> Kustomization:
    name = "infra-drift"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            decryption=SOPS_DECRYPTION,
            # Ready tracks the plan, so a drift finding shows up here as a NotReady
            # Kustomization — README § Reading a plan.
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="infra-drift",
                    namespace="flux-system",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(tofu_controller, tofu_state_db),
        ),
    )
