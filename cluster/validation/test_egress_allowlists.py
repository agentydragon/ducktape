"""The egress fences' seams with what is not generated from their tuples.

The openclaw spike's iron `allowlist` transform is a hand-written `configMapGenerator` input
that must equal the DNS rule generated from `egress_fences.OPENCLAW_SPIKE_ALLOWLIST`; the rules
over which fence holds which host group live beside the tuples, in
`//cluster/cdk8s:test_egress_fences`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest_bazel
import yaml
from cdk8s import (
    App,
    Chart,
    Testing as Cdk8sTesting,  # pytest would collect a class named Testing
)
from more_itertools import one

from cluster.cdk8s import egress_fences

HAKU_OPENCLAW_FENCE = "agents/haku-egress-proxy/openclaw-spike-iron.yaml"


def _cilium_dns_names(document: dict[str, Any]) -> set[str]:
    """Every name a `toPorts.rules.dns` rule permits the pod to look up."""
    return {
        name
        for rule in document["spec"]["egress"]
        for port_rule in rule.get("toPorts", ())
        for entry in port_rule.get("rules", {}).get("dns", ())
        for name in entry.values()
    }


def _iron_hosts(document: dict[str, Any]) -> set[str]:
    return set(one(t for t in document["transforms"] if t["name"] == "allowlist")["config"]["domains"])


def _load(k8s_dir: Path, path: str) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((k8s_dir / path).read_text()))


def _synth(build: Callable[[App], Chart]) -> dict[str, Any]:
    return cast(dict[str, Any], one(Cdk8sTesting.synth(build(Cdk8sTesting.app()))))


def test_openclaw_spike_resolves_exactly_its_iron_allowlist(k8s_dir: Path) -> None:
    """The spike's two layers are enforced by different components from two files: the iron
    allowlist is app config, the DNS rule is Cilium's. That rule is the only Cilium-side bound
    on the proxy, so it is pinned to the allowlist rather than left open -- until the iron
    config is generated from the same tuple (cluster/cdk8s/TODO.md)."""
    expected = _iron_hosts(_load(k8s_dir, HAKU_OPENCLAW_FENCE)) | set(egress_fences.CLUSTER_DNS)
    assert _cilium_dns_names(_synth(egress_fences.haku_openclaw_spike)) == expected


if __name__ == "__main__":
    pytest_bazel.main()
