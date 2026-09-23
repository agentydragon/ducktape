"""Vector on every Talos node, receiving Talos service logs on host loopback and forwarding them
to Loki. NixOS nodes ship their journal through the separate promtail-journal HelmRelease.

Scoped to Talos nodes by the `node-vendor=talos` label the Talos machine config sets
(`cluster/terraform/main/logging.tf`). `vector.toml` stays a hand-written `configMapGenerator`
input: its hash suffix rewrites the DaemonSet's volume reference and rolls the pods whenever it
changes.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml

NAME = "vector-talos-logs"
NAMESPACE = "vector-talos-logs"
OUTPUT_DIR = "cluster/k8s/vector-talos-logs"
_LABELS = {"app": NAME}
_CONFIG_MAP = ConfigMapArgs(name="vector-talos-config", namespace=NAMESPACE, files=["vector.toml"])


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={
                # Fixed-resource DaemonSet -- opt out of Goldilocks/VPA recommendations.
                "goldilocks.fairwinds.com/enabled": "false",
                # The receiver joins the host network so it can bind only 127.0.0.1:13333;
                # baseline forbids hostNetwork. This is an operator-only namespace: Flux is its
                # sole writer, while the Vector container itself remains unprivileged.
                "pod-security.kubernetes.io/enforce": "privileged",
                "pod-security.kubernetes.io/audit": "privileged",
                "pod-security.kubernetes.io/warn": "privileged",
            },
        ),
    )
    k8s.KubeDaemonSet(
        chart,
        "daemonset",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE, labels=_LABELS),
        spec=k8s.DaemonSetSpec(
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    # hostNetwork lets Vector bind the actual host loopback address. A Cilium
                    # hostPort would also expose the listener on the node's public addresses.
                    host_network=True,
                    dns_policy="ClusterFirstWithHostNet",
                    node_selector={"node-vendor": "talos"},
                    # Talos control-plane nodes carry the control-plane NoSchedule taint;
                    # tolerate all taints so the receiver runs on every Talos node
                    # (nodeSelector still restricts it).
                    tolerations=[k8s.Toleration(operator="Exists")],
                    containers=[
                        k8s.Container(
                            name="vector",
                            # renovate: datasource=docker
                            image="docker.io/timberio/vector:0.58.0-distroless-libc@sha256:6c93dfe2554cc7e38316d618961cd657d5271c7a7aa727e0007502b28cb82c2e",
                            args=["--config", "/etc/vector/vector.toml"],
                            env=[
                                k8s.EnvVar(
                                    name="NODE_NAME",
                                    value_from=k8s.EnvVarSource(
                                        field_ref=k8s.ObjectFieldSelector(field_path="spec.nodeName")
                                    ),
                                ),
                                k8s.EnvVar(name="VECTOR_LOG", value="warn"),
                                # ${NODE_NAME} in vector.toml's sink labels is config-file env var
                                # interpolation, which Vector disables by default. Safe here:
                                # vector.toml is static repo-committed content, NODE_NAME is a
                                # non-secret downward-API value, and it's only ever used as a label
                                # string (never a path/command).
                                k8s.EnvVar(name="VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION", value="true"),
                            ],
                            readiness_probe=k8s.Probe(
                                tcp_socket=k8s.TcpSocketAction(
                                    host="127.0.0.1", port=k8s.IntOrString.from_number(13333)
                                ),
                                initial_delay_seconds=5,
                                period_seconds=10,
                            ),
                            volume_mounts=[k8s.VolumeMount(name="config", mount_path="/etc/vector", read_only=True)],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("50m"),
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("500m"),
                                    "memory": k8s.Quantity.from_string("256Mi"),
                                },
                            ),
                        )
                    ],
                    volumes=[k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP.name))],
                ),
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(
            namespace=NAMESPACE, resources=[f"{NAME}.k8s.yaml"], config_map_generator=[_CONFIG_MAP]
        ),
    )


def vector_talos_logs(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, loki: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            timeout="2m",
            depends_on=[
                # loki-write is the log sink.
                flux_kustomization_depends_on(loki)
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(api_version="apps/v1", kind="DaemonSet", name=NAME, namespace=NAMESPACE)
            ],
        ),
    )
