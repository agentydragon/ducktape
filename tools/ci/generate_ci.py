#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.0", "pyyaml>=6.0"]
# ///
"""Generate .github/workflows/ci.yml from workflows.yaml.

This script reads the workflow definitions and generates the CI workflow file,
eliminating duplication in job definitions.

Usage:
    uv run tools/ci/generate_ci.py
    uv run tools/ci/generate_ci.py --check  # Verify ci.yml is up to date
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml
from models import WorkflowConfig, WorkflowManifest
from pydantic import BaseModel, Field

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent
WORKFLOWS_YAML = SCRIPT_DIR / "workflows.yaml"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

HEADER = """\
# AUTO-GENERATED from tools/ci/workflows.yaml - DO NOT EDIT DIRECTLY
# Regenerate with: uv run tools/ci/generate_ci.py
"""

BAZEL_DIFF_VERSION = "12.1.1"


class GHAStep(BaseModel):
    """A step in a GitHub Actions job."""

    name: str | None = None
    id: str | None = None
    uses: str | None = None
    run: str | None = None
    if_cond: str | None = Field(None, alias="if", serialization_alias="if")
    with_args: dict[str, Any] | None = Field(None, alias="with", serialization_alias="with")

    model_config = {"populate_by_name": True}


class GHAJob(BaseModel):
    """A job in a GitHub Actions workflow."""

    name: str | None = None
    runs_on: str | None = Field(None, alias="runs-on", serialization_alias="runs-on")
    timeout_minutes: int | None = Field(None, alias="timeout-minutes", serialization_alias="timeout-minutes")
    needs: str | None = None
    if_cond: str | None = Field(None, alias="if", serialization_alias="if")
    uses: str | None = None
    with_args: dict[str, str] | None = Field(None, alias="with", serialization_alias="with")
    secrets: str | None = None
    outputs: dict[str, str] | None = None
    steps: list[GHAStep] | None = None

    model_config = {"populate_by_name": True}


class GHAWorkflow(BaseModel):
    """A GitHub Actions workflow file."""

    name: str
    on: dict[str, Any] = Field(serialization_alias="on")
    concurrency: dict[str, Any]
    permissions: dict[str, str]
    jobs: dict[str, GHAJob]

    model_config = {"populate_by_name": True}


COMPUTE_TARGETS_JOB = GHAJob(
    name="Compute affected targets",
    runs_on="ubuntu-latest",
    timeout_minutes=30,
    outputs={
        "targets": "${{ steps.decide.outputs.targets }}",
        "workflows": "${{ steps.decide.outputs.workflows }}",
        "infra_changed": "${{ steps.decide.outputs.infra_changed }}",
    },
    steps=[
        GHAStep(uses="actions/checkout@v4", with_args={"fetch-depth": 0}),
        GHAStep(uses="astral-sh/setup-uv@v4"),
        GHAStep(uses="bazelbuild/setup-bazelisk@v3"),
        GHAStep(uses="actions/setup-java@v4", with_args={"distribution": "temurin", "java-version": "21"}),
        GHAStep(
            name="Cache bazel-diff JAR",
            id="cache-bazel-diff",
            uses="actions/cache@v4",
            with_args={"path": "bazel-diff.jar", "key": f"bazel-diff-{BAZEL_DIFF_VERSION}"},
        ),
        GHAStep(
            name="Download bazel-diff",
            if_cond="steps.cache-bazel-diff.outputs.cache-hit != 'true'",
            run=(
                f"curl -fsSL -o bazel-diff.jar \\\n"
                f'  "https://github.com/Tinder/bazel-diff/releases/download/{BAZEL_DIFF_VERSION}/bazel-diff_deploy.jar"'
            ),
        ),
        GHAStep(
            name="Cache bazel-diff hashes",
            uses="actions/cache@v4",
            with_args={
                "path": ".bazel-diff-cache",
                "key": "bazel-diff-hashes-${{ github.sha }}",
                "restore-keys": "bazel-diff-hashes-",
            },
        ),
        GHAStep(
            name="Set bazel-diff env",
            run='echo "BAZEL_DIFF_JAR=$PWD/bazel-diff.jar" >> $GITHUB_ENV\n'
            'echo "BAZEL_DIFF_CACHE_DIR=$PWD/.bazel-diff-cache" >> $GITHUB_ENV',
        ),
        GHAStep(name="Compute CI decision", id="decide", run="uv run tools/ci/ci_decide.py"),
    ],
)


def build_workflow_job(name: str, config: WorkflowConfig) -> GHAJob:
    """Build a job definition from workflow config."""
    with_args: dict[str, str] = {}
    if config.targets:
        with_args["targets"] = "${{ needs.compute-targets.outputs.targets }}"
    if config.inputs:
        with_args.update(config.inputs)

    return GHAJob(
        needs="compute-targets",
        if_cond=f"contains(fromJson(needs.compute-targets.outputs.workflows), '{name}')",
        uses=f"./.github/workflows/{name}.yml",
        with_args=with_args if with_args else None,
        secrets="inherit" if config.secrets else None,
    )


def generate_ci_config(manifest: WorkflowManifest) -> GHAWorkflow:
    """Generate the complete ci.yml config."""
    jobs: dict[str, GHAJob] = {"compute-targets": COMPUTE_TARGETS_JOB}
    for name, config in manifest.workflows.items():
        jobs[name] = build_workflow_job(name, config)

    return GHAWorkflow(
        name="CI",
        on={
            "push": {"branches": ["main", "master", "devel"]},
            "pull_request": None,
            "workflow_dispatch": {
                "inputs": {
                    "enable_profiling": {
                        "description": "Enable Bazel profiling (generates downloadable artifacts)",
                        "required": False,
                        "type": "boolean",
                        "default": False,
                    }
                }
            },
        },
        concurrency={"group": "${{ github.workflow }}-${{ github.ref }}", "cancel-in-progress": True},
        permissions={"contents": "read"},
        jobs=jobs,
    )


def generate_ci_yml(manifest: WorkflowManifest) -> str:
    """Generate the complete ci.yml content."""
    workflow = generate_ci_config(manifest)
    config = workflow.model_dump(by_alias=True, exclude_none=True)

    # Custom representer for multiline strings
    def str_representer(dumper: yaml.Dumper, data: str) -> yaml.Node:
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    yaml.add_representer(str, str_representer)

    yaml_content = yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)
    return HEADER + yaml_content


class OutOfDateError(Exception):
    """CI workflow file is out of date."""


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate ci.yml from workflows.yaml")
    parser.add_argument("--check", action="store_true", help="Check if ci.yml is up to date")
    args = parser.parse_args()

    manifest = WorkflowManifest.from_yaml(WORKFLOWS_YAML)
    generated = generate_ci_yml(manifest)

    if args.check:
        if not CI_YML.exists():
            raise FileNotFoundError(f"{CI_YML} does not exist")
        current = CI_YML.read_text()
        if current != generated:
            raise OutOfDateError(f"{CI_YML} is out of date. Run 'uv run tools/ci/generate_ci.py' to update.")
        print(f"{CI_YML} is up to date")
        return

    CI_YML.write_text(generated)
    print(f"Generated {CI_YML}")


if __name__ == "__main__":
    main()
