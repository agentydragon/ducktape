"""Plaid's cross-namespace copies of whole Secrets with a dedicated reader for each source."""

from cdk8s import ApiObjectMetadata, Chart
from cdk8s_plus_34 import ServiceAccount
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret, SecretStoreRef
from cluster.cdk8s.secret_ref import SecretRef


def copy_secret(chart: Chart, name: str, *, source_namespace: str, target: SecretRef) -> None:
    reader = ServiceAccount(
        chart,
        f"{name}-reader",
        metadata=ApiObjectMetadata(name=f"{name}-reader", namespace=target.namespace),
        automount_token=False,
    )
    store = single_secret_store(
        chart, name, reader=reader, source_namespace=source_namespace, source_secret=target.name
    )
    ExternalSecret(
        chart,
        f"{name}-copy",
        metadata=ApiObjectMetadata(name=target.name, namespace=target.namespace),
        refresh_interval="10m",
        secret_store_ref=SecretStoreRef.cluster(store),
        data_from=[DataFrom.from_extract(target.name)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.DELETE,
    )
