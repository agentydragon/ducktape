"""The cdk8s-generated ESO distribution for the temporary LiteLLM key."""

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data

_LITELLM_NAMESPACE = "litellm"
_AGENTPLANE_NAMESPACE = "agentplane-testing"
_KEY_SECRET_NAME = "litellm-key-cheap-experiments"
_READER_SERVICE_ACCOUNT_NAME = "external-creds-reader"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agentplane-testing"


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
            source_namespace=_LITELLM_NAMESPACE,
            source_secret=_KEY_SECRET_NAME,
            consumer_namespace=_AGENTPLANE_NAMESPACE,
        )
        ExternalSecret(
            self,
            "target-external-secret",
            metadata=ApiObjectMetadata(name=_KEY_SECRET_NAME, namespace=_AGENTPLANE_NAMESPACE),
            refresh_interval="1m",
            secret_store_ref=SecretStoreRef.cluster(store),
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
