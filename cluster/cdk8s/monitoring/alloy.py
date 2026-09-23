"""Grafana Alloy: the HelmRelease and the NetworkPolicy admitting OTLP from Authentik's outpost.

Hand-written beside the generated output: `config.alloy` (rendered into the `alloy-config`
ConfigMap by the directory's `configMapGenerator`) and the `kustomization.yaml` that
generates it.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
)

from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

_NAME = "alloy"
_NAMESPACE = "monitoring"
_OUTPUT_DIR = "cluster/k8s/monitoring/alloy"
_OTLP_HTTP_PORT = 4318


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    HelmRelease(
        chart,
        "helm-release",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=HelmReleaseSpec(
            interval="30m",
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart=_NAME,
                    # renovate: datasource=helm depName=alloy registryUrl=https://grafana.github.io/helm-charts
                    version="1.x",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name="grafana",
                        namespace="flux-system",
                    ),
                    interval="12h",
                )
            ),
            values={
                "alloy": {
                    # Config lives in config.alloy; generated into alloy-config ConfigMap by kustomization.yaml.
                    "configMap": {"name": "alloy-config", "key": "config.alloy", "create": False},
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
                },
                "serviceMonitor": {"enabled": True},
                "resources": {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                },
            },
        ),
    )
    k8s.KubeNetworkPolicy(
        chart,
        "otlp-ingress",
        metadata=k8s.ObjectMeta(name="alloy-otlp-ingress", namespace=_NAMESPACE),
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
                )
            ],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart)
