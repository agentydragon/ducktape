"""Flux image automation for this repository: the `all-images` ImageUpdateAutomation, the
authenticated GitRepository it pushes through, the ImageRepository scanning the GHCR
OpenClaw image, and the ImagePolicy selecting its newest CI tag.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart

from cluster.cdk8s import ducktape_flux
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.image_automation import ImageUpdatePush, newest_ci_tag_policy
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.flux.image_repository import ImageRepository

_OPENCLAW = "openclaw"
NAMESPACE = "flux-system"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/flux-image-automation-ghcr"


def automation_chart(app: App) -> Chart:
    chart = Chart(app, "image-update-automation", disable_resource_name_hashes=True)
    ImageUpdatePush(
        chart,
        "image-update-push",
        name="all-images",
        source_name="ducktape-write",
        source_description="Authenticated ducktape checkout for image automation pushes. The root flux-system "
        "GitRepository stays anonymous so Terraform can cold-bootstrap Flux before this SOPS-managed GitHub "
        "App Secret exists.",
        url=ducktape_flux.REPOSITORY_URL,
        branch=ducktape_flux.BRANCH,
        path=f"./{HAND_WRITTEN_ROOT}",
        sparse_checkout=[f"{HAND_WRITTEN_ROOT}/"],
    )
    return chart


def openclaw_chart(app: App) -> Chart:
    chart = Chart(app, "openclaw-image", disable_resource_name_hashes=True)
    newest_ci_tag_policy(
        chart,
        ImageRepository(
            chart,
            "repository",
            metadata=ApiObjectMetadata(name=_OPENCLAW, namespace=NAMESPACE),
            image="ghcr.io/agentydragon/openclaw",
            interval="5m",
        ),
    )
    return chart


def flux_image_automation_ghcr(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    name = "flux-image-automation-ghcr"
    return flux_kustomization(chart, name, directory, retry_interval=None, wait=None)
