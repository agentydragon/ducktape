"""Serves retained Session history from the `sandbox_service` database, migrates its schema, and can
ingest it from the Sandbox Service."""

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import ContainerResources, Cpu, CpuResources, MemoryResources
from constructs import Construct

from agentplane.history_service.settings import CONFIG_FILE_ENV, Settings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import database, notifications
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.probes import grpc_probe
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
_SANDBOX_SERVICE_TOKEN_DIR = "/var/run/secrets/agentplane-sandbox-service"


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
        reader: ServiceAccountRef,
        caller: ServiceRef,
        sandboxes: ServiceRef,
        sandboxes_audience: str,
        gateway: ServiceRef,
    ) -> None:
        super().__init__(scope, id)
        endpoint = service(env.namespace)
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
                # The app reads Threads; the Notification Service reads command receipts.
                "reader_accounts": [
                    reader.model_dump(),
                    ServiceAccountRef(namespace=env.namespace, name=notifications.NAME).model_dump(),
                ],
                "token_audience": TOKEN_AUDIENCE,
                "port": endpoint.port.number,
                "sandbox_service": {
                    "target": f"{sandboxes.fqdn}:{sandboxes.pod_port}",
                    "gateway_target": f"{gateway.fqdn}:{gateway.pod_port}",
                    "token_file": f"{_SANDBOX_SERVICE_TOKEN_DIR}/token",
                    "grpc_channel_options": env.app_config.sandbox_service_grpc_channel_options,
                },
                "ingester": {"settle_deltas": env.settle_deltas},
                "fold_shadow": env.fold_shadow,
            },
            path="/etc/agentplane-history-service/config.yaml",
            supplied=[("database_url",)],
        )
        # The History Service writes the `sandbox_service` database, so it connects as that
        # database's owner; its read API's sessions are read-only (main.py).
        database_url = (
            SecretRef(namespace=env.namespace, name="postgres-sandbox-service").key("uri").env_value(self, "database")
        )
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="history-service",
            image=f"{_IMAGE}:unset",
            env_variables={env_name(Settings, "database_url"): database_url},
            ports=[endpoint.port.container_port()],
            readiness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(20)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            termination_grace=Duration.seconds(30),
            migrate=migrate_init_container(
                "git.allegedly.works/ducktape-ci/agentplane-history-migrate:unset",
                name="migrate",
                env_variables={"AGENTPLANE_SANDBOX_SERVICE_DATABASE_URL": database_url},
            ),
        )
        config.mount_into(workload.container, env=CONFIG_FILE_ENV)
        # For the ingester's Sandbox Service and Runner Gateway calls.
        workload.project_tokens("sandbox-service-token", _SANDBOX_SERVICE_TOKEN_DIR, {"token": sandboxes_audience})
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[
                caller.pods.admit(endpoint.pod_port),
                notifications.service(env.namespace).pods.admit(endpoint.pod_port),
            ],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                sandboxes.egress(),
                gateway.egress(),
                database.egress(env.namespace),
            ],
        )
