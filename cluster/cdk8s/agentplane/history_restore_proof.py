"""A restored copy of agentplane staging's CNPG Cluster (`cluster/generated/agentplane-staging/
history-restore-proof`), the isolated recovery the History Service cutover needs before staging changes
(agentplane/docs/staging_data_preservation.md § 3).

The copy recovers from the staging Cluster's own barman-cloud backups and WAL, takes no traffic and archives
nothing, so it cannot affect the live Cluster.
"""

# CLEANUP(added 2026-10-20): Delete this module, its Flux Kustomization and its generated directory once
#   HISTORY_GO_STAGING_CUTOVER records the proof's result. Flux prunes the copy and its volume with them.

from __future__ import annotations

from cdk8s import App, Chart
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecBootstrapRecovery,
    ClusterSpecExternalClusters,
    ClusterSpecExternalClustersPlugin,
)

from cluster.cdk8s import cnpg, node_scheduling
from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.staging import ENV
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "agentplane-staging-history-restore-proof"
OUTPUT_DIR = f"{GENERATED_ROOT}/agentplane-staging/history-restore-proof"

COPY = cnpg.PostgresRef.generated(name="postgres-restore-proof", namespace=ENV.namespace)


def chart(app: App) -> Chart:
    chart = Chart(app, "history-restore-proof", disable_resource_name_hashes=True)
    source = database.postgres(ENV)
    cnpg.cluster(
        chart,
        "cluster",
        ref=COPY,
        annotations={"description": f"Recovers from {source.name}'s backups and WAL to prove the restore."},
        instances=1,
        placement=node_scheduling.HIL_OVH,
        storage_class=database.STORAGE_CLASS,
        size=database.STORAGE_SIZE,
        initdb=None,
        wal_archive=False,
        recovery=ClusterSpecBootstrapRecovery(source=source.name, database="app", owner="app"),
        external_clusters=[
            ClusterSpecExternalClusters(
                name=source.name,
                plugin=ClusterSpecExternalClustersPlugin(
                    name=cnpg.BARMAN_PLUGIN, parameters={"barmanObjectName": source.name, "serverName": source.name}
                ),
            )
        ],
        enable_pod_monitor=False,
    )
    return chart


def agentplane_staging_history_restore_proof(
    chart: Chart, directory: RenderedDirectory, cnpg_operator: Kustomization, db_backups: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        # The recovery replays up to a day of WAL, far past any health wait; the Cluster's status says when it is up.
        wait=False,
        depends_on=flux_kustomization_depends_on_many(cnpg_operator, db_backups),
        description="Restores a copy of the agentplane staging Cluster from its backups, to prove the restore.",
    )
