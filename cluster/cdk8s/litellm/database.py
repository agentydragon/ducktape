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
from cnpg_cluster_crds.io.cnpg.postgresql import (
    Cluster,
    ClusterSpec,
    ClusterSpecAffinity,
    ClusterSpecBootstrap,
    ClusterSpecBootstrapInitdb,
    ClusterSpecMonitoring,
    ClusterSpecProbes,
    ClusterSpecProbesLiveness,
    ClusterSpecProbesLivenessIsolationCheck,
    ClusterSpecStorage,
)

from cluster.cdk8s.cnpg import OFF_CONTROL_PLANE_NODE_AFFINITY
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

OUTPUT_DIR = "cluster/k8s/litellm/db"
_CLUSTER_NAME = "litellm-db"


def _chart(app: App) -> Chart:
    chart = Chart(app, _CLUSTER_NAME, disable_resource_name_hashes=True)
    Cluster(
        chart,
        "cluster",
        metadata=metadata(_CLUSTER_NAME, "litellm"),
        spec=ClusterSpec(
            instances=2,
            # renovate: datasource=docker
            image_name="ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie",
            probes=ClusterSpecProbes(
                liveness=ClusterSpecProbesLiveness(
                    isolation_check=ClusterSpecProbesLivenessIsolationCheck(enabled=False)
                )
            ),
            affinity=ClusterSpecAffinity(
                node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
                topology_key="kubernetes.io/hostname",
                node_affinity=OFF_CONTROL_PLANE_NODE_AFFINITY,
            ),
            storage=ClusterSpecStorage(storage_class="local-path-ovh", size="5Gi"),
            monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
            # CNPG auto-generates credentials in secret litellm-db-app.
            bootstrap=ClusterSpecBootstrap(initdb=ClusterSpecBootstrapInitdb(database="litellm", owner="litellm")),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, _chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{_CLUSTER_NAME}.k8s.yaml"])
    )
