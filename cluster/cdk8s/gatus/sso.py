"""Gatus's Authentik SSO provider (tf/gitops/gatus-sso)."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy, KustomizationSpecHealthChecks
from tofu_controller.io.fluxcd.contrib.infra import TerraformV1Alpha2SpecStoreReadablePlan

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "gatus-sso"
OUTPUT_DIR = f"{GENERATED_ROOT}/gatus/sso-tf"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(
        chart, "terraform", name=NAME, variables=None, store_readable_plan=TerraformV1Alpha2SpecStoreReadablePlan.HUMAN
    )
    return chart


def gatus_sso_tf(chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization) -> Kustomization:
    name = "gatus-sso-tf"
    return flux_kustomization(
        chart,
        name,
        directory,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
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
