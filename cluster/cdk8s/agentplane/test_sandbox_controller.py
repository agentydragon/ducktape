"""The Controller authenticates Gateway calls with its mounted service SVID."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
from more_itertools import one

from agentplane.action_service.settings import Settings as ActionSettings
from agentplane.app.settings import Settings as AppSettings
from agentplane.environments.settings import Settings
from agentplane.net.peertls.config import (
    ACTIONS_URI,
    APP_URI,
    CENTRAL_SAMPLER_URI,
    GATEWAY_RUNTIME_PROXY_URI,
    GATEWAY_SESSIONS_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
)
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.agentplane import (
    actions,
    app,
    central_sampler,
    egress,
    environment_sandbox,
    environment_service,
    notifications,
    runner_gateway,
    sandbox_controller,
    staging,
    workload_tls,
)
from cluster.cdk8s.testing.manifests import config_map_yaml, find_resource


@pytest.fixture(scope="module")
def controller_settings(staging_manifests: list[dict[str, Any]]) -> Settings:
    return Settings.model_validate(
        config_map_yaml(staging_manifests, f"{sandbox_controller.NAME}-settings", "config.yaml")
    )


def _environments_grpc_target(staging_manifests: list[dict[str, Any]]) -> str:
    service = find_resource(
        staging_manifests, "Service", environment_service.environments_service(staging.ENV.namespace).name
    )
    port = one(port for port in service["spec"]["ports"] if port["name"] == "grpc")
    return f"{service['metadata']['name']}.{service['metadata']['namespace']}.svc.cluster.local:{port['port']}"


def test_environments_selects_controller_endpoints_for_the_single_metrics_scrape(
    staging_manifests: list[dict[str, Any]],
) -> None:
    service = find_resource(staging_manifests, "Service", sandbox_controller.NAME)
    deployment = find_resource(staging_manifests, "Deployment", sandbox_controller.NAME)
    assert service["spec"]["selector"].items() <= deployment["spec"]["template"]["metadata"]["labels"].items()
    assert {port["name"]: port["port"] for port in service["spec"]["ports"]} == {"metrics": 8080, "grpc": 8082}
    monitor = find_resource(staging_manifests, "ServiceMonitor", "sandbox-controller")
    assert one(monitor["spec"]["endpoints"])["port"] == "metrics"
    selector = monitor["spec"]["selector"]["matchLabels"]
    assert [
        doc["metadata"]["name"]
        for doc in staging_manifests
        if doc["kind"] == "Service" and selector.items() <= doc["metadata"].get("labels", {}).items()
    ] == [sandbox_controller.NAME]


def test_controller_uses_its_mounted_service_certificate(
    staging_manifests: list[dict[str, Any]], controller_settings: Settings
) -> None:
    name = sandbox_controller.NAME
    config_env = Settings.config_file_env
    deployment = find_resource(staging_manifests, "Deployment", name)
    pod = deployment["spec"]["template"]["spec"]
    container = one(pod["containers"])
    env = {entry["name"]: entry for entry in container["env"]}

    assert {key for key in env if key.startswith(("AGENTPLANE_SANDBOX_CONTROLLER_", "AGENTPLANE_ENVIRONMENTS_"))} == {
        config_env
    }
    certificate = find_resource(staging_manifests, "Certificate", f"{name}-client-workload")
    client = controller_settings.runner_gateway_tls
    assert client is not None
    assert client.server_uri == client.client_uri == SANDBOX_CONTROLLER_URI
    assert certificate["spec"]["uris"] == [client.client_uri]
    assert set(certificate["spec"]["usages"]) == {"digital signature", "client auth", "server auth"}
    volume = one(
        v for v in pod["volumes"] if v.get("secret", {}).get("secretName") == certificate["spec"]["secretName"]
    )
    mount = one(m for m in container["volumeMounts"] if m["name"] == volume["name"])
    cert_path = Path(mount["mountPath"])
    assert mount["readOnly"]
    assert client.cert_file == cert_path / "tls.crt"
    assert client.key_file == cert_path / "tls.key"

    assert controller_settings.runner_security.mode == "mtls"
    listener = controller_settings.runner_security.tls
    assert listener.cert_file == str(client.cert_file)
    assert listener.key_file == str(client.key_file)
    assert listener.ca_file == str(client.ca_file) == workload_tls.TRUST_BUNDLE_FILE
    assert listener.client_ca_file is None


def test_controller_and_runtime_policies_admit_the_runtime_listener(staging_manifests: list[dict[str, Any]]) -> None:
    name = sandbox_controller.NAME
    controller_policy = find_resource(staging_manifests, "CiliumNetworkPolicy", name)["spec"]
    runtime_label = environment_sandbox.pods("agentplane").selector["app.kubernetes.io/name"]
    assert any(
        endpoint["matchLabels"].get("app.kubernetes.io/name") == runtime_label
        and any(
            port["port"] == str(environment_sandbox.ENVIRONMENT_RUNTIME_SERVICE_PORT)
            for target in rule.get("toPorts", [])
            for port in target["ports"]
        )
        for rule in controller_policy["egress"]
        for endpoint in rule.get("toEndpoints", [])
    )

    runtime_listener = find_resource(staging_manifests, "CiliumNetworkPolicy", "environment-tools-listener")["spec"]
    assert any(
        endpoint["matchLabels"].get("app.kubernetes.io/name") == name
        and any(
            port["port"] == str(environment_sandbox.ENVIRONMENT_RUNTIME_SERVICE_PORT)
            for target in rule.get("toPorts", [])
            for port in target["ports"]
        )
        for rule in runtime_listener["ingress"]
        for endpoint in rule.get("fromEndpoints", [])
    )


def test_controller_rbac_keeps_only_leader_election_lease_access(staging_manifests: list[dict[str, Any]]) -> None:
    role = find_resource(staging_manifests, "Role", sandbox_controller.NAME)
    resources = {resource for rule in role["rules"] for resource in rule["resources"]}
    assert "services" not in resources
    assert one(rule for rule in role["rules"] if rule["resources"] == ["leases"])["verbs"] == [
        "get",
        "create",
        "update",
    ]


def test_environment_receiver_owns_authority_and_uses_controller_certificate(
    staging_manifests: list[dict[str, Any]], controller_settings: Settings
) -> None:
    receiver = controller_settings.environment_service
    assert receiver is not None
    assert set(receiver.service_readers) == {APP_URI}
    assert set(receiver.service_managers) == {APP_URI}
    assert set(receiver.service_launchers) == {ACTIONS_URI}
    assert set(receiver.service_operators) == {APP_URI}
    assert receiver.service_delegation is not None
    assert [actor.spiffe_id for actor in receiver.service_delegation.actors] == [ACTIONS_URI]
    assert set(receiver.service_delegation.methods) == {
        "/agentplane.environments.v1.EnvironmentService/ListEnvironments",
        "/agentplane.environments.v1.EnvironmentService/GetEnvironment",
        "/agentplane.environments.v1.EnvironmentService/WatchEnvironment",
        "/agentplane.environments.v1.EnvironmentService/ResumeEnvironment",
    }
    assert controller_settings.runner_security.mode == "mtls"
    assert receiver.rpc.tls is not None
    assert str(receiver.rpc.tls.cert_file) == controller_settings.runner_security.tls.cert_file
    assert str(receiver.rpc.tls.key_file) == controller_settings.runner_security.tls.key_file
    service = find_resource(staging_manifests, "Service", sandbox_controller.NAME)
    port = one(port for port in service["spec"]["ports"] if port["name"] == "grpc")
    assert int(receiver.rpc.listen_address.rsplit(":", 1)[1]) == port["targetPort"]
    deployment = find_resource(staging_manifests, "Deployment", sandbox_controller.NAME)
    container = one(deployment["spec"]["template"]["spec"]["containers"])
    assert port["targetPort"] in {entry["containerPort"] for entry in container["ports"]}


def test_actions_environment_peer_matches_receiver_and_separates_dispatcher(
    staging_manifests: list[dict[str, Any]], controller_settings: Settings
) -> None:
    settings = ActionSettings.model_validate(config_map_yaml(staging_manifests, "actions-settings", "settings.yaml"))
    peer = settings.environment_service
    assert peer is not None
    assert peer.target == _environments_grpc_target(staging_manifests)
    receiver = controller_settings.environment_service
    assert receiver is not None
    assert peer.tls is not None
    assert peer.tls.client_uri in receiver.service_launchers
    assert receiver.service_delegation is not None
    assert peer.tls.client_uri in {actor.spiffe_id for actor in receiver.service_delegation.actors}
    certificate = find_resource(staging_manifests, "Certificate", f"{sandbox_controller.NAME}-client-workload")
    assert certificate["spec"]["uris"] == [peer.tls.server_uri]
    client_certificate = find_resource(staging_manifests, "Certificate", f"{actions.NAME}-workload")
    assert client_certificate["spec"]["uris"] == [peer.tls.client_uri]
    for dispatcher in (settings.session_dispatcher, settings.session_delegation_dispatcher):
        assert dispatcher is not None
        assert dispatcher.target != peer.target
        assert dispatcher.tls is not None
        assert dispatcher.tls.server_uri == SESSION_DISPATCHER_URI


@pytest.mark.parametrize(
    ("launcher", "allowed"),
    [
        (None, False),
        (ServiceAccountRef(namespace="receiver-test", name="actions"), True),
        (ServiceAccountRef(namespace="other-test", name="actions"), False),
        (ServiceAccountRef(namespace="receiver-test", name="other-launcher"), False),
    ],
)
def test_actions_service_launcher_requires_its_existing_namespaced_launcher_grant(
    launcher: ServiceAccountRef | None, allowed: bool
) -> None:
    authority = sandbox_controller.environment_service_authority(
        replace(staging.ENV, namespace="receiver-test"),
        launchers=() if launcher is None else (launcher,),
        operator_service_callers=frozenset(),
        wake_service_callers=frozenset(),
        delegation_actor=None,
    )
    assert authority.service_launchers == (frozenset({ACTIONS_URI}) if allowed else frozenset())
    assert ACTIONS_URI not in authority.service_managers
    assert ACTIONS_URI not in authority.service_operators


def test_app_environment_peer_matches_receiver_and_mounted_service_identity(
    staging_manifests: list[dict[str, Any]], controller_settings: Settings
) -> None:
    settings = AppSettings.model_validate(config_map_yaml(staging_manifests, "app-config", "config.yaml"))
    peer = settings.environment_service
    assert peer is not None
    assert peer.target == _environments_grpc_target(staging_manifests)
    certificate = find_resource(staging_manifests, "Certificate", f"{sandbox_controller.NAME}-client-workload")
    assert certificate["spec"]["uris"] == [peer.tls.server_uri]
    app_certificate = find_resource(staging_manifests, "Certificate", f"{app.NAME}-workload")
    assert app_certificate["spec"]["uris"] == [peer.tls.client_uri]
    assert peer.tls.client_uri == APP_URI
    receiver = controller_settings.environment_service
    assert receiver is not None
    assert peer.tls.client_uri in receiver.service_readers
    assert peer.tls.client_uri in receiver.service_managers
    assert peer.tls.client_uri in receiver.service_operators
    assert settings.session_dispatcher_tls is not None
    assert settings.session_dispatcher_tls.server_uri == SESSION_DISPATCHER_URI
    assert settings.session_dispatcher_target != peer.target
    assert settings.runner_gateway_target == peer.target


def test_environment_receiver_can_watch_policy_and_mutate_only_desired_state(
    staging_manifests: list[dict[str, Any]],
) -> None:
    rules = find_resource(staging_manifests, "Role", sandbox_controller.NAME)["rules"]

    def verbs(group: str, resource: str) -> set[str]:
        return {
            verb
            for rule in rules
            if group in rule["apiGroups"] and resource in rule["resources"]
            for verb in rule["verbs"]
        }

    assert verbs("agentplane.allegedly.works", "sandboxes") == {
        "get",
        "list",
        "watch",
        "create",
        "update",
        "patch",
        "delete",
    }
    for resource in ("identities", "policysets", "policybindings"):
        assert verbs("agentplane.allegedly.works", resource) == {"get", "list", "watch"}
    assert verbs("agentplane.allegedly.works", "policybindings/status") == {"patch"}
    assert verbs("", "serviceaccounts") == {"get", "list", "watch"}


def test_runtime_proxy_receiver_limits_service_grants_and_separates_binding_identity(
    staging_manifests: list[dict[str, Any]], controller_settings: Settings
) -> None:
    name = sandbox_controller.NAME
    assert controller_settings.environment_service is not None
    proxy = controller_settings.environment_service.runtime_proxy
    assert proxy is not None
    assert "audience" not in proxy.model_dump(mode="json")
    assert proxy.readers == ()
    assert set(proxy.session_callers) == {GATEWAY_SESSIONS_URI}
    assert set(proxy.controller_callers) == {SANDBOX_CONTROLLER_URI}
    assert set(proxy.executor_callers) == {CENTRAL_SAMPLER_URI}
    assert proxy.runner_security.mode == "mtls"
    assert controller_settings.runner_security.mode == "mtls"
    assert proxy.runner_security.tls.cert_file != controller_settings.runner_security.tls.cert_file
    certificate = find_resource(staging_manifests, "Certificate", f"{name}-runtime-proxy-client-workload")
    assert certificate["spec"]["uris"] == [GATEWAY_RUNTIME_PROXY_URI]
    deployment = find_resource(staging_manifests, "Deployment", name)
    pod = deployment["spec"]["template"]["spec"]
    volume = one(
        volume
        for volume in pod["volumes"]
        if volume.get("secret", {}).get("secretName") == certificate["spec"]["secretName"]
    )
    mount = one(mount for mount in one(pod["containers"])["volumeMounts"] if mount["name"] == volume["name"])
    assert mount["readOnly"]
    assert proxy.runner_security.tls.cert_file == f"{mount['mountPath']}/tls.crt"


def test_environment_receiver_network_reachability_matches_enabled_callers(
    staging_manifests: list[dict[str, Any]],
) -> None:
    receiver = environment_service.environments_service(staging.ENV.namespace)
    callers = [
        app.service(staging.ENV.namespace).pods,
        egress.proxy(staging.ENV.namespace).pods,
        runner_gateway.sessions_service(staging.ENV.namespace).pods,
        notifications.service(staging.ENV.namespace).pods,
        receiver.pods,
        environment_sandbox.pods(staging.ENV.namespace),
    ]
    if staging.ENV.central_sampler is not None:
        callers.append(central_sampler.pods(staging.ENV.namespace))
    if staging.ENV.actions.orchestration_v1 is not None or staging.ENV.actions.session_delegation is not None:
        callers.append(actions.service(staging.ENV.namespace).pods)
    policies = [doc["spec"] for doc in staging_manifests if doc["kind"] == "CiliumNetworkPolicy"]
    # Cilium unions the separate platform-route and workload policies. Check both ends
    # of each permitted connection, including the namespace.
    allowed = {
        (source["matchLabels"]["k8s:io.kubernetes.pod.namespace"], source["matchLabels"]["app.kubernetes.io/name"])
        for policy in policies
        if policy["endpointSelector"]["matchLabels"] == receiver.pods.selector
        for rule in policy.get("ingress", [])
        if any(
            str(port["port"]) == str(receiver.pod_port)
            for target in rule.get("toPorts", [])
            for port in target["ports"]
        )
        for source in rule.get("fromEndpoints", [])
    }
    assert allowed == {(caller.namespace, caller.selector["app.kubernetes.io/name"]) for caller in callers}
    for caller in callers:
        assert any(
            target["matchLabels"] == receiver.pods.cilium
            and any(
                str(port["port"]) == str(receiver.pod_port)
                for ports in rule.get("toPorts", [])
                for port in ports["ports"]
            )
            for policy in policies
            if policy["endpointSelector"]["matchLabels"] == caller.selector
            for rule in policy.get("egress", [])
            for target in rule.get("toEndpoints", [])
        )


if __name__ == "__main__":
    pytest_bazel.main()
