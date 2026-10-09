"""The central ActivityWatch server: aw-server behind a read-only proxy and a bearer-gated
write/read proxy, with its storage, Services, routes and network policy.

The aw-server image tag is the placeholder "unset"; the hand-written
cluster/k8s/activitywatch/image-pins/kustomization.yaml overrides it at `kustomize build` time
via Flux's image-automation marker. Also hand-written: the nginx configs the directory's
`configMapGenerator` renders and the two SOPS token Secrets.
"""

from __future__ import annotations

from dataclasses import replace

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEntities,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)
from constructs import Construct

from cluster.cdk8s import cilium, namespaces
from cluster.cdk8s.authentik import app as authentik
from cluster.cdk8s.flux import ConfigMapArgs
from cluster.cdk8s.forgejo.images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/activitywatch"
_NAME = "activitywatch"
_NAMESPACE = "activitywatch"
_IMAGE = "git.allegedly.works/ducktape-ci/aw-server:unset"
_DATA_CLAIM = "activitywatch-data"
_SERVER_PORT = 5600
_READONLY_CONFIG = ConfigMapArgs(
    name="activitywatch-readonly-proxy", namespace=_NAMESPACE, files=["default.conf=readonly-proxy.conf"]
)
_BEARER_CONFIG = ConfigMapArgs(
    name="activitywatch-bearer-proxy", namespace=_NAMESPACE, files=["default.conf.template=bearer-proxy.conf.template"]
)
CONFIG_MAPS = (_READONLY_CONFIG, _BEARER_CONFIG)
_PODS = Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _NAME),))
# The three Services share one Service port, each in front of a different sidecar port.
_HTTP = Port(name="http", number=5600)
# The read-only nginx, which the Authentik embedded outpost proxies to.
_READONLY = ServiceRef(name="readonly", port=_HTTP, pods=_PODS, target_port=5601)
# The bearer-proxy sidecar, one Service per direction.
_WRITE = ServiceRef(name="write", port=_HTTP, pods=_PODS, target_port=5602)
_READ = ServiceRef(name="read", port=_HTTP, pods=_PODS, target_port=5603)
# The SOPS-managed bearers the bearer-proxy checks.
_WRITE_TOKEN = SecretRef(namespace=_NAMESPACE, name="activitywatch-write-token").key("token")
_READ_TOKEN = SecretRef(namespace=_NAMESPACE, name="activitywatch-read-token").key("token")


def _namespace(scope: Construct) -> None:
    namespaces.namespace(
        scope,
        "namespace",
        name=_NAMESPACE,
        vpa=Vpa.AUTO,
        # Lets the approved agent identities read workload metadata and pod logs here,
        # so a crashlooping sidecar can be diagnosed without an operator grant.
        labels={
            "pod-security.kubernetes.io/enforce": "privileged",
            "pod-security.kubernetes.io/audit": "privileged",
            "pod-security.kubernetes.io/warn": "privileged",
        },
    )


def _http_probe(path: str, port: int, *, initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path=path, port=k8s.IntOrString.from_number(port)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def _aw_server_container() -> k8s.Container:
    return k8s.Container(
        name="aw-server",
        image=_IMAGE,
        command=[
            "/usr/local/bin/aw-server",
            "--host",
            "0.0.0.0",
            "--port",
            str(_SERVER_PORT),
            "--dbpath",
            "/data/db.sqlite3",
            "--device-id",
            "activitywatch-cluster",
        ],
        ports=[k8s.ContainerPort(container_port=_SERVER_PORT, name="http")],
        volume_mounts=[k8s.VolumeMount(name="data", mount_path="/data")],
        resources=k8s.ResourceRequirements(
            requests={"cpu": k8s.Quantity.from_string("50m"), "memory": k8s.Quantity.from_string("64Mi")},
            limits={"cpu": k8s.Quantity.from_string("500m")},
        ),
        liveness_probe=_http_probe("/api/0/info", _SERVER_PORT, initial_delay_seconds=10, period_seconds=30),
        readiness_probe=_http_probe("/api/0/info", _SERVER_PORT, initial_delay_seconds=5, period_seconds=10),
    )


def _readonly_proxy_container() -> k8s.Container:
    return k8s.Container(
        name="readonly-proxy",
        image="nginx:alpine",
        ports=[k8s.ContainerPort(container_port=_READONLY.pod_port, name="readonly")],
        volume_mounts=[k8s.VolumeMount(name="readonly-proxy-config", mount_path="/etc/nginx/conf.d", read_only=True)],
        resources=k8s.ResourceRequirements(
            requests={"cpu": k8s.Quantity.from_string("10m"), "memory": k8s.Quantity.from_string("32Mi")},
            limits={
                "cpu": k8s.Quantity.from_string("100m"),
                # TODO(vpa-memory-audit): 64Mi -> 128Mi. VPA observed a 50Mi
                # request / 60Mi upper bound, leaving no headroom at 64Mi.
                "memory": k8s.Quantity.from_string("128Mi"),
            },
        ),
        liveness_probe=_http_probe("/healthz", _READONLY.pod_port, initial_delay_seconds=5, period_seconds=30),
        readiness_probe=_http_probe("/healthz", _READONLY.pod_port, initial_delay_seconds=2, period_seconds=10),
    )


def _bearer_proxy_container() -> k8s.Container:
    return k8s.Container(
        name="bearer-proxy",
        image="nginx:alpine",
        # One nginx gating both token-checked routes: 5602 write (shared write
        # token) and 5603 read (distinct read token, read methods only). aw-server
        # has no auth of its own, so this sidecar is the gate. envsubst fills
        # ${AW_WRITE_TOKEN}/${AW_READ_TOKEN} from the secrets; the ^AW_ filter keeps
        # substitution to those two so nginx's own $-variables survive. The read
        # token is also what the Haku agent uses via the iron egress proxy.
        env=[
            _WRITE_TOKEN.env_var("AW_WRITE_TOKEN"),
            _READ_TOKEN.env_var("AW_READ_TOKEN"),
            k8s.EnvVar(name="NGINX_ENVSUBST_FILTER", value="^AW_"),
        ],
        ports=[
            k8s.ContainerPort(container_port=_WRITE.pod_port, name="write"),
            k8s.ContainerPort(container_port=_READ.pod_port, name="read"),
        ],
        volume_mounts=[k8s.VolumeMount(name="bearer-proxy-config", mount_path="/etc/nginx/templates", read_only=True)],
        resources=k8s.ResourceRequirements(
            requests={"cpu": k8s.Quantity.from_string("10m"), "memory": k8s.Quantity.from_string("32Mi")},
            limits={"memory": k8s.Quantity.from_string("64Mi")},
        ),
        liveness_probe=_http_probe("/healthz", _WRITE.pod_port, initial_delay_seconds=5, period_seconds=30),
        readiness_probe=_http_probe("/healthz", _WRITE.pod_port, initial_delay_seconds=2, period_seconds=10),
    )


def _deployment(scope: Construct) -> None:
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE, labels=_PODS.selector),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_PODS.selector),
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_PODS.selector),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    # No Kubernetes API access; don't mount a SA token.
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(fs_group=999),
                    node_selector={"topology.kubernetes.io/region": "proxmox"},
                    containers=[_aw_server_container(), _readonly_proxy_container(), _bearer_proxy_container()],
                    volumes=[
                        k8s.Volume(
                            name="data",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_DATA_CLAIM),
                        ),
                        k8s.Volume(
                            name="readonly-proxy-config",
                            config_map=k8s.ConfigMapVolumeSource(name=_READONLY_CONFIG.name),
                        ),
                        k8s.Volume(
                            name="bearer-proxy-config", config_map=k8s.ConfigMapVolumeSource(name=_BEARER_CONFIG.name)
                        ),
                    ],
                ),
            ),
        ),
    )


def _data_claim(scope: Construct) -> None:
    k8s.KubePersistentVolumeClaim(
        scope,
        "data",
        metadata=k8s.ObjectMeta(name=_DATA_CLAIM, namespace=_NAMESPACE),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="local-path-proxmox",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("10Gi")}),
        ),
    )


def _service(scope: Construct, id: str, *, service: ServiceRef, description: str | None = None) -> None:
    k8s.KubeService(
        scope,
        id,
        metadata=k8s.ObjectMeta(
            name=service.name,
            namespace=service.pods.namespace,
            annotations={"description": description} if description else None,
        ),
        spec=k8s.ServiceSpec(
            selector=service.pods.selector,
            ports=[
                k8s.ServicePort(
                    name=service.port.name,
                    port=service.port.number,
                    target_port=k8s.IntOrString.from_number(service.pod_port),
                    protocol="TCP",
                )
            ],
        ),
    )


def _route(scope: Construct, id: str, *, backend: ServiceRef, hostname: str) -> None:
    """A public route on the cluster-gateway wildcard for *.allegedly.works, straight to the
    same-named bearer-gated Service."""
    https_route(
        scope,
        id,
        metadata=ApiObjectMetadata(name=backend.name, namespace=_NAMESPACE),
        hostnames=[hostname],
        backend=backend,
        hsts=False,
        listener=None,
    )


def _network_policy(scope: Construct) -> None:
    # ActivityWatch central query + write server.
    # Ingress: kube-apiserver (health probes), Authentik proxy (read-only proxy 5601),
    # the Gateway (bearer-gated bearer-proxy: write 5602, read 5603). Egress: DNS only.
    NetworkPolicy(
        scope,
        "network-policy",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        endpoint_selector=_PODS.selector,
        ingress=[
            CiliumNetworkPolicySpecIngress(
                from_entities=[CiliumNetworkPolicySpecIngressFromEntities.KUBE_HYPHEN_APISERVER],
                to_ports=[
                    CiliumNetworkPolicySpecIngressToPorts(
                        ports=[
                            CiliumNetworkPolicySpecIngressToPortsPorts(
                                port=str(_SERVER_PORT), protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP
                            )
                        ]
                    )
                ],
            ),
            # Read-only proxy through Authentik embedded outpost (nginx on 5601).
            authentik.SERVER.pods.admit(_READONLY.pod_port),
            # Public write + read routes: the Gateway (Envoy, hostNetwork) reaches the
            # bearer-gated bearer-proxy sidecar on 5602 (write) and 5603 (read).
            # See docs/cilium_network_policy.md (fromEntities: ingress).
            IngressRule.from_gateway(_WRITE.pod_port, _READ.pod_port),
        ],
        egress=[cilium.dns_egress()],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=_NAMESPACE)
    _namespace(chart)
    _deployment(chart)
    _data_claim(chart)
    _service(chart, "readonly-service", service=_READONLY)
    # CLEANUP(added 2026-10-02): Remove once nothing resolves activitywatch-readonly.activitywatch.svc (consumers: cluster/k8s/authentik/app/blueprints/activitywatch-sso.yaml, applied by the separate authentik Kustomization).
    _service(chart, "legacy-readonly-service", service=replace(_READONLY, name="activitywatch-readonly"))
    _service(
        chart,
        "write-service",
        service=_WRITE,
        description=(
            "Bearer-gated ActivityWatch write endpoint (bearer-proxy sidecar, 5602), fronted by the public "
            "write HTTPRoute for desktop importers."
        ),
    )
    _service(
        chart,
        "read-service",
        service=_READ,
        description=(
            "Bearer-gated read-only ActivityWatch endpoint (bearer-proxy sidecar, 5603), fronted by the public "
            "read HTTPRoute for the Haku agent."
        ),
    )
    # Public write route for desktop importers. The read route is Authentik-gated, but
    # this client (Rust aw-client) only sends a static bearer and can't do the OAuth
    # exchange, so auth here is the write-proxy's bearer check and the route goes straight
    # to the bearer-gated write Service. See cluster/docs/activitywatch/revival-plan.md.
    _route(chart, "write-route", backend=_WRITE, hostname="activitywatch-write.allegedly.works")
    # Public read route for the Haku agent. Reaches the bearer-gated read Service, which
    # allows read methods only, so even a leaked read token can't write.
    _route(chart, "read-route", backend=_READ, hostname="activitywatch-read.allegedly.works")
    _network_policy(chart)
    return chart
