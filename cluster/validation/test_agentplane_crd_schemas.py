"""The kubeconform schemas under cluster/schemas/ are the Agentplane CRDs' openAPIV3Schema, and each
binding CRD's subject is the `x.agentplane.subjects.ServiceAccountRef` both services decide against.

The pre-commit kubeconform hook validates EgressPolicy, EgressBinding, EgressCredential, ActionPolicySet and
ActionPolicyBinding manifests against `cluster/schemas/<group>/<kind>_<version>.json`; each file is generated from its CRD here and
pinned, so an edit to a CRD that is not mirrored fails this test rather than letting the
hook accept manifests the API server would reject.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
import yaml
from pydantic import TypeAdapter

from util.bazel.runfiles import get_required_path
from x.agentplane.subjects import ServiceAccountRef

_CRD_FILES = [
    get_required_path("_main/cluster/k8s/agentplane-crds/crd-egresspolicies.yaml"),
    get_required_path("_main/cluster/k8s/agentplane-crds/crd-egressbindings.yaml"),
    get_required_path("_main/cluster/k8s/agentplane-crds/crd-egresscredentials.yaml"),
    get_required_path("_main/cluster/k8s/agentplane-crds/crd-actionpolicysets.yaml"),
    get_required_path("_main/cluster/k8s/agentplane-crds/crd-actionpolicybindings.yaml"),
]
_SCHEMAS_DIR = get_required_path("_main/cluster/schemas")


def _versions(crd_file: Path) -> list[tuple[Path, object]]:
    crd = yaml.safe_load(crd_file.read_text())
    group = crd["spec"]["group"]
    kind = crd["spec"]["names"]["kind"].lower()
    return [
        (_SCHEMAS_DIR / group / f"{kind}_{version['name']}.json", version["schema"]["openAPIV3Schema"])
        for version in crd["spec"]["versions"]
    ]


def _subject_schema(crd_name: str, field: str) -> dict[str, Any]:
    """The one-subject schema inside a binding CRD's spec, whether the spec holds one or a list."""
    crd: dict[str, Any] = yaml.safe_load(get_required_path(f"_main/cluster/k8s/agentplane-crds/{crd_name}").read_text())
    spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"][field]
    schema: dict[str, Any] = spec["items"] if spec["type"] == "array" else spec
    return schema


@pytest.mark.parametrize(
    ("crd_name", "field"), [("crd-egressbindings.yaml", "subjects"), ("crd-actionpolicybindings.yaml", "subject")]
)
def test_a_binding_crd_states_the_subject_the_services_decide_against(crd_name: str, field: str) -> None:
    """Both services bind to one `ServiceAccountRef`, so neither CRD may drift from it: a field the
    model does not know reaches it as an extra its config forbids, and one a CRD stops requiring
    reaches it absent.

    Checked against the model's own schema rather than against the other CRD -- two mirrors of one
    source, not two sources compared to each other.
    """
    model = TypeAdapter(ServiceAccountRef).json_schema()
    schema = _subject_schema(crd_name, field)

    assert schema["type"] == "object"
    assert set(schema["properties"]) == set(model["properties"])
    assert set(schema["required"]) == set(model["required"])
    for field_name, declared in model["properties"].items():
        assert schema["properties"][field_name]["type"] == declared["type"]
        assert schema["properties"][field_name].get("minLength") == declared.get("minLength")


@pytest.mark.parametrize(
    ("schema_file", "expected"),
    [entry for crd_file in _CRD_FILES for entry in _versions(crd_file)],
    ids=lambda value: value.name if isinstance(value, Path) else "",
)
def test_schema_file_is_crd_schema(schema_file: Path, expected: object) -> None:
    assert json.loads(schema_file.read_text()) == expected, (
        f"{schema_file.name} differs from its CRD; regenerate it by dumping the version's openAPIV3Schema as JSON"
    )


if __name__ == "__main__":
    pytest_bazel.main()
