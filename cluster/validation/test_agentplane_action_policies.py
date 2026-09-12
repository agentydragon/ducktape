"""Git-managed Action policy objects must parse under the Action Service's own wire models.

kubeconform checks them against the CRD schema; the Action Service parses `spec` more strictly
(an unknown policy kind or key, an invalid JSON Schema), and on the live cluster a refused object
contributes nothing and reports `Ready=False`. Running that parse here over every set and binding
under cluster/k8s fails CI instead, and pins the cross-object relations: each set a binding or a
launch preset names is defined in its namespace.
"""

from __future__ import annotations

from pathlib import Path
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
# Each environment's integration-app config; the directory is the namespace its presets bind in.
_APP_CONFIGS = sorted(_K8S_DIR.glob("agentplane-*/app/config.yaml"))


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


@pytest.mark.parametrize("config_path", _APP_CONFIGS, ids=lambda path: path.parent.parent.name)
def test_preset_action_policy_sets_are_defined(config_path: Path) -> None:
    """A launch preset binds every Sandbox it launches to the sets it names; the app refuses a
    launch naming a set the namespace does not hold, so a stale name here breaks every launch."""
    namespace = config_path.parent.parent.name
    config = yaml.safe_load(config_path.read_text())
    defined = {
        policy_set["metadata"]["name"]
        for policy_set in _POLICY_SETS
        if policy_set["metadata"]["namespace"] == namespace
    }
    for preset_name, preset in config["sandbox_presets"].items():
        missing = set(preset.get("action_policy_sets", [])) - defined
        assert not missing, f"{namespace} preset {preset_name} names sets not defined there: {sorted(missing)}"


def test_policy_objects_are_git_managed() -> None:
    # Anchors the parametrized tests: an empty roster would skip them rather than fail.
    assert _POLICY_SETS
    assert _BINDINGS


if __name__ == "__main__":
    pytest_bazel.main()
