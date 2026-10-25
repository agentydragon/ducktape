"""Private GitLab source, registry scanning, image commits, and confined demo reconciliation."""

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ApiResource, Role, RolePolicyRule, ServiceAccount
from flux_gitrepository_crds.io.fluxcd.toolkit.source import GitRepositorySpecRef, GitRepositorySpecSecretRef
from flux_imagerepository_crds.io.fluxcd.toolkit.image import ImageRepositorySpecSecretRef
from flux_imageupdateautomation_crds.io.fluxcd.toolkit.image import (
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
from flux_receiver_crds.io.fluxcd.toolkit.notification import ReceiverSpecSecretRef, ReceiverSpecType

from cluster.cdk8s import flux
from cluster.cdk8s.image_automation import newest_ci_tag_policy
from cluster.cdk8s.providers.flux.git_repository import GitRepository
from cluster.cdk8s.providers.flux.image_repository import ImageRepository
from cluster.cdk8s.providers.flux.image_update_automation import ImageUpdateAutomation
from cluster.cdk8s.providers.flux.notification import Receiver, ReceiverResource

NAME = "gitlab-spike-delivery"
_NAME = "gitlab-spike"
_NAMESPACE = "flux-system"
_DEMO_NAMESPACE = "gitlab-spike-demo"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    source = GitRepository(
        chart,
        "source",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        url="http://gitlab-spike-webservice-default.gitlab-spike.svc:8181/spike/acceptance.git",
        interval="1m",
        ref=GitRepositorySpecRef(branch="main"),
        secret_ref=GitRepositorySpecSecretRef(name="gitlab-spike-git"),
    )
    repository = ImageRepository(
        chart,
        "image",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        image="registry-gitlab-spike.allegedly.works/spike/acceptance",
        interval="1m",
        secret_ref=ImageRepositorySpecSecretRef(name="gitlab-spike-registry"),
    )
    newest_ci_tag_policy(chart, repository)
    ImageUpdateAutomation(
        chart,
        "image-update",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        source_ref=ImageUpdateAutomationSpecSourceRef(
            kind=ImageUpdateAutomationSpecSourceRefKind.GIT_REPOSITORY, name=source.name
        ),
        interval="1m",
        git=ImageUpdateAutomationSpecGit(
            checkout=ImageUpdateAutomationSpecGitCheckout(ref=ImageUpdateAutomationSpecGitCheckoutRef(branch="main")),
            commit=ImageUpdateAutomationSpecGitCommit(
                author=ImageUpdateAutomationSpecGitCommitAuthor(name="spike-flux", email="spike-flux@invalid.example"),
                message_template="chore: update spike image [skip ci]",
            ),
            push=ImageUpdateAutomationSpecGitPush(branch="main"),
        ),
        update=ImageUpdateAutomationSpecUpdate(strategy=ImageUpdateAutomationSpecUpdateStrategy.SETTERS, path="./k8s"),
    )
    Receiver(
        chart,
        "receiver",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        type=ReceiverSpecType.GITLAB,
        events=["Push Hook", "Pipeline Hook"],
        secret_ref=ReceiverSpecSecretRef(name="gitlab-spike-webhook"),
        resources=[
            ReceiverResource.git_repository(source.name),
            ReceiverResource.image_repository(repository.name),
            ReceiverResource.image_update_automation(_NAME),
        ],
    )
    reconciler = ServiceAccount(
        chart, "reconciler", metadata=ApiObjectMetadata(name=_NAME, namespace=flux.NAMESPACE), automount_token=False
    )
    role = Role(
        chart,
        "demo-deployer",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_DEMO_NAMESPACE),
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
        target_namespace=_DEMO_NAMESPACE,
        service_account_name=reconciler.name,
        timeout="5m",
    )
    return chart
