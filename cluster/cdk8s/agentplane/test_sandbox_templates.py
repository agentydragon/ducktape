"""Every SandboxTemplate the sandbox Actions offer is beside them and describes itself.

The Action Service reads each offered template's description when it starts, and will not start
without it, so a template renamed, dropped or left undescribed here would take it down.
"""

from __future__ import annotations

from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

from agentplane.sandbox_actions.binding import DESCRIPTION_ANNOTATION, SandboxExecutorBinding
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


if __name__ == "__main__":
    pytest_bazel.main()
