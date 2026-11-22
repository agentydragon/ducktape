"""The MCP authority's public HTTPRoute exposes protocol paths and nothing private."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
import pytest_bazel
from cdk8s import App, Chart, Testing as Cdk8sTesting
from cdk8s_plus_34 import EnvValue
from jsonschema import Draft202012Validator
from more_itertools import one

from agentplane.net.peertls.config import (
    ACTIONS_URI,
    APP_URI,
    CENTRAL_SAMPLER_URI,
    HISTORY_SERVICE_URI,
    MCP_EDGE_URI,
    NOTIFICATIONS_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
)
from cluster.cdk8s.agentplane import (
    actions,
    central_sampler,
    environment_service,
    history_service,
    mcp_edge,
    runner_gateway,
    staging,
    workload_tls,
)
from cluster.cdk8s.agentplane.environment import SessionDelegationProps
from cluster.cdk8s.monitoring import alloy_endpoints
from cluster.cdk8s.public_coder import egress as public_coder_egress
from cluster.cdk8s.testing.manifests import config_map_yaml, find_resource
from cluster.cdk8s.testing.promtool import PromtoolRules

# types-jsonschema imports referencing in its stubs.
# gazelle:include_dep @pypi//referencing

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
    route = find_resource(staging_manifests, "HTTPRoute", "mcp-edge-mcp")
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
    route = find_resource(staging_manifests, "HTTPRoute", "actions-mcp")
    metadata = staging.ENV.actions.mcp_client_metadata
    assert metadata is not None
    edge = mcp_edge.http_service(staging.ENV.namespace)
    assert route["spec"]["hostnames"] == [metadata.url.host]
    rule = one(route["spec"]["rules"])
    assert one(rule["matches"])["path"] == {"type": "Exact", "value": metadata.url.path}
    backend = one(rule["backendRefs"])
    assert backend["name"] == edge.name
    assert backend["port"] == edge.port.number


def test_mcp_edge_identity_reader_role_exists_without_oauth(scratch_app: App) -> None:
    env = replace(staging.ENV, mcp_edge=replace(staging.ENV.mcp_edge, oauth_secret_name=None))
    chart = Chart(scratch_app, "mcp-edge", disable_resource_name_hashes=True)
    mcp_edge.McpEdge(chart, "mcp-edge", env, database_url=EnvValue.from_value("postgres://unused"))

    documents = Cdk8sTesting.synth(chart)
    role = find_resource(documents, "Role", mcp_edge.NAME)
    assert role["metadata"]["namespace"] == env.namespace
    assert role["rules"] == [
        {"apiGroups": ["agentplane.allegedly.works"], "resources": ["identities"], "verbs": ["get", "list", "watch"]}
    ]
    binding = find_resource(documents, "RoleBinding", mcp_edge.NAME)
    assert binding["metadata"]["namespace"] == env.namespace
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": mcp_edge.NAME}
    assert binding["subjects"] == [
        {"apiGroup": "", "kind": "ServiceAccount", "name": mcp_edge.NAME, "namespace": env.namespace}
    ]


def test_action_service_exposes_only_grpc(staging_manifests: list[dict[str, Any]]) -> None:
    service = find_resource(staging_manifests, "Service", actions.NAME)
    ports = {port["name"] for port in service["spec"]["ports"]}
    assert "grpc" in ports
    assert "http" not in ports
    assert not any(
        doc["kind"] == "ServiceMonitor" and doc["metadata"]["name"] == actions.NAME for doc in staging_manifests
    )


def test_workload_tls_is_mounted_when_optional_secrets_are_unconfigured(scratch_app: App) -> None:
    props = replace(
        staging.ENV.actions,
        oauth_secret_items=(),
        bearer_mcp_mounts=(),
        web_push=None,
        github_mcp_client_secret_name=None,
        mcp_servers={},
        mcp_client_metadata=None,
    )
    assert props.orchestration_v1 is not None
    env = replace(staging.ENV, actions=props)
    chart = Chart(scratch_app, "actions-without-secrets", disable_resource_name_hashes=True)
    actions.Actions(
        chart,
        "actions",
        env,
        database_url=EnvValue.from_value("postgres://unused"),
        history=history_service.service(env.namespace),
        dispatcher=runner_gateway.sessions_service(env.namespace),
        placement=None,
        registry_secret_name=None,
    )
    docs = Cdk8sTesting.synth(chart)
    pod = one(doc for doc in docs if doc["kind"] == "Deployment")["spec"]["template"]["spec"]
    container = one(container for container in pod["containers"] if container["name"] == "actions")
    tls_volume = one(
        volume for volume in pod["volumes"] if volume.get("secret", {}).get("secretName") == "actions-workload-tls"
    )
    assert any(mount["name"] == tls_volume["name"] for mount in container["volumeMounts"])


@pytest.fixture(scope="module")
def actions_without_sampler(scratch_app_factory: Callable[[], App]) -> list[dict[str, Any]]:
    env = replace(
        staging.ENV,
        namespace="agentplane-test",
        actions=replace(staging.ENV.actions, central_sampler_session_delegation=False),
    )
    chart = Chart(scratch_app_factory(), "delegation-actions", disable_resource_name_hashes=True)
    component = actions.Actions(
        chart,
        "actions",
        env,
        database_url=EnvValue.from_value("postgres://unused"),
        history=history_service.service(env.namespace),
        dispatcher=runner_gateway.sessions_service(env.namespace),
        environment_service_endpoint=environment_service.environments_service(env.namespace),
    )
    component.add_platform_resources()
    return chart.to_json()


def test_action_replica_alert(actions_without_sampler: list[dict[str, Any]], promtool: PromtoolRules) -> None:
    rule = find_resource(actions_without_sampler, "PrometheusRule", "actions-availability", namespace="agentplane-test")
    promtool("ducktape/cluster/cdk8s/agentplane/testdata/action_availability.yaml", {"actions.yaml": rule["spec"]})


def test_gaffer_policy_approves_only_scoped_branch_and_pr_requests(staging_manifests: list[dict[str, Any]]) -> None:
    policy_set = find_resource(staging_manifests, "PolicySet", "gaffer-private-branch-and-pr-creation")
    required_args = {
        "create_branch": {"branch": "work"},
        "create_pull_request": {"title": "Change", "head": "work", "base": "devel"},
    }
    actions_seen = set()
    for rule in policy_set["spec"]["rules"]:
        assert rule["type"] == "action_auto_approve"
        policy = rule["policy"]
        assert policy["type"] == "argument_schema"
        ((group, targets),) = policy["actions"].items()
        assert group == "github"
        action = one(targets)
        arguments = {"owner": "agentydragon", "repo": "gaffer-private", **required_args[action]}
        schema = Draft202012Validator(policy["schema"])
        assert schema.is_valid(arguments)
        for key in arguments:
            assert not schema.is_valid({k: v for k, v in arguments.items() if k != key}), key
        assert not schema.is_valid({**arguments, "owner": "another-owner"})
        assert not schema.is_valid({**arguments, "repo": "another-repo"})
        assert not schema.is_valid({**arguments, "unexpected": True})
        if action == "create_pull_request":
            assert schema.is_valid({**arguments, "maintainer_can_modify": False})
            assert not schema.is_valid({**arguments, "maintainer_can_modify": True})
            assert not schema.is_valid({**arguments, "reviewers": ["reviewer"]})
        actions_seen.add(action)
    assert actions_seen == required_args.keys()


@pytest.fixture(scope="module")
def staging_settings(staging_manifests: list[dict[str, Any]]) -> dict[str, Any]:
    return config_map_yaml(staging_manifests, f"{actions.NAME}-settings", "settings.yaml")


def test_staging_sampler_delegation_and_credential_files(
    staging_manifests: list[dict[str, Any]], staging_settings: dict[str, Any]
) -> None:
    deployment = find_resource(staging_manifests, "Deployment", "actions")
    container = one(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "actions")
    assert staging_settings["session_delegation_service"] == CENTRAL_SAMPLER_URI
    action_policy = find_resource(staging_manifests, "CiliumNetworkPolicy", actions.NAME)
    sampler_labels = central_sampler.service(staging.ENV.namespace).pods.cilium
    sampler_rule = one(
        rule
        for rule in action_policy["spec"]["ingress"]
        if any(endpoint.get("matchLabels") == sampler_labels for endpoint in rule.get("fromEndpoints", []))
    )
    assert any(
        port == {"port": str(actions.service(staging.ENV.namespace).pod_port), "protocol": "TCP"}
        for port_rule in sampler_rule.get("toPorts", [])
        for port in port_rule.get("ports", [])
    )
    assert "database_url" not in staging_settings
    assert "private_key_pem" not in staging_settings["web_push"]
    assert staging_settings["mcp_servers"]["github"]["client_id_file"] == "/etc/agentplane-github/client_id"
    assert (
        "client_id" not in staging_settings["mcp_servers"]["github"]
        or staging_settings["mcp_servers"]["github"]["client_id"] is None
    )
    names = {entry["name"] for entry in container["env"]}
    assert "AGENTPLANE_ACTIONS_MCP_SERVERS__GITHUB__CLIENT_ID" not in names


def test_action_service_allows_only_configured_service_callers(
    staging_manifests: list[dict[str, Any]], staging_settings: dict[str, Any]
) -> None:
    deployment = find_resource(staging_manifests, "Deployment", "actions")
    pod = deployment["spec"]["template"]["spec"]
    container = one(item for item in pod["containers"] if item["name"] == "actions")

    assert staging_settings["service_readers"] == [NOTIFICATIONS_URI]
    assert staging_settings["operator_services"] == [APP_URI]
    assert staging_settings["connection_delegation_service"] == MCP_EDGE_URI
    assert {auth["mode"] for auth in staging_settings["rpc"]["auth"]} == {"spiffe"}
    spiffe = one(auth for auth in staging_settings["rpc"]["auth"] if auth["mode"] == "spiffe")
    assert spiffe["ca_file"] == workload_tls.TRUST_BUNDLE_FILE

    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    mounted = {mount["mountPath"]: volumes[mount["name"]] for mount in container["volumeMounts"]}
    assert mounted["/run/agentplane/workload-tls"]["secret"]["secretName"] == "actions-workload-tls"
    assert mounted["/run/agentplane/workload-trust"]["configMap"]["name"] == "agentplane-workload-ca-bundle"


def test_public_coder_namespace_is_not_an_action_service_caller(
    staging_manifests: list[dict[str, Any]], staging_settings: dict[str, Any]
) -> None:
    assert public_coder_egress.NAMESPACE not in staging_settings["caller_service_account_namespaces"]
    assert not any(
        doc["kind"] == "WorkloadCredential" and doc["metadata"]["namespace"] == public_coder_egress.NAMESPACE
        for doc in staging_manifests
    )
    assert not any(
        doc["kind"] == "Role"
        and doc["metadata"]["namespace"] == public_coder_egress.NAMESPACE
        and doc["metadata"]["name"] == actions.NAME
        for doc in staging_manifests
    )
    assert not any(
        doc["kind"] == "RoleBinding"
        and doc["metadata"]["namespace"] == public_coder_egress.NAMESPACE
        and doc["metadata"]["name"] == actions.NAME
        for doc in staging_manifests
    )


def test_actions_deployment_rolls(staging_manifests: list[dict[str, Any]]) -> None:
    assert find_resource(staging_manifests, "Deployment", "actions")["spec"]["strategy"]["type"] == "RollingUpdate"


def test_actions_push_otlp_metrics(staging_manifests: list[dict[str, Any]]) -> None:
    deployment = find_resource(staging_manifests, "Deployment", "actions")
    container = one(item for item in deployment["spec"]["template"]["spec"]["containers"] if item["name"] == "actions")
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["OTEL_METRICS_EXPORTER"] == "otlp"
    assert env["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] == "cumulative"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == alloy_endpoints.OTLP_GRPC_ENDPOINT


def test_disabling_sampler_delegation_removes_its_caller_and_ingress_rule(
    actions_without_sampler: list[dict[str, Any]],
) -> None:
    settings = config_map_yaml(actions_without_sampler, f"{actions.NAME}-settings", "settings.yaml")
    assert not settings.get("session_delegation_service")
    policy = find_resource(actions_without_sampler, "CiliumNetworkPolicy", actions.NAME)
    sampler = central_sampler.service(policy["metadata"]["namespace"]).pods.cilium
    assert all(
        endpoint.get("matchLabels") != sampler
        for rule in policy["spec"]["ingress"]
        for endpoint in rule.get("fromEndpoints", [])
    )


def test_staging_session_delegation_tools_and_peer_credentials(
    staging_manifests: list[dict[str, Any]], staging_settings: dict[str, Any]
) -> None:
    namespace = staging.ENV.namespace
    groups = staging_settings["action_groups"]
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
    dispatcher = runner_gateway.sessions_service(namespace)
    history = history_service.service(namespace)
    environments = environment_service.environments_service(namespace)
    assert staging_settings["environment_service"]["target"] == environments.authority
    assert staging_settings["environment_service"]["tls"]["server_uri"] == SANDBOX_CONTROLLER_URI
    deployment = find_resource(staging_manifests, "Deployment", actions.NAME)
    pod = deployment["spec"]["template"]["spec"]
    container = one(item for item in pod["containers"] if item["name"] == actions.NAME)
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    mounts = {mount["mountPath"]: volumes[mount["name"]] for mount in container["volumeMounts"]}
    certificate = find_resource(staging_manifests, "Certificate", f"{actions.NAME}-workload")
    assert certificate["spec"]["uris"] == [ACTIONS_URI]
    for peer_name, server_uri, target in [
        ("session_dispatcher", SESSION_DISPATCHER_URI, dispatcher.authority),
        ("session_delegation_dispatcher", SESSION_DISPATCHER_URI, dispatcher.authority),
        ("history_service", HISTORY_SERVICE_URI, history.authority),
        ("session_delegation_history", HISTORY_SERVICE_URI, history.authority),
    ]:
        peer = staging_settings[peer_name]
        assert peer["target"] == target
        assert peer["token_file"] == ""
        tls = peer["tls"]
        assert tls["server_uri"] == server_uri
        assert certificate["spec"]["uris"] == [tls["client_uri"]]
        for path_key in ["cert_file", "key_file"]:
            mount = mounts[tls[path_key].rsplit("/", 1)[0]]
            assert mount["secret"]["secretName"] == certificate["spec"]["secretName"]
        trust_mount = mounts[tls["ca_file"].rsplit("/", 1)[0]]
        assert trust_mount["configMap"]["name"] == "agentplane-workload-ca-bundle"
        assert peer["request_timeout_s"] == (
            actions._DELEGATED_PEER_TIMEOUT_SECONDS
            if peer_name.startswith("session_delegation_")
            else actions._PEER_TIMEOUT_SECONDS
        )


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
