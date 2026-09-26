"""The authentik Namespace, written into the directory of the Kustomization that owns it."""

from __future__ import annotations

from pathlib import Path

from cluster.cdk8s.generation import write_namespace
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT


def write_manifests(root: Path) -> None:
    write_namespace(
        root,
        f"{HAND_WRITTEN_ROOT}/authentik",
        name="authentik",
        labels={
            "goldilocks.fairwinds.com/enabled": "true",
            "goldilocks.fairwinds.com/vpa-update-mode": "initial",
            "rbac.ducktape.io/agent-readable-logs": "true",
        },
        # The Kyverno default-vpa-requests-only policy matches only auto-mode namespaces; in
        # initial mode VPA would otherwise scale the declared limits with its requests.
        annotations={
            "goldilocks.fairwinds.com/vpa-resource-policy": (
                '{"containerPolicies": [{"containerName": "*", "controlledValues": "RequestsOnly"}]}'
            )
        },
    )
