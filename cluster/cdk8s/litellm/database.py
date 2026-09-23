"""LiteLLM's virtual-key/spend database (Prisma-managed schema; LiteLLM migrates it on
startup), owned by the `litellm` Kustomization.

OVH-HA profile per docs/cnpg_conventions.md: LiteLLM must keep serving external-provider
models (Anthropic and Gemini) during a home outage, so its DB lives on the always-on OVH
nodes. The litellm Deployment deliberately floats (prefers proxmox, allows OVH); DB
traffic is light (key lookups + spend writes), so the possible cross-site hop is
acceptable.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cnpg_cluster_crds.io.cnpg.postgresql import ClusterSpecBootstrapInitdb

from cluster.cdk8s import cnpg
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml

OUTPUT_DIR = "cluster/k8s/litellm/db"
_CLUSTER_NAME = "litellm-db"


def _chart(app: App) -> Chart:
    chart = Chart(app, _CLUSTER_NAME, disable_resource_name_hashes=True)
    cnpg.cluster(
        chart,
        "cluster",
        name=_CLUSTER_NAME,
        namespace="litellm",
        affinity=cnpg.affinity(node_selector={"topology.kubernetes.io/zone": "hil-ovh"}, tolerate_control_plane=False),
        storage_class="local-path-ovh",
        size="5Gi",
        # CNPG auto-generates credentials in secret litellm-db-app.
        initdb=ClusterSpecBootstrapInitdb(database="litellm", owner="litellm"),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, _chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{_CLUSTER_NAME}.k8s.yaml"])
    )
