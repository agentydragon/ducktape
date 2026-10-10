"""tofu-controller: its HelmRepository and the HelmRelease, the `Terraform` CRD, and the Flux
image automation that rolls the controller image.

The controller image is ours: `third_party/tofu_controller` patches upstream to accept a Flux
`ExternalArtifact` as a `Terraform` source. So the CRD installed here is that module's patched
copy, not the chart's (the HelmRelease skips CRDs), and a post-renderer grants the chart's
manager ClusterRole read access to ExternalArtifacts. The runner pods' image is ours too
(`cluster/tf_runner`: upstream's runner with every provider baked in). `PINS_DIR` sets both
images in the HelmRelease from the image policies below.

`values` is an untyped dict: Helm values carry no schema for `cdk8s_import` to ingest.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart, Include
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallCrds,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecPostRenderers,
    HelmReleaseSpecPostRenderersKustomize,
    HelmReleaseSpecPostRenderersKustomizePatches,
    HelmReleaseSpecPostRenderersKustomizePatchesTarget,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeCrds,
)

from cluster.cdk8s import flux, terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.image_automation import newest_ci_tag_policy
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.flux.image_repository import ImageRepository
from util.bazel.runfiles import get_required_path

NAME = "tofu-controller"
NAMESPACE = "flux-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/tofu-controller"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}-image-pins"
# GHCR, not the Forgejo registry: the Forgejo pull credential's user is created by a Terraform
# this controller applies, so a Forgejo-hosted controller or runner could never start on a fresh
# cluster.
IMAGE = "ghcr.io/agentydragon/tofu-controller"
RUNNER_NAME = "tf-runner"
RUNNER_IMAGE = "ghcr.io/agentydragon/tf-runner"
_CRD = "ducktape_tofu_controller/infra.contrib.fluxcd.io_terraforms.yaml"
# The chart's `tofu-manager-role` lists the source kinds upstream supports; the patched
# controller also reads (and watches) ExternalArtifacts.
_EXTERNAL_ARTIFACT_RBAC_PATCH = """\
- op: add
  path: /rules/-
  value:
    apiGroups: [source.toolkit.fluxcd.io]
    resources: [externalartifacts]
    verbs: [get, list, watch]
- op: add
  path: /rules/-
  value:
    apiGroups: [source.toolkit.fluxcd.io]
    resources: [externalartifacts/status]
    verbs: [get]
"""


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    crd = Include(chart, "terraform-crd", url=str(get_required_path(_CRD)))
    for obj in crd.api_objects:
        # Pruning the CRD would delete every Terraform with it.
        obj.metadata.add_annotation("kustomize.toolkit.fluxcd.io/prune", "disabled")
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(chart, NAME, NAMESPACE, url="https://flux-iac.github.io/tofu-controller"),
        chart="tofu-controller",
        version="0.16.5",
        interval="15m",
        install=HelmReleaseSpecInstall(
            crds=HelmReleaseSpecInstallCrds.SKIP, remediation=HelmReleaseSpecInstallRemediation(retries=3)
        ),
        upgrade=HelmReleaseSpecUpgrade(crds=HelmReleaseSpecUpgradeCrds.SKIP),
        post_renderers=[
            HelmReleaseSpecPostRenderers(
                kustomize=HelmReleaseSpecPostRenderersKustomize(
                    patches=[
                        HelmReleaseSpecPostRenderersKustomizePatches(
                            target=HelmReleaseSpecPostRenderersKustomizePatchesTarget(
                                kind="ClusterRole", name="tofu-manager-role"
                            ),
                            patch=_EXTERNAL_ARTIFACT_RBAC_PATCH,
                        )
                    ]
                )
            )
        ],
        values={
            # Gitops Terraform CRs read the ducktape GitRepository in the Flux namespace.
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
    for name, image in ((NAME, IMAGE), (RUNNER_NAME, RUNNER_IMAGE)):
        newest_ci_tag_policy(
            chart,
            ImageRepository(
                chart,
                f"{name}-image-repository",
                metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
                image=image,
                interval="5m",
            ),
        )
    return chart


def tofu_controller(
    chart: Chart, directory: RenderedDirectory, kyverno: Kustomization, flux_image_automation_ghcr: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        interval="10m0s",
        timeout="10m0s",
        depends_on=flux_kustomization_depends_on_many(kyverno, flux_image_automation_ghcr),
    )
