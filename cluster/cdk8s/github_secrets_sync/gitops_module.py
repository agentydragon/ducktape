"""GitHub Actions secrets and variables for ducktape and gaffer-private
(tf/gitops/github-secrets-sync)."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "github-secrets-sync"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(chart, "terraform", name=NAME, variables={})
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"]))


def github_secrets_sync(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    github_secrets_sync_secrets: Kustomization,
    forgejo_images: Kustomization,
    seaweedfs_pr_visuals_bucket: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(
            tofu_controller,
            tofu_state_db,
            github_secrets_sync_secrets,
            # The Terraform module reads the canonical ducktape-ci registry credential
            # from forgejo-images before publishing it to gaffer-private's GitHub Actions
            # secrets.
            forgejo_images,
            seaweedfs_pr_visuals_bucket,
        ),
    )
