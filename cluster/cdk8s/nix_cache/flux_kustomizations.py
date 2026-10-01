"""Flux Kustomizations for the cluster/k8s/nix-cache slice."""

from __future__ import annotations

from cdk8s import Chart

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many


def nix_cache(
    chart: Chart,
    directory: RenderedDirectory,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    seaweedfs_operator: Kustomization,
) -> Kustomization:
    name = "nix-cache"
    return flux_kustomization(
        chart,
        name,
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(cnpg, external_secrets_operator, seaweedfs_operator),
    )
