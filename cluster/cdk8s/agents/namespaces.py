"""Namespaces of the agents area, each written into the directory of the Kustomization
that owns it."""

from __future__ import annotations

from pathlib import Path

from cluster.cdk8s.generation import write_namespace


def write_manifests(root: Path) -> None:
    write_namespace(
        root,
        "cluster/k8s/agents/plaid-mcp",
        name="plaid-mcp",
        labels={"goldilocks.fairwinds.com/enabled": "false", "rbac.ducktape.io/agent-readable-logs": "true"},
    )
