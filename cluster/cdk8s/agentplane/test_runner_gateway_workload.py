"""The Dispatcher listener and its Environments/sampler mTLS client credentials."""

from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any, cast

import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting
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


def _resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _gateway_settings(documents: list[dict[str, Any]], container: dict[str, Any]) -> dict[str, Any]:
    """The Gateway's static settings come from its mounted file, without scalar environment overrides."""
    file = cast(
        dict[str, Any],
        yaml.safe_load(_resource(documents, "ConfigMap", f"{runner_gateway.NAME}-settings")["data"]["settings.yaml"]),
    )
    allowed_runtime_envs = {Settings.config_file_env, Settings.dispatcher_database_url_env}
    gateway_envs = {entry["name"] for entry in container["env"] if entry["name"].startswith("AGENTPLANE_SESSIONS_")}
    assert gateway_envs <= allowed_runtime_envs
    return file


def _gateway_objects(app: App) -> list[dict[str, Any]]:
    chart = staging.delivery_chart(app)
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))


def test_sessions_preserves_delivery_store_authority_and_mounts(scratch_app: App) -> None:
    docs = _gateway_objects(scratch_app)
    root = Chart(scratch_app, "shared-services", disable_resource_name_hashes=True)
    runner_gateway_workload.add_service(root, staging.ENV)
    services = [doc for doc in Cdk8sTesting.synth(root) if doc["kind"] == "Service"]
    deployment = one(doc for doc in docs if doc["kind"] == "Deployment")
    assert deployment["metadata"]["name"] == runner_gateway.NAME
    service = one(services)
    assert service["metadata"]["name"] == runner_gateway.NAME
    assert service["spec"]["selector"].items() <= deployment["spec"]["template"]["metadata"]["labels"].items()
    new_pod = deployment["spec"]["template"]["spec"]
    new_container = one(new_pod["containers"])
    db = one(entry for entry in new_container["env"] if entry["name"] == Settings.dispatcher_database_url_env)
    assert db["valueFrom"]["secretKeyRef"] == {"name": "postgres-session-dispatcher", "key": "uri"}
    new_file = one(entry for entry in new_container["env"] if entry["name"] == Settings.config_file_env)
    assert new_pod["serviceAccountName"] == runner_gateway.NAME
    binding = _resource(docs, "RoleBinding", runner_gateway.NAME)
    assert binding["subjects"] == [
        {"apiGroup": "", "kind": "ServiceAccount", "name": runner_gateway.NAME, "namespace": staging.ENV.namespace}
    ]
    assert binding["roleRef"]["name"] == runner_gateway.NAME
    config = _gateway_settings(docs, new_container)
    assert config["runner_control_target_template"] == "environments.{namespace}.svc:8082"
    for key in ("cert_file", "key_file"):
        client_path = PurePosixPath(config["gateway_tls"][key])
        mount = one(mount for mount in new_container["volumeMounts"] if mount["mountPath"] == str(client_path.parent))
        secret = one(volume for volume in new_pod["volumes"] if volume["name"] == mount["name"])["secret"]["secretName"]
        cert = one(doc for doc in docs if doc["kind"] == "Certificate" and doc["spec"]["secretName"] == secret)
        assert cert["spec"]["uris"] == [GATEWAY_SESSIONS_URI]
        assert config["central_sampler"]["tls"][key] == config["gateway_tls"][key]
    settings_mount = one(
        mount
        for mount in new_container["volumeMounts"]
        if mount["mountPath"] == str(PurePosixPath(new_file["value"]).parent)
    )
    config_volume = one(volume for volume in new_pod["volumes"] if volume["name"] == settings_mount["name"])
    config = _resource(docs, "ConfigMap", config_volume["configMap"]["name"])
    assert PurePosixPath(new_file["value"]).name in config["data"]


def test_environment_provisions_the_gateway_identity_from_the_workload_issuer(
    staging_manifests: list[dict[str, Any]],
) -> None:
    workload_issuer = {"kind": WORKLOAD_ISSUER.kind, "name": WORKLOAD_ISSUER.name}
    assert not any(
        doc["kind"] == "Certificate"
        and doc["metadata"]["name"]
        in {
            "agentplane-runner-gateway",
            "runner-gateway-runtime-proxy-client-workload",
            "runner-gateway-environments-client-workload",
        }
        for doc in staging_manifests
    )

    workload_certificate = _resource(staging_manifests, "Certificate", f"{runner_gateway.NAME}-workload")
    workload_spec = workload_certificate["spec"]
    assert workload_spec["secretName"] == f"{runner_gateway.NAME}-workload-tls"
    assert workload_spec["issuerRef"] == workload_issuer
    assert workload_spec["uris"] == [SESSION_DISPATCHER_URI]
    assert set(workload_spec["usages"]) == {"digital signature", "client auth", "server auth"}
    assert workload_spec["privateKey"]["rotationPolicy"] == "Always"

    client_spec = _resource(staging_manifests, "Certificate", "sessions-client-workload")["spec"]
    assert client_spec["secretName"] == "sessions-client-workload-tls"
    assert client_spec["issuerRef"] == workload_issuer
    assert client_spec["uris"] == [GATEWAY_SESSIONS_URI]
    assert set(client_spec["usages"]) == {"digital signature", "client auth", "server auth"}
    assert client_spec["privateKey"]["rotationPolicy"] == "Always"


def test_gateway_preserves_services_and_fences_listener_ingress(scratch_app: App) -> None:
    docs = _gateway_objects(scratch_app)
    root = Chart(scratch_app, "root-services", disable_resource_name_hashes=True)
    runner_gateway_workload.add_service(root, staging.ENV)
    root_docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(root))
    assert not [doc for doc in docs if doc["kind"] == "Service"]
    deployment = _resource(docs, "Deployment", runner_gateway.NAME)
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
    assert (
        not {
            "runtime_proxy_readers",
            "runtime_proxy_session_callers",
            "runtime_proxy_controller_callers",
            "executor_service_callers",
        }
        & settings.keys()
    )
    assert settings["gateway_tls"] == {
        "ca_file": "/run/agentplane/workload-trust/ca-certificates.crt",
        "server_uri": SANDBOX_CONTROLLER_URI,
        "cert_file": "/run/agentplane/sessions-client-tls/tls.crt",
        "key_file": "/run/agentplane/sessions-client-tls/tls.key",
        "client_uri": GATEWAY_SESSIONS_URI,
    }
    assert "runner_token_file" not in settings
    sampler = settings["central_sampler"]
    assert "token_file" not in sampler
    assert sampler["tls"]["server_uri"] == CENTRAL_SAMPLER_URI
    assert sampler["tls"]["client_uri"] == GATEWAY_SESSIONS_URI
    # Both outgoing Sessions connections use the same mounted role certificate.
    for field in ("cert_file", "key_file", "ca_file"):
        assert sampler["tls"][field] == settings["gateway_tls"][field]
    assert "runner_security" not in settings
    assert set(settings["dispatcher_service_callers"]) == {ACTIONS_URI, APP_URI, NOTIFICATIONS_URI}
    assert not any(key.startswith("environment_") for key in settings)
    assert "runtime_catalog" not in settings
    service = one(root_docs)["spec"]
    assert service["selector"].items() <= deployment["spec"]["template"]["metadata"]["labels"].items()
    assert one(service["ports"]) == {
        "name": "grpc",
        "port": 8080,
        "protocol": "TCP",
        "targetPort": listener_ports["grpc"],
    }
    policy = _resource(docs, "CiliumNetworkPolicy", runner_gateway.NAME.removeprefix("agentplane-"))["spec"]
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
    runner_label = environment_sandbox.pods(staging.ENV.namespace).selector["app.kubernetes.io/name"]
    assert (runner_label, "8090") not in ingress
    runner_egress = {
        (endpoint["matchLabels"].get("app.kubernetes.io/name"), str(port["port"]))
        for rule in policy["egress"]
        for endpoint in rule.get("toEndpoints", [])
        for port in rule.get("toPorts", [{}])[0].get("ports", [])
    }
    assert (runner_label, str(environment_sandbox.ENVIRONMENT_RUNTIME_SERVICE_PORT)) not in runner_egress
    role = _resource(docs, "Role", runner_gateway.NAME.removeprefix("agentplane-"))
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
    assert "patch" in one(rule for rule in role["rules"] if rule["resources"] == ["sandboxes"])["verbs"]


def test_actions_delegation_is_explicit_for_each_receiving_backend(staging_manifests: list[dict[str, Any]]) -> None:
    namespace = staging.ENV.namespace
    gateway = yaml.safe_load(
        _resource(staging_manifests, "ConfigMap", f"{runner_gateway.NAME}-settings")["data"]["settings.yaml"]
    )
    history = yaml.safe_load(
        _resource(staging_manifests, "ConfigMap", f"{history_service.NAME}-config")["data"]["config.yaml"]
    )
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
    settings = yaml.safe_load(_resource(docs, "ConfigMap", f"{runner_gateway.NAME}-settings")["data"]["settings.yaml"])
    assert set(settings["dispatcher_service_callers"]) == {APP_URI, NOTIFICATIONS_URI}
    assert "environment_launchers" not in settings
    assert "environment_service_launchers" not in settings
    assert ACTIONS_URI not in settings.get("wake_service_callers", [])


def test_gateway_settings_and_role_certificates_are_mounted(scratch_app: App) -> None:
    docs = _gateway_objects(scratch_app)
    pod = _resource(docs, "Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]
    container = one(c for c in pod["containers"] if c["name"] == runner_gateway.NAME)
    path = PurePosixPath(one(entry["value"] for entry in container["env"] if entry["name"] == Settings.config_file_env))
    mounted = {mount["mountPath"]: mount["name"] for mount in container["volumeMounts"]}
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    settings_map = _resource(docs, "ConfigMap", f"{runner_gateway.NAME}-settings")
    assert "/run/agentplane/runner-tls" not in mounted
    assert "/run/agentplane/runner-gateway-runtime-proxy-client-tls" not in mounted
    assert "/run/agentplane/runner-gateway-environments-client-tls" not in mounted
    client_certificate_volume = volumes[mounted["/run/agentplane/sessions-client-tls"]]
    assert client_certificate_volume["secret"]["secretName"] == "sessions-client-workload-tls"
    assert volumes[mounted[str(path.parent)]]["configMap"]["name"] == settings_map["metadata"]["name"]
    assert path.name in settings_map["data"]


def test_runner_mtls_is_direct_and_outside_platform_routes(staging_manifests: list[dict[str, Any]]) -> None:
    routes = _resource(staging_manifests, "CiliumNetworkPolicy", "platform-route-clients")["spec"]
    routed_services = {
        endpoint["matchLabels"].get("app.kubernetes.io/name")
        for rule in routes["egress"]
        for endpoint in rule.get("toEndpoints", [])
    }
    assert runner_gateway.NAME not in routed_services

    gateway = _resource(staging_manifests, "CiliumNetworkPolicy", runner_gateway.NAME)["spec"]
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
    runner_listener = _resource(staging_manifests, "CiliumNetworkPolicy", "environment-tools-listener")["spec"]
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


if __name__ == "__main__":
    pytest_bazel.main()
