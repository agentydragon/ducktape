"""Isolated ESO-backed credentials for outbound authentication."""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Namespace, Role, RoleBinding, RolePolicyRule, ServiceAccount
from constructs import Construct
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

        credential_external_secret(
            self,
            namespace=namespace,
            target="agentplane-github-pat",
            source="github-agentydragon-agent",
            key="token",
            store="kubernetes-external-creds-secret-store",
        )


def credential_external_secret(
    scope: Construct, *, namespace: str, target: str, source: str, key: str, store: str
) -> None:
    """ESO copy of one credential into an egress-credentials namespace, as Secret `target`."""
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
