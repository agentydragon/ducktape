"""Grafana Alloy's central Deployment, CP-local scrape DaemonSet, and OTLP ingress policy.

Hand-written beside the generated output: the two Alloy configs (rendered into ConfigMaps by
the directory's `configMapGenerator`) and the `kustomization.yaml` that generates them.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecPostRenderers,
    HelmReleaseSpecPostRenderersKustomize,
    HelmReleaseSpecPostRenderersKustomizePatches,
    HelmReleaseSpecPostRenderersKustomizePatchesTarget,
)

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.monitoring import grafana_helmrepository

_NAME = "alloy"
_NAMESPACE = "monitoring"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/monitoring/alloy"
_OTLP_HTTP_PORT = 4318
_CONTROL_PLANE_RELEASE = "alloy-control-plane"
_CONTROL_PLANE_METRICS_RBAC = "alloy-control-plane-metrics"
_CONTROL_PLANE_READINESS_PATCH = """\
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: alloy-control-plane
spec:
  template:
    spec:
      containers:
        - name: alloy
          readinessProbe:
            httpGet:
              host: 127.0.0.1
"""


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    helm_release(
        chart,
        _NAME,
        _NAMESPACE,
        repository=grafana_helmrepository.SOURCE_REF,
        chart=_NAME,
        version="1.x",
        interval="30m",
        chart_interval="12h",
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
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
        },
    )
    helm_release(
        chart,
        _CONTROL_PLANE_RELEASE,
        _NAMESPACE,
        repository=grafana_helmrepository.SOURCE_REF,
        chart="alloy",
        version="1.x",
        interval="30m",
        chart_interval="12h",
        post_renderers=[
            HelmReleaseSpecPostRenderers(
                kustomize=HelmReleaseSpecPostRenderersKustomize(
                    patches=[
                        HelmReleaseSpecPostRenderersKustomizePatches(
                            target=HelmReleaseSpecPostRenderersKustomizePatchesTarget(
                                kind="DaemonSet", name=_CONTROL_PLANE_RELEASE
                            ),
                            patch=_CONTROL_PLANE_READINESS_PATCH,
                        )
                    ]
                )
            )
        ],
        values={
            "fullnameOverride": _CONTROL_PLANE_RELEASE,
            "alloy": {
                "configMap": {"name": "alloy-control-plane-config", "key": "control-plane.alloy", "create": False},
                # This instance uses the host network only to scrape its own loopback.
                # Keep Alloy's unauthenticated UI on host loopback too.
                "listenAddr": "127.0.0.1",
                "extraEnv": [{"name": "NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}}],
                "resources": {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                },
            },
            "controller": {
                "type": "daemonset",
                "hostNetwork": True,
                "dnsPolicy": "ClusterFirstWithHostNet",
                "nodeSelector": {node_scheduling.CONTROL_PLANE_TAINT_KEY: ""},
                "tolerations": [
                    {"key": node_scheduling.CONTROL_PLANE_TAINT_KEY, "operator": "Exists", "effect": "NoSchedule"}
                ],
            },
            "service": {"enabled": False},
            "serviceMonitor": {"enabled": False},
            "serviceAccount": {"name": _CONTROL_PLANE_RELEASE, "automountServiceAccountToken": True},
            # The chart's RBAC template cannot render one empty rules list beside a non-empty
            # list. Define the narrow metrics permission below instead of granting its defaults.
            "rbac": {"create": False},
        },
    )
    metrics_role = k8s.KubeClusterRole(
        chart,
        "control-plane-metrics-role",
        metadata=k8s.ObjectMeta(name=_CONTROL_PLANE_METRICS_RBAC),
        rules=[k8s.PolicyRule(non_resource_ur_ls=["/metrics"], verbs=["get"])],
    )
    k8s.KubeClusterRoleBinding(
        chart,
        "control-plane-metrics-binding",
        metadata=k8s.ObjectMeta(name=_CONTROL_PLANE_METRICS_RBAC),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind=metrics_role.kind, name=metrics_role.name),
        subjects=[k8s.Subject(kind="ServiceAccount", name=_CONTROL_PLANE_RELEASE, namespace=_NAMESPACE)],
    )
    k8s.KubeNetworkPolicy(
        chart,
        "otlp-ingress",
        metadata=k8s.ObjectMeta(name="alloy-otlp-ingress", namespace=_NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(
                match_labels={"app.kubernetes.io/name": _NAME, "app.kubernetes.io/instance": _NAME}
            ),
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
    write_charts(root, OUTPUT_DIR, chart)
