"""The Receiver that Forgejo's `package` webhook (container-registry push) and `push` webhook
(git push to haku-state) hit, both provisioned by tf/gitops/haku-state, and the directory's
Flux Kustomization. It force-reconciles every polling leg of the haku-ui deploy chain:
registry scan -> ImageUpdateAutomation tag-bump commit -> GitRepository fetch -> workloads
apply (kustomize-controller reconciles on the new source artifact by itself, so it needs no
entry here). The receiver is `generic` (payload ignored), so either event pokes all listed
resources; the cross-pokes are harmless no-op reconciles.

Operator-owned (NOT in Haku's haku-state): a Receiver lists resources to force-reconcile,
which the notification-controller patches cluster-wide, a cross-namespace primitive we
deliberately keep out of Haku's hands. The image-automation objects it triggers are Haku's
own (haku-sandbox), referenced cross-namespace.

`generic` because Flux has no Forgejo/Gitea receiver type, and Forgejo's HMAC headers
(X-Gitea-Signature / X-Hub-Signature-256) don't match generic-hmac's X-Signature. Security
is therefore the unguessable sha256(token) webhook path; a leaked URL only triggers harmless
re-scans/re-fetches. The token is the forgejo-webhook-token Secret, which tf/gitops/haku-state
mints and derives the Forgejo webhook URLs' path from; this chart copies it into flux-system.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from flux_receiver_crds.io.fluxcd.toolkit.notification import ReceiverSpecSecretRef, ReceiverSpecType

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo import secret_copy
from cluster.cdk8s.haku.namespace import NAMESPACE
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.flux.notification import Receiver, ReceiverResource

NAME = "haku-ui-image-webhook"
OUTPUT_DIR = f"{GENERATED_ROOT}/haku/ui-image-webhook"
_RECEIVER_NAMESPACE = "flux-system"
_TOKEN_SECRET = "forgejo-webhook-token"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    secret_copy.secret_copy(chart, _TOKEN_SECRET, reader=secret_copy.reader(chart, _RECEIVER_NAMESPACE))
    Receiver(
        chart,
        "receiver",
        metadata=ApiObjectMetadata(name="haku-ui-forgejo", namespace=_RECEIVER_NAMESPACE),
        type=ReceiverSpecType.GENERIC,
        secret_ref=ReceiverSpecSecretRef(name=_TOKEN_SECRET),
        resources=[
            ReceiverResource.image_repository("haku-ui", namespace=NAMESPACE),
            # haku-anki's registry scan rides the same package webhook.
            ReceiverResource.image_repository("haku-anki", namespace=NAMESPACE),
            ReceiverResource.image_update_automation("haku-ui", namespace=NAMESPACE),
            # The apply leg: fetch the tag-bump commit (and any haku-state push)
            # immediately; the haku-state-workloads Kustomization then reconciles off the
            # fresh artifact.
            ReceiverResource.git_repository("haku-state", namespace="flux-system"),
            # ImageUpdateAutomation's own git source. It clones directly when reconciling,
            # but a fresh artifact keeps anything else consuming this source current too.
            ReceiverResource.git_repository("haku-state-write", namespace=NAMESPACE),
        ],
    )
    return chart


def haku_ui_image_webhook(
    chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        retry_interval=None,
        wait=None,
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator),
    )
