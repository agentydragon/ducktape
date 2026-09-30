"""The Zot OCI pull-through cache: its Namespace, the `registry-cache` SeaweedFS `PrivateBucket`
holding its content, the Deployment (Zot plus the nginx basic-auth sidecar for the public
endpoint), Service, HTTPRoute, ServiceMonitor and dedupe-cache `RedisReplication`.

Hand-written beside the generated output (cluster/k8s/oci-cache): `kustomization.yaml`
(its configMapGenerator renders `config.json` and `public-auth-proxy.conf`) and
`puller-credential.sops.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import Cpu, k8s
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import namespaces
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import AgentReadable, Vpa
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.seaweedfs import s3
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.valkey import valkey_instance

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/oci-cache"
_NAMESPACE = "oci-cache"
_NAME = "zot"
_PODS = Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _NAME),))
# Zot's plain-HTTP registry on the `oci-cache` Service, where in-cluster Docker mirrors point.
HTTP = ServiceRef(name=_NAMESPACE, port=Port(name="http", number=80), pods=_PODS, target_port=5000)
# The nginx sidecar's authenticated port on the same Service.
_PUBLIC_AUTH = ServiceRef(name=_NAMESPACE, port=Port(name="public-auth", number=8080), pods=_PODS)
_VALKEY = "oci-cache-valkey"


def _tcp_probe(port: str, *, initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string(port)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def _storage(chart: Chart) -> s3.PrivateBucket:
    return s3.PrivateBucket(
        chart,
        "storage",
        name="registry-cache",
        tenant=_NAMESPACE,
        # The existing cache bucket was handed to the tenant-local CR.
        adopt_existing=True,
        description="Zot's OCI pull-through cache: manifests and blobs.",
        key_fields=None,
    )


def _deployment(chart: Chart, *, storage: s3.PrivateBucket) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            namespace=_NAMESPACE,
            labels=_PODS.selector,
            annotations={
                "description": (
                    "Zot OCI pull-through cache. On-demand mirror for docker.io, ghcr.io, quay.io,"
                    " registry.k8s.io and gcr.io addressed by path prefix (/docker-hub, /ghcr, /quay, /k8s,"
                    " /gcr). Durable content in the SeaweedFS registry-cache bucket (S3); dedupe index in the"
                    " oci-cache-valkey RedisReplication. No PVC — the only local state is ephemeral upload"
                    " staging on emptyDir, so the pod reschedules freely. The in-cluster Service is"
                    " intentionally unauthenticated for Docker registry-mirror compatibility; the public"
                    " endpoint is authenticated by the nginx sidecar."
                )
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_PODS.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_PODS.selector),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(
                        fs_group=65532, seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")
                    ),
                    containers=[
                        k8s.Container(
                            name=_NAME,
                            image="ghcr.io/project-zot/zot-linux-amd64:v2.1.21",
                            image_pull_policy="IfNotPresent",
                            args=["serve", "/etc/zot/config.json"],
                            ports=[
                                k8s.ContainerPort(name=HTTP.port.name, container_port=HTTP.pod_port, protocol="TCP")
                            ],
                            # docker/distribution S3 driver reads the AWS default credential
                            # chain when accesskey/secretkey are omitted from config.json.
                            env=[
                                storage.access_key.env_var("AWS_ACCESS_KEY_ID"),
                                storage.secret_key.env_var("AWS_SECRET_ACCESS_KEY"),
                            ],
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path="/etc/zot", read_only=True),
                                k8s.VolumeMount(name="cache", mount_path="/var/lib/zot"),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                    "cpu": k8s.Quantity.from_string("50m"),
                                },
                                limits={
                                    "memory": k8s.Quantity.from_string("512Mi"),
                                    "cpu": k8s.Quantity.from_string("1"),
                                },
                            ),
                            # TCP probe only confirms Zot is listening. Pull-through behavior is
                            # covered by the README smoke tests.
                            readiness_probe=_tcp_probe(HTTP.port.name, initial_delay_seconds=5, period_seconds=10),
                            liveness_probe=_tcp_probe(HTTP.port.name, initial_delay_seconds=20, period_seconds=20),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                run_as_non_root=True,
                                run_as_user=65532,
                                run_as_group=65532,
                                read_only_root_filesystem=True,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                            ),
                        ),
                        # Public basic-auth wrapper. Zot itself must stay unauthenticated on the
                        # in-cluster Service because dockerd's Docker Hub registry-mirror probe
                        # does not send client Docker-config credentials for the mirror host.
                        k8s.Container(
                            name="public-auth-proxy",
                            image="nginxinc/nginx-unprivileged:1.31-alpine",
                            ports=[_PUBLIC_AUTH.port.k8s_container_port()],
                            volume_mounts=[
                                k8s.VolumeMount(
                                    name="public-auth-config", mount_path="/etc/nginx/conf.d", read_only=True
                                ),
                                k8s.VolumeMount(name="public-auth", mount_path="/etc/nginx/auth", read_only=True),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("32Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("64Mi")},
                            ),
                            readiness_probe=_tcp_probe(
                                _PUBLIC_AUTH.port.name, initial_delay_seconds=5, period_seconds=10
                            ),
                            liveness_probe=_tcp_probe(
                                _PUBLIC_AUTH.port.name, initial_delay_seconds=20, period_seconds=20
                            ),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                run_as_non_root=True,
                                run_as_user=101,
                                run_as_group=101,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                            ),
                        ),
                    ],
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name="oci-cache-zot-config")),
                        k8s.Volume(
                            name="public-auth-config",
                            config_map=k8s.ConfigMapVolumeSource(name="oci-cache-public-auth-proxy"),
                        ),
                        k8s.Volume(
                            name="public-auth",
                            secret=k8s.SecretVolumeSource(
                                secret_name="puller-credential", items=[k8s.KeyToPath(key="htpasswd", path="htpasswd")]
                            ),
                        ),
                        k8s.Volume(
                            name="cache", empty_dir=k8s.EmptyDirVolumeSource(size_limit=k8s.Quantity.from_string("5Gi"))
                        ),
                    ],
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAMESPACE, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=_NAMESPACE, vpa=Vpa.AUTO, agent_readable=AgentReadable.LOGS)
    _deployment(chart, storage=_storage(chart))
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=HTTP.name, namespace=_NAMESPACE, labels=HTTP.labels),
        spec=k8s.ServiceSpec(
            selector=HTTP.pods.selector,
            ports=[
                # Exposed on 80 (→ container 5000) for conventional registry addressing by
                # unrestricted consumers. NOTE: this does NOT let a port-restricted egress
                # policy reach the mirror on :80 — Cilium's socket-LB enforces egress on the
                # backend targetPort (5000), not this Service port. So haku-ci's force-proxy
                # egress allows 5000 explicitly (see haku_ci/runner.py).
                # Plain HTTP.
                k8s.ServicePort(
                    name=HTTP.port.name,
                    port=HTTP.port.number,
                    target_port=k8s.IntOrString.from_number(HTTP.pod_port),
                    protocol="TCP",
                ),
                # Authenticated public entrypoint. The HTTPRoute for oci-cache.allegedly.works
                # targets this port; in-cluster Docker mirrors must use the unauthenticated
                # `http` port above.
                _PUBLIC_AUTH.port.k8s_service_port(),
            ],
        ),
    )
    https_route(
        chart,
        "httproute",
        metadata=ApiObjectMetadata(
            name=_NAMESPACE,
            namespace=_NAMESPACE,
            annotations={
                "description": (
                    "Authenticated public endpoint for the Zot pull-through cache. The cluster-gateway"
                    " terminates TLS for *.allegedly.works; this route targets the nginx sidecar on Service"
                    " port 8080, which enforces the puller-credential htpasswd before proxying to Zot."
                )
            },
        ),
        hostnames=["oci-cache.allegedly.works"],
        backend=_PUBLIC_AUTH,
        hsts=False,
        listener=None,
    )
    ServiceMonitor(
        chart,
        "servicemonitor",
        metadata=ApiObjectMetadata(
            name=_NAME,
            namespace=_NAMESPACE,
            annotations={"description": "Zot OCI-cache application metrics scraped into Mimir by Alloy."},
        ),
        selector=ServiceMonitorSpecSelector(match_labels=HTTP.labels),
        endpoints=[Endpoint.plain(port=HTTP.port.name, scrape_timeout="10s")],
    )
    valkey_instance(
        chart,
        name=_VALKEY,
        namespace=_NAMESPACE,
        description=(
            "Shared dedupe/metadata cache for the Zot OCI pull-through cache. Zot's cacheDriver points here"
            " (remoteCache); the durable content lives in S3, so this holds only rebuildable metadata/dedupe"
            " state. Losing it is acceptable but may cause brief cache misses or require a Zot restart/Valkey"
            " flush for stale metadb entries. Uses OVH HDD node-local storage so the operator can rebuild a"
            " fresh Valkey replica without depending on the SeaweedFS CSI path."
        ),
        memory_request=Size.mebibytes(64),
        cpu_limit=Cpu.millis(200),
        memory_limit=Size.mebibytes(256),
        max_memory_percent_of_limit=80,
        storage_class="local-path-ovh-hdd",
        storage_size=Size.gibibytes(2),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
