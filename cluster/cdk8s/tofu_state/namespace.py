"""The tofu-state Namespace, written into the directory of the Kustomization that owns it."""

from __future__ import annotations

from pathlib import Path

from cluster.cdk8s.generation import write_namespace
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa


def write_manifests(root: Path) -> None:
    write_namespace(root, f"{HAND_WRITTEN_ROOT}/tofu-state", name="tofu-state", vpa=Vpa.AUTO, agent_readable=None)
