"""The cdk8s-generated ESO distribution for the temporary LiteLLM key."""

from cdk8s import ApiObjectMetadata
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
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataRemoteRef,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s.metadata import metadata

_LITELLM_NAMESPACE = "litellm"
_AGENTPLANE_NAMESPACE = "agentplane-testing"
_KEY_SECRET_NAME = "litellm-key-cheap-experiments"
_READER_SERVICE_ACCOUNT_NAME = "external-creds-reader"
_SOURCE_READER_ROLE_NAME = "litellm-cheap-experiments-reader"
_SECRET_STORE_NAME = "kubernetes-litellm-cheap-experiments-secret-store"


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

        ExternalSecret(
            self,
            "target-external-secret",
            metadata=metadata(_KEY_SECRET_NAME, _AGENTPLANE_NAMESPACE),
            spec=ExternalSecretSpec(
                data=[
                    ExternalSecretSpecData(
                        secret_key="api-key",
                        remote_ref=ExternalSecretSpecDataRemoteRef(key=_KEY_SECRET_NAME, property="api-key"),
                    )
                ],
                refresh_interval="1m",
                secret_store_ref=ExternalSecretSpecSecretStoreRef(
                    name=_SECRET_STORE_NAME, kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE
                ),
                target=ExternalSecretSpecTarget(
                    creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                    deletion_policy=ExternalSecretSpecTargetDeletionPolicy.DELETE,
                    name=_KEY_SECRET_NAME,
                ),
            ),
        )
