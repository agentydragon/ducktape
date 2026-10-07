"""Every SandboxTemplate the sandbox Actions offer is beside them and describes itself.

The Action Service reads each offered template's description when it starts, and will not start
without it, so a template renamed, dropped or left undescribed here would take it down.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_bazel
import yaml
from more_itertools import one

from agentplane.action_service.sandbox.binding import DESCRIPTION_ANNOTATION, SandboxExecutorBinding
from cluster.cdk8s.agentplane import notifications
from cluster.cdk8s.agentplane.conftest import NAMESPACES


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

    runner = next(template for template in templates if template["metadata"]["name"] == "agentplane-runner")
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


if __name__ == "__main__":
    pytest_bazel.main()
