"""The MCP authority's public HTTPRoute exposes protocol paths and nothing private."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting
from cdk8s_plus_34 import EnvValue
from more_itertools import one

from agentplane.environment_runtime_transport.config import HISTORY_SERVICE_URI, SESSION_DISPATCHER_URI
from cluster.cdk8s.agentplane import (
    actions,
    history_service,
    mcp_edge,
    session_dispatcher,
    staging,
    telemetry,
    workload_tls,
)
from cluster.cdk8s.agentplane.environment import SessionDelegationProps

# REST, docs, health and consent live behind the Action API's own authentication, never on
# the public MCP origin.
_PRIVATE_PREFIXES = ("/v1", "/docs", "/openapi", "/health", "/consent")
# Published FastMCP/MCP protocol endpoints, not a copy of the route's whole roster.
_MCP_PROTOCOL_PATHS = {
    "/mcp",
    "/register",
    "/authorize",
    "/token",
    "/revoke",
    "/auth/callback",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource/mcp",
}


def test_public_route_exposes_protocol_paths_only(staging_manifests: list[dict[str, Any]]) -> None:
    route = one(
        doc for doc in staging_manifests if doc["kind"] == "HTTPRoute" and doc["metadata"]["name"] == "mcp-edge-mcp"
    )
    paths = set()
    for rule in route["spec"]["rules"]:
        for match in rule["matches"]:
            assert match["path"]["type"] == "Exact"
            paths.add(match["path"]["value"])
        # No rewrite may turn an allowed protocol path into a private API request.
        assert all(filter_["type"] == "ResponseHeaderModifier" for filter_ in rule.get("filters", []))
    assert not any(path == "/" or path.startswith(_PRIVATE_PREFIXES) for path in paths)
    assert paths >= _MCP_PROTOCOL_PATHS


def test_action_client_metadata_route_targets_mcp_edge(staging_manifests: list[dict[str, Any]]) -> None:
    route = one(
        doc for doc in staging_manifests if doc["kind"] == "HTTPRoute" and doc["metadata"]["name"] == "actions-mcp"
    )
    metadata = staging.ENV.actions.mcp_client_metadata
    assert metadata is not None
    edge = mcp_edge.http_service(staging.ENV.namespace)
    assert route["spec"]["hostnames"] == [metadata.url.host]
    rule = one(route["spec"]["rules"])
    assert one(rule["matches"])["path"] == {"type": "Exact", "value": metadata.url.path}
    backend = one(rule["backendRefs"])
    assert backend["name"] == edge.name
    assert backend["port"] == edge.port.number


def test_action_service_exposes_only_grpc(staging_manifests: list[dict[str, Any]]) -> None:
    service = one(
        doc for doc in staging_manifests if doc["kind"] == "Service" and doc["metadata"]["name"] == actions.NAME
    )
    ports = {port["name"] for port in service["spec"]["ports"]}
    assert "grpc" in ports
    assert "http" not in ports
    assert not any(
        doc["kind"] == "ServiceMonitor" and doc["metadata"]["name"] == actions.NAME for doc in staging_manifests
    )


def test_action_replica_alert_uses_environment_namespace(scratch_app: App) -> None:
    env = replace(staging.ENV, namespace="agentplane-test")
    chart = Chart(scratch_app, "actions", disable_resource_name_hashes=True)
    actions.Actions(
        chart,
        "actions",
        env,
        database_url=EnvValue.from_value("postgres://unused"),
        history=history_service.service(env.namespace),
        history_audience=history_service.TOKEN_AUDIENCE,
        dispatcher=session_dispatcher.service(env.namespace),
        dispatcher_audience=session_dispatcher.TOKEN_AUDIENCE,
    ).add_platform_resources()

    rule = one(doc for doc in Cdk8sTesting.synth(chart) if doc["kind"] == "PrometheusRule")
    assert rule["metadata"]["name"] == f"{actions.NAME}-availability"
    assert rule["metadata"]["namespace"] == env.namespace
    alert = one(rule["spec"]["groups"][0]["rules"])
    assert alert["alert"] == "AgentplaneActionServiceReplicasUnavailable"
    assert alert["expr"] == (
        f"max by (cluster, namespace, deployment) "
        f'(kube_deployment_spec_replicas{{namespace="{env.namespace}", deployment="{actions.NAME}"}}) '
        f"- max by (cluster, namespace, deployment) "
        f'(kube_deployment_status_replicas_available{{namespace="{env.namespace}", deployment="{actions.NAME}"}}) > 0'
    )
    assert alert["for"] == "10m"
    assert alert["labels"] == {"severity": "warning"}


def _gaffer_approval(manifests: list[dict[str, Any]], action: str) -> dict[str, Any]:
    policy = one(
        doc
        for doc in manifests
        if doc["kind"] == "PolicySet" and doc["metadata"]["name"] == "gaffer-private-branch-and-pr-creation"
    )
    return one(rule["policy"] for rule in policy["spec"]["rules"] if rule["policy"]["actions"] == {"github": [action]})


def test_gaffer_branch_policy_approves_only_create_branch(staging_manifests: list[dict[str, Any]]) -> None:
    approval = _gaffer_approval(staging_manifests, "create_branch")
    assert approval["type"] == "argument_schema"
    assert approval["schema"]["properties"]["owner"] == {"const": "agentydragon"}
    assert approval["schema"]["properties"]["repo"] == {"const": "gaffer-private"}
    assert approval["schema"]["required"] == ["owner", "repo", "branch"]
    assert approval["schema"]["additionalProperties"] is False


def test_gaffer_pr_policy_approves_only_create_pull_request(staging_manifests: list[dict[str, Any]]) -> None:
    approval = _gaffer_approval(staging_manifests, "create_pull_request")
    assert approval["type"] == "argument_schema"
    assert approval["schema"]["properties"]["owner"] == {"const": "agentydragon"}
    assert approval["schema"]["properties"]["repo"] == {"const": "gaffer-private"}
    assert approval["schema"]["required"] == ["owner", "repo", "title", "head", "base"]
    assert approval["schema"]["properties"]["maintainer_can_modify"] == {"const": False}
    assert "reviewers" not in approval["schema"]["properties"]
    assert approval["schema"]["additionalProperties"] is False


def test_cross_owner_reader_is_explicit_and_json_encoded(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = one(
        doc for doc in staging_manifests if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "actions"
    )
    container = one(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "actions")
    readers = one(item for item in container["env"] if item["name"] == "AGENTPLANE_ACTIONS_READER_ACCOUNTS")
    assert json.loads(readers["value"]) == [{"namespace": staging.ENV.namespace, "name": "notifications"}]
    assert not any(arg.startswith("--reader-accounts") for arg in container.get("args", []))


def test_actions_migrate_before_serving_and_roll(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = one(
        doc for doc in staging_manifests if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "actions"
    )
    pod = deployment["spec"]["template"]["spec"]
    server = one(item for item in pod["containers"] if item["name"] == "actions")
    assert deployment["spec"]["strategy"]["type"] == "RollingUpdate"
    assert not pod.get("initContainers")
    startup = server["startupProbe"]["exec"]["command"]
    readiness = server["readinessProbe"]["exec"]["command"]
    assert startup[:2] == readiness[:2] == ["/healthprobe", "--target"]
    assert "127.0.0.1:8080" in startup
    assert "spiffe://agentplane/actions" in startup
    assert "--service" in startup
    assert "livez" in startup


def test_actions_push_otlp_metrics(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = one(
        doc for doc in staging_manifests if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "actions"
    )
    container = one(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "actions")
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["OTEL_METRICS_EXPORTER"] == "otlp"
    assert env["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] == "cumulative"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == telemetry.OTLP_GRPC_ENDPOINT


def test_session_delegation_offers_all_reviewed_direct_tools(scratch_app: App) -> None:
    props = SessionDelegationProps(
        direct_tools_by_group={
            actions.ENVIRONMENTS_GROUP: frozenset({"list", "get", "resume"}),
            actions.SESSIONS_GROUP: frozenset(
                {"create", "submit_input", "observe_input", "get", "read_turns", "cancel_input", "interrupt_turn"}
            ),
        }
    )
    env = replace(staging.ENV, actions=replace(staging.ENV.actions, session_delegation=props))
    chart = Chart(scratch_app, "delegation-actions", disable_resource_name_hashes=True)
    action_component = actions.Actions(
        chart,
        "actions",
        env,
        database_url=EnvValue.from_value("postgres://unused"),
        history=history_service.service(env.namespace),
        history_audience=history_service.TOKEN_AUDIENCE,
        dispatcher=session_dispatcher.service(env.namespace),
        dispatcher_audience=session_dispatcher.TOKEN_AUDIENCE,
    )
    action_component.add_platform_resources()
    documents = Cdk8sTesting.synth(chart)
    config = one(
        doc for doc in documents if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == f"{actions.NAME}-settings"
    )
    settings = yaml.safe_load(config["data"]["settings.yaml"])
    groups = settings["action_groups"]
    assert groups["environments"]["executor"]["kind"] == "session_delegation"
    assert set(groups["environments"]["direct_tools"]) == {"get", "list", "resume"}
    assert groups["sessions"]["executor"]["kind"] == "session_delegation"
    assert set(groups["sessions"]["direct_tools"]) == {
        "cancel_input",
        "get",
        "interrupt_turn",
        "observe_input",
        "create",
        "read_turns",
        "submit_input",
    }
    dispatcher = session_dispatcher.service(env.namespace)
    history = history_service.service(env.namespace)
    assert settings["session_delegation_dispatcher"] == {
        "target": f"{dispatcher.fqdn}:{dispatcher.port.number}",
        "token_file": "/var/run/secrets/actions/dispatcher",
        "request_timeout_s": actions._DELEGATED_PEER_TIMEOUT_SECONDS,
        "tls": {"ca_file": workload_tls.TRUST_BUNDLE_FILE, "server_uri": SESSION_DISPATCHER_URI},
    }
    assert settings["session_delegation_history"] == {
        "target": f"{history.fqdn}:{history.pod_port}",
        "token_file": "/var/run/secrets/actions/history",
        "request_timeout_s": actions._DELEGATED_PEER_TIMEOUT_SECONDS,
        "tls": {"ca_file": workload_tls.TRUST_BUNDLE_FILE, "server_uri": HISTORY_SERVICE_URI},
    }
    deployment = one(
        doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == actions.NAME
    )
    tokens = {
        source["serviceAccountToken"]["path"]: source["serviceAccountToken"]
        for volume in deployment["spec"]["template"]["spec"]["volumes"]
        if volume["name"] == "peer-tokens"
        for source in volume["projected"]["sources"]
    }
    assert tokens["dispatcher"]["audience"] == session_dispatcher.TOKEN_AUDIENCE
    assert tokens["history"]["audience"] == history_service.TOKEN_AUDIENCE


def test_session_delegation_network_caller_requires_the_group() -> None:
    env_without_actions = replace(
        staging.ENV, actions=replace(staging.ENV.actions, orchestration_v1=None, session_delegation=None)
    )
    assert actions.session_delegation_callers(env_without_actions) == {}

    env_with_session_delegation = replace(
        env_without_actions,
        actions=replace(
            env_without_actions.actions,
            session_delegation=SessionDelegationProps(
                direct_tools_by_group={actions.ENVIRONMENTS_GROUP: frozenset(), actions.SESSIONS_GROUP: frozenset()}
            ),
        ),
    )
    caller = one(actions.session_delegation_callers(env_with_session_delegation))
    assert caller.namespace == env_with_session_delegation.namespace
    assert caller.name == actions.NAME


if __name__ == "__main__":
    pytest_bazel.main()
