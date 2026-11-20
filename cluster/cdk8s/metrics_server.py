"""metrics-server in kube-system, from its HelmRepository in flux-system."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "metrics-server"
NAMESPACE = "kube-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/metrics-server"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(
            chart, NAME, "flux-system", url="https://kubernetes-sigs.github.io/metrics-server/"
        ),
        chart="metrics-server",
        version="3.14.0",
        interval="30m",
        chart_interval="12h",
        install=RETRY_FAILED_INSTALL,
        values={
            "replicas": 2,
            "podDisruptionBudget": {"enabled": True, "minAvailable": 1},
            "affinity": {
                "podAntiAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "labelSelector": {
                                "matchLabels": {
                                    "app.kubernetes.io/instance": NAME,
                                    "app.kubernetes.io/name": NAME,
                                }
                            },
                            "topologyKey": "kubernetes.io/hostname",
                        }
                    ]
                }
            },
            # Talos-specific configuration
            "args": ["--kubelet-insecure-tls", "--kubelet-preferred-address-types=InternalIP,ExternalIP,Hostname"],
            "resources": {"limits": {"cpu": "100m", "memory": "128Mi"}, "requests": {"cpu": "10m", "memory": "32Mi"}},
        },
    )
    return chart
