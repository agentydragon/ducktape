"""Authentik's CNPG Postgres, rendered into `cluster/k8s/authentik/db`, whose manifest the
`authentik` Kustomization lists."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecBootstrapInitdb,
    ClusterSpecBootstrapInitdbSecret,
    ClusterSpecPlugins,
)

from cluster.cdk8s import cnpg
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "authentik-db-ovh"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/authentik/db"
# The database is on node-local `local-path-ovh` storage and cannot move; Authentik's server
# selects the same zone to stay beside it.
NODE_SELECTOR = {"topology.kubernetes.io/zone": "hil-ovh"}
DATABASE = "authentik"
# The credentials CNPG generated for the retired `authentik-db`, which this cluster was
# cloned from; the role's password came with the clone.
CREDENTIALS_SECRET = "authentik-db-app"


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
        # The cluster was created by pg_basebackup from the retired authentik-db, so this
        # stanza never initializes anything. It names the application database and role
        # CNPG uses: the metrics exporter runs its default queries against the database,
        # and CNPG keeps the role's password in sync with the Secret Authentik also
        # authenticates with. Without it CNPG defaults to `app`, which does not exist here.
        initdb=ClusterSpecBootstrapInitdb(
            database=DATABASE, owner=DATABASE, secret=ClusterSpecBootstrapInitdbSecret(name=CREDENTIALS_SECRET)
        ),
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
