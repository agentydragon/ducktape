"""Upstream tofu-controller chart, its separately managed CRD, and runner image tracking.

The pinned CRD comes from the same chart release. Flux applies it separately because Helm
does not upgrade CRDs in a chart's crds directory. The runner image extends the matching
upstream release with the cluster's provider mirror; `PINS_DIR` pins it for coordinated rollout.

`values` is an untyped dict: Helm values carry no schema for `cdk8s_import` to ingest.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart, Include
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallCrds,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeCrds,
)
from flux_imagerepository_crds.io.fluxcd.toolkit.image import ImageRepository, ImageRepositorySpec
from python.runfiles import Runfiles
from vpa_crds.io.k8s.autoscaling import (
    VerticalPodAutoscaler,
    VerticalPodAutoscalerSpec,
    VerticalPodAutoscalerSpecResourcePolicy,
    VerticalPodAutoscalerSpecResourcePolicyContainerPolicies,
    VerticalPodAutoscalerSpecTargetRef,
    VerticalPodAutoscalerSpecUpdatePolicy,
)

from cluster.cdk8s import flux, terraform
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.image_automation import newest_ci_tag_policy
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT

NAME = "tofu-controller"
NAMESPACE = "flux-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/tofu-controller"
VPA_NAME = f"{NAME}-vpa"
VPA_OUTPUT_DIR = f"{GENERATED_ROOT}/{VPA_NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}-image-pins"
# GHCR, not the Forgejo registry: the Forgejo pull credential's user is created by a Terraform
# this controller applies, so a Forgejo-hosted controller or runner could never start on a fresh
# cluster.
RUNNER_NAME = "tf-runner"
RUNNER_IMAGE = "ghcr.io/agentydragon/tf-runner"
_CRD = "tofu_controller_crd/file/crds.yaml"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # Bazel runfiles expose data files through symlinks, while cdk8s's local-file loader checks
    # lstat() and therefore treats those paths as empty. Resolve the runfile to its real file first.
    crd = Include(chart, "terraform-crd", url=str((Runfiles.CreateOrRaise().root() / _CRD).resolve()))
    for obj in crd.api_objects:
        # Pruning the CRD would delete every Terraform with it.
        obj.metadata.add_annotation("kustomize.toolkit.fluxcd.io/prune", "disabled")
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(chart, NAME, NAMESPACE, url="https://flux-iac.github.io/tofu-controller"),
        chart="tofu-controller",
        version="0.17.0",
        interval="15m",
        install=HelmReleaseSpecInstall(
            crds=HelmReleaseSpecInstallCrds.SKIP, remediation=HelmReleaseSpecInstallRemediation(retries=3)
        ),
        upgrade=HelmReleaseSpecUpgrade(crds=HelmReleaseSpecUpgradeCrds.SKIP),
        values={
            "featureGates": {"externalArtifact": True},
            # Gitops Terraform CRs read their module's `ExternalArtifact` in `flux.NAMESPACE`, not their own.
            "allowCrossNamespaceRefs": terraform.NAMESPACE != flux.NAMESPACE,
            "runner": {
                "grpc": {"maxMessageSize": 50},  # MB, default 4 — defense against repo growth
                "serviceAccount": {"annotations": {"eks.amazonaws.com/role-arn": ""}},  # Not needed for on-prem
            },
            # Security context for Terraform runner pods
            "podSecurityContext": {"runAsNonRoot": True, "runAsUser": 65532, "fsGroup": 65532},
            # Resource limits for runner pods
            "resources": {"limits": {"cpu": "1000m", "memory": "1Gi"}, "requests": {"cpu": "100m", "memory": "128Mi"}},
            "logLevel": "info",
        },
    )
    newest_ci_tag_policy(
        chart,
        ImageRepository(
            chart,
            f"{RUNNER_NAME}-image-repository",
            metadata=ApiObjectMetadata(name=RUNNER_NAME, namespace=NAMESPACE),
            spec=ImageRepositorySpec(image=RUNNER_IMAGE, interval="5m"),
        ),
    )
    return chart


def vpa_chart(app: App) -> Chart:
    chart = Chart(app, VPA_NAME, disable_resource_name_hashes=True)
    VerticalPodAutoscaler(
        chart,
        NAME,
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        spec=VerticalPodAutoscalerSpec(
            target_ref=VerticalPodAutoscalerSpecTargetRef(api_version="apps/v1", kind="Deployment", name=NAME),
            update_policy=VerticalPodAutoscalerSpecUpdatePolicy(update_mode="Initial"),
            resource_policy=VerticalPodAutoscalerSpecResourcePolicy(
                container_policies=[
                    VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(
                        container_name=NAME, controlled_resources=["cpu", "memory"], controlled_values="RequestsOnly"
                    ),
                    VerticalPodAutoscalerSpecResourcePolicyContainerPolicies(container_name="*", mode="Off"),
                ]
            ),
        ),
    )
    return chart
