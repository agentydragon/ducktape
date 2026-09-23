"""Authentik's CNPG Postgres, rendered into `cluster/k8s/authentik/db`, which the
`authentik` Kustomization lists as a subdirectory."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cnpg_cluster_crds.io.cnpg.postgresql import (
    Cluster,
    ClusterSpec,
    ClusterSpecAffinity,
    ClusterSpecAffinityTolerations,
    ClusterSpecMonitoring,
    ClusterSpecPlugins,
    ClusterSpecProbes,
    ClusterSpecProbesLiveness,
    ClusterSpecProbesLivenessIsolationCheck,
    ClusterSpecStorage,
)

from cluster.cdk8s.cnpg import OFF_CONTROL_PLANE_NODE_AFFINITY
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

NAME = "authentik-db-ovh"
OUTPUT_DIR = "cluster/k8s/authentik/db"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Cluster(
        chart,
        "cluster",
        metadata=metadata(NAME, "authentik"),
        spec=ClusterSpec(
            instances=2,
            image_name="ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie",
            probes=ClusterSpecProbes(
                liveness=ClusterSpecProbesLiveness(
                    isolation_check=ClusterSpecProbesLivenessIsolationCheck(enabled=False)
                )
            ),
            affinity=ClusterSpecAffinity(
                node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
                tolerations=[
                    ClusterSpecAffinityTolerations(
                        key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
                    )
                ],
                topology_key="kubernetes.io/hostname",
                node_affinity=OFF_CONTROL_PLANE_NODE_AFFINITY,
            ),
            storage=ClusterSpecStorage(storage_class="local-path-ovh", size="8Gi"),
            # The plugin sidecar archives WAL continuously and provides physical base
            # backups to the Authentik-specific SeaweedFS ObjectStore (authentik/db-backups).
            plugins=[
                ClusterSpecPlugins(
                    name="barman-cloud.cloudnative-pg.io",
                    is_wal_archiver=True,
                    parameters={"barmanObjectName": "authentik-db-ovh"},
                )
            ],
            monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"]))
