"""Flux Kustomizations for the cluster/k8s/kubevirt slice."""

from __future__ import annotations

from cdk8s import Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization


def cdi_operator(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "cdi-operator"
    return flux_kustomization(chart, name, artifact, timeout="10m")


def kubevirt_operator(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "kubevirt-operator"
    return flux_kustomization(chart, name, artifact, timeout="10m")
