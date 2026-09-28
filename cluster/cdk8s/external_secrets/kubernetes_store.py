"""ESO stores on the Kubernetes provider, reading Secrets out of this cluster's own API server.

Our policy for every such store: the API server's CA comes from the `kube-root-ca.crt`
ConfigMap kube-controller-manager publishes into every namespace, and a `ClusterSecretStore`
names the namespaces allowed to use it (`conditions`), which the
`require-secret-store-conditions` Kyverno policy enforces at admission.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import k8s
from constructs import Construct
from external_secret_store_crds.io.external_secrets import (
    ClusterSecretStoreSpecConditions,
    ClusterSecretStoreSpecProvider,
    ClusterSecretStoreSpecProviderKubernetes,
    ClusterSecretStoreSpecProviderKubernetesAuth,
    ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount,
    ClusterSecretStoreSpecProviderKubernetesServer,
    ClusterSecretStoreSpecProviderKubernetesServerCaProvider,
    ClusterSecretStoreSpecProviderKubernetesServerCaProviderType,
)
from external_secrets_secretstore_crds.io.external_secrets import (
    SecretStoreSpecProvider,
    SecretStoreSpecProviderKubernetes,
    SecretStoreSpecProviderKubernetesAuth,
    SecretStoreSpecProviderKubernetesAuthServiceAccount,
    SecretStoreSpecProviderKubernetesServer,
    SecretStoreSpecProviderKubernetesServerCaProvider,
    SecretStoreSpecProviderKubernetesServerCaProviderType,
)

from cluster.cdk8s.providers.external_secrets.secret_store import ClusterSecretStore, SecretStore

# ESO's own ServiceAccount, which holds cluster-wide Secret read.
ESO_SERVICE_ACCOUNT = ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount(
    name="external-secrets", namespace="external-secrets-system"
)
_ROOT_CA_CONFIG_MAP = "kube-root-ca.crt"
_ROOT_CA_KEY = "ca.crt"


def cluster_secret_store(
    scope: Construct,
    id: str,
    *,
    metadata: ApiObjectMetadata,
    namespaces: Sequence[str],
    remote_namespace: str,
    service_account: ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount,
) -> ClusterSecretStore:
    """A `ClusterSecretStore` reading the Secrets of `remote_namespace` as `service_account`,
    usable only by `ExternalSecret`s in `namespaces`. A `service_account` without a namespace is
    ESO referent authentication: it resolves in each consuming `ExternalSecret`'s namespace. The
    store has no namespace of its own to read the CA from, so it reads `default`'s."""
    return ClusterSecretStore(
        scope,
        id,
        metadata=metadata,
        conditions=[ClusterSecretStoreSpecConditions(namespaces=list(namespaces))],
        provider=ClusterSecretStoreSpecProvider(
            kubernetes=ClusterSecretStoreSpecProviderKubernetes(
                server=ClusterSecretStoreSpecProviderKubernetesServer(
                    ca_provider=ClusterSecretStoreSpecProviderKubernetesServerCaProvider(
                        type=ClusterSecretStoreSpecProviderKubernetesServerCaProviderType.CONFIG_MAP,
                        name=_ROOT_CA_CONFIG_MAP,
                        key=_ROOT_CA_KEY,
                        namespace="default",
                    )
                ),
                auth=ClusterSecretStoreSpecProviderKubernetesAuth(service_account=service_account),
                remote_namespace=remote_namespace,
            )
        ),
    )


def secret_store(
    scope: Construct, id: str, *, metadata: ApiObjectMetadata, reader: k8s.KubeServiceAccount
) -> SecretStore:
    """A `SecretStore` reading the Secrets of its own namespace as `reader`, a ServiceAccount there."""
    namespace = metadata.namespace
    if namespace is None or reader.metadata.namespace != namespace:
        raise ValueError(f"{metadata.name=}: the reader must be in the store's namespace: {reader.name=}")
    return SecretStore(
        scope,
        id,
        metadata=metadata,
        provider=SecretStoreSpecProvider(
            kubernetes=SecretStoreSpecProviderKubernetes(
                server=SecretStoreSpecProviderKubernetesServer(
                    ca_provider=SecretStoreSpecProviderKubernetesServerCaProvider(
                        type=SecretStoreSpecProviderKubernetesServerCaProviderType.CONFIG_MAP,
                        name=_ROOT_CA_CONFIG_MAP,
                        key=_ROOT_CA_KEY,
                    )
                ),
                auth=SecretStoreSpecProviderKubernetesAuth(
                    service_account=SecretStoreSpecProviderKubernetesAuthServiceAccount(
                        name=reader.name, namespace=namespace
                    )
                ),
                remote_namespace=namespace,
            )
        ),
    )
