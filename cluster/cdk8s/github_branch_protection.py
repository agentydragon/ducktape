"""Branch protection for the ducktape GitHub repository (tf/gitops/github-branch-protection)."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "github-branch-protection"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(chart, "terraform", name=NAME, variables=None)
    return chart


def github_branch_protection(
    chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(
            # tofu-controller runs the Terraform CR.
            tofu_controller
        ),
    )
