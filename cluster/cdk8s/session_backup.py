"""Authenticated append-only Restic REST storage for workstation AI session backups."""

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
    k8s,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import cilium, namespaces, pod_policy
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.pod_policy import harden
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy, deny_all_egress
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = NAMESPACE = "session-backup"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}"
HOSTNAME = "ai-session-backups.allegedly.works"
_DATA_CLAIM = "session-backup-data"
_STORAGE_CLASS = "piraeus-ssd-2-autodiskful"
_HDD_CLAIM = "session-backup-data-hdd"
_COPY_LABELS = {"app.kubernetes.io/name": "session-backup-hdd-copy"}
REST_SERVER_AUTH_SECRET = "session-backup-rest-server-auth"
IMPORTER_CREDENTIALS_SECRET = "session-backup-importer-credentials"
IMPORTER_CREDENTIALS_SOURCE = "session-backup.sops.yaml"
IMPORTER_CREDENTIALS_PATH = "/run/session-backup/session-backup.yaml"
_SERVER_PORT = 8000
_LABELS = {"app.kubernetes.io/name": NAME}
SERVER = ServiceRef(
    name=NAME, port=Port(name="http", number=_SERVER_PORT), pods=Pods(NAMESPACE, tuple(_LABELS.items()))
)
IMPORTER_LABELS = {"app.kubernetes.io/name": "ai-session-archive-importer"}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.DISABLED)

    claim = PersistentVolumeClaim(
        chart,
        "data-claim",
        metadata=ApiObjectMetadata(
            name=_DATA_CLAIM, namespace=NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"}
        ),
        access_modes=[PersistentVolumeAccessMode.READ_WRITE_ONCE],
        storage_class_name=_STORAGE_CLASS,
        storage=Size.gibibytes(100),
    )

    # Keep the old claim and serving Deployment intact while the new claim is
    # populated and verified. A consumer is required for WaitForFirstConsumer.
    PersistentVolumeClaim(
        chart,
        "hdd-data-claim",
        metadata=ApiObjectMetadata(
            name=_HDD_CLAIM, namespace=NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"}
        ),
        access_modes=[PersistentVolumeAccessMode.READ_WRITE_ONCE],
        storage_class_name="piraeus-hdd-2",
        storage=Size.gibibytes(100),
    )
    copier = k8s.KubeDeployment(
        chart,
        "hdd-copy",
        metadata=k8s.ObjectMeta(name="session-backup-hdd-copy", namespace=NAMESPACE, labels=_COPY_LABELS),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_COPY_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_COPY_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True, run_as_user=1000, run_as_group=1000, fs_group=1000
                    ),
                    affinity=k8s.Affinity(
                        pod_affinity=k8s.PodAffinity(
                            required_during_scheduling_ignored_during_execution=[
                                k8s.PodAffinityTerm(
                                    label_selector=k8s.LabelSelector(match_labels=_LABELS),
                                    topology_key="kubernetes.io/hostname",
                                )
                            ]
                        )
                    ),
                    containers=[
                        k8s.Container(
                            name="copy",
                            image="docker.io/chrislusf/seaweedfs:4.52@sha256:57877e32bb90073b37dc109b438ba66e84fd5ed9ced5f5c04f56444ef243ce59",
                            image_pull_policy="IfNotPresent",
                            command=["sleep", "infinity"],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("256Mi")},
                            ),
                            security_context=k8s.SecurityContext(read_only_root_filesystem=True),
                            volume_mounts=[
                                k8s.VolumeMount(name="source", mount_path="/source", read_only=True),
                                k8s.VolumeMount(name="destination", mount_path="/destination"),
                            ],
                        )
                    ],
                    volumes=[
                        k8s.Volume(
                            name="source",
                            # Publish the CSI volume read-write like the serving
                            # Pod; the container's volumeMount remains read-only.
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_DATA_CLAIM),
                        ),
                        k8s.Volume(
                            name="destination",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_HDD_CLAIM),
                        ),
                    ],
                ),
            ),
        ),
    )
    pod_policy.harden(copier)
    NetworkPolicy(
        chart,
        "hdd-copy-network-policy",
        metadata=ApiObjectMetadata(name="session-backup-hdd-copy", namespace=NAMESPACE),
        endpoint_selector=_COPY_LABELS,
        egress_deny=deny_all_egress(),
    )

    deployment = Deployment(
        chart,
        "rest-server-deployment",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE, labels=_LABELS),
        pod_metadata=ApiObjectMetadata(labels=_LABELS),
        # Pause writes while the existing Restic repositories are copied to HDD.
        replicas=0,
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
        ports=[SERVER.port.container_port()],
        readiness=Probe.from_tcp_socket(port=SERVER.pod_port),
        liveness=Probe.from_tcp_socket(port=SERVER.pod_port, initial_delay_seconds=Duration.seconds(15)),
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(100), limit=Cpu.millis(500)),
            memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
        ),
        security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
    )
    auth = Secret.from_secret_name(chart, "rest-server-auth-secret", REST_SERVER_AUTH_SECRET)
    auth_volume = Volume.from_secret(
        chart, "rest-server-auth-volume", auth, default_mode=0o440, items={".htpasswd": PathMapping(path=".htpasswd")}
    )
    deployment.containers[0].mount("/auth", auth_volume, read_only=True)
    deployment.containers[0].mount("/data", Volume.from_persistent_volume_claim(chart, "data-volume", claim))
    harden(deployment)

    Service(
        chart,
        "rest-server-service",
        metadata=ApiObjectMetadata(name=SERVER.name, namespace=NAMESPACE, labels=SERVER.labels),
        selector=deployment,
        ports=[SERVER.port.service_port()],
    )
    https_route(
        chart,
        "rest-server-route",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        hostnames=[HOSTNAME],
        backend=SERVER,
        hsts=True,
        listener=None,
    )
    NetworkPolicy(
        chart,
        "rest-server-network-policy",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        endpoint_selector=SERVER.pods.selector,
        ingress=[
            IngressRule.from_gateway(SERVER.pod_port),
            IngressRule.from_endpoints(
                cilium.endpoint_labels(NAMESPACE, IMPORTER_LABELS["app.kubernetes.io/name"]), ports=[SERVER.pod_port]
            ),
        ],
        egress_deny=deny_all_egress(),
    )

    return chart


def session_backup(chart: Chart, directory: RenderedDirectory, piraeus_cluster: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(piraeus_cluster),
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        description=(
            "Authenticated append-only Restic REST endpoint for workstation session backups, "
            "with replicated Piraeus storage."
        ),
    )
