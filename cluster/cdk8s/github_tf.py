"""The GitHub-provider Terraform CRs as one Flux unit: ducktape's branch protection, the GitHub
Actions secrets sync and Flux's webhook token."""

from __future__ import annotations

from cdk8s import Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "github-tf"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"


def github_tf(chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, NAME, directory, timeout="10m", depends_on=flux_kustomization_depends_on_many(tofu_controller)
    )
