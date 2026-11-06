"""CNPG storage for CDviz events, with separate ingestion and dashboard roles."""

from cdk8s import ApiObjectMetadata, App, Chart
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesPasswordSecret,
)
from cnpg_database_crds.io.cnpg.postgresql import (
    DatabaseSpecCluster,
    DatabaseSpecDatabaseReclaimPolicy,
    DatabaseSpecExtensions,
    DatabaseSpecExtensionsEnsure,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
)

from cluster.cdk8s import namespaces
from cluster.cdk8s.cdviz import config, timescale
from cluster.cdk8s.cnpg import backup, cluster
from cluster.cdk8s.external_secrets.minted_secret import mint_db_role_secret
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import CNPG_DATABASE_READY
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cnpg.database import Database

OUTPUT_DIR = f"{config.OUTPUT_DIR}/database"


def chart(app: App) -> Chart:
    chart = Chart(app, "cdviz-database", disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=config.NAMESPACE, vpa=Vpa.RECOMMEND)
    roles = []
    for role, secret in [("cdviz_collector", config.COLLECTOR_PASSWORD), ("cdviz_reader", config.GRAFANA_PASSWORD)]:
        mint_db_role_secret(
            chart,
            role,
            name=secret.name,
            namespace=secret.namespace,
            role=role,
            host=config.DATABASE.rw.host,
            port=config.DATABASE.rw.port.number,
            database=config.DATABASE_NAME,
        )
        roles.append(
            ClusterSpecManagedRoles(
                name=role, login=True, password_secret=ClusterSpecManagedRolesPasswordSecret(name=secret.name)
            )
        )
    cluster.cluster(
        chart,
        "cluster",
        ref=config.DATABASE,
        profile=cluster.OVH_PIRAEUS_SSD,
        size="10Gi",
        image_name=timescale.POSTGRES_IMAGE,
        initdb=cluster.same_owner_initdb(config.DATABASE_NAME),
        postgresql=timescale.postgresql(),
        managed=ClusterSpecManaged(roles=roles),
        wal_archive=True,
    )
    Database(
        chart,
        "database",
        metadata=ApiObjectMetadata(name=config.DATABASE_NAME, namespace=config.NAMESPACE),
        cluster=DatabaseSpecCluster(name=config.DATABASE.name),
        name=config.DATABASE_NAME,
        owner=config.DATABASE_NAME,
        database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.RETAIN,
        extensions=[DatabaseSpecExtensions(name="timescaledb", ensure=DatabaseSpecExtensionsEnsure.PRESENT)],
    )
    backup.CnpgBackup(chart, "backup", ref=config.DATABASE, retention="30d", adopt_existing=False)
    return chart


def cdviz_database(
    chart: Chart,
    directory: RenderedDirectory,
    cnpg: Kustomization,
    external_secrets: Kustomization,
    seaweedfs: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "cdviz-database",
        directory,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="postgresql.cnpg.io/v1",
                kind="Database",
                current=(
                    f"{CNPG_DATABASE_READY} && has(status.extensions) && "
                    "status.extensions.exists(e, e.name == 'timescaledb' && e.applied)"
                ),
            )
        ],
        depends_on=flux_kustomization_depends_on_many(cnpg, external_secrets, seaweedfs),
    )
