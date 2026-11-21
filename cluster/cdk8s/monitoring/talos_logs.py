"""Alloy receives Talos host logs on loopback and forwards their JSON to Loki.

Keep the former Vector resource/Flux identities for an in-place DaemonSet rollout: a
second DaemonSet would contend for the same host port. The ConfigMap hash rolls the pods.
"""

from __future__ import annotations

from cdk8s import App, Chart, Size
from cdk8s_plus_34 import Cpu, k8s
from python.runfiles import Runfiles

from cluster.cdk8s import namespaces, pod_policy
from cluster.cdk8s.flux import ConfigMapArgs
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import loki
from cluster.cdk8s.namespaces import PRIVILEGED_POD_SECURITY, Vpa

NAME = "vector-talos-logs"
NAMESPACE = "vector-talos-logs"
OUTPUT_DIR = f"{GENERATED_ROOT}/vector-talos-logs"
_LABELS = {"app": NAME}
# Talos has no journald; `machine.logging.destinations` (cluster/terraform/main/logging.tf) streams
# newline-delimited JSON to this address on each Talos node. Binding host loopback keeps the
# receiver off the node's Nebula, LAN and public addresses.
_LISTEN_HOST = "127.0.0.1"
_LISTEN_PORT = 13333
_CONFIG_DIR = "/etc/alloy"
_CONFIG_FILE = "talos.alloy"
IMAGE = "docker.io/grafana/alloy:v1.21.1@sha256:4039c971193053a84e53dd9ed3e7d4561c069256b024631cd3498a353ed7ece8"
CONFIG_MAP = ConfigMapArgs(
    name="vector-talos-config",
    namespace=NAMESPACE,
    data={
        _CONFIG_FILE: (Runfiles.CreateOrRaise().root() / "ducktape/cluster/cdk8s/monitoring/talos.alloy").read_text()
    },
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        # Fixed-resource DaemonSet -- opt out of Goldilocks/VPA recommendations.
        vpa=Vpa.DISABLED,
        # The receiver joins the host network so it can bind only host loopback;
        # baseline forbids hostNetwork. This is an operator-only namespace: Flux is its
        # sole writer, while the Alloy container itself remains unprivileged.
        labels=PRIVILEGED_POD_SECURITY,
    )
    daemonset = k8s.KubeDaemonSet(
        chart,
        "daemonset",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE, labels=_LABELS),
        spec=k8s.DaemonSetSpec(
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    automount_service_account_token=False,
                    # hostNetwork lets Alloy bind the actual host loopback address. A Cilium
                    # hostPort would also expose the listener on the node's public addresses.
                    host_network=True,
                    dns_policy="ClusterFirstWithHostNet",
                    node_selector={"node-vendor": "talos"},
                    # Every NoSchedule taint (control plane included) so the receiver runs
                    # on every Talos node; nodeSelector still restricts it.
                    tolerations=[k8s.Toleration(operator="Exists", effect="NoSchedule")],
                    containers=[
                        k8s.Container(
                            name="alloy",
                            image=IMAGE,
                            args=[
                                "run",
                                f"{_CONFIG_DIR}/{_CONFIG_FILE}",
                                "--stability.level=experimental",
                                "--storage.path=/tmp/alloy",
                                "--server.http.listen-addr=127.0.0.1:12347",
                                "--disable-reporting",
                            ],
                            env=[
                                k8s.EnvVar(
                                    name="NODE_NAME",
                                    value_from=k8s.EnvVarSource(
                                        field_ref=k8s.ObjectFieldSelector(field_path="spec.nodeName")
                                    ),
                                ),
                                k8s.EnvVar(name="TALOS_LISTEN_ADDRESS", value=f"{_LISTEN_HOST}:{_LISTEN_PORT}"),
                                k8s.EnvVar(name="LOKI_PUSH_URL", value=loki.PUSH_URL),
                            ],
                            security_context=k8s.SecurityContext(run_as_non_root=True, run_as_user=473),
                            readiness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(
                                    host=_LISTEN_HOST, port=k8s.IntOrString.from_number(_LISTEN_PORT)
                                ),
                                initial_delay_seconds=5,
                                period_seconds=10,
                            ),
                            volume_mounts=[k8s.VolumeMount(name="config", mount_path=_CONFIG_DIR, read_only=True)],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string(Cpu.millis(50).amount),
                                    "memory": k8s.Quantity.from_string(Size.mebibytes(128).as_string()),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string(Cpu.millis(500).amount),
                                    "memory": k8s.Quantity.from_string(Size.mebibytes(256).as_string()),
                                },
                            ),
                        )
                    ],
                    volumes=[k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=CONFIG_MAP.name))],
                ),
            ),
        ),
    )
    pod_policy.harden(daemonset)
    return chart
