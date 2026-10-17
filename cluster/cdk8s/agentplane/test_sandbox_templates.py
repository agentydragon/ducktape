"""The runner SandboxTemplates: the shared environment, the egress sidecar's tokens and the platform
routes each Sandbox's runner reaches its services over."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from agentplane.crd_group import GROUP
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


def test_ducktape_template_is_staging_only_and_projects_buildbuddy_key_to_runner_only(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    for namespace, docs in agentplane_manifests.items():
        templates = {doc["metadata"]["name"]: doc for doc in _templates(docs)}
        app_config = one(doc for doc in docs if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "app-config")
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
        settings = next(volume for volume in pod["volumes"] if volume["name"] == "egress-sidecar-settings")
        generic_settings = next(
            volume for volume in generic["pod"]["spec"]["volumes"] if volume["name"] == "egress-sidecar-settings"
        )
        # Each template owns its sidecar settings ConfigMap; the specialization adds none.
        assert settings["configMap"]["name"] == "runner-ducktape-egress-sidecar"
        assert (
            _by_name(docs, "ConfigMap", settings["configMap"]["name"])["data"]
            == _by_name(docs, "ConfigMap", generic_settings["configMap"]["name"])["data"]
        )
        settings["configMap"]["name"] = generic_settings["configMap"]["name"]
        # No extra authority in generic runners, testing, or the egress sidecar.
        assert specialized == generic


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_runner_tool_config_role_is_scoped_to_the_canonical_map(
    namespace: str, agentplane_manifests: dict[str, list[dict[str, Any]]]
) -> None:
    docs = agentplane_manifests[namespace]
    _by_name(docs, "ConfigMap", "sandbox-tool-config")
    role = _by_name(docs, "Role", "agentplane-sandbox-tool-config-reader")
    assert role["rules"] == [
        {"apiGroups": [""], "resourceNames": ["sandbox-tool-config"], "resources": ["configmaps"], "verbs": ["get"]}
    ]

    runner = next(template for template in _templates(docs) if template["metadata"]["name"] == "runner")
    volumes = runner["spec"]["pod"]["spec"]["volumes"]
    config_maps = {volume["name"]: volume["configMap"]["name"] for volume in volumes if "configMap" in volume}
    assert config_maps["platform-instructions"] == "runner-platform-instructions"
    assert config_maps["tool-config"] == "sandbox-tool-config"


if __name__ == "__main__":
    pytest_bazel.main()
