import json

import pytest_bazel
from python.runfiles import Runfiles

from cluster.cdk8s.opentofu import inline_module


def test_generated_provider_requirements_are_inlined():
    root = Runfiles.CreateOrRaise().root() / "ducktape/tf/gitops/gitlab-spike"
    expected = json.loads((root / "versions.tf.json").read_text())["terraform"]
    module = inline_module("tf/gitops/gitlab-spike", generated_versions=True)
    assert f"required_version = {json.dumps(expected['required_version'])}" in module
    for name, provider in expected["required_providers"].items():
        assert (
            f"{name} = {{ source = {json.dumps(provider['source'])}, version = {json.dumps(provider['version'])} }}"
            in module
        )
    assert 'resource "gitlab_project" "acceptance"' in module


if __name__ == "__main__":
    pytest_bazel.main()
