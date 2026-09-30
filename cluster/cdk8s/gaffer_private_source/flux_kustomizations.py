"""The `gaffer-private` bridge Flux Kustomization, whose directory lives in the private repo."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def gaffer_private_bridge(chart: Chart, kyverno: Kustomization, tofu_controller: Kustomization) -> Kustomization:
    """The cross-repo bridge: reconciles ``gaffer-private/k8s/`` from the private companion
    monorepo's ``gaffer-private`` GitRepository (built by ``source.chart``), the
    entry point for every private app's ``flux-kustomization.yaml``. Its child resources live
    in the private repo, outside this graph. ``wait`` stays off -- workload readiness is owned
    by the private child Kustomizations, which tolerate apply-then-eventually-ready."""
    name = "gaffer-private"
    return flux_kustomization(
        chart,
        name,
        KustomizationSpecSourceRef(kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name=name),
        namespace="flux-system",
        path="./k8s",
        timeout="5m",
        wait=None,
        depends_on=flux_kustomization_depends_on_many(
            # Kyverno's failurePolicy: Fail webhook admits the child Flux Kustomizations.
            kyverno,
            # The Terraform CRD: the private repo's k8s/ applies Terraform CRs.
            tofu_controller,
        ),
        description=(
            "Cross-repo bridge. Reconciles gaffer-private/k8s/ from the gaffer-private "
            "GitRepository -- entry point for every private app's flux-kustomization.yaml."
        ),
    )
