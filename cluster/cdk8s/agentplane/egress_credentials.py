"""Isolated credentials namespaces for outbound authentication.

`EgressCredentials` renders only the namespace and the egress proxy's read access to it. Each
environment's own module copies in the credentials that environment gets
(`egress_staging_credentials.py`, `egress_testing_credentials.py`).
"""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Namespace, Role, RoleBinding, RolePolicyRule, ServiceAccount
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.external_secrets.external_secret import (
    add_external_secret,
    cluster_secret_store,
    remote_data,
)

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
    add_external_secret(
        scope,
        target,
        name=target,
        namespace=namespace,
        refresh="1h",
        store=cluster_secret_store(store),
        data=[remote_data(source, key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )
