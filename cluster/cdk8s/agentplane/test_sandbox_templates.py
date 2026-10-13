"""The runner SandboxTemplates: the shared environment, the egress sidecar's tokens and the platform
routes each Sandbox's runner reaches its services over."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from agentplane.crd_group import GROUP
from cluster.cdk8s.agentplane import history_service, notifications, runner_gateway, testing
from cluster.cdk8s.agentplane.conftest import NAMESPACES
from cluster.cdk8s.pod_policy import workload_kind


def _templates(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Our SandboxTemplates that start Pods; a VM template has no `pod`."""
    return [doc for doc in docs if workload_kind(doc) == (GROUP, "SandboxTemplate") and "pod" in doc["spec"]]


def _by_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name)


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_all_sandbox_workloads_use_shared_environment_defaults(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    templates = _templates(agentplane_manifests[namespace])
    assert templates
    for template in templates:
        workload = template["spec"]["pod"]["spec"]["containers"][0]
        timezone = next(item for item in workload["env"] if item["name"] == "TZ")
        assert timezone["value"] == "America/Los_Angeles"

    runner = next(template for template in templates if template["metadata"]["name"] == "runner")
    args = runner["spec"]["pod"]["spec"]["containers"][0]["args"]
    assert any(args[index : index + 2] == ["--harness-env", "TZ"] for index in range(len(args) - 1))
    # TZDIR's value belongs to the sandbox image, but a harness child starts from only what the
    # runner declares, so the runner has to name it: left off, the child reads every TZ as UTC.
    assert any(args[index : index + 2] == ["--harness-inherit-env", "TZDIR"] for index in range(len(args) - 1))


def test_sandbox_sidecars_project_the_notifications_audience(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    for namespace, manifests in agentplane_manifests.items():
        templates = _templates(manifests)
        assert templates, namespace
        for template in templates:
            pod = template["spec"]["pod"]["spec"]
            sidecars = [container for container in pod["containers"] if container["name"] == "egress-sidecar"]
            assert sidecars, (namespace, template["metadata"]["name"])
            token_volume = next(volume for volume in pod["volumes"] if volume["name"] == "egress-token")
            audiences = {source["serviceAccountToken"]["audience"] for source in token_volume["projected"]["sources"]}
            assert notifications.TOKEN_AUDIENCE in audiences, (namespace, template["metadata"]["name"])
            token_files = json.loads(
                next(item["value"] for item in sidecars[0]["env"] if item["name"].endswith("AUDIENCE_TOKEN_FILES"))
            )
            assert token_files[notifications.TOKEN_AUDIENCE].endswith("/notifications-token"), (
                namespace,
                template["metadata"]["name"],
            )


def test_ducktape_template_is_staging_only_and_projects_buildbuddy_key_to_runner_only(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    for namespace, docs in agentplane_manifests.items():
        templates = {doc["metadata"]["name"]: doc for doc in _templates(docs)}
        app_config = one(
            doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "agentplane-app-config"
        )
        presets = yaml.safe_load(app_config["data"]["config.yaml"])["sandbox_presets"]
        key_copies = [
            doc
            for doc in docs
            if doc["kind"] == "ExternalSecret"
            and doc["metadata"]["name"] == "buildbuddy-api-key"
            and doc["metadata"]["namespace"] == namespace
        ]
        if namespace != "agentplane-staging":
            assert not key_copies
            assert "runner-ducktape" not in templates
            assert "public-coder-ducktape" not in presets
            continue
        assert presets["public-coder-ducktape"]["template"] == "runner-ducktape"
        assert len(key_copies) == 1
        copy = key_copies[0]
        assert copy["spec"]["data"] == [
            {"remoteRef": {"key": "buildbuddy-api-key", "property": "api-key"}, "secretKey": "api-key"}
        ]
        assert copy["spec"]["secretStoreRef"]["name"] == "kubernetes-external-creds-secret-store"
        generic = templates["runner"]["spec"]
        specialized = deepcopy(templates["runner-ducktape"]["spec"])
        container = specialized["pod"]["spec"]["containers"][0]
        assert container["image"] == "git.allegedly.works/ducktape-ci/runner-ducktape:unset"
        container["image"] = generic["pod"]["spec"]["containers"][0]["image"]
        pod = specialized["pod"]["spec"]
        secret = next(volume for volume in pod["volumes"] if volume["name"] == "buildbuddy-api-key")
        assert secret["secret"] == {"secretName": "buildbuddy-api-key", "defaultMode": 288}
        pod["volumes"].remove(secret)
        mount = next(mount for mount in container["volumeMounts"] if mount["name"] == "buildbuddy-api-key")
        assert mount == {"name": "buildbuddy-api-key", "mountPath": "/run/buildbuddy", "readOnly": True}
        container["volumeMounts"].remove(mount)
        assert all(mount["name"] != "buildbuddy-api-key" for mount in pod["containers"][1]["volumeMounts"])
        key_file = next(env for env in container["env"] if env["name"] == "BBR_BUILDBUDDY_API_KEY_FILE")
        assert key_file["value"] == "/run/buildbuddy/api-key"
        container["env"].remove(key_file)
        args = container["args"]
        i = args.index("BBR_BUILDBUDDY_API_KEY_FILE")
        assert args[i - 1] == "--harness-env"
        del args[i - 1 : i + 1]
        # No extra authority in generic runners, testing, or the egress sidecar.
        assert specialized == generic


def test_model_configs_are_generated_into_the_llm_ingress_configmap(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    expected = [
        {"model": "ollama/oai-chat/qwen3.8-flash-next-iq4xs-128k", "total_context_budget_tokens": 128 * 1024},
        {"model": "ollama/oai-chat/qwen3.8-flash-next-iq4xs-256k", "total_context_budget_tokens": 256 * 1024},
    ]
    # Testing alone offers the Claude harness a local model, served on the native wire.
    expected_testing = [
        *expected,
        {"model": "ollama/olm-chat/qwen3.8-flash-next-iq4xs-128k", "total_context_budget_tokens": 128 * 1024},
    ]
    for namespace, manifests in agentplane_manifests.items():
        settings = one(
            doc
            for doc in manifests
            if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "agentplane-llm-ingress-settings"
        )
        deployment = one(
            doc
            for doc in manifests
            if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "agentplane-llm-ingress"
        )
        assert (
            deployment["metadata"]["annotations"]["configmap.reloader.stakater.com/reload"]
            == "agentplane-llm-ingress-settings"
        )
        assert yaml.safe_load(settings["data"]["settings.yaml"])["models"] == (
            expected_testing if namespace == testing.ENV.namespace else expected
        ), namespace
        runners = [
            container
            for template in _templates(manifests)
            for container in template["spec"]["pod"]["spec"]["containers"]
            if container["name"] == "runner"
        ]
        assert runners
        for runner in runners:
            assert "AGENTPLANE_MODEL_CONTEXT_WINDOWS" not in {entry["name"] for entry in runner["env"]}


@pytest.mark.parametrize(
    ("route", "flag", "service", "port_name", "config", "audience_key"),
    [
        (
            "control",
            "--control-target",
            runner_gateway.NAME,
            "runner-control",
            f"{runner_gateway.NAME}-config",
            ("runner_control_audience",),
        ),
        (
            "ingress",
            "--ingress-target",
            history_service.NAME,
            "runner-ingress",
            f"{history_service.NAME}-config",
            ("ingress", "token_audience"),
        ),
    ],
)
@pytest.mark.parametrize("namespace", NAMESPACES)
def test_runner_reaches_its_service_over_the_sidecars_platform_route(
    namespace: str,
    route: str,
    flag: str,
    service: str,
    port_name: str,
    config: str,
    audience_key: tuple[str, ...],
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    docs = agentplane_manifests[namespace]
    service_port = one(
        entry for entry in _by_name(docs, "Service", service)["spec"]["ports"] if entry["name"] == port_name
    )
    accepted: Any = yaml.safe_load(_by_name(docs, "ConfigMap", config)["data"]["config.yaml"])
    for key in audience_key:
        accepted = accepted[key]
    for template in _templates(docs):
        pod = template["spec"]["pod"]["spec"]
        runner = one(container for container in pod["containers"] if container["name"] == "runner")
        sidecar = one(container for container in pod["containers"] if container["name"] == "egress-sidecar")
        routes = json.loads(one(item["value"] for item in sidecar["env"] if item["name"].endswith("PLATFORM_ROUTES")))
        # The runner dials the sidecar's loopback listener for the route.
        args = runner["args"]
        assert args[args.index(flag) + 1] == f"127.0.0.1:{routes[route]['listen_port']}", template["metadata"]["name"]
        host, port = routes[route]["upstream"].rsplit(":", 1)
        assert (host.split(".")[0], int(port)) == (service, service_port["targetPort"])
        # The sidecar attaches a token the service accepts, which only it mounts.
        token_volume = one(volume for volume in pod["volumes"] if volume["name"] == "egress-token")
        projected = {
            source["serviceAccountToken"]["path"]: source["serviceAccountToken"]["audience"]
            for source in token_volume["projected"]["sources"]
        }
        mount = one(mount for mount in sidecar["volumeMounts"] if mount["name"] == "egress-token")
        assert projected[routes[route]["token_file"].removeprefix(f"{mount['mountPath']}/")] == accepted


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_runner_reads_the_platform_instructions_its_template_mounts(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    docs = agentplane_manifests[namespace]
    for template in _templates(docs):
        pod = template["spec"]["pod"]["spec"]
        runner = one(container for container in pod["containers"] if container["name"] == "runner")
        args = runner["args"]
        file = PurePosixPath(args[args.index("--platform-instructions-file") + 1])
        mount = one(entry for entry in runner["volumeMounts"] if entry["mountPath"] == str(file.parent))
        volume = one(entry for entry in pod["volumes"] if entry["name"] == mount["name"])
        config_map = _by_name(docs, "ConfigMap", volume["configMap"]["name"])
        assert file.name in config_map["data"], template["metadata"]["name"]


if __name__ == "__main__":
    pytest_bazel.main()
