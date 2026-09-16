"""The kubeconform schemas under cluster/schemas/ are the Agentplane CRDs' openAPIV3Schema, and the
two binding kinds spell a subject the same way.

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

from util.bazel.runfiles import get_required_path
from x.agentplane.subjects import SANDBOX_KEY, SERVICE_ACCOUNT_KEY

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


_EGRESS_SUBJECT = _subject_schema("crd-egressbindings.yaml", "subjects")
_ACTION_SUBJECT = _subject_schema("crd-actionpolicybindings.yaml", "subject")


def test_both_binding_kinds_spell_a_subject_as_the_same_one_key_union() -> None:
    """`x.agentplane.subjects` discriminates on the key alone, for both services. That only decides
    correctly while both CRDs offer that same key set under the same one-of mechanism: a CRD that
    renamed a key, or dropped `maxProperties`, would hand the discriminator an object it reads as
    the wrong variant or as neither.
    """
    for subject in (_EGRESS_SUBJECT, _ACTION_SUBJECT):
        assert (subject["type"], subject["minProperties"], subject["maxProperties"]) == ("object", 1, 1)
        assert set(subject["properties"]) == {SANDBOX_KEY, SERVICE_ACCOUNT_KEY}


@pytest.mark.parametrize("key", [SANDBOX_KEY, SERVICE_ACCOUNT_KEY])
def test_a_field_both_binding_kinds_name_in_a_subject_means_the_same_thing(key: str) -> None:
    """What a reference carries legitimately differs -- the Action Service is multi-namespace and pins
    a Sandbox by UID, where the proxy serves one namespace and re-checks the UID when it
    authenticates -- so this pins the overlap rather than the roster: a field both declare has one
    schema, and `name` is always declared and always required.
    """
    egress, action = _EGRESS_SUBJECT["properties"][key], _ACTION_SUBJECT["properties"][key]
    shared = set(egress["properties"]) & set(action["properties"])

    assert "name" in shared
    assert "name" in egress["required"]
    assert "name" in action["required"]
    assert {field: egress["properties"][field] for field in shared} == {
        field: action["properties"][field] for field in shared
    }


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
