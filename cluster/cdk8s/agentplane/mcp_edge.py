"""The MCP edge serving workload, permissions and schema migration Job."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    EnvValue,
    ImagePullPolicy,
    Job,
    MemoryResources,
    PathMapping,
    PodSecurityContextProps,
    RestartPolicy,
    Role,
    RoleBinding,
    RolePolicyRule,
    Secret,
    ServiceAccount,
    Volume,
)
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from agentplane.mcp_edge.settings import Settings
from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import actions, app as app_component, database
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.mcp_routes import add_public_route
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource, named_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo_registry.pull_credentials import forgejo_images_creds_secret_ref
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from util.settings_contract import env_name, validate_settings_fragment

NAME = "mcp-edge"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-mcp-edge:unset"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-mcp-edge-migrate:unset"
_SECRET_DIR = "/etc/agentplane-mcp"


def service(namespace: str) -> ServiceRef:
    return ServiceRef(name=NAME, port=Port(name="http", number=8080), pods=Pods(namespace, tuple(_LABELS.items())))


class McpEdge(Construct):
    """The workload and its scoped Kubernetes and network permissions."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        database_url: EnvValue,
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        image_pull_policy: ImagePullPolicy = ImagePullPolicy.ALWAYS,
        migrate_pull_policy: ImagePullPolicy = ImagePullPolicy.ALWAYS,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.endpoint = service(env.namespace)
        props = env.mcp_edge
        if not props.caller_service_account_namespaces:
            raise ValueError("MCP edge needs an explicit caller namespace")
        if any(account.namespace not in props.caller_service_account_namespaces for account in props.mintable_accounts):
            raise ValueError("MCP edge mintable accounts must be in its caller namespaces")

        content = {
            "host": "0.0.0.0",
            "port": self.endpoint.pod_port,
            "actions_target": f"{actions.service(env.namespace).fqdn}:{actions.service(env.namespace).port.number}",
            "operator_oidc": props.operator_oidc,
            "caller_service_account_namespaces": sorted(props.caller_service_account_namespaces),
            "mintable_accounts": sorted(f"{account.namespace}/{account.name}" for account in props.mintable_accounts),
        }
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{NAME}-settings", namespace=env.namespace),
            content=yaml_config(validate_settings_fragment(Settings, content)),
            path="/etc/agentplane-mcp-edge/settings.yaml",
        )
        variables = {env_name(Settings, "database_url"): database_url}
        if props.oauth_secret_name is not None:
            variables[env_name(Settings, "oauth")] = (
                SecretRef(namespace=env.namespace, name=props.oauth_secret_name)
                .key("oauth")
                .env_value(self, "oauth-secret-env")
            )
        workload = add_service_workload(
            self,
            env,
            serves=[self.endpoint],
            container=NAME,
            image=_IMAGE,
            image_pull_policy=image_pull_policy,
            replica_profile=props.replicas,
            env_variables=variables,
            readiness=grpc_probe(port=self.endpoint.pod_port, initial_delay_seconds=3),
            liveness=grpc_probe(port=self.endpoint.pod_port, service="livez", initial_delay_seconds=20),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            termination_grace=Duration.seconds(30),
            migrate=migrate_init_container(
                _MIGRATE_IMAGE,
                name="migrate",
                env_variables={env_name(Settings, "database_url"): database_url},
                image_pull_policy=migrate_pull_policy,
            ),
            placement=placement,
            telemetry_enabled=True,
            registry_secret_name=registry_secret_name,
            annotations={
                "configmap.reloader.stakater.com/reload": settings.config_map.name,
                **(
                    {"secret.reloader.stakater.com/reload": props.oauth_secret_name}
                    if props.oauth_secret_name is not None
                    else {}
                ),
            },
        )
        if props.oauth_secret_name is not None:
            oauth_secret = Secret.from_secret_name(self, "oauth-secret", props.oauth_secret_name)
            oauth_volume = Volume.from_secret(
                self,
                "oauth-volume",
                oauth_secret,
                default_mode=0o440,
                items={"client-secret": PathMapping(path="client-secret")},
            )
            workload.container.mount(_SECRET_DIR, oauth_volume, read_only=True)
        settings.mount_into(workload.container, env=Settings.config_file_env)
        self._add_rbac(workload.account)

    def _add_rbac(self, account: ServiceAccount) -> None:
        props = self.env.mcp_edge
        for namespace in sorted(props.caller_service_account_namespaces):
            mintable = sorted(item.name for item in props.mintable_accounts if item.namespace == namespace)
            rules = [RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["list", "watch"])]
            rules.extend(
                RolePolicyRule(resources=[named_resource("", "serviceaccounts", name)], verbs=["get"])
                for name in mintable
            )
            rules.extend(
                RolePolicyRule(resources=[named_resource("", "serviceaccounts/token", name)], verbs=["create"])
                for name in mintable
            )
            role = Role(
                self, f"role-{namespace}", metadata=ApiObjectMetadata(name=NAME, namespace=namespace), rules=rules
            )
            binding = RoleBinding(
                self, f"rolebinding-{namespace}", metadata=ApiObjectMetadata(name=NAME, namespace=namespace), role=role
            )
            binding.add_subjects(
                account
                if namespace == self.env.namespace
                else ServiceAccount.from_service_account_name(
                    self, f"account-ref-{namespace}", NAME, namespace_name=self.env.namespace
                )
            )

    def add_platform_resources(self) -> None:
        namespace = self.env.namespace
        endpoint = self.endpoint
        hostname = self.env.mcp_edge.hostname
        if hostname is not None:
            add_public_route(self, endpoint=endpoint, hostname=hostname)
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[
                *([IngressRule.from_gateway(endpoint.pod_port)] if hostname is not None else []),
                app_component.service(namespace).pods.admit(endpoint.pod_port),
                cilium.SCRAPERS.admit(endpoint.pod_port),
            ],
            egress=[
                cilium.dns_egress(resolves=["*"]),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                database.egress(namespace),
                actions.service(namespace).egress(),
                *self.env.mcp_edge.extra_egress,
            ],
        )
        ServiceMonitor(
            self,
            "service-monitor",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            selector=ServiceMonitorSpecSelector(match_labels=endpoint.labels),
            endpoints=[Endpoint.plain(port=endpoint.port.name)],
        )


class SchemaMigration(Construct):
    """Initialize the edge schema before transferring data or starting its serving workload."""

    def __init__(self, scope: Construct, id: str, env: Environment, *, database_url: EnvValue) -> None:
        super().__init__(scope, id)
        name = "mcp-edge-schema-migrate"
        labels = {"app.kubernetes.io/name": name}
        account = ServiceAccount(
            self, "account", metadata=ApiObjectMetadata(name=name, namespace=env.namespace), automount_token=False
        )
        job = Job(
            self,
            "job",
            metadata=ApiObjectMetadata(
                name=name, namespace=env.namespace, annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"}
            ),
            pod_metadata=ApiObjectMetadata(labels=labels),
            select=False,
            backoff_limit=3,
            active_deadline=Duration.minutes(10),
            restart_policy=RestartPolicy.NEVER,
            service_account=account,
            automount_service_account_token=False,
            enable_service_links=False,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "images-creds"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
            containers=[
                migrate_init_container(
                    _MIGRATE_IMAGE, name="migrate", env_variables={env_name(Settings, "database_url"): database_url}
                )
            ],
        )
        pod_policy.place(job, node_scheduling.HIL_OVH)
        pod_policy.harden(job)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=name, namespace=env.namespace),
            endpoint_selector=labels,
            ingress=[],
            egress=[cilium.dns_egress(), database.egress(env.namespace)],
        )
