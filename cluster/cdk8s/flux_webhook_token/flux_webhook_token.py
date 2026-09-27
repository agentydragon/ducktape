"""Flux's GitHub webhook token (tf/gitops/flux-webhook-token)."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "flux-webhook-token"
OUTPUT_DIR = f"{GENERATED_ROOT}/flux-webhook-token"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(chart, "terraform", name=NAME, variables=None)
    return chart


def flux_webhook_token(chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, NAME, directory, timeout="5m", depends_on=flux_kustomization_depends_on_many(tofu_controller)
    )
