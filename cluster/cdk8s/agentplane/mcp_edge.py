"""The MCP edge serving workload and permissions."""

from __future__ import annotations

from pathlib import Path

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
)
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from agentplane.action_service.policies.resources import CALLER_LABEL
from agentplane.crd_group import GROUP
from agentplane.environment_runtime_transport.config import ACTIONS_URI
from agentplane.identity.auth.audience import WORKLOAD_TOKEN_AUDIENCE
from agentplane.mcp_edge.settings import Settings
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import actions, app as app_component, database, workload_tls
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.mcp_routes import add_public_route
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile

NAME = "mcp-edge"
DATABASE_URL_ENV = "AGENTPLANE_MCP_EDGE_DATABASE_URL"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-mcp-edge:unset"
_SECRET_DIR = "/etc/agentplane-mcp"
_DEFAULT_MEMORY_REQUEST = Size.mebibytes(128)


def service(namespace: str) -> ServiceRef:
    """The gRPC listener: ConnectionService and health."""
    return ServiceRef(name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace, tuple(_LABELS.items())))


def http_service(namespace: str) -> ServiceRef:
    """The HTTP listener: the facade, /mcp, OAuth and metrics."""
    return ServiceRef(name=NAME, port=Port(name="http", number=8081), pods=Pods(namespace, tuple(_LABELS.items())))


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
        memory_request: Size = _DEFAULT_MEMORY_REQUEST,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.endpoint = service(env.namespace)
        self.http = http_service(env.namespace)
        props = env.mcp_edge

        settings_contract = Settings(
            actions_tls=workload_tls.client_tls(ACTIONS_URI),
            actions_target=f"{actions.service(env.namespace).fqdn}:{actions.service(env.namespace).port.number}",
            policy_namespace=env.namespace,
            actions_token_file=(
                "/var/run/secrets/agentplane-mcp/actions" if props.oauth_secret_name is not None else None
            ),
            oauth_file=Path(_SECRET_DIR) / "oauth" if props.oauth_secret_name is not None else None,
            host="0.0.0.0",
            port=self.endpoint.pod_port,
            http_port=self.http.pod_port,
            operator_oidc=props.operator_oidc,
        )
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{NAME}-settings", namespace=env.namespace),
            content=yaml_config(settings_contract),
            path="/etc/agentplane-mcp-edge/settings.yaml",
        )
        variables = {DATABASE_URL_ENV: database_url}
        workload = add_service_workload(
            self,
            env,
            serves=[self.endpoint, self.http],
            container=NAME,
            image=_IMAGE,
            image_pull_policy=image_pull_policy,
            replica_profile=props.replicas,
            env_variables=variables,
            service_account_labels={CALLER_LABEL: "true"} if props.oauth_secret_name is not None else None,
            readiness=grpc_probe(port=self.endpoint.pod_port, initial_delay_seconds=3),
            liveness=grpc_probe(port=self.endpoint.pod_port, service="livez", initial_delay_seconds=20),
            startup=grpc_probe(
                port=self.endpoint.pod_port,
                service="livez",
                initial_delay_seconds=0,
                period_seconds=5,
                failure_threshold=60,
            ),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=memory_request, limit=Size.mebibytes(512)),
            ),
            termination_grace=Duration.seconds(30),
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
                items={"client-secret": PathMapping(path="client-secret"), "oauth": PathMapping(path="oauth")},
            )
            workload.container.mount(_SECRET_DIR, oauth_volume, read_only=True)
        settings.mount_into(workload.container, env=Settings.config_file_env)
        workload_tls.mount_trust_bundle(self, workload.container)
        if props.oauth_secret_name is not None:
            workload.project_tokens(
                "actions-token", "/var/run/secrets/agentplane-mcp", {"actions": WORKLOAD_TOKEN_AUDIENCE}
            )
        self._add_rbac(workload.account)

    def _add_rbac(self, account: ServiceAccount) -> None:
        # ConnectionService is registered even without OAuth, and its Identity informer is a
        # readiness dependency in every deployment. Keep this read-only role unconditional;
        # OAuth-specific secrets and projected tokens remain conditional above.
        namespace = self.env.namespace
        role = Role(
            self,
            "identity-reader-role",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            rules=[RolePolicyRule(resources=[custom_resource(GROUP, "identities")], verbs=["get", "list", "watch"])],
        )
        binding = RoleBinding(
            self, "identity-reader-rolebinding", metadata=ApiObjectMetadata(name=NAME, namespace=namespace), role=role
        )
        binding.add_subjects(account)

    def add_platform_resources(self) -> None:
        namespace = self.env.namespace
        endpoint = self.endpoint
        hostname = self.env.mcp_edge.hostname
        if hostname is not None:
            add_public_route(self, endpoint=self.http, hostname=hostname)
        action_hostname = self.env.actions.hostname
        client_metadata = self.env.actions.mcp_client_metadata
        if action_hostname is not None:
            if client_metadata is None:
                raise ValueError("Action Service hostname needs client metadata")
            metadata_url = client_metadata.url
            if metadata_url.host != action_hostname or metadata_url.port != 443:
                raise ValueError("mcp_client_metadata.url must use this Action Service's HTTPS hostname")
            assert metadata_url.path is not None
            https_route(
                self,
                "action-client-metadata-route",
                metadata=ApiObjectMetadata(name=f"{actions.NAME}-mcp", namespace=namespace),
                hostnames=[action_hostname],
                backend=self.http,
                paths=(metadata_url.path,),
                timeout="3600s",
            )
        elif client_metadata is not None:
            raise ValueError("mcp_client_metadata needs an Action Service hostname to publish it")
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[
                *(
                    [IngressRule.from_gateway(self.http.pod_port)]
                    if hostname is not None or action_hostname is not None
                    else []
                ),
                app_component.service(namespace).pods.admit(endpoint.pod_port),
                cilium.SCRAPERS.admit(self.http.pod_port),
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
            endpoints=[Endpoint.plain(port=self.http.port.name)],
        )
