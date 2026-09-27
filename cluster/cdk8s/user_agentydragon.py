"""The agentydragon user Namespace and the SOPS-encrypted Atuin password beside it.

The Secret stays hand-written: `nix/home/modules/atuin.nix` decrypts the same file.
"""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "user-agentydragon"
NAMESPACE = "agentydragon"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/user-agentydragon"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(chart, "namespace", metadata=k8s.ObjectMeta(name=NAMESPACE))
    return chart


def user_agentydragon(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(chart, NAME, directory, wait=None, timeout="10m")
