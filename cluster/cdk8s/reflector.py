"""Emberstack Reflector, which mirrors annotated Secrets and ConfigMaps across namespaces."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "reflector"
NAMESPACE = "reflector-system"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/reflector"
_VERSION = "10.0.65"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(chart, "namespace", metadata=k8s.ObjectMeta(name=NAMESPACE))
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata("emberstack", NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", url="https://emberstack.github.io/helm-charts"),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=repository,
        chart="reflector",
        version=_VERSION,
        interval="15m",
        install=RETRY_FAILED_INSTALL,
        values={
            "nameOverride": NAME,
            "fullnameOverride": NAME,
            "replicaCount": 1,
            "image": {"repository": "emberstack/kubernetes-reflector", "tag": _VERSION, "pullPolicy": "IfNotPresent"},
            "configuration": {"logging": {"minimumLevel": "Information"}, "watcher": {"timeout": 300}},
            "rbac": {"enabled": True},
            "serviceAccount": {"create": True, "name": NAME},
            "resources": {"requests": {"memory": "128Mi", "cpu": "100m"}, "limits": {"memory": "256Mi", "cpu": "200m"}},
            "nodeSelector": {},
            "tolerations": [],
            "affinity": {},
        },
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def reflector(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(chart, NAME, artifact, timeout="5m")
