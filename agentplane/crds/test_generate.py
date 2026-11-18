"""The committed CRDs are what `generate` writes (STYLE.md § Testing, generated-output snapshot):
regenerate with `bb run //agentplane/crds:generate_bin` and commit the result when this fails.
"""

from pathlib import Path

import pytest_bazel
import yaml
from jsonschema import Draft4Validator
from python.runfiles import Runfiles
from referencing import Registry

from agentplane.crds.generate import CRDS_DIR, render_crds
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def _committed(relative: Path, runfiles: Runfiles) -> str:
    return (runfiles.root() / f"ducktape/{relative}").read_text()


def test_committed_files_are_generated(runfiles: Runfiles) -> None:
    for relative, content in render_crds(runfiles.root()):
        (undeclared_outputs_dir() / relative.name).write_text(content)
        assert content == _committed(relative, runfiles), f"{relative} is stale"


def test_policybinding_schema_preserves_validation_and_condition_ownership(runfiles: Runfiles) -> None:
    crd = yaml.safe_load(_committed(CRDS_DIR / "crd-policybindings.yaml", runfiles))
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
        {"subject": {"namespace": "", "name": "runner"}, "policySets": ["basic"]},
        {"subject": {"namespace": "workloads", "name": ""}, "policySets": ["basic"]},
        {"subject": subject, "policySets": []},
        {"subject": subject, "policySets": [""]},
        {"subject": subject},
        {"policySets": ["basic"]},
    ):
        assert not validator.is_valid(invalid), invalid


def test_credential_schema_preserves_source_and_target_constraints(runfiles: Runfiles) -> None:
    crd = yaml.safe_load(_committed(CRDS_DIR / "crd-egresscredentials.yaml", runfiles))
    for version in crd["spec"]["versions"]:
        spec = version["schema"]["openAPIV3Schema"]["properties"]["spec"]
        assert set(spec["required"]) == {"description", "source", "targets"}
        assert spec["properties"]["description"]["minLength"] == 1
        assert spec["properties"]["targets"]["minItems"] == 1

        source = spec["properties"]["source"]
        assert set(source["properties"]) == {"secretRef", "kubernetesServiceAccount"}
        validator = Draft4Validator(source, registry=Registry())
        assert validator.is_valid({"secretRef": {"name": "token", "key": "value"}})
        assert validator.is_valid({"kubernetesServiceAccount": {"namespace": "accounts", "name": "diagnostics"}})
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


if __name__ == "__main__":
    pytest_bazel.main()
