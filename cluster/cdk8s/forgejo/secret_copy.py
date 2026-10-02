"""Consumer copies of the Secrets that the tf/gitops Forgejo modules (`gitops_modules`) write.

Those modules write their Secrets into the forgejo namespace and nowhere else, so their Flux
nodes wait for no consumer's namespace. Each consumer namespace copies the Secrets it needs
through `single_secret_store`: a `get` grant on that one Secret, used by the consumer's own
reader ServiceAccount.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMetadata,
)

from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.forgejo.namespace import NAMESPACE
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret, SecretStoreRef
from cluster.cdk8s.reflector import mirror_annotations


def reader(scope: Construct, namespace: str) -> ServiceAccount:
    """The identity every copy into `namespace` reads its Secret as; one per namespace."""
    return ServiceAccount(
        scope,
        f"{namespace}-forgejo-secret-reader",
        metadata=ApiObjectMetadata(name="forgejo-secret-reader", namespace=namespace),
    )


def secret_copy(
    scope: Construct,
    name: str,
    *,
    reader: ServiceAccount,
    secret_type: str | None = None,
    mirror_namespaces: Sequence[str] = (),
) -> ExternalSecret:
    """Copies the forgejo namespace's Secret `name` into `reader`'s namespace, under the same name.

    `Owner` takes over a same-named Secret that has no controller: ESO sets itself as the
    controller and replaces the data. `mirror_namespaces` makes Reflector mirror the copy
    onward.
    """
    namespace = reader.metadata.namespace
    if namespace is None:
        raise ValueError(f"{reader.name=} has no namespace to copy into")
    store_name = name if name.startswith(f"{namespace}-") else f"{namespace}-{name}"
    store = single_secret_store(
        scope, store_name, reader=reader, source_namespace=NAMESPACE, source_secret=name, consumer_namespace=namespace
    )
    return ExternalSecret(
        scope,
        store_name,
        metadata=ApiObjectMetadata(name=name, namespace=namespace),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster(store),
        data_from=[DataFrom.from_extract(name)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(
            type=secret_type,
            metadata=ExternalSecretSpecTargetTemplateMetadata(annotations=mirror_annotations(mirror_namespaces))
            if mirror_namespaces
            else None,
        )
        if secret_type or mirror_namespaces
        else None,
    )
