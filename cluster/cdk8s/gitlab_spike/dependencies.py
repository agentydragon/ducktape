"""GitLab and Praefect databases, Valkey, and dedicated SeaweedFS buckets.

ESO converts the operator-owned credential Secrets into Rails Fog, registry, and
Toolbox s3cmd configuration. It owns only the derived Secrets, never the S3 keys.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import Cpu, k8s
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
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateEngineVersion,
)

from cluster.cdk8s import cnpg, cnpg_backup
from cluster.cdk8s.external_secrets.kubernetes_store import secret_store
from cluster.cdk8s.external_secrets.minted_secret import mint_db_role_secret
from cluster.cdk8s.gitlab_spike import app
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.cnpg.database import Database
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from cluster.cdk8s.seaweedfs import s3
from cluster.cdk8s.valkey import valkey_instance

OUTPUT_DIR = f"{GENERATED_ROOT}/gitlab-spike/dependencies"
_S3_ENDPOINT = "http://seaweedfs-s3.seaweedfs.svc:8333"
_S3_HOST = "seaweedfs-s3.seaweedfs.svc:8333"
_SECRET_STORE = "gitlab-s3-credentials"
_SECRET_READER = "gitlab-s3-secret-reader"


def _databases(chart: Chart) -> None:
    mint_db_role_secret(
        chart,
        "praefect-password",
        name=app.PRAEFECT_PASSWORD.name,
        namespace=app.NAMESPACE,
        role=app.PRAEFECT_DATABASE,
        host=app.GITLAB_DB.rw.host,
        port=app.GITLAB_DB.rw.port.number,
        database=app.PRAEFECT_DATABASE,
    )
    cnpg.cluster(
        chart,
        "gitlab-cluster",
        ref=app.GITLAB_DB,
        profile=cnpg.OVH_PIRAEUS_SSD,
        size="25Gi",
        resize_in_use_volumes=False,
        instances=3,
        image_name="ghcr.io/cloudnative-pg/postgresql:18.6-system-trixie",
        initdb=cnpg.same_owner_initdb(app.GITLAB_DATABASE),
        managed=ClusterSpecManaged(
            roles=[
                ClusterSpecManagedRoles(
                    name=app.PRAEFECT_DATABASE,
                    login=True,
                    password_secret=ClusterSpecManagedRolesPasswordSecret(name=app.PRAEFECT_PASSWORD.name),
                )
            ]
        ),
        wal_archive=True,
    )
    cnpg_backup.CnpgBackup(chart, "gitlab-backup", ref=app.GITLAB_DB, retention="30d")
    Database(
        chart,
        "praefect-database",
        metadata=ApiObjectMetadata(name="praefect", namespace=app.NAMESPACE),
        cluster=DatabaseSpecCluster(name=app.GITLAB_DB.name),
        name=app.PRAEFECT_DATABASE,
        owner=app.PRAEFECT_DATABASE,
        database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.RETAIN,
    )
    # Adopt the bootstrap-created database so CNPG installs and keeps these extensions.
    # Retain prevents deleting the Database CR from deleting GitLab's data.
    Database(
        chart,
        "gitlab-extensions",
        metadata=ApiObjectMetadata(name="gitlab-extensions", namespace=app.NAMESPACE),
        cluster=DatabaseSpecCluster(name=app.GITLAB_DB.name),
        name=app.GITLAB_DATABASE,
        owner=app.GITLAB_DATABASE,
        database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.RETAIN,
        extensions=[
            DatabaseSpecExtensions(name=name, ensure=DatabaseSpecExtensionsEnsure.PRESENT)
            for name in ("amcheck", "pg_trgm", "btree_gist")
        ],
    )


def _valkey(chart: Chart) -> None:
    valkey_instance(
        chart,
        name=app.VALKEY,
        namespace=app.NAMESPACE,
        description="GitLab cache, sessions, and Sidekiq queues on the OVH workers.",
        memory_request=Size.mebibytes(512),
        cpu_limit=Cpu.units(2),
        memory_limit=Size.gibibytes(2),
        max_memory_percent_of_limit=75,
        storage_class="piraeus-hdd-local",
        storage_size=Size.gibibytes(5),
    )


def _secret_store(chart: Chart, *, source_secrets: list[str]) -> None:
    reader = k8s.KubeServiceAccount(
        chart, "s3-reader", metadata=k8s.ObjectMeta(name=_SECRET_READER, namespace=app.NAMESPACE)
    )
    k8s.KubeRole(
        chart,
        "s3-reader-role",
        metadata=k8s.ObjectMeta(name=_SECRET_READER, namespace=app.NAMESPACE),
        rules=[k8s.PolicyRule(api_groups=[""], resources=["secrets"], resource_names=source_secrets, verbs=["get"])],
    )
    k8s.KubeRoleBinding(
        chart,
        "s3-reader-binding",
        metadata=k8s.ObjectMeta(name=_SECRET_READER, namespace=app.NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_SECRET_READER),
        subjects=[k8s.Subject(kind="ServiceAccount", name=_SECRET_READER, namespace=app.NAMESPACE)],
    )
    secret_store(
        chart, "s3-secret-store", metadata=ApiObjectMetadata(name=_SECRET_STORE, namespace=app.NAMESPACE), reader=reader
    )


def _object_storage(chart: Chart) -> None:
    buckets: list[tuple[str, str, s3.PrivateBucket]] = []
    for feature, name in [*app.OBJECT_BUCKETS.items(), ("registry", app.REGISTRY_BUCKET)]:
        bucket = s3.PrivateBucket(
            chart,
            f"{feature}-bucket",
            name=name,
            tenant=app.NAMESPACE,
            adopt_existing=True,
            description=f"GitLab spike {feature} objects; retained when its Kubernetes owner is removed.",
            additional_read_write_identities=[app.BACKUP_BUCKET],
        )
        buckets.append((feature, name, bucket))
    backup_bucket = s3.PrivateBucket(
        chart,
        "backup-bucket",
        name=app.BACKUP_BUCKET,
        tenant=app.NAMESPACE,
        adopt_existing=True,
        description="Full GitLab backup archives; retained when its Kubernetes owner is removed.",
    )
    s3.PrivateBucket(
        chart,
        "backup-tmp-bucket",
        name=app.BACKUP_TMP_BUCKET,
        tenant=app.NAMESPACE,
        adopt_existing=True,
        description="Temporary GitLab restore objects; retained when its Kubernetes owner is removed.",
        additional_read_write_identities=[app.BACKUP_BUCKET],
    )
    _secret_store(
        chart,
        source_secrets=[
            *(bucket.access_key.secret.name for _, _, bucket in buckets),
            backup_bucket.access_key.secret.name,
        ],
    )
    for feature, name, bucket in buckets:
        if feature == "registry":
            key = "config"
            contents = (
                "s3_v2:\n"
                "  accesskey: '{{ .access_key }}'\n"
                "  secretkey: '{{ .secret_key }}'\n"
                "  region: us-east-1\n"
                f"  regionendpoint: {_S3_ENDPOINT}\n"
                f"  bucket: {name}\n"
                "  pathstyle: true\n"
                "  checksum_disabled: true\n"
                "redirect:\n"
                "  disable: true\n"
            )
        else:
            key = "connection"
            contents = (
                "provider: AWS\n"
                "region: us-east-1\n"
                "aws_access_key_id: '{{ .access_key }}'\n"
                "aws_secret_access_key: '{{ .secret_key }}'\n"
                f"endpoint: {_S3_ENDPOINT}\n"
                "path_style: true\n"
                "aws_signature_version: 4\n"
            )
        ExternalSecret(
            chart,
            f"{feature}-connection",
            metadata=ApiObjectMetadata(name=f"{name}-connection", namespace=app.NAMESPACE),
            refresh_interval="1h",
            secret_store_ref=SecretStoreRef.namespaced(_SECRET_STORE),
            data=[
                remote_data(bucket.access_key.secret.name, bucket.access_key.key, secret_key="access_key"),
                remote_data(bucket.secret_key.secret.name, bucket.secret_key.key, secret_key="secret_key"),
            ],
            creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
            template=ExternalSecretSpecTargetTemplate(
                engine_version=ExternalSecretSpecTargetTemplateEngineVersion.V2, data={key: contents}
            ),
        )
    # Toolbox uses s3cmd for backup-utility. Its single identity can read and write
    # every GitLab data bucket, including registry and the restore staging bucket.
    ExternalSecret(
        chart,
        "backup-s3cfg",
        metadata=ApiObjectMetadata(name=app.BACKUP_S3CFG_SECRET, namespace=app.NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.namespaced(_SECRET_STORE),
        data=[
            remote_data(backup_bucket.access_key.secret.name, backup_bucket.access_key.key, secret_key="access_key"),
            remote_data(backup_bucket.secret_key.secret.name, backup_bucket.secret_key.key, secret_key="secret_key"),
        ],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(
            engine_version=ExternalSecretSpecTargetTemplateEngineVersion.V2,
            data={
                "config": (
                    "[default]\n"
                    "access_key = {{ .access_key }}\n"
                    "secret_key = {{ .secret_key }}\n"
                    "bucket_location = us-east-1\n"
                    f"host_base = {_S3_HOST}\n"
                    f"host_bucket = {_S3_HOST}\n"
                    "use_https = False\n"
                    "signature_v2 = False\n"
                    "multipart_chunk_size_mb = 128\n"
                    "preserve_attrs = False\n"
                )
            },
        ),
    )


def chart(app_instance: App) -> Chart:
    chart = Chart(app_instance, f"{app.NAME}-dependencies", disable_resource_name_hashes=True)
    _databases(chart)
    _valkey(chart)
    _object_storage(chart)
    return chart
