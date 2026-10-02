"""Namespaces of the agents area, each in the directory of the Kustomization that owns it:
plaid-mcp's written here, haku-egress-proxy's included by `haku_egress_proxy.write_manifests`."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from cdk8s import App, Chart

from cluster.cdk8s.generation import namespace_chart, write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa


def haku_egress_proxy(app: App) -> Chart:
    return namespace_chart(app, name="haku-egress-proxy", vpa=Vpa.AUTO, labels={"name": "haku-egress-proxy"})


def write_manifests(root: Path) -> None:
    write_charts(
        root, f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp", partial(namespace_chart, name="plaid-mcp", vpa=Vpa.DISABLED)
    )
