"""The litellm Namespace and the root `kustomization.yaml` of the directory the `litellm`
Kustomization applies, which gathers it with `secrets`, the database manifest and `app`."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import manifest_file, namespace_chart, write_charts, write_yaml
from cluster.cdk8s.litellm import database
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/litellm"


def write_manifests(root: Path) -> None:
    namespace = write_charts(root, OUTPUT_DIR, partial(namespace_chart, name="litellm", vpa=Vpa.AUTO))
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[namespace, "secrets", f"db/{manifest_file(database.OUTPUT_DIR)}", "app"]),
    )
