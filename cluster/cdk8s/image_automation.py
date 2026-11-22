"""Flux image automation policy shared by the generators: the ImagePolicy picking an image's
newest CI build, and the ImageUpdateAutomation that commits picked tags back to a GitHub
repository through the ducktape-automation GitHub App."""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from flux_gitrepository_crds.io.fluxcd.toolkit.source import (
    GitRepository,
    GitRepositorySpec,
    GitRepositorySpecProvider,
    GitRepositorySpecRef,
    GitRepositorySpecSecretRef,
)
from flux_imagepolicy_crds.io.fluxcd.toolkit.image import (
    ImagePolicySpecFilterTags,
    ImagePolicySpecImageRepositoryRef,
    ImagePolicySpecPolicy,
    ImagePolicySpecPolicyAlphabetical,
    ImagePolicySpecPolicyAlphabeticalOrder,
)
from flux_imagerepository_crds.io.fluxcd.toolkit.image import ImageRepository
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

from cluster.cdk8s.providers.flux.image_policy import ImagePolicy

# CI pushes {branch}-YYYYMMDDHHMMSS-{sha7}; among devel builds, alphabetical order is chronological.
_CI_TAG_PATTERN = r"^devel-\d{14}-[0-9a-f]{7}$"
# The ducktape-automation GitHub App's credentials: a SOPS Secret Flux decrypts into flux-system
# (cluster/k8s/flux/flux-system). A GitRepository's secretRef resolves in its own namespace.
GITHUB_APP_NAMESPACE = "flux-system"
GITHUB_APP_SECRET = "ducktape-automation-github-app"
_COMMIT_MESSAGE = (
    "chore: update images [skip ci]\n"
    "\n"
    "{{ range $resource, $changes := .Changed.Objects -}}\n"
    "{{ range $_, $change := $changes -}}\n"
    "{{ $change.OldValue }} -> {{ $change.NewValue }}\n"
    "{{ end -}}\n"
    "{{ end -}}"
)


def newest_ci_tag_policy(scope: Construct, repository: ImageRepository) -> ImagePolicy:
    """The ImagePolicy picking `repository`'s newest devel CI build, named after the repository and
    in its namespace, where its by-name reference resolves."""
    return ImagePolicy(
        scope,
        f"image-policy-{repository.name}",
        metadata=ApiObjectMetadata(name=repository.name, namespace=repository.metadata.namespace),
        image_repository_ref=ImagePolicySpecImageRepositoryRef(name=repository.name),
        filter_tags=ImagePolicySpecFilterTags(pattern=_CI_TAG_PATTERN),
        policy=ImagePolicySpecPolicy(
            alphabetical=ImagePolicySpecPolicyAlphabetical(order=ImagePolicySpecPolicyAlphabeticalOrder.ASC)
        ),
    )


class ImageUpdatePush(Construct):
    """An ImageUpdateAutomation committing Setters-marker tag bumps under `path` to `branch` of the
    GitHub repository at `url`, and the GitRepository, authenticated by the ducktape-automation
    GitHub App, that it checks out and pushes through. Both live in the App Secret's namespace:
    the automation's `sourceRef` is by name alone. The source polls every `source_interval`, the
    automation every five minutes. `source_timeout` bounds the source's clone/ls attempt, whose
    default is 60s and which a large repository overruns; `None` leaves the default in place.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        source_name: str,
        url: str,
        branch: str,
        path: str,
        source_description: str,
        source_interval: str,
        description: str | None = None,
        sparse_checkout: Sequence[str] | None = None,
        source_timeout: str | None = None,
    ) -> None:
        super().__init__(scope, id)
        self.source = GitRepository(
            self,
            "source",
            metadata=ApiObjectMetadata(
                name=source_name, namespace=GITHUB_APP_NAMESPACE, annotations={"description": source_description}
            ),
            spec=GitRepositorySpec(
                interval=source_interval,
                provider=GitRepositorySpecProvider.GITHUB,
                ref=GitRepositorySpecRef(branch=branch),
                secret_ref=GitRepositorySpecSecretRef(name=GITHUB_APP_SECRET),
                sparse_checkout=sparse_checkout,
                timeout=source_timeout,
                url=url,
            ),
        )
        self.automation = ImageUpdateAutomation(
            self,
            "automation",
            metadata=ApiObjectMetadata(
                name=name,
                namespace=GITHUB_APP_NAMESPACE,
                annotations=None if description is None else {"description": description},
            ),
            spec=ImageUpdateAutomationSpec(
                interval="5m",
                source_ref=ImageUpdateAutomationSpecSourceRef(
                    kind=ImageUpdateAutomationSpecSourceRefKind.GIT_REPOSITORY, name=self.source.name
                ),
                git=ImageUpdateAutomationSpecGit(
                    checkout=ImageUpdateAutomationSpecGitCheckout(
                        ref=ImageUpdateAutomationSpecGitCheckoutRef(branch=branch)
                    ),
                    commit=ImageUpdateAutomationSpecGitCommit(
                        author=ImageUpdateAutomationSpecGitCommitAuthor(
                            name="flux-image-automation", email="flux@allegedly.works"
                        ),
                        message_template=_COMMIT_MESSAGE,
                    ),
                    push=ImageUpdateAutomationSpecGitPush(branch=branch),
                ),
                update=ImageUpdateAutomationSpecUpdate(
                    strategy=ImageUpdateAutomationSpecUpdateStrategy.SETTERS, path=path
                ),
            ),
        )
