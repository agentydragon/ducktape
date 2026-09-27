"""GitHub Actions secrets and variables for ducktape and gaffer-private
(tf/gitops/github-secrets-sync)."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "github-secrets-sync"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(chart, "terraform", name=NAME, variables=None)
    return chart


def github_secrets_sync(chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(tofu_controller),
    )
