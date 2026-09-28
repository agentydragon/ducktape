"""The private companion monorepo's GitRepository, and the `gaffer-images`
ImageUpdateAutomation that commits image-tag bumps back to it.

The `gaffer-private` bridge Flux Kustomization that reconciles `gaffer-private/k8s/`
from this source is a node in the central chart (`flux_kustomizations.gaffer_private_bridge`).
Only this directory's `kustomization.yaml` stays hand-written beside the generated output.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart

from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.image_automation import ImageUpdatePush
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

NAME = "gaffer-private"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/gaffer-private-source"


def chart(app: App) -> Chart:
    chart = Chart(app, "gaffer-private-source", disable_resource_name_hashes=True)
    ImageUpdatePush(
        chart,
        "image-update-push",
        name="gaffer-images",
        description="Sibling of `all-images` for the gaffer-private GitRepository. Watches ImagePolicies whose "
        "marker comments target gaffer-private's manifests and commits image-tag bumps back to gaffer-private/main.",
        source_name=NAME,
        source_description="Private companion monorepo. Reconciled from main branch via the ducktape-automation "
        "GitHub App (also used to push image-pin commits back here).",
        url="https://github.com/agentydragon/gaffer-private.git",
        branch="main",
        path="./k8s",
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
