"""Namespaces of the agents area, each written into the directory of the Kustomization
that owns it."""

from __future__ import annotations

from pathlib import Path

from cluster.cdk8s.generation import write_namespace
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import AgentReadable, Vpa


def write_manifests(root: Path) -> None:
    write_namespace(
        root,
        f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp",
        name="plaid-mcp",
        vpa=Vpa.DISABLED,
        agent_readable=AgentReadable.LOGS,
    )
    write_namespace(
        root,
        f"{HAND_WRITTEN_ROOT}/agents/haku-egress-proxy",
        name="haku-egress-proxy",
        vpa=Vpa.AUTO,
        agent_readable=None,
        labels={"name": "haku-egress-proxy"},
    )
