"""The home-assistant Namespace, written into the directory of the Kustomization that owns it."""

from __future__ import annotations

from pathlib import Path

from cluster.cdk8s.generation import write_namespace
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT


def write_manifests(root: Path) -> None:
    write_namespace(
        root,
        f"{HAND_WRITTEN_ROOT}/home-assistant",
        name="home-assistant",
        labels={
            "goldilocks.fairwinds.com/enabled": "true",
            # Home Assistant and Matter need the host network for mDNS, Bluetooth and
            # Matter fabric traffic on the physical home LAN.
            "pod-security.kubernetes.io/enforce": "privileged",
            "pod-security.kubernetes.io/audit": "privileged",
            "pod-security.kubernetes.io/warn": "privileged",
        },
        annotations={
            # Home Assistant stores root-owned 0600 files that require a privileged
            # VolSync mover to copy without silently omitting state.
            "volsync.backube/privileged-movers": "true"
        },
    )
