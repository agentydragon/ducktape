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
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from seaweed_bucket_crds.com.seaweedfs.seaweed import (
    Bucket,
    BucketSpec,
    BucketSpecAccess,
    BucketSpecAccessActions,
    BucketSpecClusterRef,
    BucketSpecReclaimPolicy,
)
from seaweed_resourcereferencegrant_crds.com.seaweedfs.seaweed import (
    ResourceReferenceGrant,
    ResourceReferenceGrantSpec,
    ResourceReferenceGrantSpecFrom,
    ResourceReferenceGrantSpecTo,
)
from seaweed_s3credentials_crds.com.seaweedfs.seaweed import (
    S3Credentials,
    S3CredentialsSpec,
    S3CredentialsSpecIdentityRef,
    S3CredentialsSpecReclaimPolicy,
    S3CredentialsSpecSeaweedRef,
    S3CredentialsSpecSecretRef,
)
from seaweed_s3identity_crds.com.seaweedfs.seaweed import (
    S3Identity,
    S3IdentitySpec,
    S3IdentitySpecReclaimPolicy,
    S3IdentitySpecSeaweedRef,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "authentik-db-backups"
NAMESPACE = "authentik"
OUTPUT_DIR = "cluster/k8s/authentik/db-backups"
_SEAWEED = "seaweedfs"
_SEAWEED_NAMESPACE = "seaweedfs"
_SEAWEED_GROUP = "seaweed.seaweedfs.com"
_CREDENTIALS_SECRET = "authentik-db-backup-s3"
_OBJECT_STORE = "authentik-db-ovh"


def chart(app: App) -> Chart:
    chart = Chart(app, "db-backups", disable_resource_name_hashes=True)
    Bucket(
        chart,
        "bucket",
        metadata=metadata(
            NAME, NAMESPACE, annotations={"description": "Private CNPG physical backups and WAL archive for Authentik."}
        ),
        spec=BucketSpec(
            name=NAME,
            adopt_existing=True,
            cluster_ref=BucketSpecClusterRef(name=_SEAWEED, namespace=_SEAWEED_NAMESPACE),
            reclaim_policy=BucketSpecReclaimPolicy.RETAIN,
            access=[
                BucketSpecAccess(
                    user=NAME,
                    actions=[
                        BucketSpecAccessActions.READ,
                        BucketSpecAccessActions.WRITE,
                        BucketSpecAccessActions.LIST,
                        BucketSpecAccessActions.TAGGING,
                    ],
                )
            ],
        ),
    )
    identity = S3Identity(
        chart,
        "identity",
        metadata=metadata(
            NAME, NAMESPACE, annotations={"description": "Dedicated SeaweedFS identity for Authentik CNPG backups."}
        ),
        spec=S3IdentitySpec(
            seaweed_ref=S3IdentitySpecSeaweedRef(name=_SEAWEED, namespace=_SEAWEED_NAMESPACE),
            reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN,
        ),
    )
    S3Credentials(
        chart,
        "credentials",
        metadata=metadata(NAME, NAMESPACE),
        spec=S3CredentialsSpec(
            seaweed_ref=S3CredentialsSpecSeaweedRef(name=_SEAWEED, namespace=_SEAWEED_NAMESPACE),
            identity_ref=S3CredentialsSpecIdentityRef(name=identity.name),
            # The operator creates and owns this Secret in the credential's namespace, where the
            # ObjectStore below consumes it.
            secret_ref=S3CredentialsSpecSecretRef(
                name=_CREDENTIALS_SECRET, access_key_field="AWS_ACCESS_KEY_ID", secret_key_field="AWS_SECRET_ACCESS_KEY"
            ),
            reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
        ),
    )
    # Permits only the backup resources above to reference the Seaweed cluster.
    ResourceReferenceGrant(
        chart,
        "reference-grant",
        metadata=metadata(NAME, _SEAWEED_NAMESPACE),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind=kind, namespace=NAMESPACE)
                for kind in ("S3Identity", "S3Credentials", "Bucket")
            ],
            to=[ResourceReferenceGrantSpecTo(group=_SEAWEED_GROUP, kind="Seaweed", name=_SEAWEED)],
        ),
    )
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
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="seaweed.seaweedfs.com/v1",
                kind="Bucket",
                name="authentik-db-backups",
                namespace="authentik",
            ),
            KustomizationSpecHealthChecks(
                api_version="seaweed.seaweedfs.com/v1",
                kind="S3Identity",
                name="authentik-db-backups",
                namespace="authentik",
            ),
            KustomizationSpecHealthChecks(
                api_version="seaweed.seaweedfs.com/v1",
                kind="S3Credentials",
                name="authentik-db-backups",
                namespace="authentik",
            ),
            KustomizationSpecHealthChecks(
                api_version="barmancloud.cnpg.io/v1", kind="ObjectStore", name="authentik-db-ovh", namespace="authentik"
            ),
        ],
        depends_on=flux_kustomization_depends_on_many(cnpg, seaweedfs_cluster),
        description="Creates the Authentik CNPG backup schedule and its SeaweedFS storage.",
    )
