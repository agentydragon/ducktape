"""The app's Kubernetes inventory calls have their narrowly sufficient RBAC verbs."""

from pathlib import Path
from typing import Any

import pytest_bazel
import yaml
from more_itertools import one


def manifest(root: Path, path: str) -> dict[str, Any]:
    document = yaml.safe_load((root / path).read_text())
    assert isinstance(document, dict)
    return document


def test_agentplane_app_can_list_the_sandbox_templates_its_route_offers(k8s_dir: Path) -> None:
    for namespace in ("agentplane-staging", "agentplane-testing"):
        role = manifest(k8s_dir / namespace, "app/role-agentplane-app.yaml")
        template_rule = one(
            rule
            for rule in role["rules"]
            if rule["apiGroups"] == ["extensions.agents.x-k8s.io"] and rule["resources"] == ["sandboxtemplates"]
        )
        assert role["metadata"]["namespace"] == namespace
        assert template_rule["verbs"] == ["get", "list"]


if __name__ == "__main__":
    pytest_bazel.main()
