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

from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from cluster.cdk8s.seaweedfs import s3

_OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/home-assistant/backup"
_NAME = "home-assistant-backups"
_NAMESPACE = "home-assistant"
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
    ExternalSecret(
        scope,
        "repository",
        name=_REPOSITORY_SECRET,
        namespace=_NAMESPACE,
        refresh="1h",
        store=SecretStoreRef.namespaced(_SECRET_STORE),
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
    identity = s3.Identity(chart, "identity", name=_NAME)
    bucket = s3.Bucket(
        chart,
        "bucket",
        name=_NAME,
        namespace=_NAMESPACE,
        adopt_existing=True,
        description="Home Assistant's tenant-local SeaweedFS backup bucket.",
    )
    bucket.grant_read_write(identity)
    identity.credentials(
        namespace=_NAMESPACE,
        secret=_S3_CREDENTIALS_SECRET,
        key_fields=s3.AWS_ENV_KEY_FIELDS,
        description="Home Assistant's tenant-local SeaweedFS backup credentials.",
    )
    _repository(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart)
    write_yaml(
        root / _OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{_NAME}.k8s.yaml", "credentials-secret.sops.yaml"]),
    )
