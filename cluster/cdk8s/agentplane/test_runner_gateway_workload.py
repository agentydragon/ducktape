"""The Gateway shares a TLS gRPC listener and uses mTLS connections to runner Pods."""

from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any, cast

import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting
from more_itertools import one

from agentplane.environment_runtime_transport.config import SESSION_DISPATCHER_URI
from agentplane.runner_gateway.settings import Settings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s.agentplane import (
    actions,
    app,
    chart as agentplane_chart,
    egress,
    environment_sandbox,
    history_service,
    notifications,
    runner_gateway,
    runner_gateway_workload,
    sandbox_controller,
    session_dispatcher,
    staging,
)
from cluster.cdk8s.agentplane.environment import SessionDelegationProps
from util.settings_contract import env_name


def _resource(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _gateway_settings(documents: list[dict[str, Any]], container: dict[str, Any]) -> dict[str, Any]:
    """Each setting as the Gateway reads it: the container's literal env var, else the mounted file's key."""
    file = yaml.safe_load(_resource(documents, "ConfigMap", f"{runner_gateway.NAME}-settings")["data"]["settings.yaml"])
    env = {entry["name"]: entry["value"] for entry in container["env"] if "value" in entry}
    return {
        **file,
        **{
            field: env[env_name(Settings, field)] for field in Settings.model_fields if env_name(Settings, field) in env
        },
    }


def _gateway_objects(app: App) -> list[dict[str, Any]]:
    chart = staging.delivery_chart(app)
    return cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))


def test_environment_provisions_cert_manager_runner_issuer_and_gateway_identity(
    staging_manifests: list[dict[str, Any]],
) -> None:
    issuer_ca = _resource(staging_manifests, "Certificate", agentplane_chart.RUNNER_CA_SECRET)
    assert issuer_ca["spec"]["isCA"] is True
    assert issuer_ca["spec"]["issuerRef"]["kind"] == "ClusterIssuer"
    assert issuer_ca["spec"]["privateKey"]["rotationPolicy"] == "Always"

    issuer = _resource(staging_manifests, "Issuer", runner_gateway.RUNNER_CERTIFICATE_ISSUER)
    assert issuer["spec"]["ca"]["secretName"] == agentplane_chart.RUNNER_CA_SECRET

    gateway_certificate = _resource(staging_manifests, "Certificate", agentplane_chart.RUNNER_GATEWAY_CERTIFICATE)
    spec = gateway_certificate["spec"]
    assert spec["secretName"] == runner_gateway.GATEWAY_TLS_SECRET
    assert spec["issuerRef"] == {"kind": "Issuer", "name": runner_gateway.RUNNER_CERTIFICATE_ISSUER}
    assert spec["uris"] == ["spiffe://agentplane/runner-gateway"]
    assert set(spec["usages"]) == {"digital signature", "client auth"}
    assert spec["privateKey"]["rotationPolicy"] == "Always"

    workload_certificate = _resource(staging_manifests, "Certificate", f"{runner_gateway.NAME}-workload")
    workload_spec = workload_certificate["spec"]
    assert workload_spec["secretName"] == f"{runner_gateway.NAME}-workload-tls"
    assert workload_spec["issuerRef"] == {"kind": "Issuer", "name": runner_gateway.RUNNER_CERTIFICATE_ISSUER}
    assert workload_spec["uris"] == [SESSION_DISPATCHER_URI]
    assert set(workload_spec["usages"]) == {"digital signature", "server auth"}
    assert workload_spec["privateKey"]["rotationPolicy"] == "Always"


def test_gateway_preserves_services_and_fences_listener_ingress(scratch_app: App) -> None:
    docs = _gateway_objects(scratch_app)
    root = Chart(scratch_app, "root-services", disable_resource_name_hashes=True)
    runner_gateway_workload.add_stable_services(root, staging.ENV)
    root_docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(root))
    assert not [doc for doc in docs if doc["kind"] == "Service"]
    deployment = _resource(docs, "Deployment", runner_gateway.NAME)
    pod = deployment["spec"]["template"]["spec"]
    assert not pod.get("initContainers")
    container = one(c for c in pod["containers"] if c["name"] == "runner-gateway")
    assert pod["serviceAccountName"] == runner_gateway.NAME
    listener_ports = {port["name"]: port["containerPort"] for port in container["ports"]}
    settings = _gateway_settings(docs, container)
    assert settings["rpc"]["listen_address"] == "0.0.0.0:8080"
    assert int(settings["rpc"]["listen_address"].rsplit(":", 1)[1]) == listener_ports["grpc"]
    assert settings["rpc"]["tls"] == {
        "cert_file": "/run/agentplane/workload-tls/tls.crt",
        "key_file": "/run/agentplane/workload-tls/tls.key",
    }
    bearer_auth = one(item for item in settings["rpc"]["auth"] if item["mode"] == "kubernetes_bearer")
    assert set(bearer_auth["audiences"]) == {session_dispatcher.TOKEN_AUDIENCE, runner_gateway.TOKEN_AUDIENCE}
    spiffe_auth = one(item for item in settings["rpc"]["auth"] if item["mode"] == "spiffe")
    assert spiffe_auth == {"mode": "spiffe", "ca_file": "/run/agentplane/workload-trust/ca-certificates.crt"}
    assert settings["dispatcher_audience"] == session_dispatcher.TOKEN_AUDIENCE
    assert settings["gateway_audience"] == runner_gateway.TOKEN_AUDIENCE
    assert settings["gateway_tls"] == {
        "ca_file": "/run/agentplane/workload-trust/ca-certificates.crt",
        "server_uri": SESSION_DISPATCHER_URI,
    }
    assert settings["runner_security"] == {
        "mode": "mtls",
        "tls": {
            "cert_file": f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/tls.crt",
            "key_file": f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/tls.key",
            "ca_file": f"{runner_gateway.GATEWAY_TLS_MOUNT_PATH}/ca.crt",
        },
    }
    assert set(settings["dispatcher_callers"]) == {
        f"{staging.ENV.namespace}/{app.NAME}",
        f"{staging.ENV.namespace}/{notifications.NAME}",
        f"{staging.ENV.namespace}/{actions.NAME}",
    }
    assert settings["operator_callers"] == [f"{staging.ENV.namespace}/{app.NAME}"]
    assert settings["environment_launchers"] == [f"{staging.ENV.namespace}/{actions.NAME}"]
    assert f"{staging.ENV.namespace}/{runner_gateway.NAME}" in settings["gateway_callers"]
    assert f"{staging.ENV.namespace}/{app.NAME}" in settings["wake_callers"]
    for service_name in (runner_gateway.NAME, session_dispatcher.NAME):
        service = _resource(root_docs, "Service", service_name)["spec"]
        assert service["selector"]
        assert all(
            deployment["spec"]["template"]["metadata"]["labels"][key] == value
            for key, value in service["selector"].items()
        )
        assert all(port["targetPort"] == listener_ports[port["name"]] for port in service["ports"])
    gateway_service = _resource(root_docs, "Service", runner_gateway.NAME)["spec"]
    assert one(port for port in gateway_service["ports"] if port["name"] == "grpc")["port"] == 8080
    assert {port["name"] for port in gateway_service["ports"]} == {"grpc"}
    dispatcher_service = _resource(root_docs, "Service", session_dispatcher.NAME)["spec"]
    assert {port["name"] for port in dispatcher_service["ports"]} == {"grpc"}
    policy = _resource(docs, "CiliumNetworkPolicy", runner_gateway.NAME.removeprefix("agentplane-"))["spec"]
    ingress = {
        (endpoint["matchLabels"].get("app.kubernetes.io/name"), port["port"])
        for rule in policy["ingress"]
        for endpoint in rule.get("fromEndpoints", [])
        for port in rule.get("toPorts", [{}])[0].get("ports", [])
    }
    assert (app.NAME, "8080") in ingress
    assert (notifications.NAME, "8080") in ingress
    assert (runner_gateway.NAME, "8080") in ingress
    assert (egress.NAME, str(runner_gateway.service(staging.ENV.namespace).pod_port)) in ingress
    assert (sandbox_controller.NAME, "8080") in ingress
    runner_label = environment_sandbox.pods(staging.ENV.namespace).selector["app.kubernetes.io/name"]
    assert (runner_label, "8090") not in ingress
    runner_egress = {
        (endpoint["matchLabels"].get("app.kubernetes.io/name"), str(port["port"]))
        for rule in policy["egress"]
        for endpoint in rule.get("toEndpoints", [])
        for port in rule.get("toPorts", [{}])[0].get("ports", [])
    }
    assert (runner_label, str(environment_sandbox.ENVIRONMENT_RUNTIME_SERVICE_PORT)) in runner_egress
    role = _resource(docs, "Role", runner_gateway.NAME.removeprefix("agentplane-"))
    assert {resource for rule in role["rules"] for resource in rule["resources"]} >= {
        "pods",
        "sandboxes",
        "leases",
        "endpointslices",
        "virtualmachines",
        "virtualmachineinstances",
    }
    assert "patch" in one(rule for rule in role["rules"] if rule["resources"] == ["sandboxes"])["verbs"]


def test_actions_delegation_is_explicit_for_each_receiving_backend(staging_manifests: list[dict[str, Any]]) -> None:
    namespace = staging.ENV.namespace
    actions_account = f"{namespace}/{actions.NAME}"
    gateway = yaml.safe_load(
        _resource(staging_manifests, "ConfigMap", f"{runner_gateway.NAME}-settings")["data"]["settings.yaml"]
    )
    history = yaml.safe_load(
        _resource(staging_manifests, "ConfigMap", f"{history_service.NAME}-config")["data"]["config.yaml"]
    )
    gateway_delegation = gateway["delegation"]
    assert gateway_delegation["actor"] == actions_account
    assert gateway_delegation["caller_namespaces"] == [namespace]
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
        "/agentplane.environments.v1.EnvironmentService/ListEnvironments",
        "/agentplane.environments.v1.EnvironmentService/GetEnvironment",
        "/agentplane.environments.v1.EnvironmentService/WatchEnvironment",
        "/agentplane.environments.v1.EnvironmentService/ResumeEnvironment",
    ]
    history_delegation = history["delegation"]
    assert history_delegation["actor"] == actions_account
    assert history_delegation["caller_namespaces"] == [namespace]
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


def test_empty_operator_callers_are_omitted(scratch_app: App) -> None:
    chart = runner_gateway_workload.serving_chart(scratch_app, env=staging.ENV, operator_callers={})
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    settings = yaml.safe_load(_resource(docs, "ConfigMap", f"{runner_gateway.NAME}-settings")["data"]["settings.yaml"])

    assert "operator_callers" not in settings
    assert settings["environment_launchers"] == [f"{staging.ENV.namespace}/{actions.NAME}"]


def test_gateway_can_explicitly_configure_operator_and_delegated_resume(scratch_app: App) -> None:
    env = replace(staging.ENV, namespace="test-agentplane", wake_caller_accounts=frozenset())
    operator = ServiceAccountRef(namespace=env.namespace, name=app.NAME)
    resume_method = "/agentplane.environments.v1.EnvironmentService/ResumeEnvironment"
    chart = runner_gateway_workload.serving_chart(
        scratch_app,
        env=env,
        operator_callers={operator: app.service(env.namespace).pods},
        extra_delegated_methods=(resume_method,),
    )
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    settings = yaml.safe_load(_resource(docs, "ConfigMap", f"{runner_gateway.NAME}-settings")["data"]["settings.yaml"])

    assert settings["operator_callers"] == [f"{env.namespace}/{app.NAME}"]
    assert resume_method in settings["delegation"]["methods"]


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
    assert set(settings["dispatcher_callers"]) == {
        f"{env.namespace}/{app.NAME}",
        f"{env.namespace}/{notifications.NAME}",
        f"{env.namespace}/{actions.NAME}",
    }
    assert "operator_callers" not in settings
    assert settings["environment_launchers"] == []
    assert f"{env.namespace}/{actions.NAME}" not in settings["wake_callers"]


def test_the_settings_file_the_gateway_reads_is_mounted_beside_its_token(scratch_app: App) -> None:
    docs = _gateway_objects(scratch_app)
    pod = _resource(docs, "Deployment", runner_gateway.NAME)["spec"]["template"]["spec"]
    container = one(c for c in pod["containers"] if c["name"] == "runner-gateway")
    path = PurePosixPath(one(entry["value"] for entry in container["env"] if entry["name"] == Settings.config_file_env))
    mounted = {mount["mountPath"]: mount["name"] for mount in container["volumeMounts"]}
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    settings_map = _resource(docs, "ConfigMap", f"{runner_gateway.NAME}-settings")
    tls_volume = volumes[mounted[runner_gateway.GATEWAY_TLS_MOUNT_PATH]]["projected"]
    assert tls_volume["defaultMode"] == 0o440
    secret_source = one(source["secret"] for source in tls_volume["sources"] if "secret" in source)
    assert secret_source["name"] == runner_gateway.GATEWAY_TLS_SECRET
    assert {item["path"] for item in secret_source["items"]} == {"tls.crt", "tls.key"}
    trust_source = one(source["configMap"] for source in tls_volume["sources"] if "configMap" in source)
    assert trust_source["name"] == "cluster-internal-ca-bundle"
    assert trust_source["items"] == [{"key": "ca-certificates.crt", "path": "ca.crt"}]
    assert volumes[mounted[str(path.parent)]]["configMap"]["name"] == settings_map["metadata"]["name"]
    assert path.name in settings_map["data"]
    assert "projected" in volumes[mounted["/var/run/secrets/agentplane-runner-gateway"]]


def test_runner_mtls_is_direct_and_outside_platform_routes(staging_manifests: list[dict[str, Any]]) -> None:
    routes = _resource(staging_manifests, "CiliumNetworkPolicy", "platform-route-clients")["spec"]
    routed_services = {
        endpoint["matchLabels"].get("app.kubernetes.io/name")
        for rule in routes["egress"]
        for endpoint in rule.get("toEndpoints", [])
    }
    assert runner_gateway.NAME not in routed_services

    gateway = _resource(staging_manifests, "CiliumNetworkPolicy", "runner-gateway")["spec"]
    assert all(
        port["port"] != "8090"
        for rule in gateway["ingress"]
        for target in rule.get("toPorts", [])
        for port in target["ports"]
    )
    runner_label = environment_sandbox.pods(staging.ENV.namespace).selector["app.kubernetes.io/name"]
    assert any(
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
    gateway_label = runner_gateway.service(staging.ENV.namespace).pods.selector["app.kubernetes.io/name"]
    assert any(
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
