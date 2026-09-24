"""Authentik's CNPG Postgres, rendered into `cluster/k8s/authentik/db`, which the
`authentik` Kustomization lists as a subdirectory."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cnpg_cluster_crds.io.cnpg.postgresql import ClusterSpecPlugins

from cluster.cdk8s import cnpg
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "authentik-db-ovh"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/authentik/db"
# The database is on node-local `local-path-ovh` storage and cannot move; Authentik's server
# selects the same zone to stay beside it.
NODE_SELECTOR = {"topology.kubernetes.io/zone": "hil-ovh"}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    cnpg.cluster(
        chart,
        "cluster",
        name=NAME,
        namespace="authentik",
        node_selector=NODE_SELECTOR,
        storage_class="local-path-ovh-ssd",
        size="8Gi",
        # The plugin sidecar archives WAL continuously and provides physical base
        # backups to the Authentik-specific SeaweedFS ObjectStore (authentik/db-backups).
        plugins=[
            ClusterSpecPlugins(
                name="barman-cloud.cloudnative-pg.io",
                is_wal_archiver=True,
                parameters={"barmanObjectName": "authentik-db-ovh"},
            )
        ],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{NAME}.k8s.yaml"]))
