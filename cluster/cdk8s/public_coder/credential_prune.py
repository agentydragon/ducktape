"""Temporarily adopt the remaining Public Coder Secrets into Flux before pruning them."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "public-coder-agent-secret-prune"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/public-coder-agent/secret-prune"
NAMESPACE = "public-coder-agent"

# These targets outlive their owners: the ESO ExternalSecrets use creationPolicy=Orphan, and
# cert-manager does not make its output Secret a child of the Certificate. This inventory lets
# Flux delete them in a follow-up revision without placing their contents in Git.
_SECRETS = ("brave-search-api-key", "buildbuddy-api-key", "public-coder-agent-proxy-ca")


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    for name in _SECRETS:
        k8s.KubeSecret(
            chart,
            name,
            metadata=k8s.ObjectMeta(
                name=name, namespace=NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/ssa": "IfNotPresent"}
            ),
            type="Opaque",
        )
    return chart
