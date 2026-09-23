"""Flux Kustomizations for the cluster/k8s/coredns-custom slice."""

from __future__ import annotations

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def coredns_custom(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "coredns-custom"
    return flux_kustomization(chart, name, artifact, interval="10m0s", timeout="5m")
