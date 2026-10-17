"""Flux image automation for this repository: the `all-images` ImageUpdateAutomation and the
authenticated GitRepository it pushes through.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart

from cluster.cdk8s import ducktape_flux
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.image_automation import ImageUpdatePush, newest_ci_tag_policy
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.flux.image_repository import ImageRepository

OUTPUT_DIR = f"{GENERATED_ROOT}/flux-image-automation-ghcr"
AUTOMATION_NAME = "all-images"
NEBULA_ROUTEROS_IMAGE = "ghcr.io/agentydragon/nebula-routeros"


def automation_chart(app: App) -> Chart:
    chart = Chart(app, "image-update-automation", disable_resource_name_hashes=True)
    ImageUpdatePush(
        chart,
        "image-update-push",
        name=AUTOMATION_NAME,
        source_name="ducktape-write",
        source_description="Authenticated ducktape checkout for image automation pushes. The root flux-system "
        "GitRepository stays anonymous so Terraform can cold-bootstrap Flux before this SOPS-managed GitHub "
        "App Secret exists.",
        url=ducktape_flux.REPOSITORY_URL,
        branch=ducktape_flux.BRANCH,
        path=f"./{HAND_WRITTEN_ROOT}",
        sparse_checkout=[f"{HAND_WRITTEN_ROOT}/"],
        # The automation reads only this source's URL, branch and credentials and clones on its own;
        # nothing reads the fetched artifact, so frequent polling just re-downloads the repo.
        source_interval="24h",
        # Same ~519 MB clone as every other ducktape source, and `sparse_checkout` above does not
        # shrink it. See ducktape_flux.CLONE_TIMEOUT.
        source_timeout=ducktape_flux.CLONE_TIMEOUT,
    )
    newest_ci_tag_policy(
        chart,
        ImageRepository(
            chart,
            "nebula-routeros-image-repository",
            metadata=ApiObjectMetadata(name="nebula-routeros", namespace="flux-system"),
            image=NEBULA_ROUTEROS_IMAGE,
            interval="5m",
        ),
    )
    return chart


def flux_image_automation_ghcr(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    name = "flux-image-automation-ghcr"
    return flux_kustomization(chart, name, directory, retry_interval=None, wait=None)
