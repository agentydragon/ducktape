"""Crossplane OpenTofu Workspace for CDviz's GitHub repository webhook."""

from __future__ import annotations

from cdk8s import App, Chart
from opentofu_workspace_crds.io.upbound.opentofu import WorkspaceSpecForProviderVars
from pydantic import BaseModel, ConfigDict

from cluster.cdk8s import opentofu, terraform
from cluster.cdk8s.cdviz import config
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "cdviz-github-webhook"
WORKSPACE_NAME = f"crossplane-{NAME}"
OUTPUT_DIR = f"{GENERATED_ROOT}/github-tf/{WORKSPACE_NAME}"
KUBERNETES_IDENTITY = f"opentofu-{NAME}"


class CdvizGithubWebhookVars(BaseModel):
    """Inputs of tf/gitops/cdviz-github-webhook."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    namespace: str
    webhook_secret_name: str
    webhook_url: str


def chart(app: App) -> Chart:
    chart = Chart(app, WORKSPACE_NAME, disable_resource_name_hashes=True)
    identity = opentofu.kubernetes_service_account(chart, KUBERNETES_IDENTITY)
    opentofu.secret_access(
        chart, "flux-system-secrets", identity=identity, namespace="flux-system", read=["github-secrets-sync-pat"]
    )
    opentofu.secret_access(
        chart, "cdviz-secrets", identity=identity, namespace=config.NAMESPACE, write=[config.WEBHOOK_SECRET.name]
    )
    variables = CdvizGithubWebhookVars(
        namespace=config.NAMESPACE, webhook_secret_name=config.WEBHOOK_SECRET.name, webhook_url=config.WEBHOOK_URL
    )
    opentofu.gitops_workspace(
        chart,
        "workspace",
        name=NAME,
        provider_config_name=WORKSPACE_NAME,
        schema=terraform.gitops_state_schema(NAME),
        directory=f"tf/gitops/{NAME}",
        management_policies=["Observe", "Create", "Update"],
        # Creating a named workspace initializes backend state before the provider's first state-list observation.
        external_name=NAME,
        kubernetes_identity=KUBERNETES_IDENTITY,
        variables=[WorkspaceSpecForProviderVars(key=key, value=value) for key, value in variables.model_dump().items()],
    )
    return chart
