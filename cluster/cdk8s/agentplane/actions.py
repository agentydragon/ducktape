"""The Action Service: its Deployment (+ Goose migrate initContainer), RBAC, ConfigMaps,
Service, NetworkPolicy, ServiceMonitor, optional PodDisruptionBudget, the public HTTPRoute of
an environment with an Action Service hostname, and the admission policy on the launcher grants of
an environment with the orchestration_v1 group. Staging's policy sets are added by its chart.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    DeploymentStrategy,
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
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from agentplane.action_service.catalog import ActionGroup
from agentplane.action_service.orchestration_v1.binding import (
    LaunchPreset,
    OrchestrationExecutorBinding,
    PresetLaunch,
    PresetSession,
    PresetSessionSpec,
)
from agentplane.action_service.settings import ServicePeer, Settings
from agentplane.crd_group import GROUP
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import database, notifications
from cluster.cdk8s.agentplane.environment import Environment, OrchestrationProps
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import ServiceWorkload, add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
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
SANDBOX_GROUP = "sandbox"
_PEER_TOKEN_DIR = "/var/run/secrets/actions"
# The orchestration_v1 executor and its admission policy both name launcher grants `<sandbox>.launcher`.
_LAUNCHER_SUFFIX = ".launcher"
_LAUNCHER_ACTIONS = (
    "open_session",
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
_DEFAULT_MEMORY_LIMIT = Size.mebibytes(512)
_DEFAULT_MIGRATE_MEMORY_LIMIT = Size.mebibytes(256)


def service(namespace: str) -> ServiceRef:
    """The Action Service in one environment's namespace."""
    return ServiceRef(
        name=NAME, port=Port(name="http", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def orchestration_callers(env: Environment) -> dict[ServiceAccountRef, Pods]:
    """The Action Service as a caller of the Session Dispatcher and the History Service, in an
    environment with the orchestration_v1 group."""
    if env.actions.orchestration_v1 is None:
        return {}
    return {ServiceAccountRef(namespace=env.namespace, name=NAME): service(env.namespace).pods}


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
            open_session=PresetSession(spec=spec, setup_script=thread.setup_script),
        )
    return ActionGroup(
        title="Orchestration",
        description=(
            "Launch Session Sandboxes and drive their Sessions: open them, send input, interrupt, change "
            "model or effort, and read their state. A launch waits for the operator and grants its caller "
            "what it names on the new Sandbox."
        ),
        executor=OrchestrationExecutorBinding(
            kind="orchestration_v1",
            description="Run by this service, on each request's Decision, with its own authority.",
            namespace=env.namespace,
            default_policy_sets=frozenset(app.default_policy_sets),
            presets=presets,
        ),
        direct_tools=props.direct_tools,
    )


def add_launcher_admission(scope: Construct, namespace: str) -> None:
    """Admits the `namespace` Action Service's Sandbox, PolicySet and PolicyBinding creations only in
    the shape the orchestration_v1 executor writes (agentplane/action_service/orchestration_v1/README.md): a
    grant owned by one Sandbox, named for it, naming only it and only this group's Actions, and a
    Sandbox bound to no launcher grant. A bug in the executor then cannot grant past the Sandbox
    it launched. Admission cannot see who called the Action, so it limits what a grant reaches,
    not who holds it."""
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
                        resources=["sandboxes", "policysets", "policybindings"],
                    )
                ]
            ),
            match_conditions=[
                k8s.MatchCondition(
                    name="action-service",
                    expression=f"request.userInfo.username == 'system:serviceaccount:{namespace}:{NAME}'",
                )
            ],
            variables=[
                k8s.Variable(name="owners", expression="object.metadata.?ownerReferences.orValue([])"),
                k8s.Variable(name="grant", expression="request.resource.resource != 'sandboxes'"),
            ],
            validations=[
                k8s.Validation(
                    expression=(
                        "variables.grant || !object.spec.?policySets.orValue([])"
                        f".exists(set, set.endsWith('{_LAUNCHER_SUFFIX}'))"
                    ),
                    message="A Sandbox this service launches is bound to no launcher grant.",
                ),
                k8s.Validation(
                    expression=(
                        "!variables.grant || (size(variables.owners) == 1"
                        f" && {owner}.apiVersion.startsWith('{GROUP}/') && {owner}.kind == 'Sandbox'"
                        f" && object.metadata.name == {owner}.name + '{_LAUNCHER_SUFFIX}')"
                    ),
                    message="A launcher grant is owned by one Sandbox and named for it.",
                ),
                k8s.Validation(
                    expression=(
                        "request.resource.resource != 'policybindings'"
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
    """ServiceAccount, RBAC, ConfigMaps, Deployment (+ migrate initContainer), Service,
    and optional PodDisruptionBudget for the Action Service. The fleet network policy, metrics
    scrape and public route are added by `add_platform_resources`.
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
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
        migrate_pull_policy: ImagePullPolicy = ImagePullPolicy.ALWAYS,
        migrate_memory_limit: Size | None = _DEFAULT_MIGRATE_MEMORY_LIMIT,
        container_env: Mapping[str, EnvValue] | None = None,
    ) -> None:
        """`history` and `dispatcher` are the peers the orchestration_v1 group calls, in an environment
        that has it."""
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
        self.memory_limit = memory_limit
        self.migrate_pull_policy = migrate_pull_policy
        self.migrate_memory_limit = migrate_memory_limit
        self.service = service(env.namespace)
        self.mcp_oauth = mcp_oauth_secret(env.namespace)

        props = env.actions
        content: dict[str, Any] = {
            "host": "0.0.0.0",
            "port": self.service.pod_port,
            "token_audience": WORKLOAD_TOKEN_AUDIENCE,
            # CLEANUP(added 2026-10-27): Keep false until the manifest key is removed after this image rolls out.
            "integrated_connection_service": False,
            "operator_oidc": props.operator_oidc,
            "policy_namespace": env.namespace,
            "caller_service_account_namespaces": sorted(props.caller_service_account_namespaces),
            "direct_wait_seconds": 30,
            # CLEANUP(added 2026-10-27): Retain the legacy maximum until the new image rolls out and this key is removed.
            "max_wait_seconds": 180,
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
            )
            content["history_service"] = ServicePeer(
                target=f"{history.fqdn}:{history.pod_port}",
                token_file=f"{_PEER_TOKEN_DIR}/history",
                request_timeout_s=_PEER_TIMEOUT_SECONDS,
            )
        content["action_groups"] = groups
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name="actions-settings", namespace=env.namespace),
            content=yaml_config(validate_settings_fragment(Settings, content)),
            path="/etc/agentplane-actions/settings.yaml",
        )
        workload = self._add_workload(settings)
        self._add_rbac(workload.account)
        if props.orchestration_v1 is not None:
            add_launcher_admission(self, env.namespace)

    def add_platform_resources(self) -> None:
        """Add the Cilium policy and metrics scrape used by fleet environments, and the public route
        of one with a `hostname`."""
        hostname = self.env.actions.hostname
        if hostname is not None:
            self._add_http_route(hostname)
        elif self.env.actions.mcp_client_metadata is not None:
            raise ValueError("mcp_client_metadata needs an Action Service hostname to publish it")
        self._add_network_policy(public=hostname is not None)
        ServiceMonitor(
            self,
            "service-monitor",
            metadata=ApiObjectMetadata(name=NAME, namespace=self.env.namespace),
            selector=ServiceMonitorSpecSelector(match_labels=self.service.labels),
            endpoints=[Endpoint.plain(port=self.service.port.name)],
        )

    def _add_rbac(self, service_account: ServiceAccount) -> None:
        namespace = self.env.namespace
        # TokenReview proves the Pod-bound workload bearer the central egress proxy
        # forwards, and the session-bound bearer the app forwards for its BFF/operator
        # adapter. Creating a review grants none of the reviewed identity's authority.
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
        # policy informer lists and watches both and reads only what it holds: no `get`.
        rules_by_namespace: dict[str, list[RolePolicyRule]] = {
            namespace: [
                RolePolicyRule(
                    resources=[custom_resource(GROUP, resource) for resource in ("policysets", "policybindings")],
                    verbs=["list", "watch"],
                ),
                RolePolicyRule(resources=[custom_resource(GROUP, "policybindings/status")], verbs=["patch"]),
            ]
        }
        for caller_namespace in self.env.actions.caller_service_account_namespaces:
            rules_by_namespace.setdefault(caller_namespace, []).append(
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["list", "watch"])
            )

        # The sandbox ActionGroup creates its boxes as plain Pods from its PodTemplates, running as
        # their callers and owned by the caller's ServiceAccount, and runs commands in them. This is namespace-wide and cannot
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
                # The caller's account, for the owner reference that collects its boxes with it.
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["get"]),
            ]
        )
        if self.env.actions.orchestration_v1 is not None:
            # The orchestration_v1 group launches Sandboxes and writes their launcher grants, which
            # `_add_launcher_admission` limits to that shape. `get` reads back an object a retried
            # launch already wrote.
            rules_by_namespace[namespace].append(
                RolePolicyRule(
                    resources=[
                        custom_resource(GROUP, resource) for resource in ("sandboxes", "policysets", "policybindings")
                    ],
                    verbs=["get", "create"],
                )
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

    def _add_workload(self, settings: SettingsFile) -> ServiceWorkload:
        reload_secrets = self._reload_secrets()
        readiness = grpc_probe(port=self.service.pod_port, initial_delay_seconds=3)
        liveness = grpc_probe(port=self.service.pod_port, service="livez", initial_delay_seconds=20, period_seconds=30)
        workload = add_service_workload(
            self,
            self.env,
            serves=[self.service],
            container="actions",
            image=f"{_ACTIONS_IMAGE}:{_PLACEHOLDER_TAG}",
            image_pull_policy=self.image_pull_policy,
            replica_profile=replace(self.env.replicas, strategy=DeploymentStrategy.recreate()),
            env_variables=self._container_env(),
            readiness=readiness,
            liveness=liveness,
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=self.memory_limit),
            ),
            # 20s execution drain + 5s forced persistence, with room for HTTP/adapter teardown.
            termination_grace=Duration.seconds(60),
            migrate=migrate_init_container(
                f"{_ACTIONS_IMAGE}:{_PLACEHOLDER_TAG}",
                name="migrate",
                env_variables=self._database_env(),
                command=["/usr/local/bin/actions-migrate"],
                image_pull_policy=self.migrate_pull_policy,
                memory_limit=self.migrate_memory_limit,
            ),
            placement=self.placement,
            telemetry_enabled=True,
            registry_secret_name=self.registry_secret_name,
            annotations={
                **({"secret.reloader.stakater.com/reload": ",".join(reload_secrets)} if reload_secrets else {}),
                # No configMapGenerator hash rolls the Deployment on settings changes
                # (cluster/docs/cdk8s.md); reloader does.
                "configmap.reloader.stakater.com/reload": settings.config_map.name,
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
        settings.mount_into(container, env=Settings.config_file_env)
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
        if self.env.actions.orchestration_v1 is not None:
            workload.project_tokens(
                "peer-tokens",
                _PEER_TOKEN_DIR,
                {"dispatcher": self.dispatcher_audience, "history": self.history_audience},
            )
        return workload

    def _add_http_route(self, hostname: str) -> None:
        # Actions publishes only its upstream Client ID Metadata Document; the MCP edge owns /mcp and OAuth.
        client_metadata_paths = []
        client_metadata = self.env.actions.mcp_client_metadata
        if client_metadata is not None:
            metadata_url = client_metadata.url
            if metadata_url.host != hostname or metadata_url.port != 443:
                raise ValueError("mcp_client_metadata.url must use this Action Service's HTTPS hostname")
            assert metadata_url.path is not None
            client_metadata_paths.append(metadata_url.path)
        paths = tuple(sorted(set(client_metadata_paths)))
        if not paths:
            raise ValueError("Action Service hostname needs client metadata")
        https_route(
            self,
            "httproute",
            metadata=ApiObjectMetadata(name=f"{NAME}-mcp", namespace=self.env.namespace),
            hostnames=[hostname],
            backend=self.service,
            paths=paths,
            timeout="3600s",
        )

    def _add_network_policy(self, *, public: bool) -> None:
        """`public` admits the Gateway, which serves the public route."""
        namespace = self.env.namespace
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=namespace),
            endpoint_selector=self.service.pods.selector,
            ingress=[
                *([IngressRule.from_gateway(self.service.pod_port)] if public else []),
                cilium.SCRAPERS.admit(self.service.pod_port),
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
                    if self.env.actions.orchestration_v1 is not None
                    else []
                ),
                *self.env.actions.extra_egress,
            ],
        )
