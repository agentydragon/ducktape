"""Public MCP routing does not expose the private Action API or bypass workload substitution."""

from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path
from x.agentplane.egress.policy import EgressRequest, rule_matches
from x.agentplane.egress.resources import EgressPolicy


@pytest.fixture
def manifests() -> dict[str, Any]:
    return {
        name: yaml.safe_load(get_required_path(f"_main/cluster/k8s/agentplane-staging/{path}").read_text())
        for name, path in {
            "route": "actions/httproute.yaml",
            "service": "actions/service.yaml",
            "deployment": "actions/deployment.yaml",
            "policy": "egress/egresspolicy-agentplane-actions.yaml",
            "credential": "egress/egresscredential-agentplane-workload.yaml",
        }.items()
    }


def test_public_route_exposes_protocol_paths_without_private_api(manifests: dict[str, Any]) -> None:
    route = manifests["route"]["spec"]
    service = manifests["service"]
    paths = set()
    for rule in route["rules"]:
        backend = one(rule["backendRefs"])
        assert backend["name"] == service["metadata"]["name"]
        assert backend["port"] in {port["port"] for port in service["spec"]["ports"]}
        assert rule["matches"]
        for match in rule["matches"]:
            assert match["path"]["type"] == "Exact"
            path = match["path"]["value"]
            assert not path.startswith(("/v1", "/docs", "/openapi", "/health", "/consent"))
            assert path != "/"
            paths.add(path)
        # No rewrite may turn an allowed protocol path into a private API request.
        assert all(filter_["type"] == "ResponseHeaderModifier" for filter_ in rule.get("filters", []))
    # Published FastMCP/MCP protocol endpoints, not a copy of the manifest's whole route roster.
    assert {"/mcp", "/register", "/authorize", "/token", "/revoke", "/auth/callback"} <= paths
    assert "/.well-known/oauth-authorization-server" in paths
    assert "/.well-known/oauth-protected-resource/mcp" in paths


def test_workload_mcp_uses_existing_credential_and_excludes_operator_paths(manifests: dict[str, Any]) -> None:
    policy = EgressPolicy.model_validate(manifests["policy"])
    credential = manifests["credential"]
    rule = one(rule for rule in policy.spec.rules if rule.paths is not None and "/mcp" in rule.paths)
    assert rule.credential_ref is not None
    assert rule.credential_ref.name == credential["metadata"]["name"]
    assert credential["spec"]["source"] == {"authenticatedWorkloadToken": {}}
    host = one(rule.hosts)
    assert rule_matches(rule, EgressRequest(method="POST", host=host, port=80, path="/mcp"))
    for path in ("/v1/operator/connections", "/v1/operator/identities", "/register", "/token"):
        assert not any(
            rule_matches(candidate, EgressRequest(method="POST", host=host, port=80, path=path))
            for candidate in policy.spec.rules
        )


def test_oauth_credentials_are_readonly_and_not_mounted_in_migrator(manifests: dict[str, Any]) -> None:
    pod = manifests["deployment"]["spec"]["template"]["spec"]
    actions = one(pod["containers"])
    settings = one(env for env in actions["env"] if env["name"] == "AGENTPLANE_ACTIONS_OAUTH")
    secret_name = settings["valueFrom"]["secretKeyRef"]["name"]
    volume = one(volume for volume in pod["volumes"] if volume.get("secret", {}).get("secretName") == secret_name)
    mount = one(mount for mount in actions["volumeMounts"] if mount["name"] == volume["name"])
    assert mount["readOnly"] is True
    assert volume["secret"]["defaultMode"] & 0o007 == 0
    assert pod["securityContext"]["fsGroup"] == pod["securityContext"]["runAsGroup"]
    for init in pod["initContainers"]:
        assert all(mount["name"] != volume["name"] for mount in init.get("volumeMounts", []))


if __name__ == "__main__":
    pytest_bazel.main()
