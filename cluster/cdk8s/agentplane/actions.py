"""The Action Service: its Deployment, RBAC, ConfigMaps,
Service, NetworkPolicy, optional PodDisruptionBudget, and the admission policy on the launcher grants of
an environment with the orchestration_v1 group. Staging's policy sets are added by its chart.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    PathMapping,
    Role,
    RoleBinding,
    RolePolicyRule,
    Secret,
    ServiceAccount,
    Volume,
    k8s,
)
from constructs import Construct

from agentplane.action_service.catalog import ActionGroup
from agentplane.action_service.orchestration_v1.binding import (
    LaunchPreset,
    OrchestrationExecutorBinding,
    PresetLaunch,
    PresetSession,
    PresetSessionSpec,
)
from agentplane.action_service.session_delegation.binding import SessionDelegationExecutorBinding
from agentplane.action_service.settings import ServicePeer, Settings
from agentplane.crd_group import GROUP
from agentplane.environment_runtime_transport.config import ACTIONS_URI, HISTORY_SERVICE_URI, SESSION_DISPATCHER_URI
from agentplane.identity.auth import listener
from agentplane.identity.auth.audience import WORKLOAD_TOKEN_AUDIENCE
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import database, notifications, workload_tls
from cluster.cdk8s.agentplane.environment import Environment, OrchestrationProps, SessionDelegationProps
from cluster.cdk8s.agentplane.service_workload import ServiceWorkload, add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.probes import tls_grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import env_name, validate_settings_fragment

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
NAME = "actions"
_ACTIONS_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-action-service"
_LABELS = {"app.kubernetes.io/name": NAME}

# Where the Action Service reads linked upstream MCP client credentials from the OAuth Secret.
MCP_OAUTH_DIR = "/etc/agentplane-mcp"
# The orchestration_v1 group's key, which its launcher grants' rules name.
ORCHESTRATION_GROUP = "orchestration_v1"
ENVIRONMENTS_GROUP = "environments"
SESSIONS_GROUP = "sessions"
SANDBOX_GROUP = "sandbox"
_PEER_TOKEN_DIR = "/var/run/secrets/actions"
# The orchestration_v1 executor and its admission policy both name launcher grants `<sandbox>.launcher`.
_LAUNCHER_SUFFIX = ".launcher"
# Must equal the suffix orchestration_v1 launchIdentity names its Identity grant with.
_IDENTITY_GRANT_SUFFIX = ".identity"
_LAUNCHER_ACTIONS = (
    "create_session",
    "submit_input",
    "interrupt_turn",
    "change_model",
    "change_reasoning_effort",
    "get_session",
    "list_sessions",
    "read_turns",
    "wait_for_turn",
)
_PEER_TIMEOUT_SECONDS = 15
_DELEGATED_PEER_TIMEOUT_SECONDS = 40
_DEFAULT_MEMORY_REQUEST = Size.mebibytes(128)
_DEFAULT_MEMORY_LIMIT = Size.mebibytes(512)


def service(namespace: str) -> ServiceRef:
    """The Action Service in one environment's namespace."""
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def orchestration_callers(env: Environment) -> dict[ServiceAccountRef, Pods]:
    """The Action Service as a caller of the Session Dispatcher and the History Service, in an
    environment with the orchestration_v1 group."""
    if env.actions.orchestration_v1 is None:
        return {}
    return {ServiceAccountRef(namespace=env.namespace, name=NAME): service(env.namespace).pods}


def session_delegation_callers(env: Environment) -> dict[ServiceAccountRef, Pods]:
    """The Action Service's network identity when it uses same-principal Session delegation."""
    if env.actions.session_delegation is None:
        return {}
    return {ServiceAccountRef(namespace=env.namespace, name=NAME): service(env.namespace).pods}


def _session_delegation_groups(props: SessionDelegationProps) -> dict[str, ActionGroup]:
    """Build only the existing-environment delegation groups, with their approved direct tools."""
    executor = SessionDelegationExecutorBinding(
        kind="session_delegation",
        description="Forwards the authenticated caller's ServiceAccount identity to same-principal Session APIs.",
    )
    return {
        ENVIRONMENTS_GROUP: ActionGroup(
            title="Environments",
            description="Find, refresh, and resume existing environments that the authenticated caller may use for Sessions.",
            executor=executor,
            direct_tools=props.direct_tools_by_group.get(ENVIRONMENTS_GROUP),
        ),
        SESSIONS_GROUP: ActionGroup(
            title="Sessions",
            description=(
                "Create and control Sessions in an existing same-principal environment. Creating does not start a turn; "
                "submit input separately."
            ),
            executor=executor,
            direct_tools=props.direct_tools_by_group.get(SESSIONS_GROUP),
        ),
    }


def _orchestration_group(env: Environment, props: OrchestrationProps) -> ActionGroup:
    """The orchestration_v1 group, launching with the app's presets and default PolicySets."""
    app = env.app_config
    presets = {}
    for name, sandbox in app.sandbox_presets.items():
        thread = app.thread_presets[sandbox.thread_preset]
        spec = PresetSessionSpec(
            harness=thread.harness,
            model=thread.model,
            cwd=thread.cwd,
            reasoning_effort=thread.reasoning_effort or "",
            instructions=thread.instructions,
        )
        presets[name] = LaunchPreset(
            title=sandbox.title,
            launch=PresetLaunch(
                template=sandbox.template,
                policy_sets=sandbox.policy_sets,
                inbox_tags=sandbox.inbox_tags,
                bootstrap=sandbox.bootstrap,
            ),
            create_session=PresetSession(spec=spec, setup_script=thread.setup_script),
        )
    return ActionGroup(
        title="Orchestration",
        description=(
            "Launch Session Sandboxes and drive their Sessions: create them, send input, interrupt, change "
            "model or effort, and read their state. A launch waits for the operator and grants its caller "
            "what it names on the new Sandbox."
        ),
        executor=OrchestrationExecutorBinding(
            kind="orchestration_v1",
            description="Run by this service, on each request's Decision, with its own authority.",
            namespace=env.namespace,
            presets=presets,
        ),
        direct_tools=props.direct_tools,
    )


def add_launcher_admission(scope: Construct, namespace: str) -> None:
    """Restrict Action Service launcher grants to their owning Sandbox and approved Actions.

    EnvironmentService owns Sandbox creation and refuses launcher PolicySets in create requests.
    Admission limits what an Action-owned grant reaches; it cannot see the Action caller.
    """
    name = f"{namespace}-actions-launcher-grants"
    # The objects reach CEL as plain maps: a missing key is an error, so every access is guarded, and
    # `const`, a CEL reserved word, is indexed.
    owner = "variables.owners[0]"
    target = "r.targets.sandboxes[0]"
    schema = "r.policy.schema"
    sandbox = f"{schema}.properties.sandbox"
    const = f"{sandbox}['const']"
    launcher_actions = ", ".join(f"'{action}'" for action in _LAUNCHER_ACTIONS)
    k8s.KubeValidatingAdmissionPolicy(
        scope,
        "launcher-admission",
        metadata=k8s.ObjectMeta(name=name),
        spec=k8s.ValidatingAdmissionPolicySpec(
            failure_policy="Fail",
            match_constraints=k8s.MatchResources(
                resource_rules=[
                    k8s.NamedRuleWithOperations(
                        api_groups=[GROUP],
                        api_versions=["*"],
                        operations=["CREATE"],
                        resources=["policysets", "policybindings"],
                    )
                ]
            ),
            match_conditions=[
                k8s.MatchCondition(
                    name="action-service",
                    expression=f"request.userInfo.username == 'system:serviceaccount:{namespace}:{NAME}'",
                )
            ],
            variables=[k8s.Variable(name="owners", expression="object.metadata.?ownerReferences.orValue([])")],
            validations=[
                k8s.Validation(
                    expression=(
                        "(size(variables.owners) == 1"
                        f" && {owner}.apiVersion.startsWith('{GROUP}/') && {owner}.kind == 'Sandbox'"
                        f" && object.metadata.name == {owner}.name + '{_LAUNCHER_SUFFIX}'"
                        " && (request.resource.resource != 'policybindings' || !has(object.spec.subject.identity)))"
                        # An Identity caller's launcher binding pins that caller as a second owner.
                        " || (request.resource.resource == 'policybindings' && size(variables.owners) == 2"
                        f" && {owner}.apiVersion.startsWith('{GROUP}/') && {owner}.kind == 'Sandbox'"
                        f" && object.metadata.name == {owner}.name + '{_LAUNCHER_SUFFIX}'"
                        f" && variables.owners[1].apiVersion.startsWith('{GROUP}/')"
                        " && variables.owners[1].kind == 'Identity' && variables.owners[1].uid != ''"
                        " && has(object.spec.subject.identity)"
                        " && object.spec.subject.identity.name == variables.owners[1].name)"
                        # A launch's grant to the Identity it created: owned by that Identity, named
                        # for it, and binding only it (orchestration_v1 launchIdentity).
                        " || (request.resource.resource == 'policybindings' && size(variables.owners) == 1"
                        f" && {owner}.apiVersion.startsWith('{GROUP}/') && {owner}.kind == 'Identity'"
                        f" && object.metadata.name == {owner}.name + '{_IDENTITY_GRANT_SUFFIX}'"
                        " && has(object.spec.subject.identity)"
                        f" && object.spec.subject.identity.name == {owner}.name)"
                    ),
                    message=(
                        "A launcher grant is owned by its Sandbox, with an Identity caller pinned as a second "
                        "owner for an Identity subject; a launch's Identity "
                        "grant is owned by that Identity, named for it and binds only it."
                    ),
                ),
                k8s.Validation(
                    expression=(
                        "request.resource.resource != 'policybindings'"
                        f" || {owner}.kind == 'Identity'"
                        " || object.spec.policySets == [object.metadata.name]"
                    ),
                    message="A launcher grant's PolicyBinding binds only its own PolicySet.",
                ),
                k8s.Validation(
                    expression=(
                        "request.resource.resource != 'policysets' || (size(variables.owners) == 1"
                        " && object.spec.rules.all(r, (r.type == 'session_inbox_access'"
                        " && has(r.operations) && r.operations == ['send_message'] && has(r.targets)"
                        " && !has(r.targets.inboxTags) && !has(r.targets.allSandboxes)"
                        f" && has(r.targets.sandboxes) && size(r.targets.sandboxes) == 1 && has({target}.uid)"
                        f" && {target}.name == {owner}.name && {target}.uid == {owner}.uid)"
                        " || (r.type == 'action_auto_approve' && has(r.policy) && r.policy.type == 'argument_schema'"
                        f" && r.policy.actions.all(group, group == '{ORCHESTRATION_GROUP}' &&"
                        f" r.policy.actions[group].all(action, action in [{launcher_actions}]))"
                        f" && has({schema}) && {schema}.all(key, key in ['type', 'required', 'properties'])"
                        f" && has({schema}.type) && {schema}.type == 'object'"
                        f" && has({schema}.required) && {schema}.required == ['sandbox']"
                        f" && has({schema}.properties) && {schema}.properties.all(key, key == 'sandbox')"
                        f" && has({sandbox}) && {sandbox}.all(key, key == 'const') && 'const' in {sandbox}"
                        f" && {const}.all(key, key in ['name', 'uid']) && 'name' in {const} && 'uid' in {const}"
                        f" && {const}.name == {owner}.name && {const}.uid == {owner}.uid)))"
                    ),
                    message=(
                        "A launcher grant's rules name only its Sandbox, by name and UID, and only "
                        f"{ORCHESTRATION_GROUP} Actions."
                    ),
                ),
            ],
        ),
    )
    k8s.KubeValidatingAdmissionPolicyBinding(
        scope,
        "launcher-admission-binding",
        metadata=k8s.ObjectMeta(name=name),
        spec=k8s.ValidatingAdmissionPolicyBindingSpec(
            policy_name=name,
            validation_actions=["Deny"],
            match_resources=k8s.MatchResources(
                namespace_selector=k8s.LabelSelector(match_labels={"kubernetes.io/metadata.name": namespace})
            ),
        ),
    )


def mcp_oauth_secret(namespace: str) -> SecretRef:
    """The MCP OAuth linkage Secret the Action Service reads: staging's from Terraform
    (tf/gitops/sso-providers), the k3s fixture's its own."""
    return SecretRef(namespace=namespace, name="agentplane-mcp-oauth")


class Actions(Construct):
    """ServiceAccount, RBAC, ConfigMaps, Deployment, Service,
    and optional PodDisruptionBudget for the Action Service. The fleet network policy is added by
    `add_platform_resources`.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        database_url: EnvValue,
        history: ServiceRef,
        history_audience: str,
        dispatcher: ServiceRef,
        dispatcher_audience: str,
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        image_pull_policy: ImagePullPolicy = ImagePullPolicy.ALWAYS,
        memory_request: Size = _DEFAULT_MEMORY_REQUEST,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
        container_env: Mapping[str, EnvValue] | None = None,
    ) -> None:
        """`history` and `dispatcher` serve orchestration_v1 directly and session_delegation with forwarded
        caller identity, when those groups are configured."""
        super().__init__(scope, id)
        self.env = env
        self.database_url = database_url
        self.history = history
        self.history_audience = history_audience
        self.dispatcher = dispatcher
        self.dispatcher_audience = dispatcher_audience
        self.container_env = container_env or {}
        self.placement = placement
        self.registry_secret_name = registry_secret_name
        self.image_pull_policy = image_pull_policy
        self.memory_request = memory_request
        self.memory_limit = memory_limit
        self.service = service(env.namespace)
        self.mcp_oauth = mcp_oauth_secret(env.namespace)

        props = env.actions
        content: dict[str, Any] = {
            "operator_oidc": props.operator_oidc,
            "policy_namespace": env.namespace,
            "caller_service_account_namespaces": sorted(props.caller_service_account_namespaces),
            "direct_wait_seconds": 30,
        }
        if props.web_push is not None:
            content["web_push"] = {
                "subject": props.web_push.subject,
                "public_base_url": props.web_push.public_base_url,
                "allowed_push_hosts": list(props.web_push.allowed_push_hosts),
            }
        if props.mcp_client_metadata is not None:
            content["mcp_client_metadata"] = props.mcp_client_metadata
        content["mcp_servers"] = dict(props.mcp_servers)
        groups = dict(props.action_groups)
        if props.orchestration_v1 is not None:
            groups[ORCHESTRATION_GROUP] = _orchestration_group(env, props.orchestration_v1)
            content["session_dispatcher"] = ServicePeer(
                target=f"{dispatcher.fqdn}:{dispatcher.port.number}",
                token_file=f"{_PEER_TOKEN_DIR}/dispatcher",
                request_timeout_s=_PEER_TIMEOUT_SECONDS,
                tls=workload_tls.client_tls(SESSION_DISPATCHER_URI),
            )
            content["history_service"] = ServicePeer(
                target=f"{history.fqdn}:{history.pod_port}",
                token_file=f"{_PEER_TOKEN_DIR}/history",
                request_timeout_s=_PEER_TIMEOUT_SECONDS,
                tls=workload_tls.client_tls(HISTORY_SERVICE_URI),
            )
        if props.session_delegation is not None:
            groups.update(_session_delegation_groups(props.session_delegation))
            content["session_delegation_dispatcher"] = ServicePeer(
                target=f"{dispatcher.fqdn}:{dispatcher.port.number}",
                token_file=f"{_PEER_TOKEN_DIR}/dispatcher",
                request_timeout_s=_DELEGATED_PEER_TIMEOUT_SECONDS,
                tls=workload_tls.client_tls(SESSION_DISPATCHER_URI),
            )
            content["session_delegation_history"] = ServicePeer(
                target=f"{history.fqdn}:{history.pod_port}",
                token_file=f"{_PEER_TOKEN_DIR}/history",
                request_timeout_s=_DELEGATED_PEER_TIMEOUT_SECONDS,
                tls=workload_tls.client_tls(HISTORY_SERVICE_URI),
            )
        content["action_groups"] = groups
        settings_name = "actions-settings"
        workload = self._add_workload(settings_name)
        serving_tls = workload_tls.serving_certificate(
            self, env, container=workload.container, name=NAME, uri=ACTIONS_URI
        )
        assert serving_tls.cert_file is not None
        assert serving_tls.key_file is not None
        assert serving_tls.ca_file is not None
        content["rpc"] = listener.Listener(
            listen_address=f"0.0.0.0:{self.service.pod_port}",
            tls=listener.TLS(cert_file=Path(serving_tls.cert_file), key_file=Path(serving_tls.key_file)),
            auth=[
                listener.KubernetesBearerAuth(audiences=frozenset({WORKLOAD_TOKEN_AUDIENCE})),
                listener.SPIFFEAuth(ca_file=Path(serving_tls.ca_file)),
            ],
        )
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=settings_name, namespace=env.namespace),
            content=yaml_config(validate_settings_fragment(Settings, content)),
            path="/etc/agentplane-actions/settings.yaml",
        )
        settings.mount_into(workload.container, env=Settings.config_file_env)
        self._add_rbac(workload.account)
        if props.orchestration_v1 is not None:
            add_launcher_admission(self, env.namespace)

    def add_platform_resources(self) -> None:
        """Add the Cilium policy and Action Service availability alert."""
        namespace = self.env.namespace
        self._add_network_policy()
        PrometheusRule(
            self,
            "availability-alert",
            metadata=ApiObjectMetadata(
                name=f"{NAME}-availability",
                namespace=namespace,
                annotations={
                    "description": "Alerts when the Action Service has fewer available replicas than requested."
                },
            ),
            groups=[
                group(
                    NAME,
                    [
                        Rule.alert(
                            "AgentplaneActionServiceReplicasUnavailable",
                            (
                                f"max by (cluster, namespace, deployment) "
                                f'(kube_deployment_spec_replicas{{namespace="{namespace}", deployment="{NAME}"}}) '
                                f"- max by (cluster, namespace, deployment) "
                                f'(kube_deployment_status_replicas_available{{namespace="{namespace}", deployment="{NAME}"}}) > 0'
                            ),
                            for_="10m",
                            labels={"severity": "warning"},
                            summary="Agentplane Action Service has fewer available replicas than requested",
                            description=(
                                "Deployment {{ $labels.namespace }}/{{ $labels.deployment }} has "
                                "{{ $value }} fewer available replicas than requested for 10 minutes. "
                                "The readiness check uses gRPC health; inspect the Action Service pods and logs."
                            ),
                        )
                    ],
                )
            ],
        )

    def _add_rbac(self, service_account: ServiceAccount) -> None:
        namespace = self.env.namespace
        # TokenReview authenticates backend ServiceAccount callers. Sandbox callers
        # prove their SVID separately. A review grants none of the reviewed account's authority.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-actions-token-reviewer",
            service_account_name=NAME,
            namespace=namespace,
        )
        # The runtime policy and caller watch scopes are independent. Keep the policy
        # CR permissions in the policy namespace, and grant only ServiceAccount reads in
        # caller namespaces. No Secrets or serviceaccounts/token permissions are needed. The
        # policy informer lists and watches them and reads only what it holds: no `get`, except
        # on Identities, which an Identity caller's SVID resolution reads live (uncached).
        rules_by_namespace: dict[str, list[RolePolicyRule]] = {
            namespace: [
                RolePolicyRule(
                    resources=[custom_resource(GROUP, resource) for resource in ("policysets", "policybindings")],
                    verbs=["list", "watch"],
                ),
                RolePolicyRule(resources=[custom_resource(GROUP, "identities")], verbs=["get", "list", "watch"]),
                RolePolicyRule(resources=[custom_resource(GROUP, "policybindings/status")], verbs=["patch"]),
            ]
        }
        for caller_namespace in self.env.actions.caller_service_account_namespaces:
            rules_by_namespace.setdefault(caller_namespace, []).append(
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["get", "list", "watch"])
            )
            rules_by_namespace[caller_namespace].append(
                RolePolicyRule(resources=[custom_resource(GROUP, "sandboxes")], verbs=["get", "list"])
            )
            # A caller's SVID names its WorkloadCredential, read live on every request.
            rules_by_namespace[caller_namespace].append(
                RolePolicyRule(resources=[custom_resource(GROUP, "workloadcredentials")], verbs=["get"])
            )

        # The sandbox ActionGroup creates its boxes as plain Pods from its PodTemplates, running as
        # their callers and owned by their WorkloadCredentials, and runs commands in them. This is namespace-wide and cannot
        # say "only the boxes this Action made": that boundary is the executor's own label check
        # (agentplane/action_service/sandbox/inventory.go), which is why it is an application rule
        # tested as one rather than something RBAC states.
        rules_by_namespace.setdefault(namespace, []).extend(
            [
                RolePolicyRule(resources=[custom_resource("", "podtemplates")], verbs=["get"]),
                RolePolicyRule(resources=[custom_resource("", "pods")], verbs=["create", "get", "list", "delete"]),
                # Both verbs: a WebSocket exec is an HTTP GET upgrade, and the API server
                # authorizes it as `create` too.
                RolePolicyRule(resources=[custom_resource("", "pods/exec")], verbs=["get", "create"]),
                # The caller's account: the box records' subject, and its controller their owner.
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["get"]),
                # Each box's credential, which the Policy Controller issues; no Certificate rights.
                RolePolicyRule(
                    resources=[custom_resource(GROUP, "workloadcredentials")],
                    verbs=["get", "create", "update", "delete"],
                ),
            ]
        )
        if self.env.actions.orchestration_v1 is not None:
            # The EnvironmentService creates Sandboxes; Actions reads the exact created UID and
            # writes launcher grants, which `_add_launcher_admission` limits to that shape.
            rules_by_namespace[namespace].append(
                RolePolicyRule(resources=[custom_resource(GROUP, "sandboxes")], verbs=["get"])
            )
            rules_by_namespace[namespace].append(
                RolePolicyRule(
                    resources=[custom_resource(GROUP, resource) for resource in ("policysets", "policybindings")],
                    verbs=["get", "create"],
                )
            )
            # A launch creates the Identity its Sandbox acts as, and deletes it if the Sandbox is refused.
            rules_by_namespace[namespace].append(
                RolePolicyRule(resources=[custom_resource(GROUP, "identities")], verbs=["get", "create", "delete"])
            )
        for target_namespace, rules in sorted(rules_by_namespace.items()):
            suffix = "" if target_namespace == namespace else f"-{target_namespace}"
            role = Role(
                self, f"role{suffix}", metadata=ApiObjectMetadata(name=NAME, namespace=target_namespace), rules=rules
            )
            binding = RoleBinding(
                self,
                f"rolebinding{suffix}",
                metadata=ApiObjectMetadata(name=NAME, namespace=target_namespace),
                role=role,
            )
            subject = (
                service_account
                if target_namespace == namespace
                else ServiceAccount.from_service_account_name(
                    self, f"service-account-ref-{target_namespace}", NAME, namespace_name=namespace
                )
            )
            binding.add_subjects(subject)

    def _database_env(self) -> dict[str, EnvValue]:
        return {env_name(Settings, "database_url"): self.database_url}

    def _container_env(self) -> dict[str, EnvValue]:
        env = self._database_env()
        namespace = self.env.namespace
        # Export metrics through the shared Alloy OTLP receiver.
        env["OTEL_METRICS_EXPORTER"] = EnvValue.from_value("otlp")
        env["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] = EnvValue.from_value("cumulative")
        env[env_name(Settings, "reader_accounts")] = EnvValue.from_value(
            json.dumps([ServiceAccountRef(namespace=namespace, name=notifications.NAME).model_dump()])
        )
        if self.env.actions.web_push is not None:
            env[env_name(Settings, "web_push", "private_key_pem")] = (
                SecretRef(namespace=namespace, name=self.env.actions.web_push.secret_name)
                .key("private-key-pem")
                .env_value(self, "web-push-secret")
            )
        if self.env.actions.github_mcp_client_secret_name is not None:
            env[env_name(Settings, "mcp_servers", "github", "client_id")] = (
                SecretRef(namespace=namespace, name=self.env.actions.github_mcp_client_secret_name)
                .key("client_id")
                .env_value(self, "github-mcp-client-secret-env")
            )
        return env | dict(self.container_env)

    def _reload_secrets(self) -> list[str]:
        """The Secrets `_container_env` and `_add_workload` wire in, so rotating any rolls the Deployment."""
        props = self.env.actions
        names = {mount.secret_name for mount in props.bearer_mcp_mounts}
        if props.web_push is not None:
            names.add(props.web_push.secret_name)
        if props.github_mcp_client_secret_name is not None:
            names.add(props.github_mcp_client_secret_name)
        if props.oauth_secret_items:
            names.add(self.mcp_oauth.name)
        return sorted(names)

    def _add_workload(self, settings_name: str) -> ServiceWorkload:
        reload_secrets = self._reload_secrets()
        readiness = tls_grpc_probe(
            port=self.service.pod_port,
            ca_file=workload_tls.TRUST_BUNDLE_FILE,
            server_uri=ACTIONS_URI,
            initial_delay_seconds=3,
        )
        liveness = tls_grpc_probe(
            port=self.service.pod_port,
            ca_file=workload_tls.TRUST_BUNDLE_FILE,
            server_uri=ACTIONS_URI,
            service="livez",
            initial_delay_seconds=20,
            period_seconds=30,
        )
        startup = tls_grpc_probe(
            port=self.service.pod_port,
            ca_file=workload_tls.TRUST_BUNDLE_FILE,
            server_uri=ACTIONS_URI,
            initial_delay_seconds=0,
            period_seconds=5,
            service="livez",
            failure_threshold=60,
        )
        workload = add_service_workload(
            self,
            self.env,
            serves=[self.service],
            container="actions",
            image=f"{_ACTIONS_IMAGE}:{_PLACEHOLDER_TAG}",
            image_pull_policy=self.image_pull_policy,
            replica_profile=self.env.replicas,
            env_variables=self._container_env(),
            readiness=readiness,
            liveness=liveness,
            startup=startup,
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=self.memory_request, limit=self.memory_limit),
            ),
            # 20s execution drain + 5s forced persistence, with room for adapter teardown.
            termination_grace=Duration.seconds(60),
            placement=self.placement,
            telemetry_enabled=True,
            registry_secret_name=self.registry_secret_name,
            annotations={
                **({"secret.reloader.stakater.com/reload": ",".join(reload_secrets)} if reload_secrets else {}),
                # No configMapGenerator hash rolls the Deployment on settings changes
                # (cluster/docs/cdk8s.md); reloader does.
                "configmap.reloader.stakater.com/reload": settings_name,
            },
        )
        container = workload.container

        if self.env.actions.oauth_secret_items:
            oauth_secret = Secret.from_secret_name(self, "mcp-oauth-secret", self.mcp_oauth.name)
            oauth_volume = Volume.from_secret(
                self,
                "oauth-volume",
                oauth_secret,
                default_mode=0o440,
                items={key: PathMapping(path=key) for key in self.env.actions.oauth_secret_items},
            )
            container.mount(MCP_OAUTH_DIR, oauth_volume, read_only=True)
        if self.env.actions.github_mcp_client_secret_name is not None:
            github_secret = Secret.from_secret_name(
                self, "github-mcp-client-secret", self.env.actions.github_mcp_client_secret_name
            )
            github_volume = Volume.from_secret(
                self,
                "github-mcp-client-volume",
                github_secret,
                default_mode=0o440,
                items={"client_secret": PathMapping(path="client_secret")},
            )
            container.mount("/etc/agentplane-github", github_volume, read_only=True)
        for mount in self.env.actions.bearer_mcp_mounts:
            bearer_secret = Secret.from_secret_name(self, f"{mount.name}-bearer-secret", mount.secret_name)
            bearer_volume = Volume.from_secret(
                self,
                f"{mount.name}-bearer-volume",
                bearer_secret,
                items={mount.secret_key: PathMapping(path=mount.file_name)},
                optional=mount.optional or None,
            )
            # Its own directory: a subPath file cannot be mounted inside the read-only
            # settings volume (runc: "not a directory").
            container.mount(f"/run/secrets/{mount.name}", bearer_volume, read_only=True)
        if self.env.actions.orchestration_v1 is not None or self.env.actions.session_delegation is not None:
            workload.project_tokens(
                "peer-tokens",
                _PEER_TOKEN_DIR,
                {"dispatcher": self.dispatcher_audience, "history": self.history_audience},
            )
        return workload

    def _add_network_policy(self) -> None:
        namespace = self.env.namespace
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=namespace),
            endpoint_selector=self.service.pods.selector,
            ingress=[
                notifications.service(namespace).pods.admit(self.service.pod_port),
                # egress.py and app.py import this module, so their Pods are named here.
                IngressRule.from_endpoints(cilium.endpoint_labels(namespace, "egress"), ports=[self.service.pod_port]),
                IngressRule.from_endpoints(cilium.endpoint_labels(namespace, "app"), ports=[self.service.pod_port]),
                # The staged edge has no Pods until the Connection/MCP traffic cutover.
                IngressRule.from_endpoints(
                    cilium.endpoint_labels(namespace, "mcp-edge"), ports=[self.service.pod_port]
                ),
            ],
            egress=[
                cilium.dns_egress(resolves=["*"]),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                database.egress(namespace),
                *(
                    [self.dispatcher.egress(), self.history.egress()]
                    if self.env.actions.orchestration_v1 is not None or self.env.actions.session_delegation is not None
                    else []
                ),
                *self.env.actions.extra_egress,
            ],
        )
