"""The History Service takes the Session journals runners push and serves retained history from the
`sandbox_service` database. Its read API and JournalIngress share the TLS RPC listener; bearer
audiences and Sandbox SVID authentication remain authorized per method.
"""

from collections.abc import Mapping
from pathlib import Path

from cdk8s import ApiObjectMetadata, Duration, Size
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

from agentplane.environment_runtime_transport.config import HISTORY_SERVICE_URI
from agentplane.history_service.settings import RUNNER_INGRESS_AUDIENCE, DelegationSettings, IngressSettings, Settings
from agentplane.identity.auth import listener
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import central_sampler, database, egress, workload_tls
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.probes import tls_grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac

NAME = "history-service"
TOKEN_AUDIENCE = "agentplane-history-service"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-history-service"
# Exact network destination for opaque workload TLS through the central proxy.
EGRESS_POLICY = "runner-ingress"


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
        # TokenReview authenticates backend readers and the central sampler.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{env.namespace}-history-service-token-reviewer",
            service_account_name=NAME,
            namespace=env.namespace,
        )
        Role(
            self,
            "service-account-reader-role",
            metadata=ApiObjectMetadata(name=f"{NAME}-service-accounts", namespace=env.namespace),
            rules=[
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["get"]),
                RolePolicyRule(
                    resources=[custom_resource("agentplane.allegedly.works", "sandboxes")], verbs=["get", "list"]
                ),
                # A writer's SVID names its WorkloadCredential, read live on every request.
                RolePolicyRule(
                    resources=[custom_resource("agentplane.allegedly.works", "workloadcredentials")], verbs=["get"]
                ),
                # An Identity writer's SVID names its Identity, read live in the policy namespace.
                RolePolicyRule(resources=[custom_resource("agentplane.allegedly.works", "identities")], verbs=["get"]),
            ],
        )
        RoleBinding(
            self,
            "service-account-reader-rolebinding",
            metadata=ApiObjectMetadata(name=f"{NAME}-service-accounts", namespace=env.namespace),
            role=Role.from_role_name(self, "service-account-reader-role-ref", f"{NAME}-service-accounts"),
        ).add_subjects(
            ServiceAccount.from_service_account_name(
                self, "history-service-account", NAME, namespace_name=env.namespace
            )
        )
        ingress_settings = IngressSettings(
            token_audience=RUNNER_INGRESS_AUDIENCE,
            # Sandboxes run in the environment's own namespace.
            writer_namespaces=frozenset((env.namespace,)),
            sampler_accounts=frozenset(
                (ServiceAccountRef(namespace=env.namespace, name=central_sampler.NAME),)
                if env.central_sampler is not None
                else ()
            ),
            sampler_identity_callers=env.central_sampler is not None,
            settle_deltas=env.settle_deltas,
            compact_deltas=env.compact_deltas,
        )
        delegation = None
        if delegation_actor is not None:
            delegation = DelegationSettings(
                actor=f"{delegation_actor.namespace}/{delegation_actor.name}",
                # Session delegation names an Identity in policy_namespace as the effective caller.
                identity_callers=True,
                methods=(
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
                ),
            )
        # The History Service writes the `sandbox_service` database, so it connects as that
        # database's owner; its read API's sessions are read-only (main.go).
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="history-service",
            image=f"{_IMAGE}:unset",
            env_variables={"AGENTPLANE_HISTORY_SERVICE_DATABASE_URL": database_url, **(container_env or {})},
            readiness=tls_grpc_probe(
                port=endpoint.pod_port,
                ca_file=workload_tls.TRUST_BUNDLE_FILE,
                server_uri=HISTORY_SERVICE_URI,
                initial_delay_seconds=3,
                period_seconds=10,
            ),
            liveness=tls_grpc_probe(
                port=endpoint.pod_port,
                ca_file=workload_tls.TRUST_BUNDLE_FILE,
                server_uri=HISTORY_SERVICE_URI,
                initial_delay_seconds=20,
                period_seconds=30,
            ),
            startup=tls_grpc_probe(
                port=endpoint.pod_port,
                ca_file=workload_tls.TRUST_BUNDLE_FILE,
                server_uri=HISTORY_SERVICE_URI,
                initial_delay_seconds=0,
                period_seconds=5,
                failure_threshold=180,
            ),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(20)),
                memory=MemoryResources(request=memory_request, limit=memory_limit),
            ),
            termination_grace=Duration.seconds(30),
            placement=placement,
            telemetry_enabled=True,
            registry_secret_name=registry_secret_name,
        )
        serving_tls = workload_tls.serving_certificate(
            self, env, container=workload.container, name=NAME, uri=HISTORY_SERVICE_URI
        )
        assert serving_tls.cert_file is not None
        assert serving_tls.key_file is not None
        assert serving_tls.ca_file is not None
        settings = Settings(
            reader_accounts=frozenset(callers),
            token_audience=TOKEN_AUDIENCE,
            policy_namespace=env.namespace,
            ingress=ingress_settings,
            delegation=delegation,
            rpc=listener.Listener(
                listen_address=f"0.0.0.0:{endpoint.pod_port}",
                tls=listener.TLS(cert_file=Path(serving_tls.cert_file), key_file=Path(serving_tls.key_file)),
                auth=[
                    listener.KubernetesBearerAuth(audiences=frozenset({TOKEN_AUDIENCE, RUNNER_INGRESS_AUDIENCE})),
                    listener.SPIFFEAuth(ca_file=Path(serving_tls.ca_file)),
                ],
            ),
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
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=env.namespace),
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
