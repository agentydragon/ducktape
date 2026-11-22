"""The Dispatcher listener and its Environments/sampler mTLS client credentials."""

from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any, cast

import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting
from more_itertools import one

from agentplane.net.peertls.config import (
    ACTIONS_URI,
    APP_URI,
    CENTRAL_SAMPLER_URI,
    GATEWAY_SESSIONS_URI,
    NOTIFICATIONS_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
)
from agentplane.session_dispatcher.settings import Settings
from cluster.cdk8s.agentplane import (
    actions,
    app,
    egress,
    environment_sandbox,
    history_service,
    notifications,
    runner_gateway,
    runner_gateway_workload,
    sandbox_controller,
    staging,
    workload_tls,
)
from cluster.cdk8s.agentplane.environment import SessionDelegationProps
from cluster.cdk8s.cert_manager.cluster_ca import WORKLOAD_ISSUER
from cluster.cdk8s.testing.manifests import config_map_yaml, find_resource


def _gateway_settings(documents: list[dict[str, Any]], container: dict[str, Any]) -> dict[str, Any]:
    """The Gateway's static settings come from its mounted file, without scalar environment overrides."""
    file = config_map_yaml(documents, f"{runner_gateway.NAME}-settings", "settings.yaml")
    allowed_runtime_envs = {Settings.config_file_env, Settings.dispatcher_database_url_env}
    gateway_envs = {entry["name"] for entry in container["env"] if entry["name"].startswith("AGENTPLANE_SESSIONS_")}
    assert gateway_envs <= allowed_runtime_envs
    return file


def test_gateway_client_certificate_mounts(staging_delivery_manifests: list[dict[str, Any]]) -> None:
    docs = staging_delivery_manifests
    deployment = find_resource(docs, "Deployment", runner_gateway.NAME)
    pod = deployment["spec"]["template"]["spec"]
    container = one(pod["containers"])
    config = _gateway_settings(docs, container)
    assert config["runner_control_target_template"] == "environments.{namespace}.svc:8082"
    for key in ("cert_file", "key_file"):
        client_path = PurePosixPath(config["gateway_tls"][key])
        mount = one(mount for mount in container["volumeMounts"] if mount["mountPath"] == str(client_path.parent))
        secret = one(volume for volume in pod["volumes"] if volume["name"] == mount["name"])["secret"]["secretName"]
        cert = one(doc for doc in docs if doc["kind"] == "Certificate" and doc["spec"]["secretName"] == secret)
        assert cert["spec"]["uris"] == [GATEWAY_SESSIONS_URI]


def test_environment_provisions_the_gateway_identity_from_the_workload_issuer(
    staging_manifests: list[dict[str, Any]],
) -> None:
    workload_issuer = {"kind": WORKLOAD_ISSUER.kind, "name": WORKLOAD_ISSUER.name}
    workload_certificate = find_resource(staging_manifests, "Certificate", f"{runner_gateway.NAME}-workload")
    workload_spec = workload_certificate["spec"]
    assert workload_spec["secretName"] == f"{runner_gateway.NAME}-workload-tls"
    assert workload_spec["issuerRef"] == workload_issuer
    assert workload_spec["uris"] == [SESSION_DISPATCHER_URI]
    assert set(workload_spec["usages"]) == {"digital signature", "client auth", "server auth"}
    assert workload_spec["privateKey"]["rotationPolicy"] == "Always"

    client_spec = find_resource(staging_manifests, "Certificate", "sessions-client-workload")["spec"]
    assert client_spec["secretName"] == "sessions-client-workload-tls"
    assert client_spec["issuerRef"] == workload_issuer
    assert client_spec["uris"] == [GATEWAY_SESSIONS_URI]
    assert set(client_spec["usages"]) == {"digital signature", "client auth", "server auth"}
    assert client_spec["privateKey"]["rotationPolicy"] == "Always"


def test_gateway_preserves_services_and_fences_listener_ingress(
    staging_manifests: list[dict[str, Any]], staging_delivery_manifests: list[dict[str, Any]]
) -> None:
    docs = staging_delivery_manifests
    assert not [doc for doc in docs if doc["kind"] == "Service"]
    deployment = find_resource(docs, "Deployment", runner_gateway.NAME)
    pod = deployment["spec"]["template"]["spec"]
    assert not pod.get("initContainers")
    container = one(c for c in pod["containers"] if c["name"] == runner_gateway.NAME)
    assert pod["serviceAccountName"] == runner_gateway.NAME
    listener_ports = {port["name"]: port["containerPort"] for port in container["ports"]}
    settings = _gateway_settings(docs, container)
    assert settings["policy_namespace"] == staging.ENV.namespace
    assert settings["rpc"]["listen_address"] == "0.0.0.0:8080"
    assert int(settings["rpc"]["listen_address"].rsplit(":", 1)[1]) == listener_ports["grpc"]
    assert settings["rpc"]["tls"] == {
        "cert_file": "/run/agentplane/workload-tls/tls.crt",
        "key_file": "/run/agentplane/workload-tls/tls.key",
    }
    assert settings["rpc"]["auth"] == [{"mode": "spiffe", "ca_file": workload_tls.TRUST_BUNDLE_FILE}]
    assert settings["gateway_tls"] == {
        "ca_file": "/run/agentplane/workload-trust/ca-certificates.crt",
        "server_uri": SANDBOX_CONTROLLER_URI,
        "cert_file": "/run/agentplane/sessions-client-tls/tls.crt",
        "key_file": "/run/agentplane/sessions-client-tls/tls.key",
        "client_uri": GATEWAY_SESSIONS_URI,
    }
    sampler = settings["central_sampler"]
    assert sampler["tls"]["server_uri"] == CENTRAL_SAMPLER_URI
    assert sampler["tls"]["client_uri"] == GATEWAY_SESSIONS_URI
    # Both outgoing Sessions connections use the same mounted role certificate.
    for field in ("cert_file", "key_file", "ca_file"):
        assert sampler["tls"][field] == settings["gateway_tls"][field]
    assert set(settings["dispatcher_service_callers"]) == {ACTIONS_URI, APP_URI, NOTIFICATIONS_URI}
    service = find_resource(staging_manifests, "Service", runner_gateway.NAME)["spec"]
    assert service["selector"].items() <= deployment["spec"]["template"]["metadata"]["labels"].items()
    assert one(service["ports"]) == {
        "name": "grpc",
        "port": 8080,
        "protocol": "TCP",
        "targetPort": listener_ports["grpc"],
    }
    policy = find_resource(docs, "CiliumNetworkPolicy", runner_gateway.NAME)["spec"]
    ingress = {
        (endpoint["matchLabels"].get("app.kubernetes.io/name"), port["port"])
        for rule in policy["ingress"]
        for endpoint in rule.get("fromEndpoints", [])
        for port in rule.get("toPorts", [{}])[0].get("ports", [])
    }
    assert (app.NAME, "8080") in ingress
    assert (notifications.NAME, "8080") in ingress
    assert (runner_gateway.NAME, "8080") not in ingress
    assert (egress.NAME, str(runner_gateway.sessions_service(staging.ENV.namespace).pod_port)) in ingress
    assert (sandbox_controller.NAME, "8080") not in ingress
    role = find_resource(docs, "Role", runner_gateway.NAME)
    resources = {resource for rule in role["rules"] for resource in rule["resources"]}
    assert resources >= {
        "serviceaccounts",
        "sandboxes",
        "workloadcredentials",
        "identities",
        "policysets",
        "policybindings",
    }
    assert "leases" not in resources
    assert not {"pods", "virtualmachines", "virtualmachineinstances"} & resources
    assert not any(
        doc["kind"] in {"ClusterRole", "ClusterRoleBinding"} and "token-reviewer" in doc["metadata"]["name"]
        for doc in docs
    )
    assert "endpointslices" not in resources


def test_actions_delegation_is_explicit_for_each_receiving_backend(staging_manifests: list[dict[str, Any]]) -> None:
    namespace = staging.ENV.namespace
    gateway = config_map_yaml(staging_manifests, f"{runner_gateway.NAME}-settings", "settings.yaml")
    history = config_map_yaml(staging_manifests, f"{history_service.NAME}-config", "config.yaml")
    assert history["policy_namespace"] == namespace
    gateway_delegation = gateway["dispatcher_service_delegation"]
    assert gateway_delegation["actors"] == [{"spiffe_id": ACTIONS_URI, "connection_provenance": False}]
    assert gateway_delegation["methods"] == [
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/SubmitInput",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/ChangeModel",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/ChangeReasoningEffort",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/InterruptTurn",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/CreateSession",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/GetSession",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/GetDispatch",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/WatchDispatches",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/ListDispatches",
        "/agentplane.runner_gateway.dispatcher.SessionDispatcher/CancelDispatch",
    ]
    history_delegation = history["service_delegation"]
    assert history_delegation["actors"] == [{"spiffe_id": ACTIONS_URI, "connection_provenance": False}]
    assert history_delegation["methods"] == [
        "/agentplane.history_service.HistoryService/ReadSessionEvents",
        "/agentplane.history_service.HistoryService/FollowSession",
        "/agentplane.history_service.HistoryService/ReadSessionObservations",
        "/agentplane.history_service.HistoryService/GetSessionSummary",
        "/agentplane.history_service.HistoryService/BatchGetSessionSummaries",
        "/agentplane.history_service.HistoryService/ReadThreadEntities",
        "/agentplane.history_service.HistoryService/ReadEntityEvidence",
        "/agentplane.history_service.HistoryService/ReadNativeFrames",
        "/agentplane.history_service.HistoryService/FindSessions",
        "/agentplane.history_service.HistoryService/WatchSessionSummaries",
    ]
    assert history["reader_service_callers"] == [ACTIONS_URI, APP_URI, NOTIFICATIONS_URI]


def test_session_delegation_is_a_dispatcher_caller_without_wake_authority(scratch_app: App) -> None:
    env = replace(
        staging.ENV,
        actions=replace(
            staging.ENV.actions,
            orchestration_v1=None,
            session_delegation=SessionDelegationProps(
                direct_tools_by_group={actions.ENVIRONMENTS_GROUP: frozenset(), actions.SESSIONS_GROUP: frozenset()}
            ),
        ),
    )
    chart = runner_gateway_workload.serving_chart(scratch_app, env=env)
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    settings = config_map_yaml(docs, f"{runner_gateway.NAME}-settings", "settings.yaml")
    assert set(settings["dispatcher_service_callers"]) == {APP_URI, NOTIFICATIONS_URI}
    assert ACTIONS_URI not in settings.get("wake_service_callers", [])


def test_runner_mtls_is_direct_and_outside_platform_routes(staging_manifests: list[dict[str, Any]]) -> None:
    routes = find_resource(staging_manifests, "CiliumNetworkPolicy", "platform-route-clients")["spec"]
    routed_services = {
        endpoint["matchLabels"].get("app.kubernetes.io/name")
        for rule in routes["egress"]
        for endpoint in rule.get("toEndpoints", [])
    }
    assert runner_gateway.NAME not in routed_services

    gateway = find_resource(staging_manifests, "CiliumNetworkPolicy", runner_gateway.NAME)["spec"]
    assert all(
        port["port"] != "8090"
        for rule in gateway["ingress"]
        for target in rule.get("toPorts", [])
        for port in target["ports"]
    )
    runner_label = environment_sandbox.pods(staging.ENV.namespace).selector["app.kubernetes.io/name"]
    assert not any(
        endpoint["matchLabels"].get("app.kubernetes.io/name") == runner_label
        and any(
            port["port"] == str(environment_sandbox.ENVIRONMENT_RUNTIME_SERVICE_PORT)
            for target in rule.get("toPorts", [])
            for port in target["ports"]
        )
        for rule in gateway["egress"]
        for endpoint in rule.get("toEndpoints", [])
    )
    runner_listener = find_resource(staging_manifests, "CiliumNetworkPolicy", "environment-tools-listener")["spec"]
    gateway_label = runner_gateway.sessions_service(staging.ENV.namespace).pods.selector["app.kubernetes.io/name"]
    assert not any(
        endpoint["matchLabels"].get("app.kubernetes.io/name") == gateway_label
        and any(
            port["port"] == str(environment_sandbox.ENVIRONMENT_RUNTIME_SERVICE_PORT)
            for target in rule.get("toPorts", [])
            for port in target["ports"]
        )
        for rule in runner_listener["ingress"]
        for endpoint in rule.get("fromEndpoints", [])
    )


def test_dispatcher_senders_have_credentials_and_egress(staging_manifests: list[dict[str, Any]]) -> None:
    pod = find_resource(staging_manifests, "Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]
    container = one(pod["containers"])
    settings = config_map_yaml(staging_manifests, f"{runner_gateway.NAME}-settings", "settings.yaml")
    callers = set(settings["dispatcher_service_callers"])
    wakers = set(settings.get("wake_service_callers", []))
    senders = (app.NAME, notifications.NAME, actions.NAME)
    assert callers == {
        uri
        for sender in senders
        for uri in find_resource(staging_manifests, "Certificate", f"{sender}-workload")["spec"]["uris"]
    }
    assert wakers <= callers
    database = find_resource(staging_manifests, "Database", "session-dispatcher")["spec"]
    assert database["name"] == "session_dispatcher"
    assert database["owner"] == "session_dispatcher"
    assert {item["name"]: item.get("valueFrom") for item in container["env"]}[
        "AGENTPLANE_SESSIONS_DISPATCHER_DATABASE_URL"
    ] == {"secretKeyRef": {"name": "postgres-session-dispatcher", "key": "uri"}}
    for sender in senders:
        policy = find_resource(staging_manifests, "CiliumNetworkPolicy", sender)["spec"]
        assert any(
            target["matchLabels"].get("app.kubernetes.io/name") == runner_gateway.NAME
            for rule in policy["egress"]
            for target in rule.get("toEndpoints", [])
        )


def test_app_reads_its_serving_database_uri(staging_manifests: list[dict[str, Any]]) -> None:
    pod = find_resource(staging_manifests, "Deployment", app.NAME)["spec"]["template"]["spec"]
    container = one(container for container in pod["containers"] if container["name"] == "app")
    database_url = one(variable for variable in container["env"] if variable["name"] == "AGENTPLANE_DATABASE_URL")
    assert database_url == {
        "name": "AGENTPLANE_DATABASE_URL",
        "valueFrom": {"secretKeyRef": {"name": "postgres-app", "key": "uri"}},
    }


def test_app_uses_dispatcher_grpc_target(staging_manifests: list[dict[str, Any]]) -> None:
    service = find_resource(staging_manifests, "Service", runner_gateway.sessions_service(staging.ENV.namespace).name)
    grpc_port = one(port for port in service["spec"]["ports"] if port["name"] == "grpc")
    endpoint = f"{service['metadata']['name']}.{staging.ENV.namespace}.svc.cluster.local:{grpc_port['port']}"
    app_config = config_map_yaml(staging_manifests, "app-config", "config.yaml")
    assert app_config["session_dispatcher_target"] == endpoint


if __name__ == "__main__":
    pytest_bazel.main()
