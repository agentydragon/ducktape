"""The Controller authenticates Gateway calls with its mounted service SVID."""

from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from agentplane.action_service.settings import Settings as ActionSettings
from agentplane.app.settings import Settings as AppSettings
from agentplane.environments.settings import Settings
from agentplane.net.peertls.config import ACTIONS_URI, APP_URI, SANDBOX_CONTROLLER_URI, SESSION_DISPATCHER_URI
from agentplane.runner_gateway.settings import Settings as GatewaySettings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.agentplane import (
    actions,
    app,
    egress,
    environment_sandbox,
    environment_service,
    runner_gateway,
    sandbox_controller,
    staging,
    workload_tls,
)


def _resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


def test_controller_settings_are_mounted_and_gateway_token_is_projected(
    staging_manifests: list[dict[str, Any]],
) -> None:
    config_map = _resource(staging_manifests, "ConfigMap", f"{sandbox_controller.NAME}-settings")
    deployment = _resource(staging_manifests, "Deployment", sandbox_controller.NAME)
    pod = deployment["spec"]["template"]["spec"]
    container = one(pod["containers"])
    env = {entry["name"]: entry for entry in container["env"]}

    config_path = PurePosixPath(env[Settings.config_file_env]["value"])
    assert {name for name in env if name.startswith("AGENTPLANE_SANDBOX_CONTROLLER_")} == {Settings.config_file_env}
    config_volume = one(
        volume for volume in pod["volumes"] if volume.get("configMap", {}).get("name") == config_map["metadata"]["name"]
    )
    config_mount = one(mount for mount in container["volumeMounts"] if mount["name"] == config_volume["name"])
    assert config_mount["mountPath"] == str(config_path.parent)
    assert config_mount["readOnly"] is True
    settings = yaml.safe_load(config_map["data"][config_path.name])
    Settings.model_validate(settings)
    assert "runner_gateway_token_file" not in settings
    certificate = _resource(staging_manifests, "Certificate", f"{sandbox_controller.NAME}-client-workload")
    certificate_uris = certificate["spec"]["uris"]
    assert certificate_uris == [SANDBOX_CONTROLLER_URI]
    cert_mount = one(
        mount
        for mount in container["volumeMounts"]
        if mount["name"] == f"{sandbox_controller.NAME}-client-workload-tls"
    )
    cert_path = PurePosixPath(cert_mount["mountPath"])
    assert settings["runner_gateway_tls"] == {
        "ca_file": settings["runner_security"]["tls"]["ca_file"],
        "server_uri": SESSION_DISPATCHER_URI,
        "cert_file": str(cert_path / "tls.crt"),
        "key_file": str(cert_path / "tls.key"),
        "client_uri": certificate_uris[0],
    }
    assert settings["runner_security"] == {
        "mode": "mtls",
        "tls": {
            "cert_file": str(cert_path / "tls.crt"),
            "key_file": str(cert_path / "tls.key"),
            "ca_file": workload_tls.TRUST_BUNDLE_FILE,
        },
    }

    assert certificate["spec"]["usages"] == ["digital signature", "client auth", "server auth"]
    cert_volume = one(volume for volume in pod["volumes"] if volume["name"] == cert_mount["name"])
    assert cert_mount["readOnly"] is True
    assert cert_volume["secret"]["secretName"] == f"{sandbox_controller.NAME}-client-workload-tls"

    controller_policy = _resource(staging_manifests, "CiliumNetworkPolicy", sandbox_controller.NAME)["spec"]
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

    runtime_listener = _resource(staging_manifests, "CiliumNetworkPolicy", "environment-tools-listener")["spec"]
    assert any(
        endpoint["matchLabels"].get("app.kubernetes.io/name") == sandbox_controller.NAME
        and any(
            port["port"] == str(environment_sandbox.ENVIRONMENT_RUNTIME_SERVICE_PORT)
            for target in rule.get("toPorts", [])
            for port in target["ports"]
        )
        for rule in runtime_listener["ingress"]
        for endpoint in rule.get("fromEndpoints", [])
    )


def test_controller_rbac_keeps_only_leader_election_lease_access(staging_manifests: list[dict[str, Any]]) -> None:
    role = _resource(staging_manifests, "Role", sandbox_controller.NAME.removeprefix("agentplane-"))
    resources = {resource for rule in role["rules"] for resource in rule["resources"]}
    assert "services" not in resources
    assert one(rule for rule in role["rules"] if rule["resources"] == ["leases"])["verbs"] == [
        "get",
        "create",
        "update",
    ]


def test_environment_receiver_owns_authority_and_uses_controller_certificate(
    staging_manifests: list[dict[str, Any]],
) -> None:
    controller = Settings.model_validate(
        yaml.safe_load(
            _resource(staging_manifests, "ConfigMap", f"{sandbox_controller.NAME}-settings")["data"]["config.yaml"]
        )
    )
    gateway = GatewaySettings.model_validate(
        yaml.safe_load(
            _resource(staging_manifests, "ConfigMap", f"{runner_gateway.NAME}-settings")["data"]["settings.yaml"]
        )
    )
    receiver = controller.environment_service
    assert receiver is not None
    assert [method.mode for method in receiver.rpc.auth] == ["spiffe"]
    namespace = staging.ENV.namespace
    assert set(receiver.readers) == {f"{namespace}/{app.NAME}"}
    assert set(receiver.managers) == {f"{namespace}/{app.NAME}"}
    assert set(receiver.launchers) == {f"{namespace}/{actions.NAME}"}
    assert set(receiver.operators) == {f"{namespace}/{app.NAME}"}
    assert set(receiver.service_readers) == {APP_URI}
    assert set(receiver.service_managers) == {APP_URI}
    assert set(receiver.service_launchers) == {ACTIONS_URI}
    assert set(receiver.service_operators) == {APP_URI}
    assert set(receiver.wake_callers) == set(gateway.wake_callers or ())
    assert set(receiver.wake_service_callers) == set(gateway.wake_service_callers)
    assert receiver.service_delegation is not None
    assert [actor.spiffe_id for actor in receiver.service_delegation.actors] == [ACTIONS_URI]
    assert set(receiver.service_delegation.methods) == {
        "/agentplane.environments.v1.EnvironmentService/ListEnvironments",
        "/agentplane.environments.v1.EnvironmentService/GetEnvironment",
        "/agentplane.environments.v1.EnvironmentService/WatchEnvironment",
        "/agentplane.environments.v1.EnvironmentService/ResumeEnvironment",
    }
    assert controller.runner_security.mode == "mtls"
    assert receiver.rpc.tls is not None
    assert str(receiver.rpc.tls.cert_file) == controller.runner_security.tls.cert_file
    assert str(receiver.rpc.tls.key_file) == controller.runner_security.tls.key_file
    service = _resource(staging_manifests, "Service", sandbox_controller.NAME)
    port = one(port for port in service["spec"]["ports"] if port["name"] == "grpc")
    assert int(receiver.rpc.listen_address.rsplit(":", 1)[1]) == port["targetPort"]
    deployment = _resource(staging_manifests, "Deployment", sandbox_controller.NAME)
    container = one(deployment["spec"]["template"]["spec"]["containers"])
    assert port["targetPort"] in {entry["containerPort"] for entry in container["ports"]}


def test_actions_environment_peer_matches_receiver_and_separates_dispatcher(
    staging_manifests: list[dict[str, Any]],
) -> None:
    settings = ActionSettings.model_validate(
        yaml.safe_load(_resource(staging_manifests, "ConfigMap", "actions-settings")["data"]["settings.yaml"])
    )
    peer = settings.environment_service
    assert peer is not None
    service = _resource(staging_manifests, "Service", sandbox_controller.NAME)
    port = one(port for port in service["spec"]["ports"] if port["name"] == "grpc")
    assert (
        peer.target
        == f"{service['metadata']['name']}.{service['metadata']['namespace']}.svc.cluster.local:{port['port']}"
    )
    receiver = Settings.model_validate(
        yaml.safe_load(
            _resource(staging_manifests, "ConfigMap", f"{sandbox_controller.NAME}-settings")["data"]["config.yaml"]
        )
    ).environment_service
    assert receiver is not None
    assert peer.tls is not None
    assert peer.tls.client_uri in receiver.service_launchers
    assert receiver.service_delegation is not None
    assert peer.tls.client_uri in {actor.spiffe_id for actor in receiver.service_delegation.actors}
    certificate = _resource(staging_manifests, "Certificate", f"{sandbox_controller.NAME}-client-workload")
    assert certificate["spec"]["uris"] == [peer.tls.server_uri]
    client_certificate = _resource(staging_manifests, "Certificate", f"{actions.NAME}-workload")
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
        operator_callers=(),
        launchers=() if launcher is None else (launcher,),
        operator_service_callers=frozenset(),
        wake_callers=(),
        wake_service_callers=frozenset(),
        delegation_actor=None,
    )
    assert authority.service_launchers == (frozenset({ACTIONS_URI}) if allowed else frozenset())
    assert ACTIONS_URI not in authority.service_managers
    assert ACTIONS_URI not in authority.service_operators


def test_app_environment_peer_matches_receiver_and_mounted_service_identity(
    staging_manifests: list[dict[str, Any]],
) -> None:
    settings = AppSettings.model_validate(
        yaml.safe_load(_resource(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    )
    peer = settings.environment_service
    assert peer is not None
    service = _resource(staging_manifests, "Service", sandbox_controller.NAME)
    port = one(port for port in service["spec"]["ports"] if port["name"] == "grpc")
    assert (
        peer.target
        == f"{service['metadata']['name']}.{service['metadata']['namespace']}.svc.cluster.local:{port['port']}"
    )
    certificate = _resource(staging_manifests, "Certificate", f"{sandbox_controller.NAME}-client-workload")
    assert certificate["spec"]["uris"] == [peer.tls.server_uri]
    app_certificate = _resource(staging_manifests, "Certificate", f"{app.NAME}-workload")
    assert app_certificate["spec"]["uris"] == [peer.tls.client_uri]
    assert peer.tls.client_uri == APP_URI
    receiver = Settings.model_validate(
        yaml.safe_load(
            _resource(staging_manifests, "ConfigMap", f"{sandbox_controller.NAME}-settings")["data"]["config.yaml"]
        )
    ).environment_service
    assert receiver is not None
    assert peer.tls.client_uri in receiver.service_readers
    assert peer.tls.client_uri in receiver.service_managers
    assert peer.tls.client_uri in receiver.service_operators
    assert settings.session_dispatcher_tls is not None
    assert settings.session_dispatcher_tls.server_uri == SESSION_DISPATCHER_URI
    assert settings.session_dispatcher_target != peer.target
    assert settings.runner_gateway_target != peer.target


def test_environment_receiver_can_watch_policy_and_mutate_only_desired_state(
    staging_manifests: list[dict[str, Any]],
) -> None:
    rules = _resource(staging_manifests, "Role", sandbox_controller.NAME)["rules"]

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


def test_environment_receiver_network_reachability_matches_enabled_callers(
    staging_manifests: list[dict[str, Any]],
) -> None:
    endpoint = environment_service.service(staging.ENV.namespace)
    policy = _resource(staging_manifests, "CiliumNetworkPolicy", sandbox_controller.NAME)["spec"]
    allowed = {
        source["matchLabels"]["app.kubernetes.io/name"]
        for rule in policy["ingress"]
        if any(
            str(port["port"]) == str(endpoint.pod_port)
            for target in rule.get("toPorts", [])
            for port in target["ports"]
        )
        for source in rule.get("fromEndpoints", [])
    }
    callers = [app.service(staging.ENV.namespace), egress.proxy(staging.ENV.namespace)]
    if staging.ENV.actions.orchestration_v1 is not None or staging.ENV.actions.session_delegation is not None:
        callers.append(actions.service(staging.ENV.namespace))
    assert allowed == {caller.pods.selector["app.kubernetes.io/name"] for caller in callers}
    for caller in callers:
        caller_policy = _resource(staging_manifests, "CiliumNetworkPolicy", caller.name)["spec"]
        assert any(
            target["matchLabels"].get("app.kubernetes.io/name") == endpoint.pods.selector["app.kubernetes.io/name"]
            and any(
                str(port["port"]) == str(endpoint.pod_port)
                for ports in rule.get("toPorts", [])
                for port in ports["ports"]
            )
            for rule in caller_policy["egress"]
            for target in rule.get("toEndpoints", [])
        )


if __name__ == "__main__":
    pytest_bazel.main()
