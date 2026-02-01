"""Verify generated CI workflow files are up to date with workflows.yaml."""

import pytest_bazel

from tools.ci.generate_ci_lib import (
    WORKFLOWS_DIR,
    WORKFLOWS_YAML,
    OutOfDateError,
    check_workflow,
    generate_ci_config,
    generate_release_config,
)
from tools.ci.models import WorkflowManifest


def test_ci_yml_up_to_date() -> None:
    manifest = WorkflowManifest.from_yaml(WORKFLOWS_YAML)
    expected = generate_ci_config(manifest)
    ci_yml = WORKFLOWS_DIR / "ci.yml"
    check_workflow(ci_yml, expected)


def test_release_workflows_up_to_date() -> None:
    manifest = WorkflowManifest.from_yaml(WORKFLOWS_YAML)
    for name, config in manifest.releases.items():
        expected = generate_release_config(name, config)
        path = WORKFLOWS_DIR / f"{name}-release.yml"
        try:
            check_workflow(path, expected)
        except (FileNotFoundError, OutOfDateError) as exc:
            raise AssertionError(f"{path.name} is out of date") from exc


if __name__ == "__main__":
    pytest_bazel.main()
