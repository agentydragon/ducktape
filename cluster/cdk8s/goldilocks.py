"""Goldilocks, which turns VPA recommendations into a dashboard, and the NetworkPolicy that
admits only the Authentik proxy outpost to that dashboard."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import namespaces, vpa
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "goldilocks"
NAMESPACE = "goldilocks"
OUTPUT_DIR = f"{GENERATED_ROOT}/goldilocks"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.AUTO)
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        # Declared by the vpa directory.
        repository=vpa.REPOSITORY_SOURCE_REF,
        chart=NAME,
        version="11.1.0",
        interval="30m",
        chart_interval="12h",
        install=RETRY_FAILED_INSTALL,
        values={
            "vpa": {"enabled": False},
            "controller": {
                # Manages a namespace unless it is labeled `enabled: "false"`, so `namespaces.namespace`
                # labels an update mode alone.
                "flags": {"on-by-default": "true"},
                "resources": {
                    "requests": {"cpu": "25m", "memory": "256Mi"},
                    "limits": {"cpu": "200m", "memory": "512Mi"},
                },
            },
            "dashboard": {
                "enabled": True,
                "replicaCount": 1,
                "resources": {
                    "requests": {"cpu": "25m", "memory": "128Mi"},
                    "limits": {"cpu": "200m", "memory": "256Mi"},
                },
            },
        },
    )
    k8s.KubeNetworkPolicy(
        chart,
        "dashboard-ingress",
        metadata=k8s.ObjectMeta(name="goldilocks-dashboard-ingress", namespace=NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(
                match_labels={"app.kubernetes.io/name": NAME, "app.kubernetes.io/component": "dashboard"}
            ),
            policy_types=["Ingress"],
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "authentik"}
                            ),
                            pod_selector=k8s.LabelSelector(
                                match_labels={
                                    "app.kubernetes.io/component": "server",
                                    "app.kubernetes.io/name": "authentik",
                                }
                            ),
                        )
                    ],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(8080), protocol="TCP")],
                )
            ],
        ),
    )
    return chart


def goldilocks(chart: Chart, directory: RenderedDirectory, kyverno: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="5m",
        # Kyverno's failurePolicy: Fail webhooks admit the Namespace.
        depends_on=[flux_kustomization_depends_on(kyverno)],
    )
