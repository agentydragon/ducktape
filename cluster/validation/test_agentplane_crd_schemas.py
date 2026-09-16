"""The kubeconform schemas under cluster/schemas/ are the Agentplane CRDs' openAPIV3Schema, and each
binding CRD's subject is the `x.agentplane.subjects.Subject` both services decide against.

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
from more_itertools import one
from pydantic import TypeAdapter

from util.bazel.runfiles import get_required_path
from x.agentplane.subjects import Subject

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


def _variant(schema: dict[str, Any], key: str) -> tuple[set[str], set[str]]:
    """One variant of a subject as a schema states it: the fields it declares and those it requires."""
    variant = schema["properties"][key]
    return set(variant["properties"]), set(variant["required"])


def _model_subject() -> dict[str, tuple[set[str], set[str]]]:
    """`Subject` as a schema, by variant key. Pydantic emits `anyOf` over `$defs`, one indirection
    per model, so each branch is followed to the reference whose fields the CRD has to state."""
    schema = TypeAdapter(Subject).json_schema()
    defs = schema["$defs"]
    resolve = lambda node: defs[node["$ref"].rsplit("/", maxsplit=1)[-1]]  # noqa: E731
    variants = [resolve(branch) for branch in schema["anyOf"]]
    return {
        key: (
            set(resolve(variant["properties"][key])["properties"]),
            set(resolve(variant["properties"][key])["required"]),
        )
        for variant in variants
        for key in [one(variant["properties"])]
    }


@pytest.mark.parametrize(
    ("crd_name", "field"), [("crd-egressbindings.yaml", "subjects"), ("crd-actionpolicybindings.yaml", "subject")]
)
def test_a_binding_crd_states_the_subject_the_services_decide_against(crd_name: str, field: str) -> None:
    """Both services bind to one `Subject`, so neither CRD may drift from it: a key the model does
    not know decodes to no variant, and a field a CRD stops requiring reaches the model absent.

    Checked against the model's own schema rather than against the other CRD -- two mirrors of one
    source, not two sources compared to each other.
    """
    declared = _model_subject()
    schema = _subject_schema(crd_name, field)

    assert (schema["type"], schema["minProperties"], schema["maxProperties"]) == ("object", 1, 1), (
        "the CRD must admit exactly one key, which is what makes the union unambiguous"
    )
    assert set(schema["properties"]) == set(declared)
    for key, (fields, required) in declared.items():
        crd_fields, crd_required = _variant(schema, key)
        assert crd_fields == fields, f"{key} declares different fields in the CRD than in the model"
        # A CRD may insist on a field the model tolerates absent -- ActionPolicyBinding requires the
        # Sandbox UID because it matches on it -- but never the reverse, which would let the API
        # server accept an object the model refuses.
        assert crd_required >= required, f"{key} lets the CRD accept what the model requires"


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
