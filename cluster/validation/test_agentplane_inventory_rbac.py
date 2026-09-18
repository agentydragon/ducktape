"""The app's Kubernetes inventory calls have their narrowly sufficient RBAC verbs."""

from pathlib import Path
from typing import Any

import pytest_bazel
import yaml
from more_itertools import one


def services_object(root: Path, kind: str, name: str | None = None) -> dict[str, Any]:
    documents = yaml.safe_load_all((root / "agentplane-services.k8s.yaml").read_text())
    return one(doc for doc in documents if doc["kind"] == kind and (name is None or doc["metadata"]["name"] == name))


def test_agentplane_app_can_list_the_sandbox_templates_its_route_offers(k8s_dir: Path) -> None:
    for namespace in ("agentplane-staging", "agentplane-testing"):
        role = services_object(k8s_dir / namespace, "Role", "agentplane-app")
        template_rule = one(
            rule
            for rule in role["rules"]
            if rule["apiGroups"] == ["extensions.agents.x-k8s.io"] and rule["resources"] == ["sandboxtemplates"]
        )
        assert role["metadata"]["namespace"] == namespace
        assert template_rule["verbs"] == ["get", "list"]


if __name__ == "__main__":
    pytest_bazel.main()
