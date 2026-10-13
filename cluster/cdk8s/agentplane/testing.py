"""agentplane-testing: one replica of everything, its own Dex for operator login, and
credentialless MCP fixtures in place of the real action groups.

One Flux Kustomization (`agentplane_testing`) applies the whole environment: the
generated file, holding this chart and `litellm/credentials.py`'s, and the hand-written
`image_pins` Component its root Kustomization includes across the roots.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import (
    ApiResource,
    DeploymentStrategy,
    IApiResource,
    Role,
    RoleBinding,
    RolePolicyRule,
    ServiceAccount,
    k8s,
)
from constructs import Construct
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
)
from pydantic import AnyHttpUrl
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from agentplane.action_service.catalog import ActionGroup, McpExecutorBinding
from agentplane.action_service.mcp_settings import McpOAuthServer
from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings, OperatorTokenProfile
from agentplane.action_service.sandbox.binding import PodExecutorBinding
from agentplane.app.action_federation_settings import DirectFederationSettings
from agentplane.crd_group import GROUP
from cluster.cdk8s import agent_access_profiles as access, cilium
from cluster.cdk8s.agentplane import (
    actions,
    app as app_component,
    app_settings,
    command_sandbox,
    dex,
    egress,
    policy_controller,
    sandbox_pod,
)
from cluster.cdk8s.agentplane.actions_testing_fixtures import (
    MCP_EVERYTHING_NAME,
    MCP_EVERYTHING_PORT,
    OAUTH_FIXTURE_NAME,
    OAUTH_FIXTURE_PORT,
    add_testing_fixtures,
)
from cluster.cdk8s.agentplane.chart import environment_chart
from cluster.cdk8s.agentplane.egress_credentials import TESTING_CREDENTIALS_NAMESPACE, EgressCredentials
from cluster.cdk8s.agentplane.egress_testing_credentials import add_testing_egress_credentials
from cluster.cdk8s.agentplane.environment import (
    ActionsProps,
    AppProps,
    DbProps,
    EgressProps,
    Environment,
    LlmIngressProps,
    ReplicaProfile,
)
from cluster.cdk8s.agentplane.namespaces import TESTING_NAMESPACE
from cluster.cdk8s.api_resource import custom_resource, named_resource
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import CNPG_DATABASE_READY
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.model_selections import TESTING_APP_MODELS, model_configs
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, NetworkPolicy
from model_catalog.catalog import GPT6_LUNA_RESPONSES

_HOSTNAME = "agentplane-testing.allegedly.works"
_DEX_HOSTNAME = "agentplane-dex-testing.allegedly.works"
_DEX_ISSUER = f"https://{_DEX_HOSTNAME}/dex"
# The Terraform-owned key, replicated into this namespace by `litellm/credentials.py`'s
# ExternalSecret.
_LITELLM_KEY_SECRET = "litellm-key-cheap-experiments"
_OAUTH_FIXTURE_MCP_URL = f"http://{OAUTH_FIXTURE_NAME}.{TESTING_NAMESPACE}.svc.cluster.local:{OAUTH_FIXTURE_PORT}/mcp"

_FEDERATION_TARGET = OperatorOidcSettings(
    issuer=_DEX_ISSUER,
    audience="agentplane-testing",
    jwks_uri=f"{_DEX_ISSUER}/keys",
    token_profile=OperatorTokenProfile.DEX,
)
_ACTION_FEDERATION = DirectFederationSettings(
    mode="direct",
    service_url=AnyHttpUrl(actions.service(TESTING_NAMESPACE).url),
    login_jwks_uri=f"{_DEX_ISSUER}/keys",
    login_token_profile=OperatorTokenProfile.DEX,
    target=_FEDERATION_TARGET,
    scope="openid",
)
_MCP_SERVERS = {
    "example": McpOAuthServer(
        server_id="example",
        server_url=_OAUTH_FIXTURE_MCP_URL,
        client_id="agentplane-testing-mcp",
        client_secret_file=Path("/etc/agentplane-mcp/client-secret"),
        redirect_uri=f"https://{_HOSTNAME}/mcp-linkage/callback",
        scopes=["openid"],
    )
}
_ACTION_GROUPS = {
    "everything": ActionGroup(
        title="Upstream Everything",
        description="Credentialless MCP reference server for testing acceptance.",
        executor=McpExecutorBinding(
            kind="mcp",
            description="Community-built Everything image; no user account, workload token, or mounted credentials.",
            config={
                "transport": "streamable-http",
                "url": f"http://{MCP_EVERYTHING_NAME}.{TESTING_NAMESPACE}.svc.cluster.local:{MCP_EVERYTHING_PORT}/mcp",
                "auth": "none",
            },
        ),
    ),
    "example": ActionGroup(
        title="OAuth Example",
        description="Dex-backed OAuth-linked MCP fixture for testing acceptance of the linkage flow.",
        executor=McpExecutorBinding(
            kind="mcp",
            description="MCP tool protected by Dex-issued JWTs; no real credentials.",
            config={
                "transport": "streamable-http",
                "url": _OAUTH_FIXTURE_MCP_URL,
                "server_id": "example",
                "auth": "oauth",
            },
        ),
    ),
    "sandbox": ActionGroup(
        title="Sandbox",
        description="Boxes that run as the calling ServiceAccount, and bounded commands in them.",
        executor=PodExecutorBinding(
            kind="pod",
            description="Created and exec'd by this service, as the caller, in its own namespace.",
            namespace=TESTING_NAMESPACE,
            templates=command_sandbox.TEMPLATES,
        ),
    ),
}


ENV = Environment(
    namespace=TESTING_NAMESPACE,
    description=(
        "Agentplane testing - sandboxed runner Pods (one per Sandbox) and the integration app that drives them."
    ),
    flux_description=(
        "Complete Agentplane testing environment, including namespace, database, Dex, egress, LLM ingress, "
        "Actions fixtures, app, runner template, and operator RBAC."
    ),
    output_dir=f"{GENERATED_ROOT}/{TESTING_NAMESPACE}",
    image_pins=f"{HAND_WRITTEN_ROOT}/agentplane-testing-image-pins",
    extra_resources=(),
    replicas=ReplicaProfile(count=1, strategy=DeploymentStrategy.recreate(), min_ready=None, pdb_min_available=None),
    app_config=app_settings.settings(
        namespace=TESTING_NAMESPACE,
        models=TESTING_APP_MODELS,
        thread_preset_codex_model=GPT6_LUNA_RESPONSES,
        action_federation=_ACTION_FEDERATION,
        public_coder_policy_sets=[],
    ),
    settle_deltas=True,
    db=DbProps(instances=1, wal_archive=False),
    llm_ingress=LlmIngressProps(litellm_key_secret_name=_LITELLM_KEY_SECRET, models=model_configs(TESTING_APP_MODELS)),
    egress=EgressProps(
        ca_secret_name="agentplane-testing-egress-ca", credentials_namespace=TESTING_CREDENTIALS_NAMESPACE
    ),
    app=AppProps(hostname=_HOSTNAME, oidc_issuer=_DEX_ISSUER, reach_incluster_authentik=False, runner_zone=None),
    actions=ActionsProps(
        hostname="agentplane-actions-testing.allegedly.works",
        operator_oidc=_FEDERATION_TARGET,
        caller_service_account_namespaces=frozenset({TESTING_NAMESPACE}),
        mcp_servers=_MCP_SERVERS,
        action_groups=_ACTION_GROUPS,
        extra_egress=[
            # The direct federation verifier fetches Dex's JWKS over the public-origin Gateway path.
            cilium.egress_via_gateway(_DEX_HOSTNAME),
            # MCP OAuth discovery/token exchange/tool calls for the linked "example" fixture:
            # cluster-internal only, unlike the real GitHub/Kubernetes MCP OAuth providers
            # linked in staging.
            EgressRule.to_endpoints(cilium.endpoint_labels(TESTING_NAMESPACE, OAUTH_FIXTURE_NAME), OAUTH_FIXTURE_PORT),
        ],
    ),
    vm_sandboxes=True,
    vm_console_autologin=True,
)


class TestingNamespaceResourceLimits(Construct):
    """The ResourceQuota and LimitRange applied by the testing environment only."""

    def __init__(self, scope: Construct, id: str, namespace: str) -> None:
        super().__init__(scope, id)
        # Bounds what runner sandboxes take from the node. Each costs 2100m of limits.cpu
        # (2 for the runner, 100m for the egress sidecar), about 4.1Gi of limits.memory
        # and a 10Gi state PVC, and the namespace's own service Pods count against the
        # same totals. Sized for those services plus four sandboxes at once, with room
        # left for a rollout's surge Pods; for more headroom, raise the limits, not a count.
        #
        # Aggregate resources only. A cap per object kind bounds an untrusted creator,
        # and only Flux and the integration app create objects here.
        #
        # The LimitRange supplies defaults for containers that omit them (the CNPG
        # postgres container declares none); its mutations are applied before quota validation.
        # No per-container minimum: KubeVirt's virt-launcher helper containers request 1m CPU
        # and 1M memory. The 4Gi container max also caps VM size: KubeVirt v1.8.2 gave a 2Gi
        # guest's compute container a 4632Mi limit here, which this LimitRange rejected.
        k8s.KubeResourceQuota(
            self,
            "resourcequota",
            metadata=k8s.ObjectMeta(name="quota", namespace=namespace),
            spec=k8s.ResourceQuotaSpec(
                hard={
                    "requests.cpu": k8s.Quantity.from_string("4"),
                    "requests.memory": k8s.Quantity.from_string("8Gi"),
                    "limits.cpu": k8s.Quantity.from_string("18"),
                    "limits.memory": k8s.Quantity.from_string("28Gi"),
                    "requests.storage": k8s.Quantity.from_string("80Gi"),
                }
            ),
        )
        k8s.KubeLimitRange(
            self,
            "limitrange",
            metadata=k8s.ObjectMeta(name="limits", namespace=namespace),
            spec=k8s.LimitRangeSpec(
                limits=[
                    k8s.LimitRangeItem(
                        type="Container",
                        max={"cpu": k8s.Quantity.from_string("2"), "memory": k8s.Quantity.from_string("4Gi")},
                        default={"cpu": k8s.Quantity.from_string("500m"), "memory": k8s.Quantity.from_string("512Mi")},
                        default_request={
                            "cpu": k8s.Quantity.from_string("100m"),
                            "memory": k8s.Quantity.from_string("128Mi"),
                        },
                    ),
                    k8s.LimitRangeItem(
                        type="Pod",
                        max={"cpu": k8s.Quantity.from_string("4"), "memory": k8s.Quantity.from_string("8Gi")},
                    ),
                ]
            ),
        )


TESTING_OPERATOR_ROLE_NAME = "agentplane-testing-operator"

# Deliberately no `create` on pods: Cilium policies here select Pods by label, so an unlabeled Pod
# a Sandbox creates has unrestricted egress, and the creator picks the labels.
_SANDBOX_RULES = [
    RolePolicyRule(resources=[custom_resource(GROUP, "sandboxtemplates")], verbs=["get", "list", "watch"]),
    RolePolicyRule(
        resources=[custom_resource(GROUP, "sandboxes")], verbs=["create", "get", "list", "watch", "patch", "delete"]
    ),
    RolePolicyRule(resources=[cast(IApiResource, ApiResource.PODS)], verbs=["get", "list", "watch"]),
    RolePolicyRule(
        resources=[custom_resource("", "pods/exec"), custom_resource("", "pods/portforward")], verbs=["create"]
    ),
    RolePolicyRule(resources=[custom_resource("", "pods/log")], verbs=["get"]),
    # Read-only view of a `kind: vm` Sandbox's guest and its disks, to diagnose one that never starts.
    RolePolicyRule(
        resources=[
            custom_resource("kubevirt.io", "virtualmachines"),
            custom_resource("kubevirt.io", "virtualmachineinstances"),
            custom_resource("cdi.kubevirt.io", "datavolumes"),
        ],
        verbs=["get", "list", "watch"],
    ),
    # `virtctl console`: the only way into a guest whose runner never connected.
    RolePolicyRule(
        resources=[custom_resource("subresources.kubevirt.io", "virtualmachineinstances/console")], verbs=["get"]
    ),
]

# Mints app-audience tokens for `agentplane-agent`, which has no RoleBindings: an identity for the
# app only. The app admits that subject because its Deployment names it; the audience alone gates
# nothing, since any account's token can carry it.
_TOKEN_RULE = RolePolicyRule(
    resources=[named_resource("", "serviceaccounts/token", "agentplane-agent")], verbs=["create"]
)

# testing's MCP acceptance scenario additionally creates, expires, and deletes the
# PolicySet/PolicyBinding its Sandbox is auto-approved under, reading the binding's
# ActionsReady condition to know the Action Service has seen it.
_ACTION_POLICY_RULE = RolePolicyRule(
    resources=[custom_resource(GROUP, "policysets"), custom_resource(GROUP, "policybindings")],
    verbs=["create", "get", "list", "watch", "patch", "delete"],
)


class AgentRbac(Construct):
    """The operator Role/RoleBinding an agent needs to drive Agentplane **testing**
    without a human: Sandbox lifecycle, exec/port-forward into runner Pods, and the
    token used to call the app's own API.

    **Testing only, deliberately.** `agentplane-testing` runs Dex-backed fake OAuth and
    credentialless MCP fixtures -- nothing here reaches a real account. `agentplane-staging`
    is the opposite: real Authentik-federated operator login, real GitHub/Kubernetes MCP
    OAuth linkage, and `claude-ai` Sandboxes carry the real read-only Google
    `google-readonly` egress credential (`egress_staging_credentials.py`). An agent identity holding this
    Role there could stamp a Sandbox under that ServiceAccount and reach the operator's
    real external accounts with no human in the loop -- the opposite of what "testing"
    fixtures are for. So only `testing.chart` instantiates this construct; `staging.chart`
    (via the shared `chart.environment_chart`) must not.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=TESTING_OPERATOR_ROLE_NAME, namespace=env.namespace),
            rules=[*_SANDBOX_RULES, _ACTION_POLICY_RULE, _TOKEN_RULE],
        )

        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name="agent-agentplane-testing-operator", namespace=env.namespace),
            role=Role.from_role_name(self, "role-ref", TESTING_OPERATOR_ROLE_NAME),
        ).add_subjects(
            *[
                subject.imported(self, f"operator-subject-{index}")
                for index, subject in enumerate(access.TESTING_OPERATOR_SUBJECTS)
            ]
        )


class AcceptanceToken(Construct):
    """Lets `agentplane-staging`'s `claude-ai` and `haku-agent` mint this namespace's app token,
    so their sandboxes can run the acceptance suite's harness scenarios
    (`agentplane/acceptance/README.md`), which ask the API server for nothing else. None of
    `AgentRbac`'s Sandbox lifecycle, exec or PolicySet and PolicyBinding writes are granted by this Role:
    the token is an identity for the app, as `_TOKEN_RULE` says. `claude-ai` separately
    receives `AgentRbac` in testing; `haku-agent` does not.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        role = Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name="agentplane-acceptance-token", namespace=env.namespace),
            rules=[_TOKEN_RULE],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name="claude-ai-acceptance-token", namespace=env.namespace),
            role=role,
        ).add_subjects(
            ServiceAccount.from_service_account_name(
                self, "claude-ai-sa", "claude-ai", namespace_name="agentplane-staging"
            ),
            ServiceAccount.from_service_account_name(
                self, "haku-agent-sa", "haku-agent", namespace_name="agentplane-staging"
            ),
        )


def chart(app: App) -> Chart:
    chart = environment_chart(app, ENV)
    TestingNamespaceResourceLimits(chart, "namespace-resource-limits", ENV.namespace)
    # Only this environment's chart gets the agent-operator Role/RoleBinding -- see
    # `AgentRbac`'s own docstring for why it must not be in staging's.
    AgentRbac(chart, "rbac", ENV)
    AcceptanceToken(chart, "acceptance-token", ENV)
    command_sandbox.CommandSandbox(chart, "command-sandbox", ENV)
    # claude-ai's boxes reach this app through staging's egress proxy, by its Service rather than its
    # public name, which would hairpin out through the Gateway and back.
    app_service = app_component.service(ENV.namespace)
    NetworkPolicy(
        chart,
        "networkpolicy-app-from-staging-egress",
        metadata=ApiObjectMetadata(name=f"{app_service.name}-from-staging-egress", namespace=ENV.namespace),
        endpoint_selector=app_service.pods.selector,
        ingress=[egress.proxy("agentplane-staging").pods.admit(app_service.pod_port)],
    )
    add_testing_fixtures(chart)
    dex.Dex(chart, "dex")
    # No grant catalog here: the only Kubernetes-rule set is the tool config every environment
    # serves, and the controller reports KubernetesReady on testing's PolicyBindings.
    policy_controller.PolicyController(
        chart,
        "policy-controller",
        ENV,
        sets={sandbox_pod.TOOL_CONFIG_GRANT: [sandbox_pod.tool_config_grant(ENV.namespace)]},
    )
    EgressCredentials(
        chart, "egress-credentials", namespace=ENV.egress.credentials_namespace, proxy_namespace=ENV.namespace
    )
    add_testing_egress_credentials(chart, credentials_namespace=ENV.egress.credentials_namespace)
    return chart


def agentplane_testing(
    flux_chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    health_checks: list[KustomizationSpecHealthChecks],
    agentplane_crds: Kustomization,
    cert_manager_trust: Kustomization,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    kyverno: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        ENV.namespace,
        artifact,
        description=ENV.flux_description,
        wait=None,
        timeout="10m",
        # This one Kustomization owns the CNPG Cluster's PVCs; pruning on
        # deletion would take the database with them.
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        health_checks=health_checks,
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="postgresql.cnpg.io/v1", kind="Database", current=CNPG_DATABASE_READY
            )
        ],
        depends_on=flux_kustomization_depends_on_many(
            agentplane_crds, cert_manager_trust, cnpg, external_secrets_operator, kyverno
        ),
    )
