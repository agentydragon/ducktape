"""Agentplane staging's CNPG backups (`cluster/generated/agentplane-staging/db-backups`): the
SeaweedFS `PrivateBucket` and the barman-cloud `ObjectStore` the staging Cluster archives WAL to
(`DbProps.wal_archive`). Base backups are on demand: a `Backup` with method `plugin` names this
store through the Cluster's plugin configuration."""

from __future__ import annotations

from barman_cloud_objectstore_crds.io.cnpg.barmancloud import (
    ObjectStore,
    ObjectStoreSpec,
    ObjectStoreSpecConfiguration,
    ObjectStoreSpecConfigurationData,
    ObjectStoreSpecConfigurationDataCompression,
    ObjectStoreSpecConfigurationS3Credentials,
    ObjectStoreSpecConfigurationS3CredentialsAccessKeyId,
    ObjectStoreSpecConfigurationS3CredentialsSecretAccessKey,
    ObjectStoreSpecConfigurationWal,
    ObjectStoreSpecConfigurationWalCompression,
)
from cdk8s import ApiObjectMetadata, App, Chart

from cluster.cdk8s.agentplane import database
from cluster.cdk8s.agentplane.staging import ENV
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.seaweedfs import s3

NAME = "agentplane-staging-db-backups"
OUTPUT_DIR = f"{GENERATED_ROOT}/agentplane-staging/db-backups"


def chart(app: App) -> Chart:
    chart = Chart(app, "db-backups", disable_resource_name_hashes=True)
    storage = s3.PrivateBucket(
        chart,
        "storage",
        name=NAME,
        tenant=ENV.namespace,
        adopt_existing=False,
        description="Private CNPG physical backups and WAL archive for agentplane staging.",
    )
    ObjectStore(
        chart,
        "object-store",
        metadata=ApiObjectMetadata(
            # `cnpg.cluster` points the Cluster's WAL archiver at the ObjectStore named after it.
            name=database.postgres(ENV).name,
            namespace=ENV.namespace,
            annotations={"description": "SeaweedFS S3 destination for agentplane staging CNPG base backups and WAL."},
        ),
        spec=ObjectStoreSpec(
            retention_policy="30d",
            configuration=ObjectStoreSpecConfiguration(
                destination_path=f"s3://{NAME}/",
                endpoint_url="http://seaweedfs-s3.seaweedfs.svc:8333",
                s3_credentials=ObjectStoreSpecConfigurationS3Credentials(
                    access_key_id=ObjectStoreSpecConfigurationS3CredentialsAccessKeyId(
                        name=storage.access_key.secret.name, key=storage.access_key.key
                    ),
                    secret_access_key=ObjectStoreSpecConfigurationS3CredentialsSecretAccessKey(
                        name=storage.secret_key.secret.name, key=storage.secret_key.key
                    ),
                ),
                data=ObjectStoreSpecConfigurationData(compression=ObjectStoreSpecConfigurationDataCompression.GZIP),
                wal=ObjectStoreSpecConfigurationWal(compression=ObjectStoreSpecConfigurationWalCompression.GZIP),
            ),
        ),
    )
    return chart


def agentplane_staging_db_backups(
    chart: Chart, directory: RenderedDirectory, cnpg: Kustomization, seaweedfs_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="15m",
        depends_on=flux_kustomization_depends_on_many(cnpg, seaweedfs_operator),
        description="Creates the agentplane staging CNPG backup store and its SeaweedFS storage.",
    )
