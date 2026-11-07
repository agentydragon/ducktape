"""The runner SandboxTemplates: shared execution paths, the Codex executor, sidecar tokens and routes."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Chart, Testing as Cdk8sTesting  # not collected by pytest
from cdk8s_plus_34 import k8s
from more_itertools import one

from agentplane.crd_group import GROUP
from agentplane.egress.platform_routes import PLATFORM_ROUTES_PORT
from agentplane.env.config import CONFIG_FILE, LAUNCH_FILE, CodexReadiness, Configuration
from agentplane.environment_runtime_transport.config import LLM_INGRESS_URI
from cluster.cdk8s.agentplane import llm_ingress, platform_routes, sandbox_pod, staging
from cluster.cdk8s.agentplane.environment_sandbox import EnvironmentSandboxTemplate
from cluster.cdk8s.pod_policy import workload_kind


def _templates(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Our SandboxTemplates that start Pods; a VM template has no `pod`."""
    return [doc for doc in docs if workload_kind(doc) == (GROUP, "SandboxTemplate") and "pod" in doc["spec"]]


def _by_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _runner_configuration(docs: list[dict[str, Any]], pod: dict[str, Any]) -> dict[str, Any]:
    volume = one(volume for volume in pod["volumes"] if volume["name"] == "environment-runtime-config")
    config_map = _by_name(docs, "ConfigMap", volume["configMap"]["name"])
    return cast(dict[str, Any], json.loads(config_map["data"]["environment-runtime.json"]))


def _runner_pod(
    *,
    app: App,
    isolated_executor: bool,
    environment_runtime_resources: k8s.ResourceRequirements,
    executor_resources: k8s.ResourceRequirements | None,
    session_reconcile_concurrency: int | None = None,
) -> dict[str, Any]:
    chart = Chart(app, "runner-template-test")
    EnvironmentSandboxTemplate(
        chart,
        "runner-template",
        replace(staging.ENV, isolated_codex_executor=isolated_executor),
        name="test-runner",
        image="example.invalid/runner",
        environment_runtime_resources=environment_runtime_resources,
        executor_resources=executor_resources,
        session_reconcile_concurrency=session_reconcile_concurrency,
    )
    template = one(doc for doc in Cdk8sTesting.synth(chart) if doc["kind"] == "SandboxTemplate")
    return cast(dict[str, Any], template["spec"]["pod"]["spec"])


def test_sandbox_harnesses_inherit_runtime_timezone(staging_manifests: list[dict[str, Any]]) -> None:
    templates = _templates(staging_manifests)
    assert templates
    for template in templates:
        pod = template["spec"]["pod"]["spec"]
        workload = one(container for container in pod["containers"] if container["name"] == "environment-runtime")
        workload_environment = {item["name"] for item in workload["env"]}
        config = Configuration.model_validate(_runner_configuration(staging_manifests, pod))
        inherited_environment = set(config.harness_environment.inherit)

        assert "TZ" in workload_environment & inherited_environment
        assert "TZDIR" in inherited_environment


def test_runner_pod_gives_codex_a_private_executor_with_shared_paths(staging_manifests: list[dict[str, Any]]) -> None:
    template = _by_name(staging_manifests, "SandboxTemplate", staging.POD_TEMPLATE)
    pod = template["spec"]["pod"]["spec"]
    containers = {container["name"]: container for container in pod["containers"]}
    runner = containers["environment-runtime"]
    executor = containers["codex-executor"]

    assert pod["shareProcessNamespace"] is False
    assert pod["securityContext"]["runAsUser"] == pod["securityContext"]["runAsGroup"] == 1000
    assert executor["securityContext"] == runner["securityContext"]
    assert [container["name"] for container in pod["containers"]] == ["environment-runtime", "codex-executor"]
    assert [container["name"] for container in pod["initContainers"]] == ["egress-sidecar"]
    assert pod["initContainers"][0]["restartPolicy"] == "Always"
    assert runner["command"] == ["/run/agentplane/environment/bin/agentplane-environment-runtime"]
    assert runner["args"] == ["--config", CONFIG_FILE, "--launch-file", LAUNCH_FILE]
    config = _runner_configuration(staging_manifests, pod)
    assert config["environment"]["kind"] == "pod"
    assert config["environment"]["supervision"] == "pid1"
    assert executor["command"] == ["/run/agentplane/environment/bin/agentplane-execution-service"]
    args = executor["args"]
    assert args[0] == "companion"
    assert args[args.index("--socket") + 1] == config["environment"]["executor"]["socket"]
    assert args[args.index("--listen") + 1] == config["environment"]["executor"]["url"]
    assert args[args.index("--binary") + 1] == "/bin/codex"
    health_port = int(args[args.index("--health-listen") + 1].removeprefix(":"))
    for probe in ("livenessProbe", "readinessProbe"):
        assert executor[probe]["httpGet"] == {"path": "/healthz", "port": health_port}
    assert executor["image"] == runner["image"]
    assert executor["workingDir"] == runner["workingDir"] == "/state"

    runner_mounts = {mount["mountPath"]: mount["name"] for mount in runner["volumeMounts"]}
    executor_mounts = {mount["mountPath"]: mount["name"] for mount in executor["volumeMounts"]}
    for path in (
        "/state",
        "/state/workspaces",
        "/run/agentplane/environment",
        "/home/runner",
        "/tmp",
        "/run/agentplane/executor",
        "/etc/ssl/certs/ca-certificates.crt",
        "/run/agentplane/trust/ca-certificates.crt",
        "/run/agentplane/trust/ca-certificates.p12",
    ):
        assert runner_mounts[path] == executor_mounts[path]
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    for path in ("/home/runner", "/tmp", "/run/agentplane/executor"):
        assert "emptyDir" in volumes[runner_mounts[path]]

    executor_mount_names = set(executor_mounts.values())
    # The executor shares the BuildBuddy key, which `bbr` in a Codex shell needs; the runner's own
    # config and the sidecar's token are its alone.
    assert "buildbuddy-api-key" in executor_mount_names
    assert executor_mount_names.isdisjoint({"egress-token", "environment-runtime-config"})
    executor_env = {variable["name"]: variable["value"] for variable in executor["env"]}
    runner_env = {variable["name"]: variable["value"] for variable in runner["env"]}
    for name in (*sandbox_pod.PROXY_VAR_NAMES, *sandbox_pod.NO_PROXY_VAR_NAMES, *sandbox_pod.CA_BUNDLE_VAR_NAMES):
        assert executor_env[name] == runner_env[name]
    assert "KUBECONFIG" not in executor_env | runner_env
    assert "/run/agentplane/config/kubeconfig" not in executor_mounts.keys() | runner_mounts.keys()
    assert {"ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "LITELLM_URL"}.isdisjoint(executor_env)


def test_runner_template_can_override_executor_resources_independently(scratch_app: App) -> None:
    environment_runtime_resources = k8s.ResourceRequirements(
        requests={"cpu": k8s.Quantity.from_string("17m"), "memory": k8s.Quantity.from_string("127Mi")},
        limits={"cpu": k8s.Quantity.from_string("31m"), "memory": k8s.Quantity.from_string("251Mi")},
    )
    executor_resources = k8s.ResourceRequirements(
        requests={"cpu": k8s.Quantity.from_string("43m"), "memory": k8s.Quantity.from_string("509Mi")},
        limits={"cpu": k8s.Quantity.from_string("71m"), "memory": k8s.Quantity.from_string("997Mi")},
    )
    pod = _runner_pod(
        app=scratch_app,
        isolated_executor=True,
        environment_runtime_resources=environment_runtime_resources,
        executor_resources=executor_resources,
    )
    containers = {container["name"]: container for container in pod["containers"]}
    assert containers["environment-runtime"]["resources"]["requests"] == {"cpu": "17m", "memory": "127Mi"}
    assert containers["environment-runtime"]["resources"]["limits"] == {"cpu": "31m", "memory": "251Mi"}
    assert containers["codex-executor"]["resources"]["requests"] == {"cpu": "43m", "memory": "509Mi"}
    assert containers["codex-executor"]["resources"]["limits"] == {"cpu": "71m", "memory": "997Mi"}


def test_environment_can_keep_codex_execution_inside_the_runner(scratch_app: App) -> None:
    environment_runtime_resources = k8s.ResourceRequirements(
        requests={"cpu": k8s.Quantity.from_string("17m"), "memory": k8s.Quantity.from_string("127Mi")},
        limits={"cpu": k8s.Quantity.from_string("31m"), "memory": k8s.Quantity.from_string("251Mi")},
    )
    pod = _runner_pod(
        app=scratch_app,
        isolated_executor=False,
        environment_runtime_resources=environment_runtime_resources,
        executor_resources=None,
    )
    containers = {container["name"]: container for container in pod["containers"]}
    assert "codex-executor" not in containers
    assert containers["environment-runtime"]["args"] == ["--config", CONFIG_FILE, "--launch-file", LAUNCH_FILE]
    assert {"home", "tmp", "codex-executor"}.isdisjoint({volume["name"] for volume in pod["volumes"]})


def test_runner_template_configures_session_reconcile_concurrency(scratch_app: App) -> None:
    chart = Chart(scratch_app, "runner-concurrency-test")
    EnvironmentSandboxTemplate(
        chart,
        "runner-template",
        staging.ENV,
        name="test-runner",
        image="example.invalid/runner",
        session_reconcile_concurrency=2048,
    )
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    template = one(doc for doc in docs if doc["kind"] == "SandboxTemplate")
    config = _runner_configuration(docs, template["spec"]["pod"]["spec"])
    assert config["session_reconcile_concurrency"] == 2048


def test_runner_template_serializes_codex_readiness_overrides(scratch_app: App) -> None:
    chart = Chart(scratch_app, "runner-codex-readiness-test")
    EnvironmentSandboxTemplate(
        chart,
        "runner-template",
        staging.ENV,
        name="test-runner",
        image="example.invalid/runner",
        codex_readiness=CodexReadiness(
            exec_server_ready_timeout_ms=60_000,
            exec_server_probe_timeout_ms=5_000,
            exec_server_dependency_timeout_ms=120_000,
        ),
    )
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    template = one(doc for doc in docs if doc["kind"] == "SandboxTemplate")
    config = _runner_configuration(docs, template["spec"]["pod"]["spec"])

    assert config["codex_readiness"] == {
        "exec_server_ready_timeout_ms": 60_000,
        "exec_server_probe_timeout_ms": 5_000,
        "exec_server_dependency_timeout_ms": 120_000,
    }


def test_environment_cannot_configure_resources_for_a_disabled_executor(scratch_app: App) -> None:
    resources = k8s.ResourceRequirements(limits={"memory": k8s.Quantity.from_string("1Gi")})
    with pytest.raises(ValueError, match="executor_resources cannot be set"):
        _runner_pod(
            app=scratch_app,
            isolated_executor=False,
            environment_runtime_resources=resources,
            executor_resources=resources,
        )


def test_runner_template_renders_mtls_server_listener(scratch_app: App) -> None:
    chart = Chart(scratch_app, "runner-template-test")
    EnvironmentSandboxTemplate(
        chart, "runner-template", staging.ENV, name="test-runner", image="example.invalid/runner"
    )
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    template = one(doc for doc in docs if doc["kind"] == "SandboxTemplate")
    pod = template["spec"]["pod"]["spec"]
    runner = one(container for container in pod["containers"] if container["name"] == "environment-runtime")
    config = _runner_configuration(docs, pod)

    assert len(runner["ports"]) == 1
    assert len(runner["ports"][0]["name"]) <= 15  # Kubernetes port names are limited to 15 characters.
    assert runner["ports"][0]["containerPort"] == 9090
    assert config["control"] == {
        "listen_address": "0.0.0.0:9090",
        "security": {
            "mode": "mtls",
            "tls": {
                "cert_file": "/run/agentplane/environment-runtime-tls/tls.crt",
                "key_file": "/run/agentplane/environment-runtime-tls/tls.key",
                "ca_file": "/run/agentplane/environment-runtime-tls/ca.crt",
            },
        },
    }


def test_pod_template_projects_the_buildbuddy_key_to_runner_and_executor(
    staging_manifests: list[dict[str, Any]],
) -> None:
    """The canonical key reaches the runner container and its isolated executor, and by name the harness
    children the runner spawns (agentplane/docs/buildbuddy_remote_auth.md). The egress sidecar, which
    holds a Session's bearers, gets neither. Every launch preset names this one template."""
    presets = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])[
        "sandbox_presets"
    ]
    assert {preset["template"] for preset in presets.values()} == {staging.POD_TEMPLATE}
    key_copies = [
        doc
        for doc in staging_manifests
        if doc["kind"] == "ExternalSecret"
        and doc["metadata"]["name"] == "buildbuddy-api-key"
        and doc["metadata"]["namespace"] == staging.ENV.namespace
    ]
    assert len(key_copies) == 1
    copy = key_copies[0]
    assert copy["spec"]["data"] == [
        {"remoteRef": {"key": "buildbuddy-api-key", "property": "api-key"}, "secretKey": "api-key"}
    ]
    assert copy["spec"]["secretStoreRef"]["name"] == "kubernetes-external-creds-secret-store"

    pod = one(_templates(staging_manifests))["spec"]["pod"]["spec"]
    containers = {container["name"]: container for container in pod["containers"]}
    secret = next(volume for volume in pod["volumes"] if volume["name"] == "buildbuddy-api-key")
    assert secret["secret"] == {"secretName": "buildbuddy-api-key", "defaultMode": 288}
    for name in ("environment-runtime", "codex-executor"):
        mount = next(mount for mount in containers[name]["volumeMounts"] if mount["name"] == "buildbuddy-api-key")
        assert mount == {"name": "buildbuddy-api-key", "mountPath": "/run/buildbuddy", "readOnly": True}
        key_file = next(env for env in containers[name]["env"] if env["name"] == "BBR_BUILDBUDDY_API_KEY_FILE")
        assert key_file["value"] == "/run/buildbuddy/api-key"
    sidecar = one(container for container in pod["initContainers"] if container["name"] == "egress-sidecar")
    assert all(mount["name"] != "buildbuddy-api-key" for mount in sidecar["volumeMounts"])
    assert all(env["name"] != "BBR_BUILDBUDDY_API_KEY_FILE" for env in sidecar.get("env", []))
    config = _runner_configuration(staging_manifests, pod)
    assert "BBR_BUILDBUDDY_API_KEY_FILE" in config["harness_environment"]["inherit"]
    settings = next(volume for volume in pod["volumes"] if volume["name"] == "egress-sidecar-settings")
    assert settings["configMap"]["name"] == f"{staging.POD_TEMPLATE}-egress-sidecar"


def test_staging_app_config_offers_ollama_smoke_routes(staging_manifests: list[dict[str, Any]]) -> None:
    app_config = yaml.safe_load(_by_name(staging_manifests, "ConfigMap", "app-config")["data"]["config.yaml"])
    ollama_routes = {model["model"] for model in app_config["models"]["models"] if model["model"].startswith("ollama/")}
    offered_routes = {route for routes in app_config["models"]["harnesses"].values() for route in routes}
    assert ollama_routes & offered_routes, "Agentplane staging must offer at least one Ollama route for smoke tests"


def test_launcher_relay_listens_on_every_interface_and_otherwise_matches_the_runner_sidecar(
    staging_manifests: list[dict[str, Any]],
) -> None:

    def settings(name: str) -> dict[str, Any]:
        return cast(
            dict[str, Any], yaml.safe_load(_by_name(staging_manifests, "ConfigMap", name)["data"]["settings.yaml"])
        )

    runner_settings = settings(f"{staging.POD_TEMPLATE}-egress-sidecar")
    launcher_settings = settings("launcher-relay-egress-sidecar")
    assert runner_settings["listen_address"] == "127.0.0.1:3128"
    assert launcher_settings["listen_address"] == "0.0.0.0:3128"
    assert runner_settings["proxy"]["transport"]["mode"] == "tls"
    assert runner_settings["proxy"]["transport"]["auth"]["mode"] == "spiffe"
    runner_routes = runner_settings["platform_routes"]
    launcher_routes = launcher_settings["platform_routes"]
    assert runner_routes["listen_address"] == f"127.0.0.1:{PLATFORM_ROUTES_PORT}"
    assert launcher_routes["listen_address"] == f"0.0.0.0:{PLATFORM_ROUTES_PORT}"
    for route in platform_routes.routes(staging.ENV.namespace):
        rendered = runner_routes["routes"][route.name]
        assert rendered["service_prefixes"] == list(route.service_prefixes)
        assert rendered["upstream"] == f"{route.service.fqdn}:{route.service.pod_port}"
        assert rendered["transport"] == route.transport.model_dump(mode="json", exclude_none=True)
    template = _by_name(staging_manifests, "SandboxTemplate", staging.POD_TEMPLATE)
    pod = template["spec"]["pod"]["spec"]
    runner = one(container for container in pod["containers"] if container["name"] == "environment-runtime")
    env = {entry["name"]: entry.get("value") for entry in runner["env"]}
    assert env["AGENTPLANE_EGRESS_GRPC_URL"] == f"http://127.0.0.1:{PLATFORM_ROUTES_PORT}"
    assert "AGENTPLANE_EGRESS_GRPC_CREDENTIAL" not in env
    assert "AGENTPLANE_NOTIFICATIONS_GRPC_CREDENTIAL" not in env
    assert env["AGENTPLANE_NOTIFICATIONS_GRPC_URL"] == f"http://127.0.0.1:{PLATFORM_ROUTES_PORT}"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == f"http://127.0.0.1:{PLATFORM_ROUTES_PORT}"
    assert _runner_configuration(staging_manifests, pod)["model_config_target"] == f"127.0.0.1:{PLATFORM_ROUTES_PORT}"
    launcher_routes["listen_address"] = runner_routes["listen_address"]
    launcher_settings["listen_address"] = runner_settings["listen_address"]
    assert launcher_settings == runner_settings

    policy = _by_name(staging_manifests, "ClusterPolicy", f"{staging.ENV.namespace}-launcher-relay")
    inject = one(rule for rule in policy["spec"]["rules"] if rule["name"] == "inject-relay")
    volumes = inject["mutate"]["patchStrategicMerge"]["spec"]["volumes"]
    assert {"name": "egress-sidecar-settings", "configMap": {"name": "launcher-relay-egress-sidecar"}} in volumes


def test_llm_sidecar_route_uses_the_svid_listener(staging_manifests: list[dict[str, Any]]) -> None:
    pod = one(_templates(staging_manifests))["spec"]["pod"]["spec"]
    settings_volume = one(volume for volume in pod["volumes"] if volume["name"] == "egress-sidecar-settings")
    settings_doc = _by_name(staging_manifests, "ConfigMap", settings_volume["configMap"]["name"])
    settings = cast(dict[str, Any], yaml.safe_load(settings_doc["data"]["settings.yaml"]))
    origin = llm_ingress.service(staging.ENV.namespace)
    destination = llm_ingress.service(staging.ENV.namespace)
    route = settings["workload_routes"][f"{origin.fqdn}:{origin.port.number}"]
    assert route["upstream"] == f"{destination.fqdn}:{destination.pod_port}"
    assert route["server_uri"] == LLM_INGRESS_URI


def test_runner_tool_config_role_is_scoped_to_the_canonical_map(staging_manifests: list[dict[str, Any]]) -> None:
    tool_config = _by_name(staging_manifests, "ConfigMap", "sandbox-tool-config")
    assert set(tool_config["data"]) == {"bazel.bazelrc"}
    role = _by_name(staging_manifests, "Role", "agentplane-sandbox-tool-config-reader")
    assert role["rules"] == [
        {"apiGroups": [""], "resourceNames": ["sandbox-tool-config"], "resources": ["configmaps"], "verbs": ["get"]}
    ]

    volumes = one(_templates(staging_manifests))["spec"]["pod"]["spec"]["volumes"]
    config_maps = {volume["name"]: volume["configMap"]["name"] for volume in volumes if "configMap" in volume}
    assert config_maps["environment-runtime-config"] == f"{staging.POD_TEMPLATE}-config"
    assert config_maps["tool-config"] == "sandbox-tool-config"

    pod = one(_templates(staging_manifests))["spec"]["pod"]["spec"]
    for container in [*pod["containers"], *pod.get("initContainers", [])]:
        env_names = {item["name"] for item in container.get("env", [])}
        mount_paths = {item["mountPath"] for item in container.get("volumeMounts", [])}
        assert "KUBECONFIG" not in env_names
        assert "/run/agentplane/config/kubeconfig" not in mount_paths


if __name__ == "__main__":
    pytest_bazel.main()
