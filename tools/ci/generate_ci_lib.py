"""Generate .github/workflows/ from workflows.yaml.

Generates ci.yml and per-package release workflow files,
eliminating duplication in job definitions.

The generated YAML may differ in formatting from what prettier produces.
The pre-commit hook will normalize formatting on commit; the --check mode
compares parsed models to ignore such differences.

This module provides the implementation logic. See generate_ci.py for the CLI entry point.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from tools.ci.github_actions import Job, Step, Workflow
from tools.ci.models import ReleaseConfig, WorkflowConfig, WorkflowManifest

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent
WORKFLOWS_YAML = SCRIPT_DIR / "workflows.yaml"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
CI_YML = WORKFLOWS_DIR / "ci.yml"

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
            name="Set CI env",
            run='echo "BAZEL_DIFF_JAR=$PWD/bazel-diff.jar" >> $GITHUB_ENV\n'
            'echo "BAZEL_DIFF_CACHE_DIR=$PWD/.bazel-diff-cache" >> $GITHUB_ENV\n'
            'echo "BAZEL_QUERY_LOG_DIR=$PWD/bazel-query-logs" >> $GITHUB_ENV\n'
            'echo "CI_PUSH_STRATEGY=incremental" >> $GITHUB_ENV\n'
            'echo "CI_WORKFLOWS_MANIFEST=$PWD/tools/ci/workflows.yaml" >> $GITHUB_ENV',
        ),
        Step(name="Compute CI decision", id="decide", run="uv run tools/ci/ci_decide.py"),
        Step(
            name="Upload targets file",
            if_cond="always()",
            uses="actions/upload-artifact@v4",
            with_args={"name": "targets", "path": "targets.txt", "if-no-files-found": "ignore"},
        ),
        Step(
            name="Upload query logs",
            if_cond="always()",
            uses="actions/upload-artifact@v4",
            with_args={
                "name": "bazel-query-logs-${{ github.run_id }}",
                "path": "bazel-query-logs",
                "if-no-files-found": "ignore",
            },
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


def generate_release_config(name: str, config: ReleaseConfig) -> Workflow:
    """Generate a release workflow for a package."""
    latest_release_tag = f"{name}-latest"

    check_job = Job(
        name="Check if release needed",
        runs_on="ubuntu-latest",
        outputs={"release_needed": "${{ steps.check.outputs.release_needed }}"},
        steps=[
            Step(name="Check out code", uses="actions/checkout@v4", with_args={"fetch-depth": 0}),
            Step(
                uses="./.github/actions/setup-bazel",
                id="bazel",
                with_args={"buildbuddy_api_key": "${{ secrets.BUILDBUDDY_API_KEY }}"},
            ),
            Step(
                name="Check if release needed",
                id="check",
                uses="./.github/actions/check-release-needed",
                with_args={
                    "package_prefix": name,
                    "bazel_target": config.bazel_target,
                    "latest_release_tag": latest_release_tag,
                },
            ),
            Step(
                uses="./.github/actions/bazel-cache-save",
                if_cond="always()",
                with_args={
                    "cache-hit": "${{ steps.bazel.outputs.cache-hit }}",
                    "cache-key": "${{ steps.bazel.outputs.cache-key }}",
                    "skip_cache": "${{ steps.bazel.outputs.buildbuddy-enabled }}",
                },
            ),
        ],
    )

    release_with: dict[str, str] = {
        "package_name": name,
        "wheel_name": name.replace("-", "_"),
        "bazel_target": config.bazel_target,
        "wheel_path": config.wheel_path,
        "release_body": config.release_body,
        "latest_release_tag": latest_release_tag,
    }
    if config.apt_packages:
        release_with["apt_packages"] = " ".join(config.apt_packages)

    release_job = Job(
        needs="check",
        if_cond="needs.check.outputs.release_needed == 'true' || inputs.force_release == true",
        uses="./.github/workflows/python-wheel-release.yml",
        with_args=release_with,
        secrets="inherit",
    )

    return Workflow(
        name=f"{name} Release",
        on={
            "push": {"branches": ["devel", "main"]},
            "workflow_dispatch": {
                "inputs": {
                    "force_release": {
                        "description": "Force release even if no changes detected",
                        "required": False,
                        "default": False,
                        "type": "boolean",
                    }
                }
            },
        },
        concurrency={"group": "${{ github.workflow }}-${{ github.ref }}", "cancel-in-progress": True},
        permissions={"contents": "write"},
        jobs={"check": check_job, "release": release_job},
    )


class OutOfDateError(Exception):
    """CI workflow file is out of date."""


def check_workflow(path: Path, expected: Workflow) -> None:
    """Check if a workflow file is semantically up to date."""
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")
    current = Workflow.model_validate(yaml.safe_load(path.read_text()))
    if current != expected:
        raise OutOfDateError(f"{path} is out of date. Run 'uv run tools/ci/generate_ci.py' to update.")


def write_workflow(path: Path, workflow: Workflow) -> None:
    """Write a workflow file."""
    path.write_text(generate_ci_yml(workflow))
    print(f"Generated {path}")


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Generate workflow files from workflows.yaml")
    parser.add_argument("--check", action="store_true", help="Check if workflow files are semantically up to date")
    args = parser.parse_args()

    manifest = WorkflowManifest.from_yaml(WORKFLOWS_YAML)

    # Build all expected workflows
    expected_files: dict[Path, Workflow] = {CI_YML: generate_ci_config(manifest)}
    for name, config in manifest.releases.items():
        expected_files[WORKFLOWS_DIR / f"{name}-release.yml"] = generate_release_config(name, config)

    if args.check:
        for path, expected in expected_files.items():
            check_workflow(path, expected)
        print(f"All {len(expected_files)} workflow files are up to date")
        return

    for path, workflow in expected_files.items():
        write_workflow(path, workflow)
