"""Tests for the restrict-agent-gateway-routes Kyverno policy."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.kyverno.apply import apply_policy
from cluster.validation.kyverno.paths import manifest, policy


@pytest.fixture
def gateway_routes_policy() -> Path:
    return policy("restrict-agent-gateway-routes.yaml")


class TestRestrictAgentGatewayRoutes:
    """The deny policy fences agents off the public gateway.

    Namespace-scoped (no subject match), so it is testable with plain
    `kyverno apply` — no admission --userinfo context needed.
    """

    def test_httproute_in_agent_namespace_denied(self, gateway_routes_policy: Path):
        result = apply_policy(gateway_routes_policy, manifest("httproute_in_agent_namespace.yaml"))
        assert result.failed == 1, f"HTTPRoute in claude-sandbox must be denied\n{result.stdout}"

    def test_tlsroute_in_agent_namespace_denied(self, gateway_routes_policy: Path):
        result = apply_policy(gateway_routes_policy, manifest("tlsroute_in_agent_namespace.yaml"))
        assert result.failed == 1, f"TLSRoute in haku-sandbox must be denied\n{result.stdout}"

    def test_httproute_in_service_namespace_allowed(self, gateway_routes_policy: Path):
        """Routes in non-agent (operator-owned) namespaces are untouched."""
        result = apply_policy(gateway_routes_policy, manifest("httproute_in_service_namespace.yaml"))
        assert result.failed == 0, f"HTTPRoute in forgejo must not be denied\n{result.stdout}"


if __name__ == "__main__":
    pytest_bazel.main()
