"""Authentik's CNPG backups (`cluster/k8s/authentik/db-backups`): the SeaweedFS storage, the
barman-cloud `ObjectStore` pointing at it, and the daily `ScheduledBackup`."""

from __future__ import annotations

from pathlib import Path

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
from cdk8s import App, Chart
from cnpg_scheduledbackup_crds.io.cnpg.postgresql import (
    ScheduledBackup,
    ScheduledBackupSpec,
    ScheduledBackupSpecBackupOwnerReference,
    ScheduledBackupSpecCluster,
    ScheduledBackupSpecMethod,
    ScheduledBackupSpecPluginConfiguration,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import s3

NAME = "authentik-db-backups"
NAMESPACE = "authentik"
OUTPUT_DIR = "cluster/k8s/authentik/db-backups"
_CREDENTIALS_SECRET = "authentik-db-backup-s3"
_OBJECT_STORE = "authentik-db-ovh"


def chart(app: App) -> Chart:
    chart = Chart(app, "db-backups", disable_resource_name_hashes=True)
    # Tenant-local, so the grant below covers S3Identity too.
    identity = s3.Identity(
        chart,
        "identity",
        name=NAME,
        namespace=NAMESPACE,
        description="Dedicated SeaweedFS identity for Authentik CNPG backups.",
    )
    identity.credentials(
        namespace=NAMESPACE,
        # The operator creates and owns this Secret in the credential's namespace, where the
        # ObjectStore below consumes it.
        secret=_CREDENTIALS_SECRET,
        key_fields=s3.AWS_ENV_KEY_FIELDS,
    )
    s3.Bucket(
        chart,
        "bucket",
        name=NAME,
        namespace=NAMESPACE,
        adopt_existing=True,
        description="Private CNPG physical backups and WAL archive for Authentik.",
    ).grant_read_write(identity)
    ObjectStore(
        chart,
        "object-store",
        metadata=metadata(
            _OBJECT_STORE,
            NAMESPACE,
            annotations={"description": "SeaweedFS S3 destination for Authentik CNPG base backups and WAL."},
        ),
        spec=ObjectStoreSpec(
            retention_policy="30d",
            configuration=ObjectStoreSpecConfiguration(
                destination_path=f"s3://{NAME}/",
                endpoint_url="http://seaweedfs-s3.seaweedfs.svc:8333",
                s3_credentials=ObjectStoreSpecConfigurationS3Credentials(
                    access_key_id=ObjectStoreSpecConfigurationS3CredentialsAccessKeyId(
                        name=_CREDENTIALS_SECRET, key="AWS_ACCESS_KEY_ID"
                    ),
                    secret_access_key=ObjectStoreSpecConfigurationS3CredentialsSecretAccessKey(
                        name=_CREDENTIALS_SECRET, key="AWS_SECRET_ACCESS_KEY"
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
        metadata=metadata(
            "authentik-db-ovh-daily",
            NAMESPACE,
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


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def authentik_db_backups(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, cnpg: Kustomization, seaweedfs_cluster: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="15m",
        depends_on=flux_kustomization_depends_on_many(cnpg, seaweedfs_cluster),
        description="Creates the Authentik CNPG backup schedule and its SeaweedFS storage.",
    )
