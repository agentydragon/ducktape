"""The ImageRepository scanning the GHCR OpenClaw image, and the ImagePolicy selecting its
newest CI tag.

Hand-written beside the generated output, since no cdk8s binding covers either kind:
`ducktape-write-source.yaml`, the authenticated GitRepository image automation pushes
through, and `image-update-automation.yaml`, the `all-images` ImageUpdateAutomation.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_imagepolicy_crds.io.fluxcd.toolkit.image import (
    ImagePolicy,
    ImagePolicySpec,
    ImagePolicySpecFilterTags,
    ImagePolicySpecImageRepositoryRef,
    ImagePolicySpecPolicy,
    ImagePolicySpecPolicyAlphabetical,
    ImagePolicySpecPolicyAlphabeticalOrder,
)
from flux_imagerepository_crds.io.fluxcd.toolkit.image import ImageRepository, ImageRepositorySpec

from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "openclaw"
NAMESPACE = "flux-system"
OUTPUT_DIR = "cluster/k8s/flux-image-automation-ghcr"


def chart(app: App) -> Chart:
    chart = Chart(app, "openclaw-image", disable_resource_name_hashes=True)
    repository = ImageRepository(
        chart,
        "repository",
        metadata=metadata(NAME, NAMESPACE),
        spec=ImageRepositorySpec(image="ghcr.io/agentydragon/openclaw", interval="5m"),
    )
    ImagePolicy(
        chart,
        "policy",
        metadata=metadata(NAME, NAMESPACE),
        spec=ImagePolicySpec(
            image_repository_ref=ImagePolicySpecImageRepositoryRef(name=repository.name),
            # Tags pushed by CI: {branch}-YYYYMMDDHHMMSS-{sha7} — alphabetical order == chronological
            filter_tags=ImagePolicySpecFilterTags(pattern=r"^devel-\d{14}-[0-9a-f]{7}$"),
            policy=ImagePolicySpecPolicy(
                alphabetical=ImagePolicySpecPolicyAlphabetical(order=ImagePolicySpecPolicyAlphabeticalOrder.ASC)
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
