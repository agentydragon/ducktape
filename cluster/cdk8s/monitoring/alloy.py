"""Grafana Alloy: the HelmRelease, the NetworkPolicy admitting OTLP from Authentik's outpost, and
the `alloy-config` ConfigMap.

Its `config.alloy` is rendered from the `string.Template` of that name beside this module; each
`${...}` placeholder is an address or port a generator module owns.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from string import Template

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s.flux import (
    ConfigMapArgs,
    GeneratorOptions,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import grafana_helmrepository, loki, mimir, tempo
from util.bazel.runfiles import get_required_path, own_repo_rlocation

_NAME = "alloy"
NAMESPACE = "monitoring"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/alloy"
_OTLP_HTTP_PORT = 4318
_CONFIG_SOURCE = "cluster/cdk8s/monitoring/config.alloy"
_CONFIG_FILE = PurePosixPath(_CONFIG_SOURCE).name
_CONFIG_MAP = "alloy-config"


def write_config_map(root: Path) -> ConfigMapArgs:
    """Render `config.alloy` into `OUTPUT_DIR`; return the `configMapGenerator` entry packaging it."""
    config = Template(get_required_path(own_repo_rlocation(_CONFIG_SOURCE)).read_text()).substitute(
        mimir_push_url=mimir.PUSH_URL,
        mimir_gateway_url=mimir.GATEWAY_URL,
        otlp_http_port=_OTLP_HTTP_PORT,
        loki_push_url=loki.PUSH_URL,
        tempo_otlp_grpc_endpoint=tempo.OTLP_GRPC_ENDPOINT,
    )
    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / _CONFIG_FILE).write_text(config)
    return ConfigMapArgs(
        name=_CONFIG_MAP,
        namespace=NAMESPACE,
        # The Helm values name the ConfigMap, and kustomize cannot rewrite a reference inside a
        # HelmRelease's values.
        options=GeneratorOptions(disable_name_suffix_hash=True),
        files=[_CONFIG_FILE],
    )


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
                "configMap": {"name": _CONFIG_MAP, "key": _CONFIG_FILE, "create": False},
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
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
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
                )
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
                api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=_NAME, namespace=NAMESPACE
            )
        ],
        timeout="5m",
        # the chart's serviceMonitor
        depends_on=flux_kustomization_depends_on_many(monitoring_crds),
    )
