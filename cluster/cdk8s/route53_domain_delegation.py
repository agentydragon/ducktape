"""Manage Route 53 domain registration and delegation through Crossplane OpenTofu."""

from __future__ import annotations

from cdk8s import App, Chart
from opentofu_workspace_crds.io.upbound.opentofu import WorkspaceSpecForProviderVars
from pydantic import BaseModel, ConfigDict

from cluster.cdk8s import opentofu
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.route53 import HOSTED_ZONES
from cluster.cdk8s.secret_ref import SecretRef

NAME = "crossplane-dns-records"
OUTPUT_DIR = f"{GENERATED_ROOT}/route53-domain-delegation"
TF_MODULE_NAME = "route53-domain-delegation"
# Keep the existing backend schema through the root rename so Crossplane uses the same state.
STATE_SCHEMA = "dns_records"
_CREDENTIALS_SECRET = "aws-route53-credentials"
_NAMESPACE = "flux-system"


class Route53DomainDelegationVars(BaseModel):
    """The inputs of tf/gitops/route53-domain-delegation; `aws_region` keeps its default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route53_zone_id: str


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    variables = Route53DomainDelegationVars(route53_zone_id=HOSTED_ZONES["allegedly.works"])
    opentofu.gitops_workspace(
        chart,
        "workspace",
        name=TF_MODULE_NAME,
        provider_config_name=NAME,
        schema=STATE_SCHEMA,
        management_policies=["Observe", "Create", "Update"],
        variables=[WorkspaceSpecForProviderVars(key=key, value=value) for key, value in variables.model_dump().items()],
        env=[
            opentofu.secret_env(key, SecretRef(namespace=_NAMESPACE, name=_CREDENTIALS_SECRET).key(key))
            for key in ("AWS_ACCESS_KEY_ID", "AWS_REGION", "AWS_SECRET_ACCESS_KEY")
        ],
    )
    return chart
