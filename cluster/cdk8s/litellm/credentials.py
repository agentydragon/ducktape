"""The cdk8s-generated ESO distribution for the temporary LiteLLM key."""

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from cluster.cdk8s.secret_ref import SecretRef

CHEAP_EXPERIMENTS_KEY = SecretRef(namespace="litellm", name="litellm-key-cheap-experiments").key("api-key")
_AGENTPLANE_NAMESPACE = "agentplane-testing"
_READER_SERVICE_ACCOUNT_NAME = "external-creds-reader"


class CheapExperimentsCredentials(Construct):
    """Distributes the Terraform-owned LiteLLM key into Agentplane testing."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)

        reader_service_account = ServiceAccount(
            self,
            "reader-service-account",
            metadata=ApiObjectMetadata(name=_READER_SERVICE_ACCOUNT_NAME, namespace=_AGENTPLANE_NAMESPACE),
        )
        store = single_secret_store(
            self,
            "litellm-cheap-experiments",
            reader=reader_service_account,
            source_namespace=CHEAP_EXPERIMENTS_KEY.secret.namespace,
            source_secret=CHEAP_EXPERIMENTS_KEY.secret.name,
            consumer_namespace=_AGENTPLANE_NAMESPACE,
        )
        ExternalSecret(
            self,
            "target-external-secret",
            metadata=ApiObjectMetadata(name=CHEAP_EXPERIMENTS_KEY.secret.name, namespace=_AGENTPLANE_NAMESPACE),
            refresh_interval="1m",
            secret_store_ref=SecretStoreRef.cluster(store),
            data=[remote_data(CHEAP_EXPERIMENTS_KEY.secret.name, CHEAP_EXPERIMENTS_KEY.key)],
            creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
            deletion_policy=ExternalSecretSpecTargetDeletionPolicy.DELETE,
        )


def agentplane_testing_chart(app: App) -> Chart:
    """The chart Agentplane testing's writer synthesizes beside its environment chart."""
    chart = Chart(app, "litellm-credentials", disable_resource_name_hashes=True)
    CheapExperimentsCredentials(chart, "cheap-experiments")
    return chart
