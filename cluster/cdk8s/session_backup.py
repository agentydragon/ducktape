"""Authenticated Restic REST storage for workstation AI session backups.

The primary repository and its versioned ``ResticBackup`` both use SeaweedFS; the
separate bucket is an in-cluster logical backup, not an independent substrate copy.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart, Duration, Size
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
    PathMapping,
    PersistentVolumeAccessMode,
    PersistentVolumeClaim,
    PodSecurityContextProps,
    Probe,
    Secret,
    Service,
    Volume,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.pod_policy import harden
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy, deny_all_egress
from cluster.cdk8s.restic_backup import ResticBackup
from cluster.cdk8s.seaweedfs import s3
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = NAMESPACE = "session-backup"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"
HOSTNAME = "restic.allegedly.works"
_DATA_CLAIM = "session-backup-data"
_REST_SERVER_AUTH = "session-backup-rest-server-auth"
_BUCKET = "session-backup-backups"
_S3_CREDENTIALS = "session-backup-seaweedfs-credentials"
_SERVER_PORT = 8000
_LABELS = {"app.kubernetes.io/name": NAME}
_SERVER = ServiceRef(
    name=NAME, port=Port(name="http", number=_SERVER_PORT), pods=Pods(NAMESPACE, tuple(_LABELS.items()))
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.DISABLED, agent_readable=None)

    claim = PersistentVolumeClaim(
        chart,
        "data-claim",
        metadata=ApiObjectMetadata(
            name=_DATA_CLAIM, namespace=NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"}
        ),
        access_modes=[PersistentVolumeAccessMode.READ_WRITE_ONCE],
        storage_class_name="seaweedfs-ovh",
        storage=Size.gibibytes(100),
    )

    deployment = Deployment(
        chart,
        "rest-server-deployment",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE, labels=_LABELS),
        pod_metadata=ApiObjectMetadata(labels=_LABELS),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        automount_service_account_token=False,
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
    )
    deployment.add_container(
        name="rest-server",
        # Upstream's multi-architecture 0.14.0 manifest-list digest; keep this critical
        # backup endpoint independent of our Forgejo registry during a Forgejo outage.
        image="docker.io/restic/rest-server:0.14.0@sha256:d2aff06f47eb38637dff580c3e6bce4af98f386c396a25d32eb6727ec96214a5",
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        env_variables={
            "DATA_DIRECTORY": EnvValue.from_value("/data"),
            "PASSWORD_FILE": EnvValue.from_value("/auth/.htpasswd"),
            "OPTIONS": EnvValue.from_value("--append-only --private-repos"),
        },
        ports=[_SERVER.port.container_port()],
        readiness=Probe.from_tcp_socket(port=_SERVER.pod_port),
        liveness=Probe.from_tcp_socket(port=_SERVER.pod_port, initial_delay_seconds=Duration.seconds(15)),
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(100), limit=Cpu.millis(500)),
            memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
        ),
        security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
    )
    auth = Secret.from_secret_name(chart, "rest-server-auth-secret", _REST_SERVER_AUTH)
    auth_volume = Volume.from_secret(
        chart, "rest-server-auth-volume", auth, default_mode=0o440, items={".htpasswd": PathMapping(path=".htpasswd")}
    )
    deployment.containers[0].mount("/auth", auth_volume, read_only=True)
    deployment.containers[0].mount("/data", Volume.from_persistent_volume_claim(chart, "data-volume", claim))
    harden(deployment)

    Service(
        chart,
        "rest-server-service",
        metadata=ApiObjectMetadata(name=_SERVER.name, namespace=NAMESPACE, labels=_SERVER.labels),
        selector=deployment,
        ports=[_SERVER.port.service_port()],
    )
    https_route(
        chart,
        "rest-server-route",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        hostnames=[HOSTNAME],
        backend=_SERVER,
        hsts=True,
        listener=None,
    )
    NetworkPolicy(
        chart,
        "rest-server-network-policy",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        endpoint_selector=_SERVER.pods.selector,
        ingress=[IngressRule.from_gateway(_SERVER.pod_port)],
        egress_deny=deny_all_egress(),
    )

    s3.PrivateBucket(
        chart,
        "backup-bucket",
        name=_BUCKET,
        tenant=NAMESPACE,
        adopt_existing=True,
        description="SeaweedFS copy of the session-backup Restic REST server data.",
        secret_name=_S3_CREDENTIALS,
    )
    ResticBackup(
        chart,
        "rest-server-data-backup",
        namespace=NAMESPACE,
        name="session-backup-server-data-restic",
        repository="session-backup-server-data-restic",
        source_pvc=_DATA_CLAIM,
        bucket=_BUCKET,
        s3_credentials=_S3_CREDENTIALS,
        schedule="47 08 * * *",
        cache_storage_class_name="local-path-ovh-hdd",
    )
    return chart


def session_backup(
    chart: Chart,
    directory: RenderedDirectory,
    seaweedfs_operator: Kustomization,
    external_secrets_operator: Kustomization,
    volsync: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(seaweedfs_operator, external_secrets_operator, volsync),
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        description=(
            "Authenticated append-only Restic REST endpoint for workstation session backups, "
            "with a SeaweedFS-backed primary PVC and a separate retained SeaweedFS VolSync copy."
        ),
    )
