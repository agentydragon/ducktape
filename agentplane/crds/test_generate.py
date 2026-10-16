"""The committed binding CRDs and kubeconform schemas are what `generate` writes (STYLE.md § Testing,
generated-output snapshot): regenerate with `bb run //agentplane/crds:generate_bin` and commit the
result when this fails.
"""

import re
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from jsonschema import Draft4Validator
from referencing import Registry

from agentplane.crds.generate import CRD_FILES, CRDS_DIR, GENERATED_CRDS, SANDBOX_CRDS, generated_files
from util.bazel.runfiles import get_required_path


def _committed(relative: Path) -> str:
    return get_required_path(f"_main/{relative}").read_text()


def test_committed_files_are_generated() -> None:
    generated = get_required_path(GENERATED_CRDS)
    sandbox_crds = {name: (generated / source).read_text() for name, source in SANDBOX_CRDS.items()}
    for relative, content in generated_files({name: _committed(CRDS_DIR / name) for name in CRD_FILES}, sandbox_crds):
        assert content == _committed(relative), f"{relative} is stale"


@pytest.mark.parametrize(
    ("host", "valid"),
    [
        ("*", True),
        ("example.com", True),
        ("*.example.com", True),
        ("*example.com", False),
        ("example.*", False),
        ("**", False),
        ("", False),
    ],
)
def test_policy_host_schema_admits_only_supported_wildcards(host: str, valid: bool) -> None:
    crd = yaml.safe_load(_committed(CRDS_DIR / "crd-policysets.yaml"))
    for version in crd["spec"]["versions"]:
        rule = version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["rules"]["items"]
        pattern = rule["properties"]["hosts"]["items"]["pattern"]
        assert bool(re.fullmatch(pattern, host)) is valid


@pytest.mark.parametrize(
    ("target", "valid"),
    [
        ({"method": "jsonField", "field": "password"}, True),
        ({"method": "jsonField"}, False),
        ({"method": "jsonField", "field": "password", "header": "Authorization"}, False),
        ({"method": "jsonField", "field": "password", "scheme": "Bearer"}, False),
        ({"method": "jsonField", "field": ""}, False),
        ({"method": "schemeToken", "header": "Authorization", "scheme": "Bearer"}, True),
        ({"method": "schemeToken", "header": "Authorization"}, False),
        ({"method": "wholeValue", "header": "X-Key"}, True),
        ({"method": "wholeValue", "field": "password"}, False),
        ({"method": "wholeValue", "header": "X-Key", "scheme": "Bearer"}, False),
    ],
)
def test_credential_target_schema_is_unambiguous(target: dict[str, str], valid: bool) -> None:
    crd = yaml.safe_load(_committed(CRDS_DIR / "crd-egresscredentials.yaml"))
    for version in crd["spec"]["versions"]:
        schema = version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["targets"]["items"]
        # Draft 4 includes Kubernetes' structural oneOf/not, but not if/then/else.
        assert Draft4Validator(schema, registry=Registry()).is_valid(target) is valid


@pytest.mark.parametrize(
    ("layout", "mode", "valid"),
    [
        ({"type": "DirectoryTree"}, None, True),
        ({"type": "DirectoryTree"}, "Filesystem", True),
        ({"type": "DirectoryTree", "filesystem": "ext4"}, None, True),
        ({"type": "DirectoryTree"}, "Block", True),
        ({"type": "RawDiskImage", "filesystem": "ext4"}, None, True),
        ({"type": "RawDiskImage", "filesystem": "ext4"}, "Filesystem", True),
        ({"type": "RawDiskImage"}, None, True),
        ({"type": "RawDiskImage", "filesystem": "xfs"}, None, False),
        ({"type": "RawDiskImage", "filesystem": "ext4"}, "Block", True),
        ({"type": "BlockFilesystem", "filesystem": "ext4"}, "Block", True),
        ({"type": "BlockFilesystem", "filesystem": "ext4"}, None, True),
        ({"type": "BlockFilesystem", "filesystem": "ext4"}, "Filesystem", True),
        ({"type": "Other"}, None, False),
        ({}, None, False),
        (None, None, False),
    ],
)
def test_workspace_structural_schema(layout: dict[str, str] | None, mode: str | None, valid: bool) -> None:
    crd = yaml.safe_load(_committed(CRDS_DIR / "crd-sandboxtemplates.yaml"))
    workspace: dict[str, object] = {"name": "workspace", "claim": {} if mode is None else {"volumeMode": mode}}
    if layout is not None:
        workspace["layout"] = layout
    for version in crd["spec"]["versions"]:
        schema = version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["workspaceVolume"]
        # Draft 4 does not evaluate the generated CEL rules below.
        assert Draft4Validator(schema, registry=Registry()).is_valid(workspace) is valid


def test_state_volume_schema_requires_explicit_directory_tree_layout() -> None:
    crd = yaml.safe_load(_committed(CRDS_DIR / "crd-sandboxtemplates.yaml"))
    state_volume_schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"][
        "stateVolume"
    ]
    validator = Draft4Validator(state_volume_schema, registry=Registry())
    for state_volume, valid in [
        ({"name": "state", "claim": {}}, False),
        ({"name": "state", "claim": {}, "layout": {"type": "DirectoryTree"}}, True),
        ({"name": "state", "claim": {}, "layout": {"type": "DirectoryTree", "filesystem": "ext4"}}, True),
        ({"name": "state", "claim": {}, "layout": {"type": "RawDiskImage", "filesystem": "ext4"}}, True),
        ({"name": "state", "claim": {}, "layout": {}}, False),
    ]:
        assert validator.is_valid(state_volume) is valid, state_volume


def test_sandbox_crd_validation_rules_and_defaults() -> None:
    template = yaml.safe_load(_committed(CRDS_DIR / "crd-sandboxtemplates.yaml"))
    sandbox = yaml.safe_load(_committed(CRDS_DIR / "crd-sandboxes.yaml"))
    template_schema = template["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    sandbox_schema = sandbox["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    template_spec = template_schema["properties"]["spec"]
    sandbox_spec = sandbox_schema["properties"]["spec"]

    assert set(template_spec["required"]) == {"stateVolume"}
    assert {v["rule"] for v in template_spec["x-kubernetes-validations"]} == {
        "[has(self.pod),has(self.vm)].filter(x,x==true).size() == 1",
        "!has(self.workspaceVolume) || !has(self.pod) || self.workspaceVolume.layout.type == 'DirectoryTree'",
    }
    state = template_spec["properties"]["stateVolume"]
    workspace = template_spec["properties"]["workspaceVolume"]
    assert {v["rule"] for v in state["properties"]["layout"]["x-kubernetes-validations"]} >= {
        "self.type == 'DirectoryTree' && !has(self.filesystem)"
    }
    assert {v["rule"] for v in workspace["properties"]["layout"]["x-kubernetes-validations"]} == {
        "self.type == 'DirectoryTree' ? !has(self.filesystem) : has(self.filesystem)"
    }
    assert {v["rule"] for v in workspace["x-kubernetes-validations"]} == {
        "self.layout.type == 'BlockFilesystem' ? has(self.claim.volumeMode) && self.claim.volumeMode == 'Block' : !has(self.claim.volumeMode) || self.claim.volumeMode == 'Filesystem'"
    }
    assert workspace["properties"]["claim"]["properties"]["volumeMode"]["type"] == "string"
    assert workspace["properties"]["claim"]["x-kubernetes-preserve-unknown-fields"] is True
    assert state["properties"]["claim"]["x-kubernetes-preserve-unknown-fields"] is True
    for field in ("pod", "vm"):
        assert template_spec["properties"][field]["x-kubernetes-preserve-unknown-fields"] is True

    assert set(sandbox_spec["required"]) == {"template"}
    for field, default in (("running", False), ("wakeOnSend", False), ("wakeGeneration", 0)):
        assert sandbox_spec["properties"][field]["default"] == default
    assert sandbox_spec["properties"]["policySets"]["x-kubernetes-list-type"] == "set"
    status = sandbox_schema["properties"]["status"]
    assert {v["rule"] for v in status["x-kubernetes-validations"]} == {
        "has(self.suspendedBy) == has(self.suspendedAtWakeGeneration)"
    }
    assert status["properties"]["conditions"]["x-kubernetes-list-type"] == "map"
    assert status["properties"]["conditions"]["x-kubernetes-list-map-keys"] == ["type"]
    assert {v["rule"] for v in sandbox_schema["x-kubernetes-validations"]} == {"size(self.metadata.name) <= 63"}


if __name__ == "__main__":
    pytest_bazel.main()
