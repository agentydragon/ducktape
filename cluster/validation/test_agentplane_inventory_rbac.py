"""The app's Kubernetes inventory calls have their narrowly sufficient RBAC verbs."""

from typing import Any

import pytest_bazel
from more_itertools import one

# pytest_plugins loads cluster.validation.agentplane_fixtures by name; gazelle cannot see
# the dependency.
# gazelle:include_dep //cluster/validation:agentplane_fixtures
pytest_plugins = ("cluster.validation.agentplane_fixtures",)


def services_object(documents: list[dict[str, Any]], kind: str, name: str | None = None) -> dict[str, Any]:
    return one(doc for doc in documents if doc["kind"] == kind and (name is None or doc["metadata"]["name"] == name))


def test_agentplane_app_can_list_the_sandbox_templates_its_route_offers(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    for namespace in ("agentplane-staging", "agentplane-testing"):
        role = services_object(agentplane_manifests[namespace], "Role", "agentplane-app")
        template_rule = one(
            rule
            for rule in role["rules"]
            if rule["apiGroups"] == ["extensions.agents.x-k8s.io"] and rule["resources"] == ["sandboxtemplates"]
        )
        assert role["metadata"]["namespace"] == namespace
        assert template_rule["verbs"] == ["get", "list"]


if __name__ == "__main__":
    pytest_bazel.main()
