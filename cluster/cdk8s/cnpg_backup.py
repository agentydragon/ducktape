"""A CNPG Cluster's barman-cloud backups: the SeaweedFS bucket, the `ObjectStore` the Cluster's WAL
archiver writes to (`cnpg.cluster(wal_archive=True)` names it after the Cluster), and the
`ScheduledBackup` taking its base backups. The Cluster's own chart builds them, beside it."""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class BackupPolicy:
    """When a Cluster's base backups run and how far back it can be recovered. WAL is archived
    continuously either way, so any moment inside the window is a recovery point.

    `schedule` is CNPG's six-field cron, seconds first, in UTC. `retention` is the ObjectStore's
    recovery window, `<n>d`, `<n>w` or `<n>m`: barman-cloud keeps every base backup and WAL
    segment needed to recover to any point within it, and no count-based policy exists.
    Every base backup is a full copy, so storage grows with (backups in the window) x (DB size)."""

    schedule: str
    retention: str


DAILY_30D = BackupPolicy(schedule="0 0 2 * * *", retention="30d")


class CnpgBackup(Construct):
    """`ref`'s backups into a private SeaweedFS bucket `<namespace>-db-backups`: the ObjectStore
    named `ref.name`, which `cnpg.cluster(wal_archive=True)` archives WAL to, and a ScheduledBackup
    on `policy.schedule`. `ref.namespace` must be
    in `seaweedfs.cluster.TENANTS`, and the Flux Kustomization applying this must depend on the
    SeaweedFS operator's. `secret_name` keeps the name of a credentials Secret the store already
    reads (`s3.PrivateBucket`)."""

    def __init__(
        self, scope: Construct, id: str, *, ref: cnpg.PostgresRef, policy: BackupPolicy, secret_name: str | None = None
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
                retention_policy=policy.retention,
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
                schedule=policy.schedule,
                backup_owner_reference=ScheduledBackupSpecBackupOwnerReference.SELF,
                cluster=ScheduledBackupSpecCluster(name=ref.name),
                method=ScheduledBackupSpecMethod.PLUGIN,
                plugin_configuration=ScheduledBackupSpecPluginConfiguration(name=cnpg.BARMAN_PLUGIN),
            ),
        )
