"""The Forgejo private-registry pull-credentials `ExternalSecret`, shared by
every generated directory that pulls a `git.allegedly.works`-hosted image.
"""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_33 import ISecret, Secret
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromExtract,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMergePolicy,
)

_SECRET_NAME = "forgejo-images-creds"


def forgejo_images_creds_external_secret(scope: Construct, id: str, *, namespace: str) -> ExternalSecret:
    return ExternalSecret(
        scope,
        id,
        metadata=ApiObjectMetadata(name=_SECRET_NAME, namespace=namespace),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                name="kubernetes-forgejo-images-secret-store",
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
            ),
            target=ExternalSecretSpecTarget(
                name=_SECRET_NAME,
                template=ExternalSecretSpecTargetTemplate(
                    type="kubernetes.io/dockerconfigjson",
                    merge_policy=ExternalSecretSpecTargetTemplateMergePolicy.MERGE,
                ),
            ),
            data_from=[ExternalSecretSpecDataFrom(extract=ExternalSecretSpecDataFromExtract(key=_SECRET_NAME))],
        ),
    )


def forgejo_images_creds_secret_ref(scope: Construct, id: str) -> ISecret:
    """Reference the `ExternalSecret` a sibling `forgejo_images_creds_external_secret` call created."""
    return Secret.from_secret_name(scope, id, _SECRET_NAME)
