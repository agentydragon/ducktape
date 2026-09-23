"""The agentydragon user Namespace and the SOPS-encrypted Atuin password beside it.

The Secret stays hand-written: `nix/home/modules/atuin.nix` decrypts the same file.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml

NAME = "user-agentydragon"
NAMESPACE = "agentydragon"
OUTPUT_DIR = "cluster/k8s/user-agentydragon"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(chart, "namespace", metadata=k8s.ObjectMeta(name=NAMESPACE))
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{NAME}.k8s.yaml", "atuin-user-password.sops.yaml"]),
    )


def user_agentydragon(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(chart, NAME, artifact, wait=None, timeout="10m", decryption=SOPS_DECRYPTION)
