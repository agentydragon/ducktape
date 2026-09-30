"""The Authentik-provider Terraform CRs as one Flux unit: SSO providers, agent machine access,
Gatus's SSO provider and Alloy's OTLP bearer token."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s import agent_machine_access, terraform
from cluster.cdk8s.authentik import sso_providers
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gatus import sso
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import alloy_otlp_bearer_token

NAME = "authentik-tf"
OUTPUT_DIR = f"{GENERATED_ROOT}/authentik/tf"


def authentik_tf(chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        wait=None,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="infra.contrib.fluxcd.io/v1alpha2",
                kind="Terraform",
                name=name,
                namespace=terraform.NAMESPACE,
            )
            for name in (sso_providers.NAME, agent_machine_access.NAME, sso.NAME, alloy_otlp_bearer_token.NAME)
        ],
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(tofu_controller),
    )
