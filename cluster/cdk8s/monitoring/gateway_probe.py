"""Probes each public node's Gateway listener from an ordinary Pod on that same node.

A Pod dialling its own node's Gateway takes a path no outside client takes, and it has
broken there while every external check stayed green (#7918: on Talos v1.14 control planes
the Pod's ACK and ClientHello died in netfilter, so the TCP connect succeeded and the TLS
handshake hung). A `blackbox_exporter` DaemonSet on every node in public DNS therefore
completes a TLS handshake with its own node's public and Nebula addresses. It also dials the
Gateway Service, which that bug spared, to tell a broken own-node path from a Gateway that is
down. The probe must stay off the host network: a host-network client's SYN-ACK leaves
through `lo` and never meets the bug.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import (
    PodMonitorSpecPodMetricsEndpoints,
    PodMonitorSpecPodMetricsEndpointsRelabelings,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.prometheus_operator.pod_monitor import PodMonitor
from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from cluster.scripts.nebula_mesh import Mesh

NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/gateway-probe"
_NAME = "gateway-probe"
_LABELS = {"app.kubernetes.io/name": _NAME}
_JOB = f"{NAMESPACE}/{_NAME}"
_PORT_NAME = "http"
_PORT = 9115
_CONFIG_DIR = "/etc/blackbox_exporter"
_CONFIG_FILE = "blackbox.yml"
_MODULE = "gateway_tls"
_GATEWAY_PORT = 443
# Any name under the Gateway's wildcard listener selects its certificate; this one also has a route.
_SERVER_NAME = "www.allegedly.works"
_GATEWAY_SERVICE = "cilium-gateway-cluster-gateway.gateway-system.svc.cluster.local"


class Dial(StrEnum):
    OWN_PUBLIC = "own_public"
    OWN_NEBULA = "own_nebula"
    GATEWAY_SERVICE = "gateway_service"


_OWN_NODE_DIALS = f"{Dial.OWN_PUBLIC}|{Dial.OWN_NEBULA}"
_CONFIG = {
    "modules": {
        _MODULE: {
            "prober": "tcp",
            "timeout": "5s",
            "tcp": {
                "preferred_ip_protocol": "ip4",
                "tls": True,
                # The question is whether the handshake completes, not whose certificate answers:
                # the Gateway's issuer may be Let's Encrypt staging.
                "tls_config": {"server_name": _SERVER_NAME, "insecure_skip_verify": True},
            },
        }
    }
}
_CONFIG_MAP = ConfigMapArgs(
    name=f"{_NAME}-config", namespace=NAMESPACE, literals=[f"{_CONFIG_FILE}={yaml.safe_dump(_CONFIG)}"]
)


def _daemon_set(scope: Chart, nodes: list[str]) -> None:
    k8s.KubeDaemonSet(
        scope,
        "daemonset",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=NAMESPACE, labels=_LABELS),
        spec=k8s.DaemonSetSpec(
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    # Exactly the nodes public DNS resolves to (tf/gitops/dns-records).
                    affinity=k8s.Affinity(
                        node_affinity=k8s.NodeAffinity(
                            required_during_scheduling_ignored_during_execution=k8s.NodeSelector(
                                node_selector_terms=[
                                    k8s.NodeSelectorTerm(
                                        match_expressions=[
                                            k8s.NodeSelectorRequirement(
                                                key="kubernetes.io/hostname", operator="In", values=nodes
                                            )
                                        ]
                                    )
                                ]
                            )
                        )
                    ),
                    tolerations=[
                        k8s.Toleration(
                            key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
                        )
                    ],
                    automount_service_account_token=False,
                    # The image sets no USER, so it would otherwise run as root.
                    security_context=k8s.PodSecurityContext(
                        run_as_user=65534,
                        run_as_group=65534,
                        run_as_non_root=True,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        k8s.Container(
                            name="blackbox-exporter",
                            image=(
                                "quay.io/prometheus/blackbox-exporter:v0.28.0"
                                "@sha256:e753ff9f3fc458d02cca5eddab5a77e1c175eee484a8925ac7d524f04366c2fc"
                            ),
                            args=[f"--config.file={_CONFIG_DIR}/{_CONFIG_FILE}"],
                            ports=[k8s.ContainerPort(name=_PORT_NAME, container_port=_PORT)],
                            readiness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(
                                    path="/-/healthy", port=k8s.IntOrString.from_string(_PORT_NAME)
                                ),
                                period_seconds=10,
                            ),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                read_only_root_filesystem=True,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                            ),
                            volume_mounts=[k8s.VolumeMount(name="config", mount_path=_CONFIG_DIR, read_only=True)],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("32Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("64Mi")},
                            ),
                        )
                    ],
                    volumes=[k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP.name))],
                ),
            ),
        ),
    )


def _endpoint(
    dial: Dial, targets: list[PodMonitorSpecPodMetricsEndpointsRelabelings], params: dict[str, list[str]]
) -> PodMonitorSpecPodMetricsEndpoints:
    return PodMonitorSpecPodMetricsEndpoints(
        port=_PORT_NAME,
        path="/probe",
        params={"module": [_MODULE], **params},
        interval="30s",
        scrape_timeout="10s",
        relabelings=[
            *targets,
            PodMonitorSpecPodMetricsEndpointsRelabelings(source_labels=["__param_target"], target_label="instance"),
            PodMonitorSpecPodMetricsEndpointsRelabelings(
                source_labels=["__meta_kubernetes_pod_node_name"], target_label="node"
            ),
            PodMonitorSpecPodMetricsEndpointsRelabelings(target_label="dial", replacement=dial),
        ],
    )


def _own_node_endpoint(dial: Dial, targets: dict[str, str]) -> PodMonitorSpecPodMetricsEndpoints:
    """Each Pod dials the `host:port` that `targets` maps its own node's name to."""
    return _endpoint(
        dial,
        [
            PodMonitorSpecPodMetricsEndpointsRelabelings(
                source_labels=["__meta_kubernetes_pod_node_name"],
                regex=node,
                target_label="__param_target",
                replacement=target,
            )
            for node, target in sorted(targets.items())
        ],
        {},
    )


def _rules(nodes: list[str]) -> list[Rule]:
    probe = f'probe_success{{job="{_JOB}", dial=~"{_OWN_NODE_DIALS}"}}'
    return [
        Rule.alert(
            "OwnNodeGatewayHandshakeFailing",
            f"{probe} == 0",
            for_="5m",
            labels={"severity": "warning"},
            summary="A Pod on {{ $labels.node }} cannot complete a TLS handshake with its own node's Gateway",
            description=(
                "The {{ $labels.dial }} dial to {{ $labels.instance }} has failed for 5 minutes. Pods on "
                "{{ $labels.node }} that resolve an *.allegedly.works name to this node hang (#7918). If the same "
                f"node's {Dial.GATEWAY_SERVICE} dial fails too, the Gateway or the probe Pod's own network is down "
                "instead."
            ),
        ),
        Rule.alert(
            "OwnNodeGatewayProbeMissing",
            f'group by (node) (kube_node_info{{job="kube-state-metrics", node=~"{"|".join(nodes)}"}}) '
            f"unless on (node) group by (node) ({probe})",
            for_="15m",
            labels={"severity": "warning"},
            summary="No own-node Gateway probe results from {{ $labels.node }}",
            description=(
                "{{ $labels.node }} is in public DNS but has reported no gateway-probe result for 15 minutes, so "
                "a broken own-node Gateway path there would go unnoticed. Check the gateway-probe DaemonSet Pod "
                "on that node (a new taint keeps it off) and its scrape."
            ),
        ),
    ]


def chart(app: App, mesh: Mesh) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    public_nodes = mesh.public_kubernetes_nodes()
    nodes = sorted(public_nodes)
    _daemon_set(chart, nodes)
    PodMonitor(
        chart,
        "pod-monitor",
        metadata=metadata(_NAME, NAMESPACE),
        selector=_LABELS,
        pod_metrics_endpoints=[
            _own_node_endpoint(
                Dial.OWN_PUBLIC, {name: f"{h.public_ip}:{_GATEWAY_PORT}" for name, h in public_nodes.items()}
            ),
            _own_node_endpoint(
                Dial.OWN_NEBULA, {name: f"{h.nebula_ip}:{_GATEWAY_PORT}" for name, h in public_nodes.items()}
            ),
            _endpoint(Dial.GATEWAY_SERVICE, [], {"target": [f"{_GATEWAY_SERVICE}:{_GATEWAY_PORT}"]}),
        ],
    )
    PrometheusRule(
        chart,
        "prometheus-rule",
        metadata=metadata(_NAME, NAMESPACE, labels={"release": "kube-prometheus-stack"}),
        groups=[group(_NAME, _rules(nodes))],
    )
    add_fleet_rules(chart)
    return chart


def write_manifests(root: Path, mesh: Mesh) -> None:
    write_charts(root, OUTPUT_DIR, lambda app: chart(app, mesh))
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(
            namespace=NAMESPACE, resources=[f"{_NAME}.k8s.yaml"], config_map_generator=[_CONFIG_MAP]
        ),
    )


def gateway_probe(
    flux_chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, monitoring_crds: Kustomization
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "monitoring-gateway-probe",
        artifact,
        timeout="5m",
        # the PodMonitor and PrometheusRule CRDs
        depends_on=[flux_kustomization_depends_on(monitoring_crds)],
    )
