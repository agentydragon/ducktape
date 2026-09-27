"""GitHub Actions secrets and variables for ducktape and gaffer-private
(tf/gitops/github-secrets-sync)."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import terraform

NAME = "github-secrets-sync"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(chart, "terraform", name=NAME, variables=None)
    return chart
