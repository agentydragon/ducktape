"""Private GitLab source, registry scanning, image commits, and confined demo reconciliation."""

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ApiResource, Role, RolePolicyRule, ServiceAccount
from flux_gitrepository_crds.io.fluxcd.toolkit.source import (
    GitRepository,
    GitRepositorySpec,
    GitRepositorySpecRef,
    GitRepositorySpecSecretRef,
)
from flux_imagerepository_crds.io.fluxcd.toolkit.image import (
    ImageRepository,
    ImageRepositorySpec,
    ImageRepositorySpecSecretRef,
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
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind
from flux_receiver_crds.io.fluxcd.toolkit.notification import (
    Receiver,
    ReceiverSpec,
    ReceiverSpecSecretRef,
    ReceiverSpecType,
)

from cluster.cdk8s import flux
from cluster.cdk8s.gitlab_spike import demo
from cluster.cdk8s.gitlab_spike.app import DEMO_NAMESPACE, WEB
from cluster.cdk8s.image_automation import newest_ci_tag_policy
from cluster.cdk8s.providers.flux.notification import ReceiverResource

NAME = "gitlab-spike-delivery"
_NAME = "gitlab-spike"
_NAMESPACE = "flux-system"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    source = GitRepository(
        chart,
        "source",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        spec=GitRepositorySpec(
            url=f"{WEB.url}/{demo.PROJECT}.git",
            interval="1m",
            ref=GitRepositorySpecRef(branch="main"),
            secret_ref=GitRepositorySpecSecretRef(name="gitlab-spike-git"),
        ),
    )
    repository = ImageRepository(
        chart,
        "image",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        spec=ImageRepositorySpec(
            image=demo.IMAGE_REPOSITORY,
            interval="1m",
            secret_ref=ImageRepositorySpecSecretRef(name="gitlab-spike-registry"),
        ),
    )
    newest_ci_tag_policy(chart, repository)
    ImageUpdateAutomation(
        chart,
        "image-update",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        spec=ImageUpdateAutomationSpec(
            source_ref=ImageUpdateAutomationSpecSourceRef(
                kind=ImageUpdateAutomationSpecSourceRefKind.GIT_REPOSITORY, name=source.name
            ),
            interval="1m",
            git=ImageUpdateAutomationSpecGit(
                checkout=ImageUpdateAutomationSpecGitCheckout(
                    ref=ImageUpdateAutomationSpecGitCheckoutRef(branch="main")
                ),
                commit=ImageUpdateAutomationSpecGitCommit(
                    author=ImageUpdateAutomationSpecGitCommitAuthor(
                        name="spike-flux", email="spike-flux@invalid.example"
                    ),
                    message_template="chore: update spike image [skip ci]",
                ),
                push=ImageUpdateAutomationSpecGitPush(branch="main"),
            ),
            update=ImageUpdateAutomationSpecUpdate(
                strategy=ImageUpdateAutomationSpecUpdateStrategy.SETTERS, path="./k8s"
            ),
        ),
    )
    Receiver(
        chart,
        "receiver",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        spec=ReceiverSpec(
            type=ReceiverSpecType.GITLAB,
            events=["Push Hook", "Pipeline Hook"],
            secret_ref=ReceiverSpecSecretRef(name="gitlab-spike-webhook"),
            resources=[
                ReceiverResource.git_repository(source.name),
                ReceiverResource.image_repository(repository.name),
                ReceiverResource.image_update_automation(_NAME),
            ],
        ),
    )
    reconciler = ServiceAccount(
        chart, "reconciler", metadata=ApiObjectMetadata(name=_NAME, namespace=flux.NAMESPACE), automount_token=False
    )
    role = Role(
        chart,
        "demo-deployer",
        metadata=ApiObjectMetadata(name=_NAME, namespace=DEMO_NAMESPACE),
        rules=[
            RolePolicyRule(
                resources=[ApiResource.CONFIG_MAPS, ApiResource.SERVICES, ApiResource.DEPLOYMENTS],
                verbs=["get", "list", "watch", "create", "update", "patch", "delete"],
            ),
            RolePolicyRule(resources=[ApiResource.REPLICA_SETS], verbs=["get", "list", "watch"]),
        ],
    )
    role.bind(reconciler)
    flux.flux_kustomization(
        chart,
        "gitlab-spike-demo",
        KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name=source.name, namespace=source.metadata.namespace
        ),
        path="./k8s",
        interval="1m",
        target_namespace=DEMO_NAMESPACE,
        service_account_name=reconciler.name,
        timeout="5m",
    )
    return chart
