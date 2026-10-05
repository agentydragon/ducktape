"""The ducktape-ci Forgejo registry tenant: the `Terraform` CR provisioning it
(tf/gitops/forgejo-images) and the pull-credentials `ExternalSecret`, shared by every
generated directory that pulls a `git.allegedly.works`-hosted image.

The module also creates the webhook that Forgejo calls when CI pushes an image, and the
token Secret in this namespace that authenticates it. The webhook URL's path is
`sha256(token + receiver name + namespace)`, so the module takes the Receiver's identity
from here.

Hand-written beside the generated output: `registry-creds.sops.yaml`, the tenant's
canonical registry credential.
"""

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ISecret, Secret
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMergePolicy,
)
from pydantic import BaseModel, ConfigDict

from cluster.cdk8s import namespaces, terraform
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.flux_webhook.chart import WEBHOOK_HOST
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret, SecretStoreRef

NAME = "forgejo-images"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/forgejo-images"
SECRET_NAME = "forgejo-images-creds"
# The Receiver that a package webhook on the ducktape-ci user triggers, and the token Secret it
# reads. ESO mints the Secret in this namespace and the Terraform module reads it from there.
RECEIVER_NAME = "receiver"
WEBHOOK_TOKEN_SECRET = "webhook-token"


class ForgejoImagesVars(BaseModel):
    """The inputs of tf/gitops/forgejo-images; `forgejo_url` keeps its variables.tf default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    webhook_host: str
    receiver_name: str
    receiver_namespace: str
    webhook_token_secret: str


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAME,
        vpa=Vpa.RECOMMEND,
        annotations={
            "description": (
                "Holds the shared credential for the ducktape-ci Forgejo registry tenant"
                " (CI-pushed in-cluster images) and its provisioning Terraform."
            )
        },
    )
    terraform.gitops_terraform(
        chart,
        "terraform",
        name=NAME,
        variables=ForgejoImagesVars(
            webhook_host=WEBHOOK_HOST,
            receiver_name=RECEIVER_NAME,
            receiver_namespace=NAME,
            webhook_token_secret=WEBHOOK_TOKEN_SECRET,
        ),
    )
    # Flux's own copy, for the image-automation ImageRepositories that scan the registry.
    forgejo_images_creds_external_secret(chart, "flux-system-creds", namespace="flux-system")
    # Minted once: a new token would change the webhook's path.
    mint_bearer_secret(chart, "webhook-token", name=WEBHOOK_TOKEN_SECRET, namespace=NAME, key="token")
    return chart


def forgejo_images(
    chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization, tofu_controller: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator, tofu_controller),
        description=(
            "ducktape-ci Forgejo registry tenant — shared credential (read by "
            "consumers, including flux-system, via per-namespace ExternalSecrets "
            "against kubernetes-forgejo-images-secret-store) + Terraform that "
            "provisions the Forgejo user."
        ),
    )


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
