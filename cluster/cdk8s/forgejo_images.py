"""The ducktape-ci Forgejo registry tenant: the `Terraform` CR provisioning it
(tf/gitops/forgejo-images) and the pull-credentials `ExternalSecret`, shared by every
generated directory that pulls a `git.allegedly.works`-hosted image.
"""

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ISecret, Secret
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

from cluster.cdk8s import terraform
from cluster.cdk8s.generation import write_charts

NAME = "forgejo-images"
OUTPUT_DIR = "cluster/k8s/forgejo-images"
SECRET_NAME = "forgejo-images-creds"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    terraform.gitops_terraform(chart, "terraform", name=NAME, variables={})
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def forgejo_images_creds_external_secret(scope: Construct, id: str, *, namespace: str) -> ExternalSecret:
    return ExternalSecret(
        scope,
        id,
        metadata=ApiObjectMetadata(name=SECRET_NAME, namespace=namespace),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                name="kubernetes-forgejo-images-secret-store",
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
            ),
            target=ExternalSecretSpecTarget(
                name=SECRET_NAME,
                template=ExternalSecretSpecTargetTemplate(
                    type="kubernetes.io/dockerconfigjson",
                    merge_policy=ExternalSecretSpecTargetTemplateMergePolicy.MERGE,
                ),
            ),
            data_from=[ExternalSecretSpecDataFrom(extract=ExternalSecretSpecDataFromExtract(key=SECRET_NAME))],
        ),
    )


def forgejo_images_creds_secret_ref(scope: Construct, id: str) -> ISecret:
    """Reference the `ExternalSecret` a sibling `forgejo_images_creds_external_secret` call created."""
    return Secret.from_secret_name(scope, id, SECRET_NAME)
