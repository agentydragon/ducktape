"""Flux Kustomizations for the cluster/k8s/github-branch-protection slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def github_branch_protection(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    github_secrets_sync_secrets: Kustomization,
) -> Kustomization:
    name = "github-branch-protection"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            timeout="10m",
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="github-branch-protection",
                    namespace="flux-system",
                )
            ],
            depends_on=flux_kustomization_depends_on_many(
                # tofu-controller runs the Terraform CR.
                tofu_controller,
                tofu_state_db,
                # Provides github-secrets-sync-pat (Administration:R/W on ducktape).
                github_secrets_sync_secrets,
            ),
        ),
    )
