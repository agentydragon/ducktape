"""metrics-server in kube-system, from its HelmRepository in flux-system."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "metrics-server"
NAMESPACE = "kube-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/metrics-server"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    repository = HelmRepository(
        chart,
        "repository",
        metadata=ApiObjectMetadata(name=NAME, namespace="flux-system"),
        spec=HelmRepositorySpec(interval="24h", url="https://kubernetes-sigs.github.io/metrics-server/"),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=repository,
        chart="metrics-server",
        version="3.14.0",
        interval="30m",
        chart_interval="12h",
        install=RETRY_FAILED_INSTALL,
        values={
            # Talos-specific configuration
            "args": ["--kubelet-insecure-tls", "--kubelet-preferred-address-types=InternalIP,ExternalIP,Hostname"],
            "resources": {"limits": {"cpu": "100m", "memory": "128Mi"}, "requests": {"cpu": "10m", "memory": "32Mi"}},
        },
    )
    return chart


def metrics_server(chart: Chart, directory: RenderedDirectory, kyverno: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="5m",
        depends_on=[
            # Kyverno webhook must be ready before creating workloads
            flux_kustomization_depends_on(kyverno)
        ],
    )
