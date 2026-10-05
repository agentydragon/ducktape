"""Private Electric sync service for Agentplane's materialized conversation view."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    Service,
    Volume,
)
from constructs import Construct

from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_NAME = "agentplane-electric"
_LABELS = {"app.kubernetes.io/name": _NAME}
_IMAGE = "docker.io/electricsql/electric:1.8.1@sha256:9b4cebe2d8f51fb3ebaeb156e443ba09deaa7c9e731f146e81f7e48238bae211"
_STORAGE_DIR = "/var/lib/electric"
_RUN_AS = 65534  # The pinned image owns /app as nobody but leaves Config.User empty.


def service(namespace: str) -> ServiceRef:
    """Electric in one environment's namespace."""
    return ServiceRef(
        name=_NAME, port=Port(name="http", number=3000), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


class Electric(Construct):
    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        electric = service(env.namespace)
        # The replication role's login, which database.py mints.
        role = SecretRef(namespace=env.namespace, name="postgres-electric")
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(name=_NAME, namespace=env.namespace, labels=_LABELS),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=1,
            strategy=DeploymentStrategy.recreate(),
            termination_grace_period=Duration.seconds(60),
            automount_service_account_token=False,
            security_context=PodSecurityContextProps(
                ensure_non_root=True, user=_RUN_AS, group=_RUN_AS, fs_group=_RUN_AS
            ),
        )
        container = deployment.add_container(
            name="electric",
            image=_IMAGE,
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            env_variables={
                "POSTGRES_USER": role.key("username").env_value(self, "postgres-user-ref"),
                "POSTGRES_PASSWORD": role.key("password").env_value(self, "postgres-password-ref"),
                "POSTGRES_HOST": role.key("host").env_value(self, "postgres-host-ref"),
                "POSTGRES_PORT": role.key("port").env_value(self, "postgres-port-ref"),
                "POSTGRES_DB": role.key("dbname").env_value(self, "postgres-db-ref"),
                "DATABASE_URL": EnvValue.from_value(
                    "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)"
                ),
                # Staging OOM: 20 idle Electric pool sessions plus one replication connection.
                "ELECTRIC_DB_POOL_SIZE": EnvValue.from_value("10"),
                "ELECTRIC_INSECURE": EnvValue.from_value("true"),
                "ELECTRIC_PORT": EnvValue.from_value(str(electric.pod_port)),
                "ELECTRIC_STORAGE": EnvValue.from_value("fast_file"),
                "ELECTRIC_STORAGE_DIR": EnvValue.from_value(_STORAGE_DIR),
                "ELECTRIC_PERSISTENT_STATE": EnvValue.from_value("file"),
                "ELECTRIC_MANUAL_TABLE_PUBLISHING": EnvValue.from_value("true"),
                "ELECTRIC_REPLICATION_STREAM_ID": EnvValue.from_value("agentplane_conversation"),
                # Window rotation and exact payload reads create finite shapes. Enable Electric's
                # once-per-minute LRU expiry; the upstream default retains every shape forever.
                "ELECTRIC_MAX_SHAPES": EnvValue.from_value("1024"),
            },
            ports=[electric.port.container_port()],
            readiness=http_probe("/v1/health", port=electric.pod_port, initial_delay_seconds=5, period_seconds=10),
            liveness=http_probe("/v1/health", port=electric.pod_port, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(100)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=Size.gibibytes(1)),
            ),
            # Writable: its root filesystem writes are unaudited.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )
        # Shape logs are a cache of Postgres: a restart starts them empty, and each reader refetches
        # on its shape's 409.
        container.mount(
            _STORAGE_DIR, Volume.from_empty_dir(self, "storage-volume", "storage", size_limit=Size.gibibytes(5))
        )
        pod_policy.place(deployment, node_scheduling.HIL_OVH)
        pod_policy.harden(deployment)

        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=electric.name, namespace=env.namespace, labels=electric.labels),
            selector=deployment,
            ports=[electric.port.service_port()],
        )
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=_NAME, namespace=env.namespace),
            endpoint_selector=electric.pods.selector,
            # app.py imports this module, so the app's Pods are named here.
            ingress=[
                IngressRule.from_endpoints(
                    cilium.endpoint_labels(env.namespace, "agentplane-app"), ports=[electric.pod_port]
                )
            ],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": env.namespace, "k8s:cnpg.io/cluster": "postgres"},
                    database.POSTGRES_PORT,
                ),
            ],
        )
