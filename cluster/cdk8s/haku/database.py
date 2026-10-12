"""The console's CNPG Postgres: the distributed approval ledger for MCP tool calls.

This shares the `haku-console` Kustomization with the migration Job and the console
workloads that read the `<cluster>-app` Secret CNPG mints, so nothing orders them behind
it; they retry until the Cluster accepts connections.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from cnpg_database_crds.io.cnpg.postgresql import (
    DatabaseSpecCluster,
    DatabaseSpecDatabaseReclaimPolicy,
    DatabaseSpecExtensions,
    DatabaseSpecExtensionsEnsure,
)
from constructs import Construct

from cluster.cdk8s import cnpg, cnpg_backup, node_scheduling
from cluster.cdk8s.providers.cnpg.database import Database

NAMESPACE = "haku-console"
POSTGRES = cnpg.PostgresRef.generated(name="haku-console-db", namespace=NAMESPACE)
DATABASE = "approval_store"


class Db(Construct):
    """The Cluster and the `approval_store` Database it adopts."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        cnpg.cluster(
            self,
            "cluster",
            ref=POSTGRES,
            # CNPG owns the data PVCs through this object, so a prune loses them and leaves
            # only a restore from backup. The annotation exempts it whichever
            # Kustomization's inventory lists it (cluster/cdk8s/AGENTS.md) -- including
            # while ownership moves between them. Removing this Cluster is a deliberate
            # `kubectl delete`, never a manifest edit.
            annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"},
            placement=node_scheduling.HIL_OVH,
            storage_class="local-path-ovh",
            size="2Gi",
            initdb=cnpg.same_owner_initdb(DATABASE),
            wal_archive=True,
        )
        cnpg_backup.CnpgBackup(self, "db-backup", ref=POSTGRES, policy=cnpg_backup.DAILY_30D)
        # `vector`: the console's 0135 migration baseline creates a `halfvec` column on a fresh
        # database (0136 drops it), and the migration role cannot install the extension itself.
        # This Database adopts the one created by `bootstrap.initdb`, hence `retain`: with
        # `delete`, dropping this object would drop the console's whole database.
        Database(
            self,
            "database",
            metadata=ApiObjectMetadata(name=f"{POSTGRES.name}-approval-store", namespace=NAMESPACE),
            cluster=DatabaseSpecCluster(name=POSTGRES.name),
            name=DATABASE,
            owner=DATABASE,
            database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.RETAIN,
            extensions=[DatabaseSpecExtensions(name="vector", ensure=DatabaseSpecExtensionsEnsure.PRESENT)],
        )
