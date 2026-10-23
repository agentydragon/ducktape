"""Move the Forgejo SSH-key root from tofu-controller to Crossplane OpenTofu."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from opentofu_providerconfig_crds.io.upbound.opentofu import ProviderConfig, ProviderConfigSpec
from opentofu_workspace_crds.io.upbound.opentofu import (
    Workspace,
    WorkspaceSpec,
    WorkspaceSpecForProvider,
    WorkspaceSpecForProviderEnv,
    WorkspaceSpecForProviderEnvSecretKeyRef,
    WorkspaceSpecProviderConfigRef,
)

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "crossplane-forgejo-agentydragon"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
TF_MODULE_NAME = "forgejo-agentydragon"
PROVIDER_CONFIG_NAME = NAME
WORKSPACE_NAME = TF_MODULE_NAME
REMOTE_MODULE = f"git::https://github.com/agentydragon/ducktape.git//tf/gitops/{TF_MODULE_NAME}?ref=devel"
STATE_SCHEMA = terraform.gitops_state_schema(TF_MODULE_NAME)


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
    Workspace(
        chart,
        "workspace",
        metadata=ApiObjectMetadata(name=WORKSPACE_NAME, annotations={"crossplane.io/external-name": "default"}),
        spec=WorkspaceSpec(
            deletion_policy="Orphan",
            # Crossplane takes over observe/create/update. Omitting Delete means removing
            # this Workspace from Flux cannot destroy the four existing Forgejo keys.
            management_policies=["Observe", "Create", "Update"],
            provider_config_ref=WorkspaceSpecProviderConfigRef(name=PROVIDER_CONFIG_NAME),
            for_provider=WorkspaceSpecForProvider(
                source="Remote",
                module=REMOTE_MODULE,
                remote_pull_policy="Always",
                plan_args=["-lock-timeout=5m"],
                env=[
                    WorkspaceSpecForProviderEnv(
                        name="PGPASSWORD",
                        secret_key_ref=WorkspaceSpecForProviderEnvSecretKeyRef(
                            name=terraform.STATE_DATABASE_PASSWORD.secret.name,
                            namespace=terraform.STATE_DATABASE_PASSWORD.secret.namespace,
                            key=terraform.STATE_DATABASE_PASSWORD.key,
                        ),
                    )
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
    forgejo_gitops: Kustomization,
) -> Kustomization:
    """Start Crossplane only after tofu-controller has pruned the old Terraform root."""
    return flux_kustomization(
        flux_chart,
        NAME,
        directory,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(opentofu_provider, tofu_state_db, forgejo_gitops),
        description=(
            "Take over forgejo-agentydragon from tofu-controller using its existing PostgreSQL state; "
            "the dependency on forgejo-gitops serializes the controller handoff."
        ),
    )
