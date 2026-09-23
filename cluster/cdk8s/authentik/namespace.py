"""The authentik Namespace, written into the directory of the Kustomization that owns it."""

from __future__ import annotations

from pathlib import Path

from cluster.cdk8s.generation import write_namespace


def write_manifests(root: Path) -> None:
    write_namespace(
        root,
        "cluster/k8s/authentik",
        name="authentik",
        labels={
            "goldilocks.fairwinds.com/enabled": "true",
            "goldilocks.fairwinds.com/vpa-update-mode": "initial",
            "rbac.ducktape.io/agent-readable-logs": "true",
        },
    )
