"""Temporary inventory to prune orphaned Haku Kubernetes API proxy Secrets."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "haku-kube-api-proxy-secret-prune"
OUTPUT_DIR = f"{GENERATED_ROOT}/haku-kube-api-proxy-secret-prune"
NAMESPACE = "haku-console"

# cert-manager and Reflector leave these output Secrets without owner references. This
# temporary inventory adopts them without applying data; remove its Flux Kustomization
# after the source owners have reconciled away to let Flux prune the keys.
_SECRETS = (("haku-console-public-coder-agent", "Opaque"), ("haku-kube-api-proxy-tls", "kubernetes.io/tls"))


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for name, secret_type in _SECRETS:
        k8s.KubeSecret(
            chart,
            name,
            metadata=k8s.ObjectMeta(
                name=name, namespace=NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/ssa": "IfNotPresent"}
            ),
            type=secret_type,
        )
    return chart
