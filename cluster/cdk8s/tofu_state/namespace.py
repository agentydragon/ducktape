"""The tofu-state Namespace, written into the directory of the Kustomization that owns it."""

from __future__ import annotations

from pathlib import Path

from cluster.cdk8s.generation import write_namespace


def write_manifests(root: Path) -> None:
    write_namespace(
        root,
        "cluster/k8s/tofu-state",
        name="tofu-state",
        labels={"goldilocks.fairwinds.com/enabled": "true", "goldilocks.fairwinds.com/vpa-update-mode": "auto"},
    )
