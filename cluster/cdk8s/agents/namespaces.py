"""Namespace of the Plaid MCP Kustomization."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from cluster.cdk8s.generation import namespace_chart, write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa


def write_manifests(root: Path) -> None:
    write_charts(
        root, f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp", partial(namespace_chart, name="plaid-mcp", vpa=Vpa.DISABLED)
    )
