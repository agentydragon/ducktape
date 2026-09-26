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

from cdk8s import App, Chart
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import (
    PodMonitor,
    PodMonitorSpec,
    PodMonitorSpecPodMetricsEndpoints,
    PodMonitorSpecPodMetricsEndpointsRelabelings,
    PodMonitorSpecSelector,
)
from prometheus_operator_prometheusrule_crds.com.coreos.monitoring import (
    PrometheusRule,
    PrometheusRuleSpec,
    PrometheusRuleSpecGroups,
    PrometheusRuleSpecGroupsRules,
    PrometheusRuleSpecGroupsRulesExpr,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.monitoring import stack
from cluster.scripts.nebula_mesh import Mesh

NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/gateway-probe"
_NAME = "gateway-probe"
_JOB = f"{NAMESPACE}/{_NAME}"
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


def _values(nodes: list[str]) -> dict[str, object]:
    return {
        "kind": "DaemonSet",
        "nameOverride": _NAME,
        "fullnameOverride": _NAME,
        "config": {
            "modules": {
                _MODULE: {
                    "prober": "tcp",
                    "timeout": "5s",
                    "tcp": {
                        "preferred_ip_protocol": "ip4",
                        "tls": True,
                        # The question is whether the handshake completes, not whose certificate
                        # answers: the Gateway's issuer may be Let's Encrypt staging.
                        "tls_config": {"server_name": _SERVER_NAME, "insecure_skip_verify": True},
                    },
                }
            }
        },
        # Exactly the nodes public DNS resolves to (tf/gitops/dns-records).
        "affinity": {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": nodes}]}
                    ]
                }
            }
        },
        "tolerations": [{"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}],
        "podSecurityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "64Mi"}},
    }


def _endpoint(
    dial: Dial, targets: list[PodMonitorSpecPodMetricsEndpointsRelabelings], params: dict[str, list[str]]
) -> PodMonitorSpecPodMetricsEndpoints:
    return PodMonitorSpecPodMetricsEndpoints(
        port="http",
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


def _rules(nodes: list[str]) -> list[PrometheusRuleSpecGroupsRules]:
    probe = f'probe_success{{job="{_JOB}", dial=~"{_OWN_NODE_DIALS}"}}'
    return [
        PrometheusRuleSpecGroupsRules(
            alert="OwnNodeGatewayHandshakeFailing",
            expr=PrometheusRuleSpecGroupsRulesExpr.from_string(f"{probe} == 0"),
            for_="5m",
            labels={"severity": "warning"},
            annotations={
                "summary": "A Pod on {{ $labels.node }} cannot complete a TLS handshake with its own node's Gateway",
                "description": (
                    "The {{ $labels.dial }} dial to {{ $labels.instance }} has failed for 5 minutes. Pods on "
                    "{{ $labels.node }} that resolve an *.allegedly.works name to this node hang (#7918). If the same "
                    f"node's {Dial.GATEWAY_SERVICE} dial fails too, the Gateway or the probe Pod's own network is down "
                    "instead."
                ),
            },
        ),
        PrometheusRuleSpecGroupsRules(
            alert="OwnNodeGatewayProbeMissing",
            expr=PrometheusRuleSpecGroupsRulesExpr.from_string(
                f'group by (node) (kube_node_info{{job="kube-state-metrics", node=~"{"|".join(nodes)}"}}) '
                f"unless on (node) group by (node) ({probe})"
            ),
            for_="15m",
            labels={"severity": "warning"},
            annotations={
                "summary": "No own-node Gateway probe results from {{ $labels.node }}",
                "description": (
                    "{{ $labels.node }} is in public DNS but has reported no gateway-probe result for 15 minutes, so "
                    "a broken own-node Gateway path there would go unnoticed. Check the gateway-probe DaemonSet Pod "
                    "on that node (a new taint keeps it off) and its scrape."
                ),
            },
        ),
    ]


def chart(app: App, mesh: Mesh) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    public_nodes = mesh.public_kubernetes_nodes()
    nodes = sorted(public_nodes)
    helm_release(
        chart,
        _NAME,
        NAMESPACE,
        repository=stack.HELM_REPOSITORY_SOURCE_REF,
        chart="prometheus-blackbox-exporter",
        version="11.19.1",
        interval="30m",
        chart_interval="12h",
        install=RETRY_FAILED_INSTALL,
        values=_values(nodes),
        description="Per-node blackbox_exporter dialling its own node's Gateway listener (gateway_probe.py).",
    )
    PodMonitor(
        chart,
        "pod-monitor",
        metadata=metadata(_NAME, NAMESPACE),
        spec=PodMonitorSpec(
            selector=PodMonitorSpecSelector(
                match_labels={"app.kubernetes.io/name": _NAME, "app.kubernetes.io/instance": _NAME}
            ),
            pod_metrics_endpoints=[
                _own_node_endpoint(
                    Dial.OWN_PUBLIC, {name: f"{h.public_ip}:{_GATEWAY_PORT}" for name, h in public_nodes.items()}
                ),
                _own_node_endpoint(
                    Dial.OWN_NEBULA, {name: f"{h.nebula_ip}:{_GATEWAY_PORT}" for name, h in public_nodes.items()}
                ),
                _endpoint(Dial.GATEWAY_SERVICE, [], {"target": [f"{_GATEWAY_SERVICE}:{_GATEWAY_PORT}"]}),
            ],
        ),
    )
    PrometheusRule(
        chart,
        "prometheus-rule",
        metadata=metadata(_NAME, NAMESPACE, labels={"release": "kube-prometheus-stack"}),
        spec=PrometheusRuleSpec(groups=[PrometheusRuleSpecGroups(name=_NAME, rules=_rules(nodes))]),
    )
    return chart


def write_manifests(root: Path, mesh: Mesh) -> None:
    write_charts(root, OUTPUT_DIR, lambda app: chart(app, mesh))


def gateway_probe(
    flux_chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    monitoring_crds: Kustomization,
    monitoring_stack: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "monitoring-gateway-probe",
        artifact,
        timeout="5m",
        # the PodMonitor and PrometheusRule CRDs; the prometheus-community HelmRepository
        depends_on=flux_kustomization_depends_on_many(monitoring_crds, monitoring_stack),
    )
