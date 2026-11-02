"""The committed CRDs are what `generate` writes (STYLE.md § Testing, generated-output snapshot):
regenerate with `bb run //agentplane/crds:generate_bin` and commit the result when this fails.
"""

from pathlib import Path

import pytest_bazel
import yaml
from jsonschema import Draft4Validator
from referencing import Registry

from agentplane.crds.generate import (
    CRD_FILES,
    CRDS_DIR,
    EGRESS_CRDS,
    EGRESS_GENERATED_CRDS,
    POLICY_CRDS,
    POLICY_GENERATED_CRDS,
    SANDBOX_CRDS,
    SANDBOX_GENERATED_CRDS,
    generated_files,
)
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir


def _committed(relative: Path) -> str:
    return get_required_path(f"_main/{relative}").read_text()


def test_committed_files_are_generated() -> None:
    sandbox_generated = get_required_path(SANDBOX_GENERATED_CRDS)
    egress_generated = get_required_path(EGRESS_GENERATED_CRDS)
    sandbox_crds = {name: (sandbox_generated / source).read_text() for name, source in SANDBOX_CRDS.items()}
    egress_crds = {name: (egress_generated / source).read_text() for name, source in EGRESS_CRDS.items()}
    policy_generated = get_required_path(POLICY_GENERATED_CRDS)
    policy_crds = {name: (policy_generated / source).read_text() for name, source in POLICY_CRDS.items()}
    for relative, content in generated_files(
        {name: _committed(CRDS_DIR / name) for name in CRD_FILES}, sandbox_crds, egress_crds, policy_crds
    ):
        (undeclared_outputs_dir() / relative.name).write_text(content)
        assert content == _committed(relative), f"{relative} is stale"


def test_policybinding_schema_preserves_validation_and_condition_ownership() -> None:
    crd = yaml.safe_load(_committed(CRDS_DIR / "crd-policybindings.yaml"))
    version = crd["spec"]["versions"][0]
    assert version["subresources"] == {"status": {}}
    properties = version["schema"]["openAPIV3Schema"]["properties"]
    conditions = properties["status"]["properties"]["conditions"]
    assert conditions["x-kubernetes-list-type"] == "map"
    assert conditions["x-kubernetes-list-map-keys"] == ["type"]
    validator = Draft4Validator(properties["spec"], registry=Registry())
    subject = {"namespace": "workloads", "name": "runner"}
    assert validator.is_valid({"subject": subject, "policySets": ["basic"]})
    for invalid in (
        {"subject": {}, "policySets": ["basic"]},
        {"subject": {"namespace": "", "name": "runner"}, "policySets": ["basic"]},
        {"subject": {"namespace": "workloads", "name": ""}, "policySets": ["basic"]},
        {"subject": subject, "policySets": []},
        {"subject": subject, "policySets": [""]},
        {"subject": subject},
        {"policySets": ["basic"]},
    ):
        assert not validator.is_valid(invalid), invalid


def test_credential_schema_preserves_source_and_target_constraints() -> None:
    crd = yaml.safe_load(_committed(CRDS_DIR / "crd-egresscredentials.yaml"))
    for version in crd["spec"]["versions"]:
        spec = version["schema"]["openAPIV3Schema"]["properties"]["spec"]
        assert set(spec["required"]) == {"description", "source", "targets"}
        assert spec["properties"]["description"]["minLength"] == 1
        assert spec["properties"]["targets"]["minItems"] == 1

        source = spec["properties"]["source"]
        assert source["properties"]["authenticatedWorkloadToken"]["maxProperties"] == 0
        validator = Draft4Validator(source, registry=Registry())
        assert validator.is_valid({"secretRef": {"name": "token", "key": "value"}})
        assert validator.is_valid({"authenticatedWorkloadToken": {}})
        assert validator.is_valid({"projectedWorkloadToken": {"audience": "api"}})
        assert "filter(x,x==true).size() == 1" in source["x-kubernetes-validations"][0]["rule"]

        target = spec["properties"]["targets"]["items"]
        assert target["properties"]["method"]["enum"] == [
            "wholeValue",
            "schemeToken",
            "basicUsername",
            "basicPassword",
            "basicWhole",
            "jsonField",
        ]
        assert target["properties"]["field"]["maxLength"] == 256
        assert target["x-kubernetes-validations"]


def test_sandbox_resume_policy_is_optional_and_defaults_to_deny() -> None:
    crd = yaml.safe_load(_committed(CRDS_DIR / "crd-sandboxes.yaml"))
    spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    assert "resumePolicy" not in spec.get("required", [])
    policy = spec["properties"]["resumePolicy"]
    assert policy["type"] == "object"
    allow = policy["properties"]["allowOwnServiceAccount"]
    assert allow["type"] == "boolean"
    assert allow["default"] is False
    assert "allowOwnServiceAccount" not in policy.get("required", [])


if __name__ == "__main__":
    pytest_bazel.main()
