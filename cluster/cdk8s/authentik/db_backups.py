"""Authentik's CNPG backups (`cluster/generated/authentik/db-backups`): the SeaweedFS
`PrivateBucket`, the barman-cloud `ObjectStore` pointing at it, and the daily `ScheduledBackup`."""

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
from cnpg_scheduledbackup_crds.io.cnpg.postgresql import (
    ScheduledBackup,
    ScheduledBackupSpec,
    ScheduledBackupSpecBackupOwnerReference,
    ScheduledBackupSpecCluster,
    ScheduledBackupSpecMethod,
    ScheduledBackupSpecPluginConfiguration,
)

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.seaweedfs import s3

NAME = "authentik-db-backups"
NAMESPACE = "authentik"
OUTPUT_DIR = f"{GENERATED_ROOT}/authentik/db-backups"
_CREDENTIALS_SECRET = "authentik-db-backup-s3"
_OBJECT_STORE = "authentik-db-ovh"


def chart(app: App) -> Chart:
    chart = Chart(app, "db-backups", disable_resource_name_hashes=True)
    storage = s3.PrivateBucket(
        chart,
        "storage",
        name=NAME,
        tenant=NAMESPACE,
        adopt_existing=True,
        description="Private CNPG physical backups and WAL archive for Authentik.",
        secret_name=_CREDENTIALS_SECRET,
    )
    ObjectStore(
        chart,
        "object-store",
        metadata=ApiObjectMetadata(
            name=_OBJECT_STORE,
            namespace=NAMESPACE,
            annotations={"description": "SeaweedFS S3 destination for Authentik CNPG base backups and WAL."},
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
    ScheduledBackup(
        chart,
        "scheduled-backup",
        metadata=ApiObjectMetadata(
            name="authentik-db-ovh-daily",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Daily physical Authentik database backup; WAL archiving provides continuous recovery points."
                )
            },
        ),
        spec=ScheduledBackupSpec(
            # Six-field cron with seconds; run daily at 02:00 UTC.
            schedule="0 0 2 * * *",
            backup_owner_reference=ScheduledBackupSpecBackupOwnerReference.SELF,
            cluster=ScheduledBackupSpecCluster(name="authentik-db-ovh"),
            method=ScheduledBackupSpecMethod.PLUGIN,
            plugin_configuration=ScheduledBackupSpecPluginConfiguration(name="barman-cloud.cloudnative-pg.io"),
        ),
    )
    return chart


def authentik_db_backups(
    chart: Chart, directory: RenderedDirectory, cnpg: Kustomization, seaweedfs_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="15m",
        depends_on=flux_kustomization_depends_on_many(cnpg, seaweedfs_operator),
        description="Creates the Authentik CNPG backup schedule and its SeaweedFS storage.",
    )
