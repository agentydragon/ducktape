"""Flux Kustomizations for the cluster/k8s/gaffer-private-source slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT


def gaffer_private_source(chart: Chart, flux_image_automation_ghcr: Kustomization) -> Kustomization:
    name = "gaffer-private-source"
    return flux_kustomization(
        chart,
        name,
        KustomizationSpecSourceRef(kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="flux-system"),
        namespace="flux-system",
        wait=None,
        timeout="10m",
        path=f"./{HAND_WRITTEN_ROOT}/gaffer-private-source",
        decryption=SOPS_DECRYPTION,
        depends_on=[flux_kustomization_depends_on(flux_image_automation_ghcr)],
    )


def gaffer_private_bridge(
    chart: Chart,
    gaffer_private_source: Kustomization,
    authentik: Kustomization,
    gateway: Kustomization,
    cert_manager_issuer_config: Kustomization,
) -> Kustomization:
    """The cross-repo bridge: reconciles ``gaffer-private/k8s/`` from the private companion
    monorepo's ``gaffer-private`` GitRepository (built by ``gaffer_private_source``), the
    entry point for every private app's ``flux-kustomization.yaml``. Its child resources live
    in the private repo, outside this graph. ``wait`` stays off -- workload readiness is owned
    by the private child Kustomizations, which tolerate apply-then-eventually-ready. It
    depends on ``gaffer_private_source`` so the ``gaffer-private`` source exists before this
    reconciles (previously guaranteed by colocation in the source directory)."""
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
            gaffer_private_source, authentik, gateway, cert_manager_issuer_config
        ),
        description=(
            "Cross-repo bridge. Reconciles gaffer-private/k8s/ from the gaffer-private "
            "GitRepository -- entry point for every private app's flux-kustomization.yaml."
        ),
    )
