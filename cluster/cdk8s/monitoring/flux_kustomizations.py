"""Generated Flux Kustomizations for the monitoring slice."""

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def monitoring_crds(chart: Chart) -> Kustomization:
    return flux_kustomization(
        chart,
        "monitoring-crds",
        # The description-bearing variant: the CRDs kube-prometheus-stack's own `crds`
        # subchart installed, so adopting them changes no schema.
        KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY,
            name="prometheus-operator-source",
            namespace="ducktape-flux",
        ),
        interval="1h",
        path="./example/prometheus-operator-crd-full",
        prune=False,  # Don't delete CRDs on uninstall (safety)
        timeout="5m",
    )
