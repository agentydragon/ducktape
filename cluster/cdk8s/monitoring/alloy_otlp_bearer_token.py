"""Alloy's OTLP bearer token (tf/gitops/alloy-otlp-bearer-token)."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "alloy-otlp-bearer-token"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/alloy-otlp-bearer-token-tf"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(chart, "terraform", name=NAME, variables=None)
    return chart


def alloy_otlp_bearer_token_tf(
    chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "alloy-otlp-bearer-token-tf",
        directory,
        wait=None,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="infra.contrib.fluxcd.io/v1alpha2",
                kind="Terraform",
                name=NAME,
                namespace=terraform.NAMESPACE,
            )
        ],
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(tofu_controller),
    )
