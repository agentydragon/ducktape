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
re-scans/re-fetches. The token is the forgejo-webhook-token Secret (flux-system), which the
Forgejo webhook URLs also derive their path from.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from flux_receiver_crds.io.fluxcd.toolkit.notification import (
    Receiver,
    ReceiverSpec,
    ReceiverSpecResources,
    ReceiverSpecResourcesKind,
    ReceiverSpecSecretRef,
    ReceiverSpecType,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.haku.namespace import NAMESPACE
from cluster.cdk8s.metadata import metadata

NAME = "haku-ui-image-webhook"
OUTPUT_DIR = "cluster/k8s/haku/ui-image-webhook"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Receiver(
        chart,
        "receiver",
        metadata=metadata("haku-ui-forgejo", "flux-system"),
        spec=ReceiverSpec(
            type=ReceiverSpecType.GENERIC,
            secret_ref=ReceiverSpecSecretRef(name="forgejo-webhook-token"),
            resources=[
                ReceiverSpecResources(
                    api_version="image.toolkit.fluxcd.io/v1",
                    kind=ReceiverSpecResourcesKind.IMAGE_REPOSITORY,
                    name="haku-ui",
                    namespace=NAMESPACE,
                ),
                # haku-anki's registry scan rides the same package webhook.
                ReceiverSpecResources(
                    api_version="image.toolkit.fluxcd.io/v1",
                    kind=ReceiverSpecResourcesKind.IMAGE_REPOSITORY,
                    name="haku-anki",
                    namespace=NAMESPACE,
                ),
                # ImageUpdateAutomation is NOT in notification-controller's
                # defaultFluxAPIVersions map, so the explicit apiVersion is load-bearing (an
                # omitted apiVersion fails to resolve), unlike the other entries where it's
                # convention.
                ReceiverSpecResources(
                    api_version="image.toolkit.fluxcd.io/v1",
                    kind=ReceiverSpecResourcesKind.IMAGE_UPDATE_AUTOMATION,
                    name="haku-ui",
                    namespace=NAMESPACE,
                ),
                # The apply leg: fetch the tag-bump commit (and any haku-state push)
                # immediately; the haku-state-workloads Kustomization then reconciles off the
                # fresh artifact.
                ReceiverSpecResources(
                    api_version="source.toolkit.fluxcd.io/v1",
                    kind=ReceiverSpecResourcesKind.GIT_REPOSITORY,
                    name="haku-state",
                    namespace="flux-system",
                ),
                # ImageUpdateAutomation's own git source. It clones directly when reconciling,
                # but a fresh artifact keeps anything else consuming this source current too.
                ReceiverSpecResources(
                    api_version="source.toolkit.fluxcd.io/v1",
                    kind=ReceiverSpecResourcesKind.GIT_REPOSITORY,
                    name="haku-state-write",
                    namespace=NAMESPACE,
                ),
            ],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def haku_ui_image_webhook(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, haku_state: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            depends_on=[
                # haku-state provisions the forgejo-webhook-token Secret (the Receiver's secretRef)
                # and the Forgejo package webhook that targets this receiver.
                flux_kustomization_depends_on(haku_state)
            ],
        ),
    )
