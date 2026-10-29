"""Fixture lookups and focused synthesis for the Kyverno CLI tests."""

from __future__ import annotations

from pathlib import Path

import yaml
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)

from cluster.cdk8s.kyverno import policies, proxy_injection
from util.bazel.runfiles import get_required_path

_POLICY_BUILDERS = {
    "default-vpa-requests-only": policies.default_vpa_requests_only_chart,
    "default-disable-service-links": policies.default_disable_service_links_chart,
    "restrict-agent-gateway-routes": policies.restrict_agent_gateway_routes_chart,
    "require-secret-store-conditions": policies.require_secret_store_conditions_chart,
    "inject-haku-egress-proxy": proxy_injection.inject_haku_egress_proxy_chart,
}


def manifest(name: str) -> Path:
    """An input resource from this package's testdata directory."""
    return get_required_path(f"_main/cluster/cdk8s/kyverno/testdata/{name}")


def policy(app: App, name: str, directory: Path) -> Path:
    """Synthesize one policy constructor to a file for the real Kyverno CLI."""
    chart = _POLICY_BUILDERS[name](app)
    matches = [
        doc for doc in Cdk8sTesting.synth(chart) if doc["kind"] == "ClusterPolicy" and doc["metadata"]["name"] == name
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one ClusterPolicy named {name!r}, got {len(matches)}")
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump(matches[0], sort_keys=False))
    return path
