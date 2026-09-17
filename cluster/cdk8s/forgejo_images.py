"""The Forgejo private-registry pull-credentials `ExternalSecret`, shared by
every generated directory that pulls a `git.allegedly.works`-hosted image.
"""

from cdk8s import ApiObjectMetadata
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


def forgejo_images_creds_external_secret(scope: Construct, id: str, *, namespace: str) -> ExternalSecret:
    return ExternalSecret(
        scope,
        id,
        metadata=ApiObjectMetadata(name="forgejo-images-creds", namespace=namespace),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                name="kubernetes-forgejo-images-secret-store",
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
            ),
            target=ExternalSecretSpecTarget(
                name="forgejo-images-creds",
                template=ExternalSecretSpecTargetTemplate(
                    type="kubernetes.io/dockerconfigjson",
                    merge_policy=ExternalSecretSpecTargetTemplateMergePolicy.MERGE,
                ),
            ),
            data_from=[
                ExternalSecretSpecDataFrom(extract=ExternalSecretSpecDataFromExtract(key="forgejo-images-creds"))
            ],
        ),
    )
