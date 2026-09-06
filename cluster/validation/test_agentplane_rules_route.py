"""The deployed rules destination must not loop back into the forward proxy or admin listener."""

from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path


def manifest(path: str) -> dict[str, Any]:
    document = yaml.safe_load(get_required_path(f"_main/cluster/k8s/agentplane-staging/{path}").read_text())
    assert isinstance(document, dict)
    return document


def test_service_routes_rules_to_the_separate_declared_listener() -> None:
    service = manifest("egress/service-agentplane-egress.yaml")["spec"]
    deployment = manifest("egress/deployment-agentplane-egress.yaml")["spec"]
    pod = deployment["template"]
    container = one(c for c in pod["spec"]["containers"] if c["name"] == "proxy")
    ports = {p["name"]: p["containerPort"] for p in container["ports"]}
    http = one(p for p in service["ports"] if p["port"] == 80)
    proxy = one(p for p in service["ports"] if p["port"] == 8888)

    assert deployment["replicas"] == 1
    assert deployment["strategy"]["type"] == "Recreate"
    assert service["selector"].items() <= pod["metadata"]["labels"].items()
    assert ports[http["targetPort"]] not in (ports[proxy["targetPort"]], ports["admin"])
    assert f"--agent-api-port={ports[http['targetPort']]}" in container["args"]
    assert f"--listen-port={ports[proxy['targetPort']]}" in container["args"]


def test_default_policy_and_nonsecret_instructions_bootstrap_existing_workload_credential() -> None:
    policy = manifest("egress/egresspolicy-egress-rules.yaml")
    credential = manifest("egress/egresscredential-agentplane-workload.yaml")
    config = manifest("app/config.yaml")
    resources = manifest("egress/kustomization.yaml")["resources"]
    rule = one(policy["spec"]["rules"])
    target = one(credential["spec"]["targets"])
    instructions = config["thread_presets"][config["sandbox_presets"]["public-coder"]["thread_preset"]]["instructions"]

    assert "egresspolicy-egress-rules.yaml" in resources
    assert "egresscredential-agentplane-workload.yaml" in resources
    assert policy["metadata"]["name"] in config["default_policies"]
    assert rule["credentialRef"]["name"] == credential["metadata"]["name"]
    assert credential["spec"]["source"] == {"authenticatedWorkloadToken": {}}
    assert target == {"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}
    assert rule["methods"] == ["GET"]
    assert rule["paths"] == ["/v1/rules"]
    assert rule["clusterInternal"] is True
    assert f"http://{one(rule['hosts'])}{one(rule['paths'])}" in instructions
    assert f"agentplane-credential-{credential['metadata']['name']}" in instructions
    assert "Authorization: Bearer" in instructions


if __name__ == "__main__":
    pytest_bazel.main()
