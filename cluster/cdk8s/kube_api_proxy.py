"""`kubeapi.allegedly.works`: the Gateway route and the nginx reverse proxy that bridges HTTP
to HTTPS in front of the Kubernetes API (`cluster/cdk8s/kube_api_proxy.md`).

Cilium Gateway API doesn't support backend TLS re-encryption (no BackendTLSPolicy, no
`appProtocol: https`), so nginx accepts plain HTTP from the Gateway after TLS termination and
proxies to the apiserver over HTTPS, preserving the Authorization header.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ConfigMap, k8s
from python.runfiles import Runfiles

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.container_resources import container_resources
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import VPA_UPDATE_MODE_LABEL, Vpa
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.vpa_policy import (
    VPA_RESOURCE_POLICY_ANNOTATION,
    ContainerPolicy,
    ControlledValues,
    resource_policy_annotation,
)

NAME = "kube-api-proxy"
NAMESPACE = "default"
OUTPUT_DIR = f"{GENERATED_ROOT}/kube-api-proxy"
_PROXY = "kubeapi-proxy"
_NGINX_CPU_REQUEST = "10m"
_NGINX_MEMORY_REQUEST = "16Mi"
_CONFIG_MAP = "kubeapi-proxy-config"
_ROUTE = "kubeapi-allegedly-works"
_SERVICE = ServiceRef(
    name=_PROXY, port=Port(name="http", number=8080), pods=Pods(namespace=NAMESPACE, labels=(("app", _PROXY),))
)
# kube-controller-manager publishes the apiserver's CA as this ConfigMap in every namespace.
_CLUSTER_CA_CONFIG_MAP = "kube-root-ca.crt"
_CLUSTER_CA_DIR = "/etc/nginx/cluster-ca"
_NGINX_CONF = (
    (Runfiles.CreateOrRaise().root() / "ducktape/cluster/cdk8s/kube-api-proxy.conf")
    .read_text()
    .replace("@LISTEN_PORT@", str(_SERVICE.pod_port))
    .replace("@CLUSTER_CA_DIR@", _CLUSTER_CA_DIR)
)


def _deployment(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_PROXY,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "nginx reverse proxy: HTTP 8080 → HTTPS kubernetes.default.svc:443.\n"
                    "Bridges the gap between Cilium Gateway (TLS terminate) and the\n"
                    "apiserver (requires HTTPS). Used by kubeapi.allegedly.works route.\n"
                ),
                VPA_UPDATE_MODE_LABEL: Vpa.AUTO,
                VPA_RESOURCE_POLICY_ANNOTATION: resource_policy_annotation(
                    # Cap the global 100Mi recommendation floor for this small proxy.
                    ContainerPolicy.memory_requests_only(
                        "nginx", cpu_request=_NGINX_CPU_REQUEST, memory_floor=_NGINX_MEMORY_REQUEST, memory_limit="24Mi"
                    ),
                    ContainerPolicy(controlled_values=ControlledValues.REQUESTS_ONLY, mode="Off"),
                ),
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=2,
            selector=k8s.LabelSelector(match_labels=_SERVICE.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_SERVICE.pods.selector),
                spec=k8s.PodSpec(
                    affinity=k8s.Affinity(
                        pod_anti_affinity=k8s.PodAntiAffinity(
                            required_during_scheduling_ignored_during_execution=[
                                k8s.PodAffinityTerm(
                                    label_selector=k8s.LabelSelector(match_labels=_SERVICE.pods.selector),
                                    topology_key="kubernetes.io/hostname",
                                )
                            ]
                        )
                    ),
                    topology_spread_constraints=[
                        k8s.TopologySpreadConstraint(
                            topology_key=node_scheduling.ZONE_LABEL,
                            max_skew=1,
                            # A single eligible zone can keep both replicas on different hosts.
                            min_domains=1,
                            when_unsatisfiable="DoNotSchedule",
                            label_selector=k8s.LabelSelector(match_labels=_SERVICE.pods.selector),
                            match_label_keys=["pod-template-hash"],
                            node_taints_policy="Honor",
                        )
                    ],
                    # nginx forwards each caller's own bearer and needs only the cluster CA.
                    automount_service_account_token=False,
                    containers=[
                        k8s.Container(
                            name="nginx",
                            image="nginxinc/nginx-unprivileged:1.31-alpine",
                            ports=[k8s.ContainerPort(container_port=_SERVICE.pod_port)],
                            # Mark the Pod ready after nginx starts accepting connections.
                            readiness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_number(_SERVICE.pod_port)),
                                period_seconds=5,
                            ),
                            volume_mounts=[
                                # A subPath mount doesn't hot-reload: Reloader's `autoReloadAll`
                                # rolls the pods when the config changes.
                                k8s.VolumeMount(
                                    name="config",
                                    mount_path="/etc/nginx/nginx.conf",
                                    sub_path="nginx.conf",
                                    read_only=True,
                                ),
                                k8s.VolumeMount(name="cluster-ca", mount_path=_CLUSTER_CA_DIR, read_only=True),
                                k8s.VolumeMount(name="tmp", mount_path="/tmp"),
                                k8s.VolumeMount(name="cache", mount_path="/var/cache/nginx"),
                            ],
                            resources=container_resources(
                                cpu=_NGINX_CPU_REQUEST, memory=_NGINX_MEMORY_REQUEST, memory_limit="64Mi"
                            ),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                                read_only_root_filesystem=True,
                                run_as_non_root=True,
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP)),
                        k8s.Volume(
                            name="cluster-ca", config_map=k8s.ConfigMapVolumeSource(name=_CLUSTER_CA_CONFIG_MAP)
                        ),
                        k8s.Volume(
                            name="tmp",
                            empty_dir=k8s.EmptyDirVolumeSource(
                                medium="Memory", size_limit=k8s.Quantity.from_string("1Mi")
                            ),
                        ),
                        k8s.Volume(
                            name="cache",
                            empty_dir=k8s.EmptyDirVolumeSource(
                                medium="Memory", size_limit=k8s.Quantity.from_string("8Mi")
                            ),
                        ),
                    ],
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(
            name=_ROUTE,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "HTTPRoute for the Kubernetes API at kubeapi.allegedly.works. The Cilium\n"
                    "Gateway terminates the wildcard LE cert at the `https-wildcard` listener,\n"
                    "then forwards to the `kubeapi-proxy` nginx pod which re-encrypts to the\n"
                    "apiserver over HTTPS (Cilium doesn't support backend TLS natively).\n"
                    "\n"
                    "Purpose: works through Claude Code web's L7 TLS-terminating egress proxy,\n"
                    "which rejects the cluster-CA-signed cert that api.allegedly.works presents\n"
                    "on the direct API endpoint (:6443). See ../docs/lessons_learned/ for the full story.\n"
                    "Auth is bearer-JWT only (client certs die at the MITM boundary; see the\n"
                    "kubeconfig.py docstring in devinfra/k8s/kubeconfig.py).\n"
                )
            },
        ),
        hostnames=["kubeapi.allegedly.works"],
        backend=_SERVICE,
        hsts=False,
    )
    ConfigMap(
        chart,
        "config",
        metadata=ApiObjectMetadata(name=_CONFIG_MAP, namespace=NAMESPACE),
        data={"nginx.conf": _NGINX_CONF},
    )
    _deployment(chart)
    k8s.KubePodDisruptionBudget(
        chart,
        "disruption-budget",
        metadata=k8s.ObjectMeta(name=_PROXY, namespace=NAMESPACE),
        spec=k8s.PodDisruptionBudgetSpec(
            min_available=k8s.IntOrString.from_number(1),
            selector=k8s.LabelSelector(match_labels=_SERVICE.pods.selector),
        ),
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_SERVICE.name, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(selector=_SERVICE.pods.selector, ports=[_SERVICE.k8s_service_port()]),
    )
    return chart
