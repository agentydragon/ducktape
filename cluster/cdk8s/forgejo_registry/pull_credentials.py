"""The pull-credentials `ExternalSecret` shared by every generated directory that pulls a
`git.allegedly.works`-hosted image."""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ISecret, Secret
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMergePolicy,
)

from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret, SecretStoreRef

SECRET_NAME = "forgejo-images-creds"


def forgejo_images_creds_external_secret(scope: Construct, id: str, *, namespace: str) -> ExternalSecret:
    return ExternalSecret(
        scope,
        id,
        metadata=ApiObjectMetadata(name=SECRET_NAME, namespace=namespace),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.cluster("kubernetes-forgejo-images-secret-store"),
        data_from=[DataFrom.from_extract(SECRET_NAME)],
        template=ExternalSecretSpecTargetTemplate(
            type="kubernetes.io/dockerconfigjson", merge_policy=ExternalSecretSpecTargetTemplateMergePolicy.MERGE
        ),
    )


def forgejo_images_creds_secret_ref(scope: Construct, id: str) -> ISecret:
    """Reference the `ExternalSecret` a sibling `forgejo_images_creds_external_secret` call created."""
    return Secret.from_secret_name(scope, id, SECRET_NAME)
