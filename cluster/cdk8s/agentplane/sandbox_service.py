"""Sandbox lifecycle gateway; history and command admission schemas migrate before Pod startup."""

from pathlib import Path
from typing import cast

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from agentplane_egresspolicy_crds.works.allegedly.agentplane import (
    EgressPolicySpecRules,
    EgressPolicySpecRulesCredentialRef,
    EgressPolicySpecRulesMethods,
)
from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ApiResource,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    EnvFieldPaths,
    EnvValue,
    IApiResource,
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    Role,
    RoleBinding,
    RolePolicyRule,
    Service,
    ServiceAccount,
    k8s,
)
from constructs import Construct

from agentplane.egress.resources import placeholder_of
from agentplane.runner_gateway.instructions import combine_instructions, render_platform_instructions
from agentplane.runner_gateway.settings import RUNNER_CHANNEL_AUDIENCE
from agentplane.sandbox_service.kubernetes_grants import ClusterRoleBindingGrant, RoleBindingGrant
from agentplane.sandbox_service.runner_endpoints import RunnerEndpoint
from agentplane.sandbox_service.settings import CONFIG_FILE_ENV, CombinedSettings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import actions, database, egress, history_service, notifications
from cluster.cdk8s.agentplane.environment import Environment, HistoryWriter
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.pod_disruption_budget import add_pod_disruption_budget
from cluster.cdk8s.api_resource import custom_resource, named_resource
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_secret_ref
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source
from cluster.cdk8s.providers.agentplane.egress_policy import EgressPolicy
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import env_name

NAME = "agentplane-sandbox-service"
TOKEN_AUDIENCE = "agentplane-sandbox-service"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-sandbox-service"
_HEALTH = Port(name="health", number=8081)
# The port every runner listens on (app.py's SandboxTemplate); this service dials it.
RUNNER_PORT = 7000
KUBERNETES_ADMIN_INSTRUCTIONS = (
    Path(__file__).with_name("kubernetes_admin_instructions.md").read_text(encoding="utf-8").strip()
)


# Granted by this service with its `runner_control` endpoint, so no launch selection can cut a
# Sandbox off from its own control plane.
RUNNER_CHANNEL_POLICY = "runner-channel"
RUNNER_CHANNEL_CREDENTIAL = "agentplane-runner-channel"
# The bearer a runner sends on the channel; the egress proxy swaps it for the Pod's channel token.
RUNNER_CHANNEL_BEARER = placeholder_of(RUNNER_CHANNEL_CREDENTIAL)
_RUNNER_CHANNEL_CONNECT = "/ducktape.agentplane.runner_channel.v1.RunnerChannel/Connect"


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def runner_channel(namespace: str) -> ServiceRef:
    """The listener runners dial out to (agentplane/docs/runner_channel.md), through the egress proxy."""
    return ServiceRef(
        name=NAME,
        port=Port(name="runner-channel", number=8082),
        pods=Pods(namespace=namespace, labels=tuple(_LABELS.items())),
    )


class SandboxService(Construct):
    def __init__(
        self, scope: Construct, id: str, env: Environment, *, manager: ServiceAccountRef, caller: ServiceRef
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        endpoint = service(env.namespace)
        channel = runner_channel(env.namespace)
        account = ServiceAccount(
            self, "account", metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace), automount_token=True
        )
        self._add_rbac(account)
        actions_service = actions.service(env.namespace)
        caller_accounts = {
            manager,
            ServiceAccountRef(namespace=env.namespace, name=notifications.NAME),
            # Subscribes to Sessions (WatchSessions, FollowSession) for its ingester.
            ServiceAccountRef(namespace=env.namespace, name=history_service.NAME),
        }
        config = SettingsFile(
            self,
            "config",
            metadata=ApiObjectMetadata(name=f"{NAME}-config", namespace=env.namespace),
            model=CombinedSettings,
            content={
                "sandbox_namespace": env.namespace,
                "caller_accounts": [
                    account.model_dump()
                    for account in sorted(caller_accounts, key=lambda account: (account.namespace, account.name))
                ],
                "token_audience": TOKEN_AUDIENCE,
                "platform_instructions": combine_instructions(
                    render_platform_instructions(
                        egress_api_url=f"http://{egress.agent_api(env.namespace).fqdn}",
                        actions_service_url=f"http://{actions_service.fqdn}:{actions_service.port.number}",
                        notifications_service_url=f"http://{notifications.service(env.namespace).fqdn}:8080",
                    ),
                    KUBERNETES_ADMIN_INSTRUCTIONS,
                ),
                "default_egress_policies": list(egress.DEFAULT_POLICIES),
                "kubernetes_grants": {
                    name: grant.model_dump(mode="json", exclude_none=True)
                    for name, grant in env.app_config.kubernetes_grants.items()
                },
                "kubernetes_binding_cleanup_namespaces": sorted(env.kubernetes_binding_cleanup_namespaces),
                "kubernetes_cluster_binding_cleanup": env.kubernetes_cluster_binding_cleanup,
                "runner_grpc_channel_options": dict(env.runner_grpc_channel_options),
                "runner_channel_audience": RUNNER_CHANNEL_AUDIENCE,
                "port": endpoint.port.number,
                "runner_channel_port": channel.pod_port,
                "health_port": _HEALTH.number,
                "runner_port": RUNNER_PORT,
                "ingest_history": env.history_writer is HistoryWriter.SANDBOX_SERVICE,
                **(
                    {
                        # Through the egress proxy, which swaps the placeholder bearer for the Pod's token.
                        "runner_control": RunnerEndpoint(
                            target=f"{channel.fqdn}:{channel.port.number}",
                            bearer=RUNNER_CHANNEL_BEARER,
                            egress_policy=RUNNER_CHANNEL_POLICY,
                        ).model_dump()
                    }
                    if env.runner_control
                    else {}
                ),
            },
            path="/etc/agentplane-sandbox-service/config.yaml",
            supplied=[("database_url",), ("commands_database_url",), ("pod_ip",)],
        )
        # Like the other database-backed services, a migration failure blocks Pod
        # startup, including existing lifecycle RPCs. The command has no retry loop.
        migration_env = {
            "AGENTPLANE_SANDBOX_SERVICE_DATABASE_URL": SecretRef(
                namespace=env.namespace, name="postgres-sandbox-service"
            )
            .key("uri")
            .env_value(self, "database")
        }
        commands_migration_env = {
            "AGENTPLANE_SANDBOX_SERVICE_COMMANDS_DATABASE_URL": SecretRef(
                namespace=env.namespace, name="postgres-sandbox-commands"
            )
            .key("uri")
            .env_value(self, "commands-database")
        }
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace, labels=_LABELS),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=env.replicas.count,
            strategy=env.replicas.strategy,
            min_ready=env.replicas.min_ready,
            termination_grace_period=Duration.seconds(60),
            service_account=account,
            automount_service_account_token=True,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "images-creds"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
            init_containers=[
                migrate_init_container(
                    "git.allegedly.works/ducktape-ci/agentplane-sandbox-service-history-migrate:unset",
                    name="migrate",
                    env_variables=migration_env,
                ),
                migrate_init_container(
                    "git.allegedly.works/ducktape-ci/agentplane-sandbox-commands-migrate:unset",
                    name="migrate-commands",
                    env_variables=commands_migration_env,
                ),
            ],
        )
        container = deployment.add_container(
            name="sandbox-service",
            image=f"{_IMAGE}:unset",
            env_variables=migration_env
            | commands_migration_env
            # Where the Sandbox control Services this replica writes point (runner_channel.md).
            | {env_name(CombinedSettings, "pod_ip"): EnvValue.from_field_ref(EnvFieldPaths.POD_IP)},
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            ports=[endpoint.port.container_port(), channel.port.container_port(), _HEALTH.container_port()],
            readiness=http_probe("/healthz", port=_HEALTH.number, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=_HEALTH.number, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )
        config.mount_into(container, env=CONFIG_FILE_ENV)
        pod_policy.place(deployment, node_scheduling.HIL_OVH)
        pod_policy.harden(deployment)
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace, labels=_LABELS),
            selector=deployment,
            ports=[endpoint.port.service_port(), channel.port.service_port()],
        )
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[
                caller.pods.admit(endpoint.pod_port),
                notifications.service(env.namespace).pods.admit(endpoint.pod_port),
                history_service.service(env.namespace).pods.admit(endpoint.pod_port),
                egress.proxy(env.namespace).pods.admit(channel.pod_port),
            ],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                EgressRule.to_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": env.namespace, "k8s:cnpg.io/cluster": "postgres"},
                    database.POSTGRES_PORT,
                ),
                EgressRule.to_endpoints(cilium.endpoint_labels(env.namespace, "agentplane-runner"), RUNNER_PORT),
            ],
        )
        self._add_runner_channel_egress(channel)
        if env.replicas.pdb_min_available is not None:
            add_pod_disruption_budget(
                self,
                "pdb",
                name=NAME,
                namespace=env.namespace,
                min_available=env.replicas.pdb_min_available,
                selector=_LABELS,
            )

    def _add_runner_channel_egress(self, channel: ServiceRef) -> None:
        """The egress path a runner dials the channel on: the proxy swaps the placeholder for the
        Pod's own token for the channel's audience, which no other recipient accepts."""
        namespace = self.env.namespace
        EgressCredential(
            self,
            "runner-channel-credential",
            metadata=ApiObjectMetadata(name=RUNNER_CHANNEL_CREDENTIAL, namespace=namespace),
            description=(
                "The calling Sandbox Pod's short-lived, Pod-bound identity minted only for its runner's "
                "channel to the Sandbox Service. It conveys no other authority."
            ),
            source=Source.projected_workload_token(audience=RUNNER_CHANNEL_AUDIENCE),
            targets=[
                EgressCredentialSpecTargets(
                    header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
                )
            ],
        )
        EgressPolicy(
            self,
            "runner-channel-policy",
            metadata=ApiObjectMetadata(name=RUNNER_CHANNEL_POLICY, namespace=namespace),
            rules=[
                EgressPolicySpecRules(
                    hosts=[channel.fqdn],
                    cluster_internal=True,
                    methods=[EgressPolicySpecRulesMethods.POST],
                    paths=[_RUNNER_CHANNEL_CONNECT],
                    credential_ref=EgressPolicySpecRulesCredentialRef(name=RUNNER_CHANNEL_CREDENTIAL),
                )
            ],
        )
        # Additive to the proxy's own policy, which egress.py owns and which cannot name this
        # module: this one imports that one.
        NetworkPolicy(
            self,
            "runner-channel-proxy-egress",
            metadata=ApiObjectMetadata(name=f"{NAME}-runner-channel-proxy", namespace=namespace),
            endpoint_selector=egress.proxy(namespace).pods.selector,
            egress=[channel.egress()],
        )

    def _add_rbac(self, service_account: ServiceAccount) -> None:
        namespace = self.env.namespace
        grants = tuple(self.env.app_config.kubernetes_grants.values())
        # Retain management of scopes with live bindings even if an operator removes a
        # catalog entry. Removing an enabled choice must not strand its old bindings.
        cleanup_namespaces = set(self.env.kubernetes_binding_cleanup_namespaces)
        role_binding_namespaces = {
            grant.namespace for grant in grants if isinstance(grant, RoleBindingGrant)
        } | cleanup_namespaces
        role_binding_grant_names = sorted(
            {
                grant.role_ref.name
                for grant in grants
                if (
                    isinstance(grant, RoleBindingGrant)
                    and grant.namespace == namespace
                    and grant.role_ref.kind == "Role"
                )
            }
        )
        role_binding_rules: list[RolePolicyRule] = []
        if namespace in role_binding_namespaces:
            role_binding_rules.append(
                RolePolicyRule(
                    resources=[custom_resource("rbac.authorization.k8s.io", "rolebindings")],
                    verbs=["create", "get", "list", "delete"],
                )
            )
            # Persisted grants can outlive catalog entries; keep role reads for
            # their old names while bind remains limited to configured names.
            role_binding_rules.append(
                RolePolicyRule(resources=[custom_resource("rbac.authorization.k8s.io", "roles")], verbs=["get"])
            )
        if role_binding_grant_names:
            role_binding_rules.extend(
                RolePolicyRule(resources=[named_resource("rbac.authorization.k8s.io", "roles", name)], verbs=["bind"])
                for name in role_binding_grant_names
            )
        # External namespace delegation is rendered into one independent Flux
        # Kustomization per target by binding_delegation.py. This service's
        # Kustomization must not fail because an unrelated namespace is absent.
        bind_cluster_roles = sorted({grant.role_ref.name for grant in grants if grant.role_ref.kind == "ClusterRole"})
        uses_cluster_binding = self.env.kubernetes_cluster_binding_cleanup or any(
            isinstance(grant, ClusterRoleBindingGrant) for grant in grants
        )
        if bind_cluster_roles or uses_cluster_binding:
            k8s.KubeClusterRole(
                self,
                "managed-cluster-bindings-role",
                metadata=k8s.ObjectMeta(name=f"{namespace}-managed-cluster-bindings"),
                rules=[
                    *(
                        [
                            k8s.PolicyRule(
                                api_groups=["rbac.authorization.k8s.io"],
                                resources=["clusterrolebindings"],
                                verbs=["create", "get", "list", "delete"],
                            )
                        ]
                        if uses_cluster_binding
                        else []
                    ),
                    *[
                        k8s.PolicyRule(
                            api_groups=["rbac.authorization.k8s.io"],
                            resources=["clusterroles"],
                            resource_names=[name],
                            verbs=["bind"],
                        )
                        for name in bind_cluster_roles
                    ],
                    # The stored selection can refer to a catalog entry later
                    # removed; GET validates it without widening BIND.
                    k8s.PolicyRule(api_groups=["rbac.authorization.k8s.io"], resources=["clusterroles"], verbs=["get"]),
                ],
            )
            k8s.KubeClusterRoleBinding(
                self,
                "managed-cluster-bindings-binding",
                metadata=k8s.ObjectMeta(name=f"{namespace}-managed-cluster-bindings"),
                role_ref=k8s.RoleRef(
                    api_group="rbac.authorization.k8s.io",
                    kind="ClusterRole",
                    name=f"{namespace}-managed-cluster-bindings",
                ),
                subjects=[k8s.Subject(kind="ServiceAccount", name=NAME, namespace=namespace)],
            )
        # TokenReview proves the caller's projected workload token. Creating a
        # review grants none of the reviewed identity's authority.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-sandbox-service-token-reviewer",
            service_account_name=NAME,
            namespace=namespace,
        )
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            rules=[
                # GET /sandboxes/templates lists them; a get-only Role 403'd the route (#7023).
                RolePolicyRule(
                    resources=[custom_resource("extensions.agents.x-k8s.io", "sandboxtemplates")], verbs=["get", "list"]
                ),
                RolePolicyRule(
                    resources=[custom_resource("agents.x-k8s.io", "sandboxes")],
                    verbs=["create", "get", "list", "watch", "patch", "delete"],
                ),
                RolePolicyRule(resources=[cast(IApiResource, ApiResource.PODS)], verbs=["get", "list", "watch"]),
                # Runner connection records, each owned by its Sandbox: the Lease every replica
                # watches, and the control Service and EndpointSlice its holder points at itself.
                RolePolicyRule(
                    resources=[custom_resource("coordination.k8s.io", "leases")],
                    verbs=["get", "list", "watch", "create", "update"],
                ),
                RolePolicyRule(
                    resources=[custom_resource("", "services"), custom_resource("discovery.k8s.io", "endpointslices")],
                    verbs=["get", "create", "update"],
                ),
                # Read the UID-owned ServiceAccount metadata when replaying Create; never read its tokens.
                RolePolicyRule(
                    resources=[custom_resource("", "serviceaccounts")], verbs=["create", "get", "patch", "delete"]
                ),
                RolePolicyRule(
                    resources=[
                        custom_resource("agentplane.allegedly.works", resource)
                        for resource in ("egresspolicies", "egressbindings", "egresscredentials")
                    ],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(
                    resources=[custom_resource("agentplane.allegedly.works", "egressbindings")],
                    verbs=["create", "delete"],
                ),
                RolePolicyRule(
                    resources=[
                        custom_resource("agentplane.allegedly.works", resource)
                        for resource in ("actionpolicysets", "actionpolicybindings")
                    ],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(
                    resources=[custom_resource("agentplane.allegedly.works", "actionpolicybindings")],
                    verbs=["create", "delete"],
                ),
                *role_binding_rules,
            ],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            role=Role.from_role_name(self, "role-ref", NAME),
        ).add_subjects(service_account)
