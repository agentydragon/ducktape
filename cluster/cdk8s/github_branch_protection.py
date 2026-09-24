"""Branch protection for the ducktape GitHub repository (tf/gitops/github-branch-protection)."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "github-branch-protection"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(chart, "terraform", name=NAME, variables={})
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def github_branch_protection(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    github_secrets_sync_secrets: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(
            # tofu-controller runs the Terraform CR.
            tofu_controller,
            tofu_state_db,
            # Provides github-secrets-sync-pat (Administration:R/W on ducktape).
            github_secrets_sync_secrets,
        ),
    )
