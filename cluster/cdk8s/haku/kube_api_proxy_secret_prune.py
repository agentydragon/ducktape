"""Temporary inventory for pruning the retained Haku Kubernetes API proxy TLS Secret."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "haku-kube-api-proxy-secret-prune"
OUTPUT_DIR = f"{GENERATED_ROOT}/haku-kube-api-proxy-secret-prune"
NAMESPACE = "haku-console"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeSecret(
        chart,
        "haku-kube-api-proxy-tls",
        metadata=k8s.ObjectMeta(
            name="haku-kube-api-proxy-tls",
            namespace=NAMESPACE,
            annotations={"kustomize.toolkit.fluxcd.io/ssa": "Merge"},
        ),
        type="kubernetes.io/tls",
    )
    return chart
