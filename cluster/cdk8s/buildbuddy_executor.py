"""Opt-in KVM execution on wyrm2, deployed by the official executor Helm chart."""

from cdk8s import Chart
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
        # KubeVirt supplies the node's devices.kubevirt.io/kvm device allocation.
        depends_on=flux_kustomization_depends_on_many(kubevirt, kyverno),
    )
