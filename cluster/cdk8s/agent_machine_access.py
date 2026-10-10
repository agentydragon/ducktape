"""Authentik machine identities and OAuth clients for agents and services (tf/gitops/agent-machine-access)."""

from __future__ import annotations

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts
from tofu_controller.io.fluxcd.contrib.infra import TerraformV1Alpha2SpecStoreReadablePlan

from cluster.cdk8s import terraform

NAME = "agent-machine-access"


def chart(app: App, module: ArtifactGeneratorSpecArtifacts) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(
        chart,
        "terraform",
        module=module,
        variables=None,
        store_readable_plan=TerraformV1Alpha2SpecStoreReadablePlan.HUMAN,
    )
    return chart
