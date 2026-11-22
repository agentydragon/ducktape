"""Inline OpenTofu roots from native HCL files shipped as Bazel runfiles.

Design, source-change path and Kubernetes identities: cluster/docs/crossplane-opentofu-artifact-triggers.md.
"""

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ApiResource, Role, RolePolicyRule, Secret, ServiceAccount
from constructs import Construct
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
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
from python.runfiles import Runfiles
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import flux
from cluster.cdk8s.flux import (
    Kustomization,
    RenderedDirectory,
    artifact_directory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.secret_ref import SecretKey
from cluster.cdk8s.tofu_state import db

NAMESPACE = "crossplane-system"
RUNTIME_SERVICE_ACCOUNT = "crossplane-opentofu"
KUBECONFIG_DIRECTORY = "/etc/opentofu/kubernetes"
_WORKSPACE_HEALTH_CHECK_EXPR = KustomizationSpecHealthCheckExprs(
    api_version=Workspace.GVK.api_version,
    kind=Workspace.GVK.kind,
    current=(
        "has(status.conditions) && "
        "status.conditions.exists(c, has(c.type) && c.type == 'Synced' && "
        "has(c.status) && c.status == 'True' && has(c.observedGeneration) && "
        "c.observedGeneration == metadata.generation) && "
        "status.conditions.exists(c, has(c.type) && c.type == 'Ready' && has(c.status) && c.status == 'True')"
    ),
)


def inline_module(directory: str) -> str:
    """Combine an ordinary HCL root; the consumer declares its *.tf files as data.

    This does not package local modules, auxiliary files, JSON, or override semantics.
    """
    sources = sorted((Runfiles.CreateOrRaise().root() / f"ducktape/{directory}").glob("*.tf"))
    if not sources:
        raise ValueError(f"No Terraform source files in {directory}")
    return "\n".join(source.read_text() for source in sources)


def secret_env(name: str, key: SecretKey) -> WorkspaceSpecForProviderEnv:
    return WorkspaceSpecForProviderEnv(
        name=name,
        secret_key_ref=WorkspaceSpecForProviderEnvSecretKeyRef(
            name=key.secret.name, namespace=key.secret.namespace, key=key.key
        ),
    )


def gitops_workspace(
    scope: Construct,
    id: str,
    *,
    name: str,
    provider_config_name: str,
    schema: str | None = None,
    management_policies: Sequence[str],
    external_name: str = "default",
    kubernetes_identity: str | None = None,
    variables: Sequence[WorkspaceSpecForProviderVars] = (),
    env: Sequence[WorkspaceSpecForProviderEnv] = (),
) -> Workspace:
    """Use PostgreSQL state; callers choose the workspace, lifecycle and Kubernetes identity."""
    if schema is None:
        schema = db.gitops_state_schema(name)
    configuration = 'terraform {\n  backend "pg" {}\n}\n'
    if kubernetes_identity is not None:
        configuration += (
            f'provider "kubernetes" {{\n  config_path = "{KUBECONFIG_DIRECTORY}/{kubernetes_identity}"\n}}\n'
        )
    ProviderConfig(
        scope,
        f"{id}-provider-config",
        metadata=ApiObjectMetadata(name=provider_config_name),
        spec=ProviderConfigSpec(
            configuration=configuration,
            backend_file=f'conn_str = "{db.STATE_CONNECTION_STRING}"\nschema_name = "{schema}"\n',
        ),
    )
    return Workspace(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, annotations={"crossplane.io/external-name": external_name}),
        spec=WorkspaceSpec(
            deletion_policy="Orphan",
            management_policies=management_policies,
            provider_config_ref=WorkspaceSpecProviderConfigRef(name=provider_config_name),
            for_provider=WorkspaceSpecForProvider(
                source="Inline",
                module=inline_module(f"tf/gitops/{name}"),
                plan_args=["-lock-timeout=5m"],
                vars=list(variables) or None,
                env=[secret_env("PGPASSWORD", db.STATE_PASSWORD), *env],
            ),
        ),
    )


def kubernetes_service_account(scope: Construct, name: str) -> ServiceAccount:
    """An impersonated identity; no token Secret or token-minting permission is created."""
    identity = ServiceAccount(
        scope, f"{name}-identity", metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE), automount_token=False
    )
    role = Role(
        scope,
        f"{name}-impersonate",
        metadata=ApiObjectMetadata(name=f"{name}-impersonate", namespace=NAMESPACE),
        rules=[RolePolicyRule(resources=[identity], verbs=["impersonate"])],
    )
    role.bind(
        ServiceAccount.from_service_account_name(
            scope, f"{name}-controller", RUNTIME_SERVICE_ACCOUNT, namespace_name=NAMESPACE
        )
    )
    return identity


def secret_access(
    scope: Construct,
    id: str,
    *,
    identity: ServiceAccount,
    namespace: str,
    read: Sequence[str] = (),
    write: Sequence[str] = (),
) -> None:
    """Secret GET and named writes; CREATE cannot be resourceNames-scoped in Kubernetes RBAC."""
    role = Role(
        scope,
        id,
        metadata=ApiObjectMetadata(name=identity.name, namespace=namespace),
        rules=[
            *[
                RolePolicyRule(resources=[Secret.from_secret_name(scope, f"{id}-read-{name}", name)], verbs=["get"])
                for name in read
            ],
            *[
                RolePolicyRule(
                    resources=[Secret.from_secret_name(scope, f"{id}-write-{name}", name)],
                    verbs=["get", "update", "patch", "delete"],
                )
                for name in write
            ],
            *([RolePolicyRule(resources=[ApiResource.SECRETS], verbs=["create"])] if write else []),
        ],
    )
    role.bind(identity)


def gitops_workspace_chart(
    app: App,
    *,
    module_name: str,
    identity_name: str,
    schema: str | None = None,
    secret_accesses: Sequence[tuple[str, str, Sequence[str], Sequence[str]]],
) -> Chart:
    """Build the standard crossplane-prefixed chart for a managed GitOps Workspace.

    The module name supplies the chart and ProviderConfig names and the `tf/gitops/` path.
    The Workspace uses Observe/Create/Update; callers provide its identity, exact Secret grants and any historical schema override.
    """
    chart_name = f"crossplane-{module_name}"
    chart = Chart(app, chart_name, disable_resource_name_hashes=True)
    identity = kubernetes_service_account(chart, identity_name)
    for access_id, namespace, reads, writes in secret_accesses:
        secret_access(chart, access_id, identity=identity, namespace=namespace, read=reads, write=writes)
    gitops_workspace(
        chart,
        "workspace",
        name=module_name,
        provider_config_name=chart_name,
        schema=schema,
        management_policies=["Observe", "Create", "Update"],
        kubernetes_identity=identity_name,
    )
    return chart


def workspace_kustomization(
    chart: Chart,
    name: str,
    directory: RenderedDirectory,
    source: ArtifactGeneratorSpecArtifacts | None = None,
    *,
    depends_on: Sequence[Kustomization],
    timeout: str = "20m",
    description: str | None = None,
) -> Kustomization:
    """Use the directory's artifact unless a shared parent artifact supplies it."""
    source_ref: RenderedDirectory | KustomizationSpecSourceRef = directory
    path = None
    decryption = None
    if source is not None:
        source_ref = KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=source.name, namespace=flux.NAMESPACE
        )
        path = f"./{artifact_directory(directory.artifact)}"
        decryption = directory.decryption
    return flux_kustomization(
        chart,
        name,
        source_ref,
        path=path,
        decryption=decryption,
        timeout=timeout,
        depends_on=flux_kustomization_depends_on_many(*depends_on),
        health_check_exprs=[_WORKSPACE_HEALTH_CHECK_EXPR],
        description=description,
    )
