"""The litellm Namespace and the root `kustomization.yaml` of the directory the `litellm`
Kustomization applies, which gathers it with `secrets`, `db` and `app`."""

from __future__ import annotations

from pathlib import Path

from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_namespace, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/litellm"


def write_manifests(root: Path) -> None:
    write_namespace(
        root,
        OUTPUT_DIR,
        name="litellm",
        labels={
            "goldilocks.fairwinds.com/enabled": "true",
            "goldilocks.fairwinds.com/vpa-update-mode": "auto",
            "rbac.ducktape.io/agent-readable-logs": "true",
        },
    )
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=["namespace.k8s.yaml", "secrets", "db", "app"]),
    )
