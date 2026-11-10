"""Opt-in KVM execution on wyrm2, deployed by the official executor Helm chart."""

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/buildbuddy-executor"


def buildbuddy_executor(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, kubevirt: Kustomization, kyverno: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "buildbuddy-executor",
        artifact,
        timeout="15m",
        decryption=SOPS_DECRYPTION,
        # The staged Firecracker HelmRelease is suspended and has no Ready status yet.
        # Verify the active OCI release; add the driver here when it is activated.
        wait=False,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="helm.toolkit.fluxcd.io/v2",
                kind="HelmRelease",
                name="buildbuddy-executor",
                namespace="buildbuddy-executor",
            )
        ],
        # KubeVirt supplies the node's devices.kubevirt.io/kvm device allocation.
        depends_on=flux_kustomization_depends_on_many(kubevirt, kyverno),
    )
