"""Serves retained Session history from the `sandbox_service` database, whose schema the
Sandbox Service's migrate init container still owns."""

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    Service,
    ServiceAccount,
)
from constructs import Construct

from agentplane.history_service.settings import CONFIG_FILE_ENV, Settings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.pod_disruption_budget import add_pod_disruption_budget
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_secret_ref
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import env_name

NAME = "agentplane-history-service"
TOKEN_AUDIENCE = "agentplane-history-service"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-history-service"
_HEALTH = Port(name="health", number=8081)


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


class HistoryService(Construct):
    def __init__(
        self, scope: Construct, id: str, env: Environment, *, reader: ServiceAccountRef, caller: ServiceRef
    ) -> None:
        super().__init__(scope, id)
        endpoint = service(env.namespace)
        account = ServiceAccount(
            self, "account", metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace), automount_token=True
        )
        # TokenReview proves the reader's projected workload token.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{env.namespace}-history-service-token-reviewer",
            service_account_name=NAME,
            namespace=env.namespace,
        )
        config = SettingsFile(
            self,
            "config",
            metadata=ApiObjectMetadata(name=f"{NAME}-config", namespace=env.namespace),
            model=Settings,
            content={
                "reader_accounts": [reader.model_dump()],
                "token_audience": TOKEN_AUDIENCE,
                "port": endpoint.port.number,
                "health_port": _HEALTH.number,
            },
            path="/etc/agentplane-history-service/config.yaml",
            supplied=[("database_url",)],
        )
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace, labels=_LABELS),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=env.replicas.count,
            strategy=env.replicas.strategy,
            min_ready=env.replicas.min_ready,
            termination_grace_period=Duration.seconds(30),
            service_account=account,
            automount_service_account_token=True,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "images-creds"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
        )
        container = deployment.add_container(
            name="history-service",
            image=f"{_IMAGE}:unset",
            # The History Service takes over the whole `sandbox_service` database, so it connects
            # as that database's owner; until it writes, its sessions are read-only (main.py).
            env_variables={
                env_name(Settings, "database_url"): SecretRef(namespace=env.namespace, name="postgres-sandbox-service")
                .key("uri")
                .env_value(self, "database")
            },
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            ports=[endpoint.port.container_port(), _HEALTH.container_port()],
            readiness=http_probe("/healthz", port=_HEALTH.number, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=_HEALTH.number, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(20)),
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
            ports=[endpoint.port.service_port()],
        )
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[caller.pods.admit(endpoint.pod_port)],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                EgressRule.to_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": env.namespace, "k8s:cnpg.io/cluster": "postgres"},
                    database.POSTGRES_PORT,
                ),
            ],
        )
        if env.replicas.pdb_min_available is not None:
            add_pod_disruption_budget(
                self,
                "pdb",
                name=NAME,
                namespace=env.namespace,
                min_available=env.replicas.pdb_min_available,
                selector=_LABELS,
            )
