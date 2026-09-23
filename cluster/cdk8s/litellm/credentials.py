"""The cdk8s-generated ESO distribution for the temporary LiteLLM key."""

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import Role, RoleBinding, RolePolicyRule, Secret, ServiceAccount
from constructs import Construct
from external_secret_store_crds.io.external_secrets import (
    ClusterSecretStore,
    ClusterSecretStoreSpec,
    ClusterSecretStoreSpecConditions,
    ClusterSecretStoreSpecProvider,
    ClusterSecretStoreSpecProviderKubernetes,
    ClusterSecretStoreSpecProviderKubernetesAuth,
    ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount,
    ClusterSecretStoreSpecProviderKubernetesServer,
    ClusterSecretStoreSpecProviderKubernetesServerCaProvider,
    ClusterSecretStoreSpecProviderKubernetesServerCaProviderType,
)
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s.external_secrets.external_secret import add_external_secret, cluster_secret_store, remote_data
from cluster.cdk8s.metadata import metadata

_LITELLM_NAMESPACE = "litellm"
_AGENTPLANE_NAMESPACE = "agentplane-testing"
_KEY_SECRET_NAME = "litellm-key-cheap-experiments"
_READER_SERVICE_ACCOUNT_NAME = "external-creds-reader"
_SOURCE_READER_ROLE_NAME = "litellm-cheap-experiments-reader"
_SECRET_STORE_NAME = "kubernetes-litellm-cheap-experiments-secret-store"
OUTPUT_DIR = "cluster/k8s/agentplane-testing"


class CheapExperimentsCredentials(Construct):
    """Distributes the Terraform-owned LiteLLM key into Agentplane testing."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)

        reader_service_account = ServiceAccount(
            self, "reader-service-account", metadata=metadata(_READER_SERVICE_ACCOUNT_NAME, _AGENTPLANE_NAMESPACE)
        )
        source_secret = Secret.from_secret_name(self, "source-secret", _KEY_SECRET_NAME)
        Role(
            self,
            "source-reader-role",
            metadata=metadata(_SOURCE_READER_ROLE_NAME, _LITELLM_NAMESPACE),
            rules=[RolePolicyRule(resources=[source_secret], verbs=["get"])],
        )
        RoleBinding(
            self,
            "source-reader-role-binding",
            metadata=metadata(_SOURCE_READER_ROLE_NAME, _LITELLM_NAMESPACE),
            role=Role.from_role_name(self, "source-reader-role-reference", _SOURCE_READER_ROLE_NAME),
        ).add_subjects(reader_service_account)

        ClusterSecretStore(
            self,
            "secret-store",
            metadata=ApiObjectMetadata(name=_SECRET_STORE_NAME),
            spec=ClusterSecretStoreSpec(
                conditions=[ClusterSecretStoreSpecConditions(namespaces=[_AGENTPLANE_NAMESPACE])],
                provider=ClusterSecretStoreSpecProvider(
                    kubernetes=ClusterSecretStoreSpecProviderKubernetes(
                        auth=ClusterSecretStoreSpecProviderKubernetesAuth(
                            service_account=ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount(
                                name=_READER_SERVICE_ACCOUNT_NAME
                            )
                        ),
                        remote_namespace=_LITELLM_NAMESPACE,
                        server=ClusterSecretStoreSpecProviderKubernetesServer(
                            ca_provider=ClusterSecretStoreSpecProviderKubernetesServerCaProvider(
                                name="kube-root-ca.crt",
                                key="ca.crt",
                                namespace="default",
                                type=ClusterSecretStoreSpecProviderKubernetesServerCaProviderType.CONFIG_MAP,
                            )
                        ),
                    )
                ),
            ),
        )

        add_external_secret(
            self,
            "target-external-secret",
            name=_KEY_SECRET_NAME,
            namespace=_AGENTPLANE_NAMESPACE,
            refresh="1m",
            store=cluster_secret_store(_SECRET_STORE_NAME),
            data=[remote_data(_KEY_SECRET_NAME, "api-key")],
            creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
            deletion_policy=ExternalSecretSpecTargetDeletionPolicy.DELETE,
        )


def write_agentplane_testing_manifests(root: Path) -> None:
    credentials_dir = root / OUTPUT_DIR
    credentials_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(credentials_dir))
    chart = Chart(app, "litellm-credentials", disable_resource_name_hashes=True)
    CheapExperimentsCredentials(chart, "cheap-experiments")
    app.synth()
