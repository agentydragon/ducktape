"""Isolated credentials namespaces for outbound authentication.

`EgressCredentials` renders only the namespace and the egress proxy's read access to it. Each
environment's own module copies in the credentials that environment gets
(`egress_staging_credentials.py`, `egress_testing_credentials.py`).
"""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Namespace, Role, RoleBinding, RolePolicyRule, Secret, ServiceAccount
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

from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.metadata import metadata

STAGING_NAMESPACE = "agentplane-staging-egress-credentials"
TESTING_NAMESPACE = "agentplane-testing-egress-credentials"
# The Secret `egress.py`'s `github-pat` EgressCredential reads, in both environments.
GITHUB_PAT_SECRET = "agentplane-github-pat"
EXTERNAL_CREDS_STORE = "kubernetes-external-creds-secret-store"
# The store authenticates as this ServiceAccount in the consuming namespace (ESO referent auth), so a
# namespace that copies from external-creds needs its own.
EXTERNAL_CREDS_READER = "external-creds-reader"


class EgressCredentials(Construct):
    """Keep the proxy's namespace-wide Secret watch away from application credentials."""

    def __init__(self, scope: Construct, id: str, *, namespace: str, proxy_namespace: str) -> None:
        super().__init__(scope, id)
        Namespace(
            self,
            "namespace",
            metadata=ApiObjectMetadata(
                name=namespace,
                annotations={
                    "description": f"Outbound credentials readable only by the {proxy_namespace} egress proxy."
                },
            ),
        )
        proxy_role = Role(
            self,
            "proxy-role",
            metadata=metadata("agentplane-egress-credentials-reader", namespace),
            rules=[RolePolicyRule(resources=[custom_resource("", "secrets")], verbs=["get", "list", "watch"])],
        )
        RoleBinding(
            self, "proxy-binding", metadata=metadata(f"{proxy_namespace}-egress", namespace), role=proxy_role
        ).add_subjects(
            ServiceAccount.from_service_account_name(self, "proxy", "agentplane-egress", namespace_name=proxy_namespace)
        )


def credential_external_secret(
    scope: Construct, *, namespace: str, target: str, source: str, key: str, store: str
) -> None:
    """ESO copy of one credential into `namespace`, as Secret `target`."""
    ExternalSecret(
        scope,
        target,
        metadata=metadata(target, namespace),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE, name=store
            ),
            data=[
                ExternalSecretSpecData(
                    secret_key=key, remote_ref=ExternalSecretSpecDataRemoteRef(key=source, property=key)
                )
            ],
            target=ExternalSecretSpecTarget(
                name=target,
                creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
            ),
        ),
    )


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
