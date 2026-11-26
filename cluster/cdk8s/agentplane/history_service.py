"""The History Service takes the Session journals runners push and serves retained history from the
`sandbox_service` database. Its read API and JournalIngress share the TLS RPC listener;
workload Identity and exact service SVID grants are authorized per method.
"""

from collections.abc import Mapping

from cdk8s import ApiObjectMetadata, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    EnvValue,
    MemoryResources,
    Role,
    RoleBinding,
    RolePolicyRule,
    ServiceAccount,
)
from constructs import Construct

from agentplane.crd_group import GROUP
from agentplane.history_service.settings import IngressSettings, ServiceDelegationSettings, ServiceDelegator, Settings
from agentplane.net.peertls.config import (
    ACTIONS_URI,
    APP_URI,
    CENTRAL_SAMPLER_URI,
    HISTORY_SERVICE_URI,
    NOTIFICATIONS_URI,
)
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import central_sampler, database, egress, workload_tls
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.probes import TlsGrpcHealth
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile

NAME = "history-service"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-history-service"
# Exact network destination for opaque workload TLS through the central proxy.
EGRESS_POLICY = "runner-ingress"
_HISTORY_READ_METHODS = (
    "/agentplane.history_service.HistoryService/ReadSessionEvents",
    "/agentplane.history_service.HistoryService/FollowSession",
    "/agentplane.history_service.HistoryService/ReadSessionObservations",
    "/agentplane.history_service.HistoryService/GetSessionSummary",
    "/agentplane.history_service.HistoryService/BatchGetSessionSummaries",
    "/agentplane.history_service.HistoryService/ReadThreadEntities",
    "/agentplane.history_service.HistoryService/ReadEntityEvidence",
    "/agentplane.history_service.HistoryService/ReadNativeFrames",
    "/agentplane.history_service.HistoryService/FindSessions",
    "/agentplane.history_service.HistoryService/WatchSessionSummaries",
)


_DEFAULT_MEMORY_REQUEST = Size.mebibytes(128)
_DEFAULT_MEMORY_LIMIT = Size.mebibytes(512)


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


class HistoryService(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        database_url: EnvValue,
        callers: dict[ServiceAccountRef, Pods],
        delegation_actor: ServiceAccountRef | None,
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        container_env: Mapping[str, EnvValue] | None = None,
        memory_request: Size = _DEFAULT_MEMORY_REQUEST,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.callers = callers
        endpoint = service(env.namespace)
        role = Role(
            self,
            "service-account-reader-role",
            metadata=ApiObjectMetadata(name=f"{NAME}-service-accounts", namespace=env.namespace),
            rules=[
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["get"]),
                RolePolicyRule(resources=[custom_resource(GROUP, "sandboxes")], verbs=["get", "list"]),
                # A writer's SVID names its WorkloadCredential, read live on every request.
                RolePolicyRule(resources=[custom_resource(GROUP, "workloadcredentials")], verbs=["get"]),
                # An Identity writer's SVID names its Identity, read live in the policy namespace.
                RolePolicyRule(resources=[custom_resource(GROUP, "identities")], verbs=["get"]),
            ],
        )
        RoleBinding(
            self,
            "service-account-reader-rolebinding",
            metadata=ApiObjectMetadata(name=f"{NAME}-service-accounts", namespace=env.namespace),
            role=role,
        ).add_subjects(
            ServiceAccount.from_service_account_name(
                self, "history-service-account", NAME, namespace_name=env.namespace
            )
        )
        ingress_settings = IngressSettings(
            # Sandboxes run in the environment's own namespace.
            writer_namespaces=frozenset((env.namespace,)),
            sampler_service_actor=CENTRAL_SAMPLER_URI,
            settle_deltas=env.settle_deltas,
            compact_deltas=env.compact_deltas,
            compact_execution_evidence=env.compact_execution_evidence,
            event_block_writes=env.history_event_block_writes,
        )
        service_delegation = (
            ServiceDelegationSettings(actors=(ServiceDelegator(spiffe_id=ACTIONS_URI),), methods=_HISTORY_READ_METHODS)
            if delegation_actor is not None
            else None
        )
        # The History Service writes the `sandbox_service` database, so it connects as that
        # database's owner; its read API's sessions are read-only (main.go).
        health = TlsGrpcHealth(
            port=endpoint.pod_port, ca_file=workload_tls.TRUST_BUNDLE_FILE, server_uri=HISTORY_SERVICE_URI
        )
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="history-service",
            image=f"{_IMAGE}:unset",
            env_variables={"AGENTPLANE_HISTORY_SERVICE_DATABASE_URL": database_url, **(container_env or {})},
            readiness=health.readiness(),
            liveness=health.liveness(),
            startup=health.startup(failure_threshold=180),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(20)),
                memory=MemoryResources(request=memory_request, limit=memory_limit),
            ),
            placement=placement,
            telemetry_enabled=True,
            registry_secret_name=registry_secret_name,
        )
        serving_tls = workload_tls.service_certificate(
            self, env, container=workload.container, name=NAME, uri=HISTORY_SERVICE_URI
        )
        settings = Settings(
            reader_service_callers=frozenset((ACTIONS_URI, APP_URI, NOTIFICATIONS_URI)),
            service_delegation=service_delegation,
            policy_namespace=env.namespace,
            ingress=ingress_settings,
            rpc=serving_tls.spiffe_listener(endpoint.pod_port),
        )
        config = SettingsFile(
            self,
            "config",
            metadata=ApiObjectMetadata(name=f"{NAME}-config", namespace=env.namespace),
            content=yaml_config(settings),
            path="/etc/agentplane-history-service/config.yaml",
        )
        config.mount_into(workload.container, env=Settings.config_file_env)

    def add_platform_resources(self) -> None:
        """Add fleet network admission."""
        env = self.env
        endpoint = service(env.namespace)
        proxy = egress.proxy(env.namespace)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[
                *(pods.admit(endpoint.pod_port) for pods in self.callers.values()),
                proxy.pods.admit(endpoint.pod_port),
                *(
                    [central_sampler.pods(env.namespace).admit(endpoint.pod_port)]
                    if env.central_sampler is not None
                    else []
                ),
            ],
            egress=[cilium.dns_egress(), EgressRule.to_entities(Entity.KUBE_APISERVER), database.egress(env.namespace)],
        )
        egress.add_runner_egress_network_policy(self, policy=EGRESS_POLICY, endpoint=endpoint)
