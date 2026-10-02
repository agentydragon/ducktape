"""The tofu-state Namespace, written into the directory of the Kustomization that owns it."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from cluster.cdk8s.generation import namespace_chart, write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa


def write_manifests(root: Path) -> None:
    write_charts(root, f"{HAND_WRITTEN_ROOT}/tofu-state", partial(namespace_chart, name="tofu-state", vpa=Vpa.AUTO))
