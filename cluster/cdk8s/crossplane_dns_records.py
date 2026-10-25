"""Move the Route 53 domain registration root from tofu-controller to Crossplane OpenTofu."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from opentofu_providerconfig_crds.io.upbound.opentofu import ProviderConfig, ProviderConfigSpec
from opentofu_workspace_crds.io.upbound.opentofu import (
    Workspace,
    WorkspaceSpec,
    WorkspaceSpecForProvider,
    WorkspaceSpecForProviderEnv,
    WorkspaceSpecForProviderEnvSecretKeyRef,
    WorkspaceSpecForProviderVars,
    WorkspaceSpecProviderConfigRef,
)
from pydantic import BaseModel, ConfigDict

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.route53 import HOSTED_ZONES

NAME = "crossplane-dns-records"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
TF_MODULE_NAME = "dns-records"
PROVIDER_CONFIG_NAME = NAME
WORKSPACE_NAME = TF_MODULE_NAME
REMOTE_MODULE = f"git::https://github.com/agentydragon/ducktape.git//tf/gitops/{TF_MODULE_NAME}?ref=devel"
STATE_SCHEMA = terraform.gitops_state_schema(TF_MODULE_NAME)
_CREDENTIALS_SECRET = "aws-route53-credentials"
_NAMESPACE = "flux-system"


class DnsRecordsVars(BaseModel):
    """The inputs of tf/gitops/dns-records; `aws_region` keeps its variables.tf default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route53_zone_id: str


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    ProviderConfig(
        chart,
        "provider-config",
        metadata=ApiObjectMetadata(name=PROVIDER_CONFIG_NAME),
        spec=ProviderConfigSpec(
            configuration='terraform {\n  backend "pg" {}\n}\n',
            backend_file=(
                f'conn_str = "{terraform.STATE_DATABASE_CONNECTION_STRING}"\nschema_name = "{STATE_SCHEMA}"\n'
            ),
        ),
    )
    variables = DnsRecordsVars(route53_zone_id=HOSTED_ZONES["allegedly.works"])
    Workspace(
        chart,
        "workspace",
        metadata=ApiObjectMetadata(name=WORKSPACE_NAME, annotations={"crossplane.io/external-name": "default"}),
        spec=WorkspaceSpec(
            deletion_policy="Orphan",
            # Do not grant the Workspace Delete: removing it from Flux must leave Route 53 and state intact.
            management_policies=["Observe", "Create", "Update"],
            provider_config_ref=WorkspaceSpecProviderConfigRef(name=PROVIDER_CONFIG_NAME),
            for_provider=WorkspaceSpecForProvider(
                source="Remote",
                module=REMOTE_MODULE,
                remote_pull_policy="Always",
                plan_args=["-lock-timeout=5m"],
                vars=[
                    WorkspaceSpecForProviderVars(key=key, value=value) for key, value in variables.model_dump().items()
                ],
                env=[
                    WorkspaceSpecForProviderEnv(
                        name="PGPASSWORD",
                        secret_key_ref=WorkspaceSpecForProviderEnvSecretKeyRef(
                            name=terraform.STATE_DATABASE_PASSWORD.secret.name,
                            namespace=terraform.STATE_DATABASE_PASSWORD.secret.namespace,
                            key=terraform.STATE_DATABASE_PASSWORD.key,
                        ),
                    ),
                    *[
                        WorkspaceSpecForProviderEnv(
                            name=key,
                            secret_key_ref=WorkspaceSpecForProviderEnvSecretKeyRef(
                                name=_CREDENTIALS_SECRET, namespace=_NAMESPACE, key=key
                            ),
                        )
                        for key in ("AWS_ACCESS_KEY_ID", "AWS_REGION", "AWS_SECRET_ACCESS_KEY")
                    ],
                ],
            ),
        ),
    )
    return chart


def resources(
    flux_chart: Chart,
    directory: RenderedDirectory,
    opentofu_provider: Kustomization,
    tofu_state_db: Kustomization,
    dns_automation: Kustomization,
) -> Kustomization:
    """Start Crossplane only after tofu-controller has pruned the old Terraform root."""
    return flux_kustomization(
        flux_chart,
        NAME,
        directory,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(opentofu_provider, tofu_state_db, dns_automation),
        description=(
            "Take over dns-records from tofu-controller using its existing PostgreSQL state; "
            "the dependency on dns-automation serializes the controller handoff."
        ),
    )
