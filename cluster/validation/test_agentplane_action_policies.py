"""Git-managed Action policy objects must parse under the Action Service's own wire models.

kubeconform checks them against the CRD schema; the Action Service parses `spec` more strictly
(an unknown policy kind or key, an invalid JSON Schema), and on the live cluster a refused object
contributes nothing and reports `Ready=False`. Running that parse here over every set and binding
under cluster/k8s fails CI instead, and pins the one cross-object relation a binding has: each set
it names is defined in its namespace.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path
from x.agentplane.action_service.policies.resources import GROUP, VERSION, BindingSpec, PolicySetSpec

_K8S_DIR = get_required_path("_main/cluster/k8s/kustomization.yaml").parent
_DOCUMENTS = [
    document
    for path in sorted(_K8S_DIR.rglob("*.yaml"))
    # Authentik blueprints use custom YAML tags and are not Kubernetes resources.
    if "blueprints" not in path.parts
    for document in yaml.safe_load_all(path.read_text())
    if isinstance(document, dict) and document.get("apiVersion") == f"{GROUP}/{VERSION}"
]
_POLICY_SETS = [document for document in _DOCUMENTS if document["kind"] == "ActionPolicySet"]
_BINDINGS = [document for document in _DOCUMENTS if document["kind"] == "ActionPolicyBinding"]


def _key(document: dict[str, Any]) -> str:
    return f"{document['metadata']['namespace']}/{document['metadata']['name']}"


@pytest.mark.parametrize("document", _POLICY_SETS, ids=_key)
def test_policy_set_spec_parses(document: dict[str, Any]) -> None:
    PolicySetSpec.model_validate(document["spec"])


@pytest.mark.parametrize("document", _BINDINGS, ids=_key)
def test_binding_spec_parses_and_names_defined_sets(document: dict[str, Any]) -> None:
    spec = BindingSpec.model_validate(document["spec"])
    namespace = document["metadata"]["namespace"]
    defined = {
        policy_set["metadata"]["name"]
        for policy_set in _POLICY_SETS
        if policy_set["metadata"]["namespace"] == namespace
    }
    missing = set(spec.policy_sets) - defined
    assert not missing, f"{_key(document)} names sets not defined in {namespace}: {sorted(missing)}"


def test_policy_objects_are_git_managed() -> None:
    # Anchors the parametrized tests: an empty roster would skip them rather than fail.
    assert _POLICY_SETS
    assert _BINDINGS


if __name__ == "__main__":
    pytest_bazel.main()
