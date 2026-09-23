"""A ClusterSecretStore that lets one namespace read one Secret out of another."""

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

from cluster.cdk8s.metadata import metadata


def single_secret_store(
    scope: Construct,
    name: str,
    *,
    reader: ServiceAccount,
    source_namespace: str,
    source_secret: str,
    consumer_namespace: str,
) -> str:
    """A ClusterSecretStore through which `consumer_namespace` reads `source_secret` out of
    `source_namespace`, returning the store's name. It authenticates as the consumer namespace's
    own `reader` (ESO referent auth), which a Role in `source_namespace` lets get that one Secret
    and nothing else -- unlike a store on ESO's own ServiceAccount, which reads the whole
    `source_namespace`."""
    reader_role = f"{name}-reader"
    source_role = Role(
        scope,
        f"{name}-source-role",
        metadata=metadata(reader_role, source_namespace),
        rules=[
            RolePolicyRule(resources=[Secret.from_secret_name(scope, f"{name}-source", source_secret)], verbs=["get"])
        ],
    )
    RoleBinding(
        scope, f"{name}-source-binding", metadata=metadata(reader_role, source_namespace), role=source_role
    ).add_subjects(reader)
    store = f"kubernetes-{name}-secret-store"
    ClusterSecretStore(
        scope,
        f"{name}-store",
        metadata=ApiObjectMetadata(name=store),
        spec=ClusterSecretStoreSpec(
            conditions=[ClusterSecretStoreSpecConditions(namespaces=[consumer_namespace])],
            provider=ClusterSecretStoreSpecProvider(
                kubernetes=ClusterSecretStoreSpecProviderKubernetes(
                    remote_namespace=source_namespace,
                    auth=ClusterSecretStoreSpecProviderKubernetesAuth(
                        service_account=ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount(name=reader.name)
                    ),
                    server=ClusterSecretStoreSpecProviderKubernetesServer(
                        ca_provider=ClusterSecretStoreSpecProviderKubernetesServerCaProvider(
                            type=ClusterSecretStoreSpecProviderKubernetesServerCaProviderType.CONFIG_MAP,
                            name="kube-root-ca.crt",
                            key="ca.crt",
                            namespace="default",
                        )
                    ),
                )
            ),
        ),
    )
    return store
