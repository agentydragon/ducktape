"""Temporary Secret inventory for pruning retired Public Coder targets without storing values."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "public-coder-agent-secret-prune"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/public-coder-agent/secret-prune"
NAMESPACE = "public-coder-agent"
_MERGE_ANNOTATION = {"kustomize.toolkit.fluxcd.io/ssa": "Merge"}
_SECRET_TARGETS = (
    ("brave-search-api-key", "Opaque"),
    ("public-coder-agent-github-token", "Opaque"),
    ("public-coder-agent-proxy-ca", "kubernetes.io/tls"),
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for name, secret_type in _SECRET_TARGETS:
        k8s.KubeSecret(
            chart,
            f"secret-{name}",
            metadata=k8s.ObjectMeta(name=name, namespace=NAMESPACE, annotations=_MERGE_ANNOTATION),
            type=secret_type,
        )
    return chart
