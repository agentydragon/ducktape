"""Generate .github/workflows/ci.yml from workflows.yaml.

This script reads the workflow definitions and generates the CI workflow file,
eliminating duplication in job definitions.

This module provides the implementation logic. See generate_ci.py for the CLI entry point.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from tools.ci.models import Job, Step, Workflow, WorkflowConfig, WorkflowManifest

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent
WORKFLOWS_YAML = SCRIPT_DIR / "workflows.yaml"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

HEADER = """\
# AUTO-GENERATED from tools/ci/workflows.yaml - DO NOT EDIT DIRECTLY
# Regenerate with: uv run tools/ci/generate_ci.py
"""

BAZEL_DIFF_VERSION = "12.1.1"


COMPUTE_TARGETS_JOB = Job(
    name="Compute affected targets",
    runs_on="ubuntu-latest",
    timeout_minutes=30,
    outputs={
        "targets": "${{ steps.decide.outputs.targets }}",
        "workflows": "${{ steps.decide.outputs.workflows }}",
        "infra_changed": "${{ steps.decide.outputs.infra_changed }}",
    },
    steps=[
        Step(uses="actions/checkout@v4", with_args={"fetch-depth": 0}),
        Step(uses="astral-sh/setup-uv@v4"),
        Step(uses="bazelbuild/setup-bazelisk@v3"),
        Step(uses="actions/setup-java@v4", with_args={"distribution": "temurin", "java-version": "21"}),
        Step(
            name="Cache bazel-diff JAR",
            id="cache-bazel-diff",
            uses="actions/cache@v4",
            with_args={"path": "bazel-diff.jar", "key": f"bazel-diff-{BAZEL_DIFF_VERSION}"},
        ),
        Step(
            name="Download bazel-diff",
            if_cond="steps.cache-bazel-diff.outputs.cache-hit != 'true'",
            run=(
                f"curl -fsSL -o bazel-diff.jar \\\n"
                f'  "https://github.com/Tinder/bazel-diff/releases/download/{BAZEL_DIFF_VERSION}/bazel-diff_deploy.jar"'
            ),
        ),
        Step(
            name="Cache bazel-diff hashes",
            uses="actions/cache@v4",
            with_args={
                "path": ".bazel-diff-cache",
                "key": "bazel-diff-hashes-${{ github.sha }}",
                "restore-keys": "bazel-diff-hashes-",
            },
        ),
        Step(
            name="Set bazel-diff env",
            run='echo "BAZEL_DIFF_JAR=$PWD/bazel-diff.jar" >> $GITHUB_ENV\n'
            'echo "BAZEL_DIFF_CACHE_DIR=$PWD/.bazel-diff-cache" >> $GITHUB_ENV\n'
            'echo "BAZEL_QUERY_LOG_DIR=$PWD/bazel-query-logs" >> $GITHUB_ENV',
        ),
        Step(name="Compute CI decision", id="decide", run="uv run tools/ci/ci_decide.py"),
        Step(
            name="Debug query logs directory",
            if_cond="always()",
            run='echo "BAZEL_QUERY_LOG_DIR=$BAZEL_QUERY_LOG_DIR"\n'
            'echo "PWD=$PWD"\n'
            'ls -la "$BAZEL_QUERY_LOG_DIR" 2>/dev/null || echo "Directory does not exist"',
        ),
        Step(
            name="Upload bazel query logs",
            if_cond="failure()",
            uses="actions/upload-artifact@v4",
            with_args={
                "name": "bazel-query-logs-${{ github.run_id }}",
                "path": "bazel-query-logs",
                "if-no-files-found": "ignore",
            },
        ),
        Step(
            name="Upload targets file",
            uses="actions/upload-artifact@v4",
            with_args={"name": "targets", "path": "targets.txt", "if-no-files-found": "error"},
        ),
    ],
)


def build_workflow_job(name: str, config: WorkflowConfig) -> Job:
    """Build a job definition from workflow config."""
    with_args: dict[str, str] = {}
    if config.targets:
        with_args["targets"] = "${{ needs.compute-targets.outputs.targets }}"
    if config.inputs:
        with_args.update(config.inputs)

    return Job(
        needs="compute-targets",
        if_cond=f"contains(fromJson(needs.compute-targets.outputs.workflows), '{name}')",
        uses=f"./.github/workflows/{name}.yml",
        with_args=with_args if with_args else None,
        secrets="inherit" if config.secrets else None,
    )


def generate_ci_config(manifest: WorkflowManifest) -> Workflow:
    """Generate the complete ci.yml config."""
    jobs: dict[str, Job] = {"compute-targets": COMPUTE_TARGETS_JOB}
    for name, config in manifest.workflows.items():
        jobs[name] = build_workflow_job(name, config)

    return Workflow(
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


def generate_ci_yml(workflow: Workflow) -> str:
    """Generate the complete ci.yml content."""
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
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Generate ci.yml from workflows.yaml")
    parser.add_argument("--check", action="store_true", help="Check if ci.yml is semantically up to date")
    args = parser.parse_args()

    manifest = WorkflowManifest.from_yaml(WORKFLOWS_YAML)
    expected = generate_ci_config(manifest)

    if args.check:
        if not CI_YML.exists():
            raise FileNotFoundError(f"{CI_YML} does not exist")
        # Compare parsed models to ignore formatting differences (prettier, etc.)
        current = Workflow.model_validate(yaml.safe_load(CI_YML.read_text()))
        if current != expected:
            raise OutOfDateError(f"{CI_YML} is out of date. Run 'uv run tools/ci/generate_ci.py' to update.")
        print(f"{CI_YML} is up to date")
        return

    CI_YML.write_text(generate_ci_yml(expected))
    print(f"Generated {CI_YML}")
