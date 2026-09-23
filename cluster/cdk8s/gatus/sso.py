"""Gatus's Authentik SSO provider (tf/gitops/gatus-sso)."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "gatus-sso"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/gatus/sso-tf"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(chart, "terraform", name=NAME, variables={})
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def gatus_sso_tf(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    authentik: Kustomization,
) -> Kustomization:
    name = "gatus-sso-tf"
    return flux_kustomization(
        chart,
        name,
        artifact,
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
        depends_on=flux_kustomization_depends_on_many(tofu_controller, tofu_state_db, authentik),
    )
