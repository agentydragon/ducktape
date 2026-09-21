"""Isolated ESO-backed credentials for outbound authentication."""

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
_FORGEJO_STORE = "kubernetes-agentplane-staging-forgejo-secret-store"
_READER = "external-creds-reader"


class EgressCredentials(Construct):
    """Keep the proxy's namespace-wide Secret watch away from application credentials."""

    def __init__(
        self, scope: Construct, id: str, *, namespace: str, proxy_namespace: str, include_forgejo: bool
    ) -> None:
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
        reader = ServiceAccount(self, "reader", metadata=metadata(_READER, namespace))
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

        secrets = [
            ("agentplane-github-pat", "github-agentydragon-agent", "token", "kubernetes-external-creds-secret-store")
        ]
        if include_forgejo:
            secrets.append(("haku-forgejo-git", "haku-forgejo-git", "password", _FORGEJO_STORE))
            source_role = Role(
                self,
                "forgejo-source-role",
                metadata=metadata("agentplane-staging-egress-forgejo-reader", "haku-sandbox"),
                rules=[
                    RolePolicyRule(
                        resources=[Secret.from_secret_name(self, "forgejo-source", "haku-forgejo-git")], verbs=["get"]
                    )
                ],
            )
            RoleBinding(
                self,
                "forgejo-source-binding",
                metadata=metadata("agentplane-staging-egress-forgejo-reader", "haku-sandbox"),
                role=source_role,
            ).add_subjects(reader)
            ClusterSecretStore(
                self,
                "forgejo-store",
                metadata=ApiObjectMetadata(name=_FORGEJO_STORE),
                spec=ClusterSecretStoreSpec(
                    conditions=[ClusterSecretStoreSpecConditions(namespaces=[namespace])],
                    provider=ClusterSecretStoreSpecProvider(
                        kubernetes=ClusterSecretStoreSpecProviderKubernetes(
                            remote_namespace="haku-sandbox",
                            auth=ClusterSecretStoreSpecProviderKubernetesAuth(
                                service_account=ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount(name=_READER)
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
        for target, source, key, store in secrets:
            ExternalSecret(
                self,
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
