"""Central, node-local, NixOS journal and syslog Alloy releases.

Node-local Alloy collects pod logs on every node and journals on NixOS nodes.
State preservation and rollback are documented in docs/alloy.md.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ConfigMap, k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from python.runfiles import Runfiles

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.agentplane import telemetry
from cluster.cdk8s.agentplane.namespaces import STAGING_NAMESPACE
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import alloy_endpoints as endpoints, grafana_helmrepository, loki, mimir, tempo
from cluster.cdk8s.monitoring.namespace import NAMESPACE

OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/alloy"
# The chart's default `alloy.listenPort`, serving Alloy's own /metrics.
_HTTP_PORT = 12345
_CONFIG_KEY = "config.alloy"
_NODE_CONFIG_KEY = "node.alloy"
# Node-local disk for the per-node WAL, so a buffered outage survives a pod restart.
_NODE_STORAGE_PATH = "/var/lib/alloy-node"
_JOURNAL_CONFIG_KEY = "journal.alloy"
_JOURNAL_STORAGE_PATH = "/var/lib/alloy-journal"
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
    "OTLP_GRPC_LISTEN_ADDRESS": f"0.0.0.0:{endpoints.OTLP_GRPC.pod_port}",
    "OTLP_HTTP_LISTEN_ADDRESS": f"0.0.0.0:{endpoints.OTLP_HTTP.pod_port}",
}


def chart(app: App) -> Chart:
    chart = Chart(app, endpoints.CENTRAL_NAME, disable_resource_name_hashes=True)
    config_dir = Runfiles.CreateOrRaise().root() / "ducktape/cluster/cdk8s/monitoring"

    def make_config_map(release_name: str, key: str, *includes: str) -> ConfigMap:
        name = f"{release_name}-config"
        config = "".join((config_dir / file).read_text() for file in (key, *includes))
        return ConfigMap(chart, name, metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE), data={key: config})

    config_map = make_config_map(endpoints.CENTRAL_NAME, _CONFIG_KEY)
    node_config_map = make_config_map(endpoints.NODE_NAME, _NODE_CONFIG_KEY, "pod-logs.alloy")
    syslog_config_map = make_config_map(endpoints.SYSLOG_NAME, _SYSLOG_CONFIG_KEY)
    journal_config_map = make_config_map(endpoints.JOURNAL_NAME, _JOURNAL_CONFIG_KEY)

    def release(name: str, *, values: dict[str, object]) -> None:
        # All four collectors run the same chart and publish their own scrape target.
        helm_release(
            chart,
            name,
            NAMESPACE,
            repository=grafana_helmrepository.SOURCE_REF,
            chart="alloy",
            version="1.x",
            interval="30m",
            chart_interval="12h",
            values={**values, "serviceMonitor": {"enabled": True}},
        )

    release(
        endpoints.CENTRAL_NAME,
        values={
            "alloy": {
                "configMap": {"name": config_map.name, "key": _CONFIG_KEY, "create": False},
                "extraEnv": [{"name": name, "value": value} for name, value in _CONFIG_ENV.items()],
                # The Grafana Alloy chart reads extraPorts from .Values.alloy and reuses
                # them for both the Service and the container port list.
                "extraPorts": [
                    {"name": ref.port.name, "port": ref.port.number, "targetPort": ref.pod_port, "protocol": "TCP"}
                    for ref in (endpoints.OTLP_GRPC, endpoints.OTLP_HTTP)
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
        },
    )
    release(
        endpoints.NODE_NAME,
        values={
            # Its own name label, so neither the central release's selectors nor the OTLP
            # NetworkPolicy below match these pods.
            "nameOverride": endpoints.NODE_NAME,
            "alloy": {
                "configMap": {"name": node_config_map.name, "key": _NODE_CONFIG_KEY, "create": False},
                "storagePath": _NODE_STORAGE_PATH,
                "mounts": {
                    "extra": [
                        {"name": "wal", "mountPath": _NODE_STORAGE_PATH},
                        {"name": "pod-logs", "mountPath": "/var/log/pods", "readOnly": True},
                    ]
                },
                "securityContext": {
                    "runAsUser": 0,
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "extraEnv": [
                    {"name": "MIMIR_PUSH_URL", "value": mimir.PUSH_URL},
                    {"name": "LOKI_PUSH_URL", "value": loki.PUSH_URL},
                    {"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
                ],
                # Keep both the metrics working set and Promtail's measured page-cache
                # headroom; never reinstate its etcd-starving 128Mi cap (2026_06_19 incident).
                "resources": {"requests": {"cpu": "70m", "memory": "512Mi"}, "limits": {"memory": "2Gi"}},
            },
            "controller": {
                "type": "daemonset",
                "volumes": {
                    "extra": [
                        {"name": "wal", "hostPath": {"path": _NODE_STORAGE_PATH, "type": "DirectoryOrCreate"}},
                        {"name": "pod-logs", "hostPath": {"path": "/var/log/pods", "type": "Directory"}},
                    ]
                },
                "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION, node_scheduling.ROAMING_TOLERATION],
            },
        },
    )
    # Journal paths and root journal-reader permissions stay off Talos nodes. A
    # separate Alloy is necessary because host mounts are a Pod-template property.
    journal_volumes = (
        ("positions", _JOURNAL_STORAGE_PATH, "DirectoryOrCreate"),
        ("journal", "/var/log/journal", "Directory"),
        ("machine-id", "/etc/machine-id", "File"),
    )
    release(
        endpoints.JOURNAL_NAME,
        values={
            "nameOverride": endpoints.JOURNAL_NAME,
            "rbac": {"create": False},
            "serviceAccount": {"automountServiceAccountToken": False},
            "alloy": {
                "configMap": {"name": journal_config_map.name, "key": _JOURNAL_CONFIG_KEY, "create": False},
                "storagePath": _JOURNAL_STORAGE_PATH,
                "extraEnv": [{"name": "LOKI_PUSH_URL", "value": loki.PUSH_URL}],
                "mounts": {
                    "extra": [
                        {"name": name, "mountPath": path, "readOnly": name != "positions"}
                        for name, path, _ in journal_volumes
                    ]
                },
                "securityContext": {
                    "runAsUser": 0,
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "resources": {
                    "requests": {"cpu": "50m", "memory": "256Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                },
            },
            "controller": {
                "type": "daemonset",
                "nodeSelector": node_scheduling.NIXOS_NODE_SELECTOR,
                "tolerations": [{"effect": "NoSchedule", "operator": "Exists"}],
                "volumes": {
                    "extra": [
                        {"name": name, "hostPath": {"path": path, "type": kind}} for name, path, kind in journal_volumes
                    ]
                },
            },
        },
    )
    release(
        endpoints.SYSLOG_NAME,
        values={
            "nameOverride": endpoints.SYSLOG_NAME,
            "alloy": {
                "configMap": {"name": syslog_config_map.name, "key": _SYSLOG_CONFIG_KEY, "create": False},
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
                # The gateway listener's `raw` syslog format is experimental.
                "stabilityLevel": "experimental",
            },
            "controller": {
                "type": "deployment",
                "replicas": 1,
                "nodeSelector": node_scheduling.OPTIPLEX.node_selector,
                # A surge pod could never schedule beside the old one holding the hostPort.
                "updateStrategy": {"type": "Recreate"},
            },
        },
    )
    k8s.KubeNetworkPolicy(
        chart,
        "otlp-ingress",
        metadata=k8s.ObjectMeta(name="alloy-otlp-ingress", namespace=NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=endpoints.CENTRAL.selector),
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
                    ports=[
                        k8s.NetworkPolicyPort(
                            port=k8s.IntOrString.from_number(endpoints.OTLP_HTTP.pod_port), protocol="TCP"
                        )
                    ],
                ),
                # Agentplane services, runners, and their egress sidecars emit from labelled Pods
                # in the staging namespace.
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": STAGING_NAMESPACE}
                            ),
                            pod_selector=k8s.LabelSelector(match_labels=telemetry.EMITTER_LABELS),
                        )
                    ],
                    ports=[
                        k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(port), protocol="TCP")
                        for port in (endpoints.OTLP_GRPC.pod_port, endpoints.OTLP_HTTP.pod_port)
                    ],
                ),
                # The chart's ServiceMonitor targets this pod, so the alloy-node on its node scrapes it.
                k8s.NetworkPolicyIngressRule(
                    from_=[k8s.NetworkPolicyPeer(pod_selector=k8s.LabelSelector(match_labels=endpoints.NODE.selector))],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(_HTTP_PORT), protocol="TCP")],
                ),
            ],
        ),
    )
    return chart


def alloy(
    chart: Chart,
    directory: RenderedDirectory,
    monitoring_crds: Kustomization,
    *,
    health_checks: list[KustomizationSpecHealthChecks],
) -> Kustomization:
    return flux_kustomization(
        chart,
        endpoints.CENTRAL_NAME,
        directory,
        wait=None,
        health_checks=health_checks,
        timeout="5m",
        # the chart's serviceMonitor
        depends_on=flux_kustomization_depends_on_many(monitoring_crds),
    )
