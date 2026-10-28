from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import pytest_bazel
import yaml
from cdk8s import App
from jsonschema import Draft202012Validator, ValidationError

from devinfra.x.schema_codegen.infrastructure.kubernetes.construct_consumer import widget_chart
from util.bazel.runfiles import get_required_path


def test_generated_construct_synthesizes_against_its_crd(tmp_path: Path) -> None:
    outdir = tmp_path / "synth"
    app = App(outdir=str(outdir))
    widget_chart(app)
    app.synth()

    [resource] = yaml.safe_load_all((outdir / "widgets.k8s.yaml").read_text())
    assert resource["apiVersion"] == "codegen.schema.example.test/v1alpha1"
    assert resource["kind"] == "Widget"
    assert resource["spec"] == {"title": "example", "replicas": 2}

    crd_path = get_required_path("_main/devinfra/x/schema_codegen/infrastructure/kubernetes/widget.crd.yaml")
    crd = yaml.safe_load(crd_path.read_text())
    resource_schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    Draft202012Validator.check_schema(resource_schema)
    validator = Draft202012Validator(resource_schema)
    validator.validate(resource)

    invalid_resource = deepcopy(resource)
    invalid_resource["spec"]["replicas"] = 0
    with pytest.raises(ValidationError):
        validator.validate(invalid_resource)


if __name__ == "__main__":
    pytest_bazel.main()
