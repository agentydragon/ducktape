"""Every SandboxTemplate the sandbox Actions offer is beside them and describes itself.

The Action Service reads each offered template's description when it starts, and will not start
without it, so a template renamed, dropped or left undescribed here would take it down.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from agentplane.action_service.sandbox.binding import DESCRIPTION_ANNOTATION, SandboxExecutorBinding
from agentplane.egress.resources import placeholder_of
from agentplane.sandbox_service.runner_endpoints import RunnerEndpoint
from cluster.cdk8s.agentplane import history_ingress, notifications, runner_gateway, sandbox_service
from cluster.cdk8s.agentplane.conftest import NAMESPACES


def _by_name(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return one(doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name)


def test_every_offered_template_exists_and_describes_itself(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    checked = 0
    for namespace in NAMESPACES:
        docs = agentplane_manifests[namespace]
        settings = one(
            doc
            for doc in docs
            if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "agentplane-actions-settings"
        )
        templates = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "SandboxTemplate"}
        for group in yaml.safe_load(settings["data"]["settings.yaml"])["action_groups"].values():
            if group["executor"]["kind"] != "sandbox":
                continue
            for name in SandboxExecutorBinding.model_validate(group["executor"]).templates:
                assert templates[name]["metadata"]["annotations"][DESCRIPTION_ANNOTATION], (namespace, name)
                checked += 1
    # Keeps this from passing vacuously should no namespace offer a template.
    assert checked


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_all_sandbox_workloads_use_shared_environment_defaults(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    templates = [doc for doc in agentplane_manifests[namespace] if doc["kind"] == "SandboxTemplate"]
    assert templates
    for template in templates:
        workload = template["spec"]["podTemplate"]["spec"]["containers"][0]
        timezone = next(item for item in workload["env"] if item["name"] == "TZ")
        assert timezone["value"] == "America/Los_Angeles"

    runner = next(template for template in templates if template["metadata"]["name"] == "runner")
    args = runner["spec"]["podTemplate"]["spec"]["containers"][0]["args"]
    assert any(args[index : index + 2] == ["--harness-env", "TZ"] for index in range(len(args) - 1))
    # TZDIR's value belongs to the sandbox image, but a harness child starts from only what the
    # runner declares, so the runner has to name it: left off, the child reads every TZ as UTC.
    assert any(args[index : index + 2] == ["--harness-inherit-env", "TZDIR"] for index in range(len(args) - 1))


def test_sandbox_sidecars_project_the_notifications_audience(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    for namespace, manifests in agentplane_manifests.items():
        templates = [doc for doc in manifests if doc["kind"] == "SandboxTemplate"]
        assert templates, namespace
        for template in templates:
            pod = template["spec"]["podTemplate"]["spec"]
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
        templates = {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == "SandboxTemplate"}
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
        container = specialized["podTemplate"]["spec"]["containers"][0]
        assert container["image"] == "git.allegedly.works/ducktape-ci/runner-ducktape:unset"
        container["image"] = generic["podTemplate"]["spec"]["containers"][0]["image"]
        pod = specialized["podTemplate"]["spec"]
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
        assert yaml.safe_load(settings["data"]["settings.yaml"])["models"] == expected, namespace
        runners = [
            container
            for template in manifests
            if template["kind"] == "SandboxTemplate"
            for container in template["spec"]["podTemplate"]["spec"]["containers"]
            if container["name"] == "runner"
        ]
        assert runners
        for runner in runners:
            assert "AGENTPLANE_MODEL_CONTEXT_WINDOWS" not in {entry["name"] for entry in runner["env"]}


@pytest.mark.parametrize(
    ("setting", "service", "port_name", "flags"),
    [
        ("runner_control", runner_gateway.NAME, "runner-control", {"--control-target", "--control-bearer"}),
        ("runner_ingress", history_ingress.NAME, "grpc", {"--ingress-target", "--ingress-bearer"}),
    ],
)
@pytest.mark.parametrize("namespace", NAMESPACES)
def test_runner_endpoint_reaches_its_listener_with_the_pods_token(
    namespace: str,
    setting: str,
    service: str,
    port_name: str,
    flags: set[str],
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    docs = agentplane_manifests[namespace]
    config = yaml.safe_load(_by_name(docs, "ConfigMap", f"{sandbox_service.NAME}-config")["data"]["config.yaml"])
    endpoint = RunnerEndpoint.model_validate(config[setting])
    host, port = endpoint.target.rsplit(":", 1)
    service_ports = _by_name(docs, "Service", service)["spec"]["ports"]
    assert int(port) == one(entry for entry in service_ports if entry["name"] == port_name)["port"]
    # The proxy matches the host on the exact string the policy names, and swaps the bearer only
    # where it is the placeholder of the credential that rule substitutes.
    rule = one(_by_name(docs, "EgressPolicy", endpoint.egress_policy)["spec"]["rules"])
    assert host in rule["hosts"], rule
    assert endpoint.bearer == placeholder_of(rule["credentialRef"]["name"])
    # The substituted token is one the proxy may use and every Sandbox's sidecar projects.
    credential = _by_name(docs, "EgressCredential", rule["credentialRef"]["name"])
    audience = credential["spec"]["source"]["projectedWorkloadToken"]["audience"]
    proxy_settings = yaml.safe_load(_by_name(docs, "ConfigMap", "agentplane-egress-settings")["data"]["settings.yaml"])
    assert audience in proxy_settings["projected_token_audiences"]
    for template in docs:
        if template["kind"] != "SandboxTemplate":
            continue
        pod = template["spec"]["podTemplate"]["spec"]
        sidecar = one(container for container in pod["containers"] if container["name"] == "egress-sidecar")
        token_files = json.loads(
            one(item["value"] for item in sidecar["env"] if item["name"].endswith("AUDIENCE_TOKEN_FILES"))
        )
        token_volume = one(volume for volume in pod["volumes"] if volume["name"] == "egress-token")
        projected = {
            source["serviceAccountToken"]["audience"]: source["serviceAccountToken"]["path"]
            for source in token_volume["projected"]["sources"]
        }
        assert token_files[audience].endswith(f"/{projected[audience]}"), template["metadata"]["name"]
        # The Sandbox Service refuses a template whose runner already names the endpoint.
        for container in pod["containers"]:
            assert not flags & set(container.get("args", [])), template["metadata"]["name"]


if __name__ == "__main__":
    pytest_bazel.main()
