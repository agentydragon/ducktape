"""Flux image automation for this repository: the `all-images` ImageUpdateAutomation, the
authenticated GitRepository it pushes through, the ImageRepository scanning the GHCR
OpenClaw image, and the ImagePolicy selecting its newest CI tag.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from flux_gitrepository_crds.io.fluxcd.toolkit.source import (
    GitRepositorySpecProvider,
    GitRepositorySpecRef,
    GitRepositorySpecSecretRef,
)
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
from flux_imageupdateautomation_crds.io.fluxcd.toolkit.image import (
    ImageUpdateAutomation,
    ImageUpdateAutomationSpec,
    ImageUpdateAutomationSpecGit,
    ImageUpdateAutomationSpecGitCheckout,
    ImageUpdateAutomationSpecGitCheckoutRef,
    ImageUpdateAutomationSpecGitCommit,
    ImageUpdateAutomationSpecGitCommitAuthor,
    ImageUpdateAutomationSpecGitPush,
    ImageUpdateAutomationSpecSourceRef,
    ImageUpdateAutomationSpecSourceRefKind,
    ImageUpdateAutomationSpecUpdate,
    ImageUpdateAutomationSpecUpdateStrategy,
)

from cluster.cdk8s import ducktape_flux
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.flux.git_repository import GitRepository

_OPENCLAW = "openclaw"
NAMESPACE = "flux-system"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/flux-image-automation-ghcr"


def automation_chart(app: App) -> Chart:
    chart = Chart(app, "image-update-automation", disable_resource_name_hashes=True)
    source = GitRepository(
        chart,
        "source",
        metadata=ApiObjectMetadata(
            name="ducktape-write",
            namespace=NAMESPACE,
            annotations={
                "description": "Authenticated ducktape checkout for image automation pushes. The root flux-system "
                "GitRepository stays anonymous so Terraform can cold-bootstrap Flux before this SOPS-managed GitHub "
                "App Secret exists."
            },
        ),
        interval="1m",
        provider=GitRepositorySpecProvider.GITHUB,
        ref=GitRepositorySpecRef(branch=ducktape_flux.BRANCH),
        secret_ref=GitRepositorySpecSecretRef(name="ducktape-automation-github-app"),
        sparse_checkout=[f"{HAND_WRITTEN_ROOT}/"],
        url=ducktape_flux.REPOSITORY_URL,
    )
    ImageUpdateAutomation(
        chart,
        "automation",
        metadata=ApiObjectMetadata(name="all-images", namespace=NAMESPACE),
        spec=ImageUpdateAutomationSpec(
            interval="5m",
            source_ref=ImageUpdateAutomationSpecSourceRef(
                kind=ImageUpdateAutomationSpecSourceRefKind.GIT_REPOSITORY, name=source.name
            ),
            git=ImageUpdateAutomationSpecGit(
                checkout=ImageUpdateAutomationSpecGitCheckout(
                    ref=ImageUpdateAutomationSpecGitCheckoutRef(branch=ducktape_flux.BRANCH)
                ),
                commit=ImageUpdateAutomationSpecGitCommit(
                    author=ImageUpdateAutomationSpecGitCommitAuthor(
                        name="flux-image-automation", email="flux@allegedly.works"
                    ),
                    message_template=(
                        "chore: update images [skip ci]\n"
                        "\n"
                        "{{ range $resource, $changes := .Changed.Objects -}}\n"
                        "{{ range $_, $change := $changes -}}\n"
                        "{{ $change.OldValue }} -> {{ $change.NewValue }}\n"
                        "{{ end -}}\n"
                        "{{ end -}}"
                    ),
                ),
                push=ImageUpdateAutomationSpecGitPush(branch=ducktape_flux.BRANCH),
            ),
            update=ImageUpdateAutomationSpecUpdate(
                strategy=ImageUpdateAutomationSpecUpdateStrategy.SETTERS, path=f"./{HAND_WRITTEN_ROOT}"
            ),
        ),
    )
    return chart


def openclaw_chart(app: App) -> Chart:
    chart = Chart(app, "openclaw-image", disable_resource_name_hashes=True)
    repository = ImageRepository(
        chart,
        "repository",
        metadata=ApiObjectMetadata(name=_OPENCLAW, namespace=NAMESPACE),
        spec=ImageRepositorySpec(image="ghcr.io/agentydragon/openclaw", interval="5m"),
    )
    ImagePolicy(
        chart,
        "policy",
        metadata=ApiObjectMetadata(name=_OPENCLAW, namespace=NAMESPACE),
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


def flux_image_automation_ghcr(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    name = "flux-image-automation-ghcr"
    return flux_kustomization(chart, name, directory, retry_interval=None, wait=None)
