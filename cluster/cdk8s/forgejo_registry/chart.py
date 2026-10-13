"""The ducktape-ci Forgejo registry tenant: the `Terraform` CR provisioning it
(tf/gitops/forgejo-images) and the Receiver that Forgejo's `package` webhook hits when
CI pushes an image. The pull-credentials `ExternalSecret` every generated directory shares
is in `pull_credentials`.

The module creates that webhook on the ducktape-ci user (an owner-level hook fires for
packages linked to no repository) and reads the token Secret that ESO mints beside the Receiver. The
webhook URL's path is `sha256(token + receiver name + namespace)`, so the module takes the
Receiver's identity from here. The Receiver is `generic` because Flux has no Forgejo
receiver type, and it checks no signature: the unguessable path is the secret, and a leaked
URL only triggers harmless re-scans. Flux's own namespace holds the ImageRepositories it
scans, so it names them explicitly.

Hand-written beside the generated output: `registry-creds.sops.yaml`, the tenant's
canonical registry credential.
"""

from cdk8s import ApiObjectMetadata, App, Chart
from flux_receiver_crds.io.fluxcd.toolkit.notification import ReceiverSpecSecretRef, ReceiverSpecType
from pydantic import BaseModel, ConfigDict
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import namespaces, terraform
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.flux_image_automation_ghcr.image_automation import AUTOMATION_NAME
from cluster.cdk8s.flux_webhook.chart import WEBHOOK_HOST
from cluster.cdk8s.forgejo_registry import image_automation as forgejo_image_automation
from cluster.cdk8s.forgejo_registry.pull_credentials import forgejo_images_creds_external_secret
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.flux.notification import Receiver, ReceiverResource

NAME = "forgejo-images"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/forgejo-images"
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


def chart(app: App, module: ArtifactGeneratorSpecArtifacts) -> Chart:
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
        module=module,
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
    Receiver(
        chart,
        "receiver",
        metadata=ApiObjectMetadata(name=RECEIVER_NAME, namespace=NAME),
        type=ReceiverSpecType.GENERIC,
        secret_ref=ReceiverSpecSecretRef(name=WEBHOOK_TOKEN_SECRET),
        # A new container package version is a pushed image; a deleted one is not.
        resource_filter="req.action == 'created' && req.package.type == 'container'",
        resources=[
            # Each push scans only its own image: the filter reads the package name off the payload.
            *(
                ReceiverResource.image_repository(
                    image,
                    namespace=forgejo_image_automation.NAMESPACE,
                    filter=f"req.package.name == '{forgejo_image_automation.package_name(image)}'",
                )
                for image in forgejo_image_automation.IMAGES
            ),
            ReceiverResource.image_update_automation(AUTOMATION_NAME, namespace=forgejo_image_automation.NAMESPACE),
        ],
    )
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
