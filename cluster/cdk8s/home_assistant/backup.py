"""Home Assistant's backup bucket: the SeaweedFS identity, tenant-local Bucket and S3Credentials,
the grant letting them reference the SeaweedFS cluster, and the ESO wiring that composes the
Restic repository Secret.

Hand-written beside the generated output: `credentials-secret.sops.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
)
from external_secrets_secretstore_crds.io.external_secrets import (
    SecretStore,
    SecretStoreSpec,
    SecretStoreSpecProvider,
    SecretStoreSpecProviderKubernetes,
    SecretStoreSpecProviderKubernetesAuth,
    SecretStoreSpecProviderKubernetesAuthServiceAccount,
    SecretStoreSpecProviderKubernetesServer,
    SecretStoreSpecProviderKubernetesServerCaProvider,
    SecretStoreSpecProviderKubernetesServerCaProviderType,
)
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

from cluster.cdk8s.external_secrets.external_secret import add_external_secret, remote_data, secret_store
from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

_OUTPUT_DIR = "cluster/k8s/home-assistant/backup"
_NAME = "home-assistant-backups"
_NAMESPACE = "home-assistant"
_SEAWEEDFS = "seaweedfs"
_SEAWEED_GROUP = "seaweed.seaweedfs.com"
# SOPS-encrypted in credentials-secret.sops.yaml.
_RESTIC_SECRET = "home-assistant-config-restic"
# Written by the S3Credentials.
_S3_CREDENTIALS_SECRET = "home-assistant-seaweedfs-credentials"
_SECRET_READER = "home-assistant-backup-secret-reader"
_SECRET_STORE = "home-assistant-backup-secrets"
_REPOSITORY_SECRET = "home-assistant-config-restic-tenant"


def _repository(scope: Construct) -> None:
    """Compose the operator-owned S3 credentials with the existing SOPS-managed Restic fields
    without changing the old rollback Secret during the handoff."""
    k8s.KubeServiceAccount(
        scope, "secret-reader-sa", metadata=k8s.ObjectMeta(name=_SECRET_READER, namespace=_NAMESPACE)
    )
    k8s.KubeRole(
        scope,
        "secret-reader-role",
        metadata=k8s.ObjectMeta(name=_SECRET_READER, namespace=_NAMESPACE),
        rules=[
            k8s.PolicyRule(
                api_groups=[""],
                resources=["secrets"],
                resource_names=[_RESTIC_SECRET, _S3_CREDENTIALS_SECRET],
                verbs=["get"],
            )
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "secret-reader-rolebinding",
        metadata=k8s.ObjectMeta(name=_SECRET_READER, namespace=_NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_SECRET_READER),
        subjects=[k8s.Subject(kind="ServiceAccount", name=_SECRET_READER, namespace=_NAMESPACE)],
    )
    SecretStore(
        scope,
        "secret-store",
        metadata=metadata(_SECRET_STORE, _NAMESPACE),
        spec=SecretStoreSpec(
            provider=SecretStoreSpecProvider(
                kubernetes=SecretStoreSpecProviderKubernetes(
                    server=SecretStoreSpecProviderKubernetesServer(
                        ca_provider=SecretStoreSpecProviderKubernetesServerCaProvider(
                            type=SecretStoreSpecProviderKubernetesServerCaProviderType.CONFIG_MAP,
                            name="kube-root-ca.crt",
                            key="ca.crt",
                        )
                    ),
                    auth=SecretStoreSpecProviderKubernetesAuth(
                        service_account=SecretStoreSpecProviderKubernetesAuthServiceAccount(
                            name=_SECRET_READER, namespace=_NAMESPACE
                        )
                    ),
                    remote_namespace=_NAMESPACE,
                )
            )
        ),
    )
    add_external_secret(
        scope,
        "repository",
        name=_REPOSITORY_SECRET,
        namespace=_NAMESPACE,
        refresh="1h",
        store=secret_store(_SECRET_STORE),
        data=[
            remote_data(secret, key)
            for key, secret in (
                ("RESTIC_REPOSITORY", _RESTIC_SECRET),
                ("RESTIC_PASSWORD", _RESTIC_SECRET),
                ("AWS_DEFAULT_REGION", _RESTIC_SECRET),
                ("AWS_ACCESS_KEY_ID", _S3_CREDENTIALS_SECRET),
                ("AWS_SECRET_ACCESS_KEY", _S3_CREDENTIALS_SECRET),
            )
        ],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(type="Opaque"),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    S3Identity(
        chart,
        "s3-identity",
        metadata=metadata(_NAME, _SEAWEEDFS),
        spec=S3IdentitySpec(
            seaweed_ref=S3IdentitySpecSeaweedRef(name=_SEAWEEDFS), reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN
        ),
    )
    Bucket(
        chart,
        "bucket",
        metadata=metadata(
            _NAME, _NAMESPACE, annotations={"description": "Home Assistant's tenant-local SeaweedFS backup bucket."}
        ),
        spec=BucketSpec(
            name=_NAME,
            adopt_existing=True,
            cluster_ref=BucketSpecClusterRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            reclaim_policy=BucketSpecReclaimPolicy.RETAIN,
            access=[
                BucketSpecAccess(
                    user=_NAME,
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
    S3Credentials(
        chart,
        "s3-credentials",
        metadata=metadata(
            _NAME,
            _NAMESPACE,
            annotations={"description": "Home Assistant's tenant-local SeaweedFS backup credentials."},
        ),
        spec=S3CredentialsSpec(
            seaweed_ref=S3CredentialsSpecSeaweedRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            identity_ref=S3CredentialsSpecIdentityRef(name=_NAME),
            secret_ref=S3CredentialsSpecSecretRef(
                name=_S3_CREDENTIALS_SECRET,
                access_key_field="AWS_ACCESS_KEY_ID",
                secret_key_field="AWS_SECRET_ACCESS_KEY",
            ),
            reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
        ),
    )
    # Permit only Home Assistant's tenant-local Bucket and S3Credentials to reference the
    # SeaweedFS cluster in its namespace.
    ResourceReferenceGrant(
        chart,
        "reference-grant",
        metadata=metadata(_NAME, _SEAWEEDFS),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="Bucket", namespace=_NAMESPACE),
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="S3Credentials", namespace=_NAMESPACE),
            ],
            to=[ResourceReferenceGrantSpecTo(group=_SEAWEED_GROUP, kind="Seaweed", name=_SEAWEEDFS)],
        ),
    )
    _repository(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart)
    write_yaml(
        root / _OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{_NAME}.k8s.yaml", "credentials-secret.sops.yaml"]),
    )
