"""Emberstack Reflector, which mirrors annotated Secrets and ConfigMaps across namespaces."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "reflector"
NAMESPACE = "reflector-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/reflector"
_VERSION = "10.0.65"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(chart, "namespace", metadata=k8s.ObjectMeta(name=NAMESPACE))
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=helm_repository(chart, "emberstack", NAMESPACE, url="https://emberstack.github.io/helm-charts"),
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


def reflector(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(chart, NAME, directory, timeout="5m")
