"""Verify generated CI workflow files are up to date with workflows.yaml."""

import pytest_bazel
import yaml

from tools.ci.generate_ci import WORKFLOWS_DIR, WORKFLOWS_YAML, Workflow, generate_ci_config, generate_release_config
from tools.ci.models import WorkflowManifest


class OutOfDateError(Exception):
    """CI workflow file is out of date."""


def check_workflow(path, expected: Workflow) -> None:
    """Check if a workflow file is semantically up to date."""
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")
    current = Workflow.model_validate(yaml.safe_load(path.read_text()))
    if current != expected:
        raise OutOfDateError(f"{path} is out of date. Run 'bazel run //tools/ci:generate_ci_bin' to update.")


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
