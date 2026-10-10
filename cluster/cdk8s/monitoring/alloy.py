"""Grafana Alloy, as three HelmReleases: the central `alloy` Deployment for cluster-wide work, the
`alloy-node` DaemonSet scraping each node's own targets into a WAL on that node's disk, and the
`alloy-syslog` Deployment receiving the home switch's and the AT&T gateway's syslog on optiplex. Also the NetworkPolicy
admitting OTLP from Authentik's outpost to the central one, and the ConfigMaps.

Their configs are `config.alloy`, `node.alloy` and `syslog.alloy` beside this module. They read the addresses other
modules own through `sys.env`, from the environment the HelmReleases set.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    GeneratorOptions,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.generation import copy_source_file
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import grafana_helmrepository, loki, mimir, tempo

_NAME = "alloy"
NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/alloy"
_OTLP_HTTP_PORT = 4318
# The chart's default `alloy.listenPort`, serving Alloy's own /metrics.
_HTTP_PORT = 12345
_CONFIG_MAP = "alloy-config"
_CONFIG_KEY = "config.alloy"
_NODE_NAME = "alloy-node"
_NODE_CONFIG_MAP = "alloy-node-config"
_NODE_CONFIG_KEY = "node.alloy"
# Node-local disk for the per-node WAL, so a buffered outage survives a pod restart.
_NODE_STORAGE_PATH = "/var/lib/alloy-node"
_SYSLOG_NAME = "alloy-syslog"
_SYSLOG_CONFIG_MAP = "alloy-syslog-config"
_SYSLOG_CONFIG_KEY = "syslog.alloy"
# Bound on all of optiplex's addresses, its fixed LAN address (`HOME_LAN.optiplex`) among them.
# The home switch sends here (`home_switch.py`).
SYSLOG_HOST_PORT = 514
_SYSLOG_LISTEN_PORT = 5514
# The AT&T gateway sends its firewall log here (`cluster/cdk8s/att_gateway_exporter`); its own
# port, so its lines get their own job.
GATEWAY_SYSLOG_HOST_PORT = 1514
_GATEWAY_SYSLOG_LISTEN_PORT = 5515
# What config.alloy's `sys.env` calls read.
_CONFIG_ENV = {
    "MIMIR_PUSH_URL": mimir.PUSH_URL,
    "MIMIR_GATEWAY_URL": mimir.GATEWAY_URL,
    "LOKI_PUSH_URL": loki.PUSH_URL,
    "TEMPO_OTLP_GRPC_ENDPOINT": tempo.OTLP_GRPC_ENDPOINT,
    "OTLP_HTTP_LISTEN_ADDRESS": f"0.0.0.0:{_OTLP_HTTP_PORT}",
}


def write_config_maps(root: Path) -> list[ConfigMapArgs]:
    """Copy both Alloy configs into `OUTPUT_DIR`; return the `configMapGenerator` entries packaging them."""
    return [
        ConfigMapArgs(
            name=name,
            namespace=NAMESPACE,
            # The Helm values name the ConfigMap, and kustomize cannot rewrite a reference inside a
            # HelmRelease's values.
            options=GeneratorOptions(disable_name_suffix_hash=True),
            files=[copy_source_file(root, OUTPUT_DIR, f"cluster/cdk8s/monitoring/{key}")],
        )
        for name, key in (
            (_CONFIG_MAP, _CONFIG_KEY),
            (_NODE_CONFIG_MAP, _NODE_CONFIG_KEY),
            (_SYSLOG_CONFIG_MAP, _SYSLOG_CONFIG_KEY),
        )
    ]


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    helm_release(
        chart,
        _NAME,
        NAMESPACE,
        repository=grafana_helmrepository.SOURCE_REF,
        chart=_NAME,
        version="1.x",
        interval="30m",
        chart_interval="12h",
        values={
            "alloy": {
                "configMap": {"name": _CONFIG_MAP, "key": _CONFIG_KEY, "create": False},
                "extraEnv": [{"name": name, "value": value} for name, value in _CONFIG_ENV.items()],
                # The Grafana Alloy chart reads extraPorts from .Values.alloy and reuses
                # them for both the Service and the container port list.
                "extraPorts": [
                    {"name": "otlp-http", "port": _OTLP_HTTP_PORT, "targetPort": _OTLP_HTTP_PORT, "protocol": "TCP"}
                ],
            },
            "controller": {
                "type": "deployment",
                # Single replica is load-bearing, not just sizing: config.alloy's
                # `loki.source.kubernetes_events` watches events cluster-wide, so a second
                # replica ingests every event a second time. Scaling this up means scoping
                # or removing that component first.
                "replicas": 1,
                # Every cluster-wide metric reaches Mimir through this one pod, so it runs beside
                # Mimir: on a home node, a home WAN outage would cut off the whole cluster's metrics.
                "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR,
            },
            "serviceMonitor": {"enabled": True},
        },
    )
    helm_release(
        chart,
        _NODE_NAME,
        NAMESPACE,
        repository=grafana_helmrepository.SOURCE_REF,
        chart=_NAME,
        version="1.x",
        interval="30m",
        chart_interval="12h",
        values={
            # Its own name label, so neither the central release's selectors nor the OTLP
            # NetworkPolicy below match these pods.
            "nameOverride": _NODE_NAME,
            "alloy": {
                "configMap": {"name": _NODE_CONFIG_MAP, "key": _NODE_CONFIG_KEY, "create": False},
                "storagePath": _NODE_STORAGE_PATH,
                "mounts": {"extra": [{"name": "wal", "mountPath": _NODE_STORAGE_PATH}]},
                "extraEnv": [
                    {"name": "MIMIR_PUSH_URL", "value": mimir.PUSH_URL},
                    {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
                ],
                "resources": {"requests": {"cpu": "20m", "memory": "256Mi"}},
            },
            "controller": {
                "type": "daemonset",
                "volumes": {
                    "extra": [{"name": "wal", "hostPath": {"path": _NODE_STORAGE_PATH, "type": "DirectoryOrCreate"}}]
                },
                "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
                # Roaming laptops are left out: their metrics aren't wanted, and an offline
                # laptop's pod would hold the DaemonSet's rollout budget.
                "affinity": k8s.Affinity(
                    node_affinity=k8s.NodeAffinity(
                        required_during_scheduling_ignored_during_execution=k8s.NodeSelector(
                            node_selector_terms=[
                                k8s.NodeSelectorTerm(
                                    match_expressions=[
                                        k8s.NodeSelectorRequirement(
                                            key=node_scheduling.REGION_LABEL,
                                            operator="NotIn",
                                            values=[node_scheduling.ROAMING_REGION],
                                        )
                                    ]
                                )
                            ]
                        )
                    )
                ),
            },
            "serviceMonitor": {"enabled": True},
        },
    )
    helm_release(
        chart,
        _SYSLOG_NAME,
        NAMESPACE,
        repository=grafana_helmrepository.SOURCE_REF,
        chart=_NAME,
        version="1.x",
        interval="30m",
        chart_interval="12h",
        values={
            "nameOverride": _SYSLOG_NAME,
            "alloy": {
                "configMap": {"name": _SYSLOG_CONFIG_MAP, "key": _SYSLOG_CONFIG_KEY, "create": False},
                "extraEnv": [
                    {"name": "LOKI_PUSH_URL", "value": loki.PUSH_URL},
                    {"name": "SYSLOG_LISTEN_ADDRESS", "value": f"0.0.0.0:{_SYSLOG_LISTEN_PORT}"},
                    {"name": "GATEWAY_SYSLOG_LISTEN_ADDRESS", "value": f"0.0.0.0:{_GATEWAY_SYSLOG_LISTEN_PORT}"},
                ],
                "extraPorts": [
                    {
                        "name": "syslog",
                        "port": SYSLOG_HOST_PORT,
                        "targetPort": _SYSLOG_LISTEN_PORT,
                        "hostPort": SYSLOG_HOST_PORT,
                        "protocol": "UDP",
                    },
                    {
                        "name": "gateway-syslog",
                        "port": GATEWAY_SYSLOG_HOST_PORT,
                        "targetPort": _GATEWAY_SYSLOG_LISTEN_PORT,
                        "hostPort": GATEWAY_SYSLOG_HOST_PORT,
                        "protocol": "UDP",
                    },
                ],
                "resources": {"requests": {"cpu": "10m", "memory": "128Mi"}},
            },
            "controller": {
                "type": "deployment",
                "replicas": 1,
                "nodeSelector": node_scheduling.OPTIPLEX.node_selector,
                # A surge pod could never schedule beside the old one holding the hostPort.
                "updateStrategy": {"type": "Recreate"},
            },
            "serviceMonitor": {"enabled": True},
        },
    )
    k8s.KubeNetworkPolicy(
        chart,
        "otlp-ingress",
        metadata=k8s.ObjectMeta(name="alloy-otlp-ingress", namespace=NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels={"app.kubernetes.io/name": _NAME}),
            policy_types=["Ingress"],
            ingress=[
                # Allow OTLP/HTTP from Authentik embedded outpost (proxies external clients).
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "authentik"}
                            ),
                            pod_selector=k8s.LabelSelector(
                                match_labels={
                                    "app.kubernetes.io/component": "server",
                                    "app.kubernetes.io/instance": "authentik",
                                }
                            ),
                        )
                    ],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(_OTLP_HTTP_PORT), protocol="TCP")],
                ),
                # The chart's ServiceMonitor targets this pod, so the alloy-node on its node scrapes it.
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            pod_selector=k8s.LabelSelector(match_labels={"app.kubernetes.io/name": _NODE_NAME})
                        )
                    ],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(_HTTP_PORT), protocol="TCP")],
                ),
            ],
        ),
    )
    return chart


def alloy(chart: Chart, directory: RenderedDirectory, monitoring_crds: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        _NAME,
        directory,
        wait=None,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=name, namespace=NAMESPACE
            )
            for name in (_NAME, _NODE_NAME, _SYSLOG_NAME)
        ],
        timeout="5m",
        # the chart's serviceMonitor
        depends_on=flux_kustomization_depends_on_many(monitoring_crds),
    )
