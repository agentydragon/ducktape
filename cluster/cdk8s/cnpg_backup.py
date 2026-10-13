"""A CNPG Cluster's barman-cloud backups: the SeaweedFS bucket, the `ObjectStore` the Cluster's WAL
archiver writes to (`cnpg.cluster(wal_archive=True)` names it after the Cluster), and the
`ScheduledBackup` taking its base backups. The Cluster's own chart builds them, beside it."""

from __future__ import annotations

import zlib
from enum import StrEnum

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
from cdk8s import ApiObjectMetadata
from cnpg_scheduledbackup_crds.io.cnpg.postgresql import (
    ScheduledBackup,
    ScheduledBackupSpec,
    ScheduledBackupSpecBackupOwnerReference,
    ScheduledBackupSpecCluster,
    ScheduledBackupSpecMethod,
    ScheduledBackupSpecPluginConfiguration,
)
from constructs import Construct

from cluster.cdk8s import cnpg
from cluster.cdk8s.seaweedfs import s3

_S3_ENDPOINT = "http://seaweedfs-s3.seaweedfs.svc:8333"


# Base backups start within [01:00, 05:00) UTC, each Cluster at its own minute (`_schedule`).
_WINDOW_START_MINUTE = 60
_WINDOW_MINUTES = 4 * 60


class Cadence(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"


def _schedule(ref: cnpg.PostgresRef, cadence: Cadence) -> str:
    """A six-field cron (seconds first, UTC) at a minute of the window derived from a hash of `ref`,
    so the Clusters' base backups spread over the window instead of all reading their disks and
    writing to SeaweedFS at once; a weekly one also takes its day of the week from the hash. Stable
    across renders; two Clusters can still land close."""
    digest = zlib.crc32(f"{ref.namespace}/{ref.name}".encode())
    hour, minute = divmod(_WINDOW_START_MINUTE + digest % _WINDOW_MINUTES, 60)
    match cadence:
        case Cadence.DAILY:
            day_of_week = "*"
        case Cadence.WEEKLY:
            day_of_week = str(digest // _WINDOW_MINUTES % 7)
    return f"0 {minute} {hour} * * {day_of_week}"


class CnpgBackup(Construct):
    """`ref`'s backups into a private SeaweedFS bucket `<namespace>-db-backups`: the ObjectStore
    named `ref.name`, which `cnpg.cluster(wal_archive=True)` archives WAL to, and a `cadence`
    ScheduledBackup (`_schedule`). WAL is archived continuously, so any moment within `retention`
    (the ObjectStore's recovery window, `<n>d`, `<n>w` or `<n>m`) is a recovery point. Every base
    backup is a full copy, so storage grows with (backups in the window) x (DB size); a large DB
    takes `Cadence.WEEKLY` base backups, trading longer WAL replay on restore for fewer copies.
    `ref.namespace` must be in `seaweedfs.cluster.TENANTS`, and the Flux Kustomization applying
    this must depend on the SeaweedFS operator's. `secret_name` keeps the name of a credentials
    Secret the store already reads (`s3.PrivateBucket`)."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        ref: cnpg.PostgresRef,
        retention: str,
        cadence: Cadence = Cadence.DAILY,
        secret_name: str | None = None,
    ) -> None:
        super().__init__(scope, id)
        bucket = f"{ref.namespace}-db-backups"
        storage = s3.PrivateBucket(
            self,
            "storage",
            name=bucket,
            tenant=ref.namespace,
            # A Cluster rebuilt under the same name finds its Retained bucket already there.
            adopt_existing=True,
            description=f"Private CNPG physical backups and WAL archive for {ref.namespace}/{ref.name}.",
            secret_name=secret_name,
        )
        ObjectStore(
            self,
            "object-store",
            metadata=ApiObjectMetadata(
                name=ref.name,
                namespace=ref.namespace,
                annotations={
                    "description": f"SeaweedFS S3 destination for {ref.namespace}/{ref.name} base backups and WAL."
                },
            ),
            spec=ObjectStoreSpec(
                retention_policy=retention,
                configuration=ObjectStoreSpecConfiguration(
                    destination_path=f"s3://{bucket}/",
                    endpoint_url=_S3_ENDPOINT,
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
            self,
            "scheduled-backup",
            metadata=ApiObjectMetadata(name=ref.name, namespace=ref.namespace),
            spec=ScheduledBackupSpec(
                schedule=_schedule(ref, cadence),
                backup_owner_reference=ScheduledBackupSpecBackupOwnerReference.SELF,
                cluster=ScheduledBackupSpecCluster(name=ref.name),
                method=ScheduledBackupSpecMethod.PLUGIN,
                plugin_configuration=ScheduledBackupSpecPluginConfiguration(name=cnpg.BARMAN_PLUGIN),
            ),
        )
