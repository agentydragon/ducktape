"""Branch protection for the ducktape GitHub repository (tf/gitops/github-branch-protection)."""

from __future__ import annotations

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import terraform

NAME = "github-branch-protection"


def chart(app: App, module: ArtifactGeneratorSpecArtifacts) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(chart, "terraform", module=module, variables=None)
    return chart
