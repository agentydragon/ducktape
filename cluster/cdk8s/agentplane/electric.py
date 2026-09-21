"""Private Electric sync service for Agentplane's materialized conversation view."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerPort,
    ContainerResources,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    PersistentVolumeAccessMode,
    PersistentVolumeClaim,
    PodSecurityContextProps,
    Protocol,
    Secret,
    SecretValue,
    Service,
    ServicePort,
    Volume,
)
from constructs import Construct

from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import container_security, database, node_scheduling
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import apply_pod_spec_patches
from cluster.cdk8s.probes import http_probe

NAME = "agentplane-electric"
PORT = 3000
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "docker.io/electricsql/electric:1.8.1@sha256:9b4cebe2d8f51fb3ebaeb156e443ba09deaa7c9e731f146e81f7e48238bae211"
_STORAGE_DIR = "/var/lib/electric"


class Electric(Construct):
    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        secret = Secret.from_secret_name(self, "postgres-electric-secret", "postgres-electric")
        claim = PersistentVolumeClaim(
            self,
            "storage",
            metadata=metadata(f"{NAME}-storage", env.namespace),
            access_modes=[PersistentVolumeAccessMode.READ_WRITE_ONCE],
            storage=Size.gibibytes(5),
            storage_class_name="seaweedfs-ovh",
        )
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(NAME, env.namespace, labels=_LABELS),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=1,
            strategy=DeploymentStrategy.recreate(),
            termination_grace_period=Duration.seconds(60),
            automount_service_account_token=False,
            security_context=PodSecurityContextProps(ensure_non_root=True, fs_group=1000),
        )
        container = deployment.add_container(
            name="electric",
            image=_IMAGE,
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            env_variables={
                "POSTGRES_USER": EnvValue.from_secret_value(SecretValue(secret=secret, key="username")),
                "POSTGRES_PASSWORD": EnvValue.from_secret_value(SecretValue(secret=secret, key="password")),
                "POSTGRES_HOST": EnvValue.from_secret_value(SecretValue(secret=secret, key="host")),
                "POSTGRES_PORT": EnvValue.from_secret_value(SecretValue(secret=secret, key="port")),
                "POSTGRES_DB": EnvValue.from_secret_value(SecretValue(secret=secret, key="dbname")),
                "DATABASE_URL": EnvValue.from_value(
                    "postgresql://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@$(POSTGRES_HOST):$(POSTGRES_PORT)/$(POSTGRES_DB)"
                ),
                "ELECTRIC_INSECURE": EnvValue.from_value("true"),
                "ELECTRIC_PORT": EnvValue.from_value(str(PORT)),
                "ELECTRIC_STORAGE": EnvValue.from_value("fast_file"),
                "ELECTRIC_STORAGE_DIR": EnvValue.from_value(_STORAGE_DIR),
                "ELECTRIC_PERSISTENT_STATE": EnvValue.from_value("file"),
                "ELECTRIC_MANUAL_TABLE_PUBLISHING": EnvValue.from_value("true"),
                "ELECTRIC_REPLICATION_STREAM_ID": EnvValue.from_value("agentplane_conversation"),
                # Window rotation and exact payload reads create finite shapes. Enable Electric's
                # once-per-minute LRU expiry; the upstream default retains every shape forever.
                "ELECTRIC_MAX_SHAPES": EnvValue.from_value("1024"),
            },
            ports=[ContainerPort(name="http", number=PORT, protocol=Protocol.TCP)],
            readiness=http_probe("/v1/health", port=PORT, initial_delay_seconds=5, period_seconds=10),
            liveness=http_probe("/v1/health", port=PORT, initial_delay_seconds=20, period_seconds=30),
            # WAL-loss recovery takes Electric out of service while it drops shapes and builds a
            # fresh replication pipeline. The real PG18/Electric 1.8.1 probe remained unready
            # for over one minute, so permit three minutes before liveness can restart it.
            startup=http_probe(
                "/v1/health", port=PORT, initial_delay_seconds=0, period_seconds=10, failure_threshold=18
            ),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(100)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=Size.gibibytes(1)),
            ),
            security_context=container_security.WRITABLE_ROOT,
        )
        container.mount(_STORAGE_DIR, Volume.from_persistent_volume_claim(self, "storage-volume", claim))
        node_scheduling.attract_to_zone(deployment)
        apply_pod_spec_patches(deployment)

        Service(
            self,
            "service",
            metadata=metadata(NAME, env.namespace, labels=_LABELS),
            selector=deployment,
            ports=[ServicePort(name="http", port=PORT, target_port=PORT, protocol=Protocol.TCP)],
        )
        cilium.network_policy(
            self,
            "networkpolicy",
            metadata=metadata(NAME, env.namespace),
            selector=_LABELS,
            ingress=[cilium.ingress_from(cilium.endpoint_labels(env.namespace, "agentplane-app"), ports=[PORT])],
            egress=[
                cilium.dns_egress(),
                cilium.egress_to(
                    {"k8s:io.kubernetes.pod.namespace": env.namespace, "k8s:cnpg.io/cluster": "postgres"},
                    database.POSTGRES_PORT,
                ),
            ],
        )
