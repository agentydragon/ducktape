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
from agentplane.runner.configuration import CONFIG_FILE, LAUNCH_FILE, CodexReadiness, Configuration
from cluster.cdk8s.agentplane import sandbox_pod, staging
from cluster.cdk8s.agentplane.runner_sandbox import RunnerTemplate
from cluster.cdk8s.pod_policy import workload_kind


def _templates(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Our SandboxTemplates that start Pods; a VM template has no `pod`."""
    return [doc for doc in docs if workload_kind(doc) == (GROUP, "SandboxTemplate") and "pod" in doc["spec"]]


def _by_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name)


def _runner_configuration(docs: list[dict[str, Any]], pod: dict[str, Any]) -> dict[str, Any]:
    volume = one(volume for volume in pod["volumes"] if volume["name"] == "runner-config")
    config_map = _by_name(docs, "ConfigMap", volume["configMap"]["name"])
    return cast(dict[str, Any], json.loads(config_map["data"]["runner.json"]))


def _runner_pod(
    *,
    app: App,
    isolated_executor: bool,
    runner_resources: k8s.ResourceRequirements,
    executor_resources: k8s.ResourceRequirements | None,
    session_reconcile_concurrency: int | None = None,
) -> dict[str, Any]:
    chart = Chart(app, "runner-template-test")
    RunnerTemplate(
        chart,
        "runner-template",
        replace(staging.ENV, isolated_codex_executor=isolated_executor),
        name="test-runner",
        image="example.invalid/runner",
        runner_resources=runner_resources,
        executor_resources=executor_resources,
        session_reconcile_concurrency=session_reconcile_concurrency,
    )
    template = one(doc for doc in Cdk8sTesting.synth(chart) if doc["kind"] == "SandboxTemplate")
    return cast(dict[str, Any], template["spec"]["pod"]["spec"])


def test_all_sandbox_workloads_use_shared_environment_defaults(staging_manifests: list[dict[str, Any]]) -> None:
    templates = _templates(staging_manifests)
    assert templates
    for template in templates:
        workload = template["spec"]["pod"]["spec"]["containers"][0]
        timezone = next(item for item in workload["env"] if item["name"] == "TZ")
        assert timezone["value"] == "America/Los_Angeles"

    # One Pod execution environment: no plain runner template beside the ducktape one.
    assert [template["metadata"]["name"] for template in templates] == [staging.POD_TEMPLATE]
    runner_config = _runner_configuration(staging_manifests, templates[0]["spec"]["pod"]["spec"])
    assert "codex_readiness" not in runner_config
    config = Configuration.model_validate(runner_config)
    assert config.session_reconcile_concurrency is None
    assert "TZ" in config.harness_environment.inherit
    assert "TZDIR" in config.harness_environment.inherit


def test_runner_pod_gives_codex_a_private_executor_with_shared_paths(staging_manifests: list[dict[str, Any]]) -> None:
    template = _by_name(staging_manifests, "SandboxTemplate", staging.POD_TEMPLATE)
    pod = template["spec"]["pod"]["spec"]
    containers = {container["name"]: container for container in pod["containers"]}
    runner = containers["runner"]
    executor = containers["codex-executor"]

    assert pod["shareProcessNamespace"] is False
    assert pod["restartPolicy"] == "Always"
    assert pod["securityContext"]["runAsUser"] == pod["securityContext"]["runAsGroup"] == 1000
    assert executor["securityContext"] == runner["securityContext"]
    assert [container["name"] for container in pod["containers"]] == ["runner", "codex-executor", "egress-sidecar"]
    assert runner["args"] == ["--config", CONFIG_FILE, "--launch-file", LAUNCH_FILE]
    config = _runner_configuration(staging_manifests, pod)
    assert config["environment"] == {
        "kind": "pod",
        "supervision": "pid1",
        "executor": {"url": "ws://127.0.0.1:4501", "socket": "/run/agentplane/executor/control.sock"},
    }
    assert executor["command"] == ["/run/agentplane/runner/bin/agentplane-runner"]
    assert executor["args"] == [
        "executor",
        "--socket",
        "/run/agentplane/executor/control.sock",
        "--listen",
        "ws://127.0.0.1:4501",
        "--health-listen",
        ":4502",
        "--binary",
        "/bin/codex",
    ]
    health = {
        "httpGet": {"path": "/healthz", "port": 4502},
        "initialDelaySeconds": 5,
        "periodSeconds": 5,
        "timeoutSeconds": 2,
        "failureThreshold": 1,
    }
    assert executor["livenessProbe"] == health
    assert executor["readinessProbe"] == {
        "httpGet": {"path": "/healthz", "port": 4502},
        "periodSeconds": 2,
        "timeoutSeconds": 2,
        "failureThreshold": 1,
    }
    assert executor["image"] == runner["image"]
    assert executor["workingDir"] == runner["workingDir"] == "/state"

    runner_mounts = {mount["mountPath"]: mount["name"] for mount in runner["volumeMounts"]}
    executor_mounts = {mount["mountPath"]: mount["name"] for mount in executor["volumeMounts"]}
    for path in (
        "/state",
        "/state/workspaces",
        "/run/agentplane/runner",
        "/home/runner",
        "/tmp",
        "/run/agentplane/executor",
        "/etc/ssl/certs/ca-certificates.crt",
        "/run/agentplane/trust/ca-certificates.crt",
        "/run/agentplane/trust/ca-certificates.p12",
        "/run/agentplane/config/kubeconfig",
    ):
        assert runner_mounts[path] == executor_mounts[path]
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    for path in ("/home/runner", "/tmp", "/run/agentplane/executor"):
        assert "emptyDir" in volumes[runner_mounts[path]]

    executor_mount_names = set(executor_mounts.values())
    assert executor_mounts["/run/agentplane/config/kubeconfig"] == "tool-config"
    # The executor shares the BuildBuddy key, which `bbr` in a Codex shell needs; the runner's own
    # config and the sidecar's token are its alone.
    assert "buildbuddy-api-key" in executor_mount_names
    assert executor_mount_names.isdisjoint({"egress-token", "runner-config"})
    executor_env = {variable["name"]: variable["value"] for variable in executor["env"]}
    runner_env = {variable["name"]: variable["value"] for variable in runner["env"]}
    for name in (*sandbox_pod.PROXY_VAR_NAMES, *sandbox_pod.NO_PROXY_VAR_NAMES, *sandbox_pod.CA_BUNDLE_VAR_NAMES):
        assert executor_env[name] == runner_env[name]
    assert executor_env["KUBECONFIG"] == runner_env["KUBECONFIG"]
    assert {"ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "LITELLM_URL"}.isdisjoint(executor_env)


def test_runner_template_can_override_executor_resources_independently(scratch_app: App) -> None:
    runner_resources = k8s.ResourceRequirements(
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
        runner_resources=runner_resources,
        executor_resources=executor_resources,
    )
    containers = {container["name"]: container for container in pod["containers"]}
    assert containers["runner"]["resources"]["requests"] == {"cpu": "17m", "memory": "127Mi"}
    assert containers["runner"]["resources"]["limits"] == {"cpu": "31m", "memory": "251Mi"}
    assert containers["codex-executor"]["resources"]["requests"] == {"cpu": "43m", "memory": "509Mi"}
    assert containers["codex-executor"]["resources"]["limits"] == {"cpu": "71m", "memory": "997Mi"}


def test_environment_can_keep_codex_execution_inside_the_runner(scratch_app: App) -> None:
    runner_resources = k8s.ResourceRequirements(
        requests={"cpu": k8s.Quantity.from_string("17m"), "memory": k8s.Quantity.from_string("127Mi")},
        limits={"cpu": k8s.Quantity.from_string("31m"), "memory": k8s.Quantity.from_string("251Mi")},
    )
    pod = _runner_pod(
        app=scratch_app, isolated_executor=False, runner_resources=runner_resources, executor_resources=None
    )
    containers = {container["name"]: container for container in pod["containers"]}
    assert "codex-executor" not in containers
    assert containers["runner"]["args"] == ["--config", CONFIG_FILE, "--launch-file", LAUNCH_FILE]
    assert {"home", "tmp", "codex-executor"}.isdisjoint({volume["name"] for volume in pod["volumes"]})


def test_runner_template_configures_session_reconcile_concurrency(scratch_app: App) -> None:
    chart = Chart(scratch_app, "runner-concurrency-test")
    RunnerTemplate(
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
    RunnerTemplate(
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
        _runner_pod(app=scratch_app, isolated_executor=False, runner_resources=resources, executor_resources=resources)


def test_runner_template_renders_mtls_server_listener(scratch_app: App) -> None:
    chart = Chart(scratch_app, "runner-template-test")
    RunnerTemplate(chart, "runner-template", staging.ENV, name="test-runner", image="example.invalid/runner")
    docs = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    template = one(doc for doc in docs if doc["kind"] == "SandboxTemplate")
    pod = template["spec"]["pod"]["spec"]
    runner = one(container for container in pod["containers"] if container["name"] == "runner")
    config = _runner_configuration(docs, pod)

    assert runner["ports"] == [{"name": "runner-control", "containerPort": 9090}]
    assert config["control"] == {
        "listen_address": "0.0.0.0:9090",
        "security": {
            "mode": "mtls",
            "tls": {
                "cert_file": "/run/agentplane/runner-tls/tls.crt",
                "key_file": "/run/agentplane/runner-tls/tls.key",
                "ca_file": "/run/agentplane/runner-tls/ca.crt",
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
    for name in ("runner", "codex-executor"):
        mount = next(mount for mount in containers[name]["volumeMounts"] if mount["name"] == "buildbuddy-api-key")
        assert mount == {"name": "buildbuddy-api-key", "mountPath": "/run/buildbuddy", "readOnly": True}
        key_file = next(env for env in containers[name]["env"] if env["name"] == "BBR_BUILDBUDDY_API_KEY_FILE")
        assert key_file["value"] == "/run/buildbuddy/api-key"
    sidecar = containers["egress-sidecar"]
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
    assert "listen_address" not in runner_settings
    assert launcher_settings["listen_address"] == "0.0.0.0:3128"
    for name, route in runner_settings["platform_routes"].items():
        _, port = route["listen_address"].rsplit(":", 1)
        assert route["listen_address"] == f"127.0.0.1:{port}"
        assert launcher_settings["platform_routes"][name]["listen_address"] == f"0.0.0.0:{port}"
        launcher_settings["platform_routes"][name]["listen_address"] = route["listen_address"]
    del launcher_settings["listen_address"]
    assert launcher_settings == runner_settings
    policy = _by_name(staging_manifests, "ClusterPolicy", f"{staging.ENV.namespace}-launcher-relay")
    inject = one(rule for rule in policy["spec"]["rules"] if rule["name"] == "inject-relay")
    volumes = inject["mutate"]["patchStrategicMerge"]["spec"]["volumes"]
    assert {"name": "egress-sidecar-settings", "configMap": {"name": "launcher-relay-egress-sidecar"}} in volumes


def test_runner_tool_config_role_is_scoped_to_the_canonical_map(staging_manifests: list[dict[str, Any]]) -> None:
    _by_name(staging_manifests, "ConfigMap", "sandbox-tool-config")
    role = _by_name(staging_manifests, "Role", "agentplane-sandbox-tool-config-reader")
    assert role["rules"] == [
        {"apiGroups": [""], "resourceNames": ["sandbox-tool-config"], "resources": ["configmaps"], "verbs": ["get"]}
    ]

    volumes = one(_templates(staging_manifests))["spec"]["pod"]["spec"]["volumes"]
    config_maps = {volume["name"]: volume["configMap"]["name"] for volume in volumes if "configMap" in volume}
    assert config_maps["runner-config"] == f"{staging.POD_TEMPLATE}-config"
    assert config_maps["tool-config"] == "sandbox-tool-config"


if __name__ == "__main__":
    pytest_bazel.main()
