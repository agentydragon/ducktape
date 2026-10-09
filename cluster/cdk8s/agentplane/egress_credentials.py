"""Isolated credentials namespaces for outbound authentication.

`EgressCredentials` renders only the namespace and the egress proxy's read access to it. Each
environment's own module copies in the credentials that environment gets
(`egress_staging_credentials.py`, `egress_testing_credentials.py`).
"""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Role, RoleBinding, RolePolicyRule, ServiceAccount
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s import namespaces
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.litellm.credentials import CHEAP_EXPERIMENTS_KEY
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.ollama.app import DIRECT_TOKEN
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data

STAGING_CREDENTIALS_NAMESPACE = "agentplane-staging-egress-credentials"
TESTING_CREDENTIALS_NAMESPACE = "agentplane-testing-egress-credentials"
# The Secret `egress.py`'s `github-pat` EgressCredential reads, in both environments.
GITHUB_PAT_SECRET = "agentplane-github-pat"
BUILDBUDDY_API_KEY_SECRET = "buildbuddy-api-key"
EXTERNAL_CREDS_STORE = "kubernetes-external-creds-secret-store"
# The store authenticates as this ServiceAccount in the consuming namespace (ESO referent auth), so a
# namespace that copies from external-creds needs its own.
EXTERNAL_CREDS_READER = "external-creds-reader"


class EgressCredentials(Construct):
    """Keep the proxy's namespace-wide Secret watch away from application credentials."""

    def __init__(self, scope: Construct, id: str, *, namespace: str, proxy_namespace: str) -> None:
        super().__init__(scope, id)
        namespaces.namespace(
            self,
            "namespace",
            name=namespace,
            vpa=Vpa.RECOMMEND,
            annotations={"description": f"Outbound credentials readable only by the {proxy_namespace} egress proxy."},
        )
        proxy_role = Role(
            self,
            "proxy-role",
            metadata=ApiObjectMetadata(name="agentplane-egress-credentials-reader", namespace=namespace),
            rules=[RolePolicyRule(resources=[custom_resource("", "secrets")], verbs=["get", "list", "watch"])],
        )
        RoleBinding(
            self,
            "proxy-binding",
            metadata=ApiObjectMetadata(name=f"{proxy_namespace}-egress", namespace=namespace),
            role=proxy_role,
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
        metadata=ApiObjectMetadata(name=target, namespace=namespace),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster(store),
        data=[remote_data(source, key)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )


def inference_credentials(scope: Construct, *, reader: ServiceAccount, credentials_namespace: str) -> None:
    """Copy existing inference keys for proxy substitution, never into a sandbox."""
    for source in (DIRECT_TOKEN, CHEAP_EXPERIMENTS_KEY):
        credential_external_secret(
            scope,
            namespace=credentials_namespace,
            target=source.secret.name,
            source=source.secret.name,
            key=source.key,
            store=single_secret_store(
                scope,
                f"{credentials_namespace}-{source.secret.name}",
                reader=reader,
                source_namespace=source.secret.namespace,
                source_secret=source.secret.name,
                consumer_namespace=credentials_namespace,
            ),
        )
