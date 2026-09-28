"""The authentik Namespace, written into the directory of the Kustomization that owns it."""

from __future__ import annotations

from pathlib import Path

from cluster.cdk8s.generation import write_namespace
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import AgentReadable, Vpa


def write_manifests(root: Path) -> None:
    write_namespace(
        root,
        f"{HAND_WRITTEN_ROOT}/authentik",
        name="authentik",
        vpa=Vpa.INITIAL,
        agent_readable=AgentReadable.LOGS,
        # The Kyverno default-vpa-requests-only policy matches only auto-mode namespaces; in
        # initial mode VPA would otherwise scale the declared limits with its requests.
        annotations={
            "goldilocks.fairwinds.com/vpa-resource-policy": (
                '{"containerPolicies": [{"containerName": "*", "controlledValues": "RequestsOnly"}]}'
            )
        },
    )
