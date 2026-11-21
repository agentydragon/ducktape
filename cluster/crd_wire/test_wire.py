"""Exercise CRD-derived wire types against the objects this repository deploys."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
import yaml
from jsonschema import Draft7Validator, ValidationError as SchemaValidationError
from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic.errors import PydanticSchemaGenerationError
from python.runfiles import Runfiles
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifactsCopy

from cluster.crd_wire.artifact_generator import ArtifactGenerator
from cluster.crd_wire.sandbox_template import SandboxTemplate


def _objects(runfiles_root: Path, filename: str, kind: str) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all((runfiles_root / filename).read_text()) if doc and doc["kind"] == kind]


@pytest.fixture
def artifacts(runfiles: Runfiles) -> list[dict[str, Any]]:
    return _objects(
        runfiles.root(),
        "ducktape/cluster/generated/artifact-generators/artifact-generators.k8s.yaml",
        "ArtifactGenerator",
    )


@pytest.fixture
def templates(runfiles: Runfiles) -> list[dict[str, Any]]:
    return _objects(
        runfiles.root(), "ducktape/cluster/k8s/agentplane-staging/agentplane-staging.k8s.yaml", "SandboxTemplate"
    )


@pytest.fixture
def artifact_schema(runfiles: Runfiles) -> Draft7Validator:
    return Draft7Validator(
        json.loads((runfiles.root() / "ducktape/cluster/crd_wire/artifact_generator.json").read_text())
    )


@pytest.fixture
def sandbox_schema(runfiles: Runfiles) -> Draft7Validator:
    return Draft7Validator(
        json.loads((runfiles.root() / "ducktape/cluster/crd_wire/sandbox_template.json").read_text())
    )


def test_artifact_wire_keys_are_preserved(artifacts: list[dict[str, Any]], artifact_schema: Draft7Validator) -> None:
    assert artifacts
    for manifest in artifacts:
        artifact_schema.validate(manifest)
        wire = cast(ArtifactGenerator, manifest)
        copy = wire["spec"]["artifacts"][0]["copy"][0]
        assert copy["from"].startswith("@")
        assert copy["to"].startswith("@artifact/")
        assert TypeAdapter(ArtifactGenerator).validate_python(manifest) == manifest

    # The schema distinguishes a missing required wire key from an omitted optional key.
    invalid = deepcopy(artifacts[0])
    copy = invalid["spec"]["artifacts"][0]["copy"][0]
    assert "strategy" not in copy
    copy["from_"] = copy.pop("from")
    with pytest.raises(SchemaValidationError, match="'from' is a required property"):
        artifact_schema.validate(invalid)
    with pytest.raises(ValidationError, match=r"copy\.0\.from"):
        TypeAdapter(ArtifactGenerator).validate_python(invalid)
    copy["from"] = "missing-source-alias"
    with pytest.raises(SchemaValidationError, match="does not match"):
        artifact_schema.validate(invalid)


def test_sandbox_validation_keeps_opaque_claims(
    templates: list[dict[str, Any]], sandbox_schema: Draft7Validator
) -> None:
    assert templates
    for manifest in templates:
        expected = deepcopy(manifest)
        sandbox_schema.validate(manifest)
        wire = cast(SandboxTemplate, manifest)
        assert wire["spec"]["stateVolume"]["claim"]["resources"]["requests"]["storage"]
        assert manifest == expected

    firecracker = next(manifest for manifest in templates if "firecracker" in manifest["spec"])
    copy = deepcopy(firecracker)
    copy["spec"]["firecracker"]["memoryMiB"] = 127
    with pytest.raises(SchemaValidationError, match="less than the minimum"):
        sandbox_schema.validate(copy)

    typed_adapter = TypeAdapter(SandboxTemplate)
    parsed = typed_adapter.validate_python(copy)
    assert parsed["spec"]["firecracker"]["memoryMiB"] == 127
    raw_claim = copy["spec"]["workspaceVolume"]["claim"]
    assert "resources" in raw_claim
    assert "resources" not in parsed["spec"]["workspaceVolume"]["claim"]


def test_sandbox_cel_remains_an_admission_check(
    templates: list[dict[str, Any]], sandbox_schema: Draft7Validator
) -> None:
    invalid = deepcopy(next(manifest for manifest in templates if "pod" in manifest["spec"]))
    invalid["spec"]["vm"] = {}
    assert sandbox_schema.is_valid(invalid)


def test_cdk8s_copy_props_do_not_deserialize_wire_keys(artifacts: list[dict[str, Any]]) -> None:
    wire = artifacts[0]["spec"]["artifacts"][0]["copy"][0]
    with pytest.raises(TypeError, match="unexpected keyword argument 'from'"):
        ArtifactGeneratorSpecArtifactsCopy(**wire)
    props = ArtifactGeneratorSpecArtifactsCopy(from_=wire["from"], to=wire["to"])
    assert props.from_ == wire["from"]

    with pytest.raises(PydanticSchemaGenerationError, match="Unable to generate pydantic-core schema"):
        TypeAdapter(ArtifactGeneratorSpecArtifactsCopy)
    adapter = TypeAdapter(ArtifactGeneratorSpecArtifactsCopy, config=ConfigDict(arbitrary_types_allowed=True))
    assert adapter.validate_python(props) is props
    with pytest.raises(ValidationError, match="should be an instance"):
        adapter.validate_python(wire)


if __name__ == "__main__":
    pytest_bazel.main()
