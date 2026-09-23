"""The private companion monorepo's GitRepository, and the `gaffer-images`
ImageUpdateAutomation that commits image-tag bumps back to it.

Hand-written beside the generated output: `bridge.yaml`, the Flux Kustomization that
reconciles `gaffer-private/k8s/` from this source.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_gitrepository_crds.io.fluxcd.toolkit.source import (
    GitRepository,
    GitRepositorySpec,
    GitRepositorySpecProvider,
    GitRepositorySpecRef,
    GitRepositorySpecSecretRef,
)
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

from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "gaffer-private"
NAMESPACE = "flux-system"
OUTPUT_DIR = "cluster/k8s/gaffer-private-source"
_BRANCH = "main"


def chart(app: App) -> Chart:
    chart = Chart(app, "gaffer-private-source", disable_resource_name_hashes=True)
    source = GitRepository(
        chart,
        "source",
        metadata=metadata(
            NAME,
            NAMESPACE,
            annotations={
                "description": "Private companion monorepo. Reconciled from main branch via the ducktape-automation "
                "GitHub App (also used to push image-pin commits back here)."
            },
        ),
        spec=GitRepositorySpec(
            interval="1m",
            provider=GitRepositorySpecProvider.GITHUB,
            ref=GitRepositorySpecRef(branch=_BRANCH),
            secret_ref=GitRepositorySpecSecretRef(name="ducktape-automation-github-app"),
            url="https://github.com/agentydragon/gaffer-private.git",
        ),
    )
    ImageUpdateAutomation(
        chart,
        "automation",
        metadata=metadata(
            "gaffer-images",
            NAMESPACE,
            annotations={
                "description": "Sibling of `all-images` for the gaffer-private GitRepository. Watches ImagePolicies "
                "whose marker comments target gaffer-private's manifests and commits image-tag bumps back to "
                "gaffer-private/main."
            },
        ),
        spec=ImageUpdateAutomationSpec(
            interval="5m",
            source_ref=ImageUpdateAutomationSpecSourceRef(
                kind=ImageUpdateAutomationSpecSourceRefKind.GIT_REPOSITORY, name=source.name
            ),
            git=ImageUpdateAutomationSpecGit(
                checkout=ImageUpdateAutomationSpecGitCheckout(
                    ref=ImageUpdateAutomationSpecGitCheckoutRef(branch=_BRANCH)
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
                push=ImageUpdateAutomationSpecGitPush(branch=_BRANCH),
            ),
            update=ImageUpdateAutomationSpecUpdate(
                strategy=ImageUpdateAutomationSpecUpdateStrategy.SETTERS, path="./k8s"
            ),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
