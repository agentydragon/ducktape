"""The deployed rules destination must not loop back into the forward proxy or admin listener."""

from typing import Any

import pytest_bazel
from more_itertools import one

from cluster.cdk8s.agentplane import staging_config

# pytest_plugins loads cluster.validation.agentplane_fixtures by name; gazelle cannot see
# the dependency.
# gazelle:include_dep //cluster/validation:agentplane_fixtures
pytest_plugins = ("cluster.validation.agentplane_fixtures",)


def egress_object(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


def test_service_routes_rules_to_the_separate_declared_listener(
    agentplane_services: dict[str, list[dict[str, Any]]],
) -> None:
    documents = agentplane_services["agentplane-staging"]
    service = egress_object(documents, "Service", "agentplane-egress")["spec"]
    deployment = egress_object(documents, "Deployment", "agentplane-egress")["spec"]
    pod = deployment["template"]
    container = one(c for c in pod["spec"]["containers"] if c["name"] == "proxy")
    ports = {p["name"]: p["containerPort"] for p in container["ports"]}
    http = one(p for p in service["ports"] if p["port"] == 80)
    proxy = one(p for p in service["ports"] if p["port"] == 8888)

    assert service["selector"].items() <= pod["metadata"]["labels"].items()
    assert http["targetPort"] not in (proxy["targetPort"], ports["admin"])
    assert f"--agent-api-port={http['targetPort']}" in container["args"]
    assert f"--listen-port={proxy['targetPort']}" in container["args"]


def test_public_coder_defaults_and_nonsecret_instructions_bootstrap_workload_credentials(
    agentplane_services: dict[str, list[dict[str, Any]]],
) -> None:
    documents = agentplane_services["agentplane-staging"]
    policy = egress_object(documents, "EgressPolicy", "basic")
    credential = egress_object(documents, "EgressCredential", "agentplane-workload")
    config = staging_config.config()
    rules = {one(rule["hosts"]): rule for rule in policy["spec"]["rules"]}
    rules_host = "agentplane-egress.agentplane-staging.svc.cluster.local"
    actions_host = "agentplane-actions.agentplane-staging.svc.cluster.local"
    llm_host = "agentplane-llm-ingress.agentplane-staging.svc.cluster.local"
    rule = rules[rules_host]
    target = one(credential["spec"]["targets"])

    assert policy["metadata"]["name"] == "basic"
    assert config["sandbox_presets"]["public-coder"]["policies"] == ["basic", "github-public"]
    assert config["default_policies"] == ["basic"]
    assert set(rules) == {rules_host, actions_host, llm_host}
    assert rules[llm_host]["methods"] == ["GET", "POST"]
    assert rules[actions_host]["methods"] == ["GET", "POST"]
    assert rules[actions_host]["paths"] == [
        "/mcp",
        "/openapi.json",
        "/v1/action-groups",
        "/v1/action-groups/**",
        "/v1/action-policy",
        "/v1/action-requests",
        "/v1/action-requests/**",
    ]
    assert rule["credentialRef"]["name"] == credential["metadata"]["name"]
    assert credential["spec"]["source"] == {"authenticatedWorkloadToken": {}}
    assert target == {"header": "Authorization", "method": "schemeToken", "scheme": "Bearer"}
    assert rule["methods"] == ["GET"]
    assert rule["paths"] == ["/openapi.json", "/v1/rules"]
    assert rule["clusterInternal"] is True
    assert config["agent_egress_api_url"] == f"http://{one(rule['hosts'])}"


def test_staging_egress_retains_one_available_replica_during_voluntary_changes(
    agentplane_services: dict[str, list[dict[str, Any]]],
) -> None:
    documents = agentplane_services["agentplane-staging"]
    deployment = egress_object(documents, "Deployment", "agentplane-egress")["spec"]
    budget = egress_object(documents, "PodDisruptionBudget", "agentplane-egress")["spec"]
    pod = deployment["template"]
    assert deployment["replicas"] == 2  # Staging capacity requested by the operator.
    assert deployment["strategy"]["type"] == "RollingUpdate"
    rolling = deployment["strategy"]["rollingUpdate"]
    assert deployment["replicas"] - rolling["maxUnavailable"] >= budget["minAvailable"] == 1
    assert rolling["maxSurge"] == 1
    assert budget["selector"]["matchLabels"].items() <= pod["metadata"]["labels"].items()
    for spread in pod["spec"]["topologySpreadConstraints"]:
        assert spread["whenUnsatisfiable"] == "ScheduleAnyway"  # Placement must not block scheduling.
        assert spread["labelSelector"]["matchLabels"].items() <= pod["metadata"]["labels"].items()
        assert spread["topologyKey"] == "kubernetes.io/hostname"
    assert pod["spec"]["terminationGracePeriodSeconds"] >= 60
    container = one(c for c in pod["spec"]["containers"] if c["name"] == "proxy")
    assert container["readinessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/livez"


if __name__ == "__main__":
    pytest_bazel.main()
