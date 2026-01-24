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
import sys
from pathlib import Path
from typing import Any

import yaml
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


class WorkflowConfig(BaseModel):
    """Configuration for a workflow from workflows.yaml."""

    bazel_pattern: str | None = None
    path_pattern: str | None = None
    always: bool = False
    targets: bool = False
    inputs: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)


class WorkflowManifest(BaseModel):
    """Collection of all workflow configurations."""

    workflows: dict[str, WorkflowConfig]

    @classmethod
    def from_yaml(cls, path: Path) -> WorkflowManifest:
        """Load from YAML file."""
        with path.open() as f:
            data = yaml.safe_load(f)
        workflows = {name: WorkflowConfig.model_validate(config) for name, config in data.items()}
        return cls(workflows=workflows)


class GHAStep(BaseModel):
    """A step in a GitHub Actions job."""

    name: str | None = None
    id: str | None = None
    uses: str | None = None
    run: str | None = None
    if_cond: str | None = Field(None, alias="if")
    with_args: dict[str, Any] | None = Field(None, alias="with")

    model_config = {"populate_by_name": True}

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for YAML output, omitting None values."""
        d: dict[str, Any] = {}
        if self.name:
            d["name"] = self.name
        if self.id:
            d["id"] = self.id
        if self.uses:
            d["uses"] = self.uses
        if self.if_cond:
            d["if"] = self.if_cond
        if self.run:
            d["run"] = self.run
        if self.with_args:
            d["with"] = self.with_args
        return d


class GHAJob(BaseModel):
    """A job in a GitHub Actions workflow."""

    name: str | None = None
    runs_on: str | None = Field(None, alias="runs-on")
    timeout_minutes: int | None = Field(None, alias="timeout-minutes")
    needs: str | None = None
    if_cond: str | None = Field(None, alias="if")
    uses: str | None = None
    with_args: dict[str, str] | None = Field(None, alias="with")
    secrets: str | None = None  # "inherit" to pass all secrets
    outputs: dict[str, str] | None = None
    steps: list[GHAStep] | None = None

    model_config = {"populate_by_name": True}

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for YAML output."""
        d: dict[str, Any] = {}
        if self.name:
            d["name"] = self.name
        if self.runs_on:
            d["runs-on"] = self.runs_on
        if self.timeout_minutes:
            d["timeout-minutes"] = self.timeout_minutes
        if self.needs:
            d["needs"] = self.needs
        if self.if_cond:
            d["if"] = self.if_cond
        if self.uses:
            d["uses"] = self.uses
        if self.outputs:
            d["outputs"] = self.outputs
        if self.with_args:
            d["with"] = self.with_args
        if self.secrets:
            d["secrets"] = self.secrets
        if self.steps:
            d["steps"] = [s.to_dict() for s in self.steps]
        return d


def build_compute_targets_job() -> GHAJob:
    """Build the compute-targets job definition."""
    return GHAJob(
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
            GHAStep(uses="bazelbuild/setup-bazelisk@v3"),
            GHAStep(uses="actions/setup-java@v4", with_args={"distribution": "temurin", "java-version": "21"}),
            GHAStep(
                name="Cache bazel-diff",
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
            GHAStep(name="Set bazel-diff path", run='echo "BAZEL_DIFF_JAR=$PWD/bazel-diff.jar" >> $GITHUB_ENV'),
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


def generate_ci_config(manifest: WorkflowManifest) -> dict[str, Any]:
    """Generate the complete ci.yml config dict."""
    jobs: dict[str, Any] = {"compute-targets": build_compute_targets_job().to_dict()}

    for name, config in manifest.workflows.items():
        jobs[name] = build_workflow_job(name, config).to_dict()

    return {
        "name": "CI",
        "on": {
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
        "concurrency": {"group": "${{ github.workflow }}-${{ github.ref }}", "cancel-in-progress": True},
        "permissions": {"contents": "read"},
        "jobs": jobs,
    }


def generate_ci_yml(manifest: WorkflowManifest) -> str:
    """Generate the complete ci.yml content."""
    config = generate_ci_config(manifest)

    # Custom representer for multiline strings
    def str_representer(dumper: yaml.Dumper, data: str) -> yaml.Node:
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    yaml.add_representer(str, str_representer)

    yaml_content = yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)
    return HEADER + yaml_content


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate ci.yml from workflows.yaml")
    parser.add_argument("--check", action="store_true", help="Check if ci.yml is up to date (exit 1 if not)")
    args = parser.parse_args()

    manifest = WorkflowManifest.from_yaml(WORKFLOWS_YAML)
    generated = generate_ci_yml(manifest)

    if args.check:
        if not CI_YML.exists():
            print(f"Error: {CI_YML} does not exist", file=sys.stderr)
            return 1
        current = CI_YML.read_text()
        if current != generated:
            print(f"Error: {CI_YML} is out of date. Run 'uv run tools/ci/generate_ci.py' to update.", file=sys.stderr)
            return 1
        print(f"{CI_YML} is up to date")
        return 0

    CI_YML.write_text(generated)
    print(f"Generated {CI_YML}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
