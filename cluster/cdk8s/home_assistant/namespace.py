"""The home-assistant Namespace, written into the directory of the Kustomization that owns it."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from cluster.cdk8s.generation import namespace_chart, write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import PRIVILEGED_POD_SECURITY, Vpa

NAMESPACE = "home-assistant"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/home-assistant"


def write_manifests(root: Path) -> None:
    write_charts(
        root,
        OUTPUT_DIR,
        partial(
            namespace_chart,
            name=NAMESPACE,
            vpa=Vpa.RECOMMEND,
            # Home Assistant and Matter need the host network for mDNS, Bluetooth and
            # Matter fabric traffic on the physical home LAN.
            labels=PRIVILEGED_POD_SECURITY,
            annotations={
                # Home Assistant stores root-owned 0600 files that require a privileged
                # VolSync mover to copy without silently omitting state.
                "volsync.backube/privileged-movers": "true"
            },
        ),
    )
