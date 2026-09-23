"""Forgejo's metadata database: a CNPG `Cluster` on the SSD tier, owned by the `forgejo`
Kustomization, beside its hand-written application-user Secret."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cnpg_cluster_crds.io.cnpg.postgresql import (
    Cluster,
    ClusterSpec,
    ClusterSpecAffinity,
    ClusterSpecAffinityTolerations,
    ClusterSpecBootstrap,
    ClusterSpecBootstrapInitdb,
    ClusterSpecBootstrapInitdbSecret,
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

OUTPUT_DIR = "cluster/k8s/forgejo/db"
NAMESPACE = "forgejo"
CLUSTER_NAME = "forgejo-db-ssd"
DATABASE = "forgejo"
# Not `<cluster>-app`: CNPG reserves that name for a Secret it generates itself.
CREDENTIALS_SECRET = f"{CLUSTER_NAME}-creds"
_CREDENTIALS_FILE = f"{CREDENTIALS_SECRET}.sops.yaml"


def _chart(app: App) -> Chart:
    chart = Chart(app, CLUSTER_NAME, disable_resource_name_hashes=True)
    Cluster(
        chart,
        "cluster",
        metadata=metadata(
            CLUSTER_NAME,
            NAMESPACE,
            annotations={
                "description": (
                    "Forgejo metadata DB on the SSD tier (physically cloned from the retired forgejo-db,"
                    " Case B git-latency migration)"
                )
            },
        ),
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
                tolerations=[
                    ClusterSpecAffinityTolerations(
                        key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
                    )
                ],
                # Existing SSD-local replicas remain pinned by their PVs; the node
                # affinity only steers placements no existing claim constrains.
                topology_key="kubernetes.io/hostname",
                node_affinity=OFF_CONTROL_PLANE_NODE_AFFINITY,
            ),
            storage=ClusterSpecStorage(storage_class="local-path-ovh-ssd", size="10Gi"),
            monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
            # The cluster was created by pg_basebackup from the retired forgejo-db, so this
            # stanza never initializes anything. It names the application role CNPG
            # reconciles: the cloned owner `forgejo`, not CNPG's default `app`, which does
            # not exist here. CNPG keeps that role's password in sync with the Secret,
            # which Forgejo also authenticates with.
            bootstrap=ClusterSpecBootstrap(
                initdb=ClusterSpecBootstrapInitdb(
                    database=DATABASE, owner=DATABASE, secret=ClusterSpecBootstrapInitdbSecret(name=CREDENTIALS_SECRET)
                )
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, _chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{CLUSTER_NAME}.k8s.yaml", _CREDENTIALS_FILE]),
    )
