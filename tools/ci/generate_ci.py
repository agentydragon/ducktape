#!/usr/bin/env python3
"""Generate .github/workflows/ci.yml from workflows.yaml.

This script reads the workflow definitions and generates the CI workflow file,
eliminating duplication in job definitions.

Usage:
    python tools/ci/generate_ci.py
    python tools/ci/generate_ci.py --check  # Verify ci.yml is up to date
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent
WORKFLOWS_YAML = SCRIPT_DIR / "workflows.yaml"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

HEADER = """\
# AUTO-GENERATED from tools/ci/workflows.yaml - DO NOT EDIT DIRECTLY
# Regenerate with: python tools/ci/generate_ci.py
"""


def build_compute_targets_job() -> dict:
    """Build the compute-targets job definition."""
    return {
        "name": "Compute affected targets",
        "runs-on": "ubuntu-latest",
        "timeout-minutes": 30,
        "outputs": {
            "targets": "${{ steps.decide.outputs.targets }}",
            "workflows": "${{ steps.decide.outputs.workflows }}",
            "infra_changed": "${{ steps.decide.outputs.infra_changed }}",
        },
        "steps": [
            {"uses": "actions/checkout@v4", "with": {"fetch-depth": 0}},
            {"uses": "bazelbuild/setup-bazelisk@v3"},
            {"uses": "actions/setup-java@v4", "with": {"distribution": "temurin", "java-version": "21"}},
            {"uses": "actions/setup-python@v5", "with": {"python-version": "3.12"}},
            {"name": "Install dependencies", "run": "pip install pyyaml"},
            {
                "name": "Cache bazel-diff",
                "id": "cache-bazel-diff",
                "uses": "actions/cache@v4",
                "with": {"path": "bazel-diff.jar", "key": "bazel-diff-12.1.1"},
            },
            {
                "name": "Download bazel-diff",
                "if": "steps.cache-bazel-diff.outputs.cache-hit != 'true'",
                "run": (
                    "curl -fsSL -o bazel-diff.jar \\\n"
                    '  "https://github.com/Tinder/bazel-diff/releases/download/12.1.1/bazel-diff_deploy.jar"'
                ),
            },
            {"name": "Set bazel-diff path", "run": 'echo "BAZEL_DIFF_JAR=$PWD/bazel-diff.jar" >> $GITHUB_ENV'},
            {"name": "Compute CI decision", "id": "decide", "run": "python tools/ci/ci_decide.py"},
        ],
    }


def build_workflow_job(name: str, config: dict) -> dict:
    """Build a job definition from workflow config."""
    job: dict = {
        "needs": "compute-targets",
        "if": f"contains(fromJson(needs.compute-targets.outputs.workflows), '{name}')",
        "uses": f"./.github/workflows/{name}.yml",
    }

    # Build 'with' section
    with_section: dict = {}
    if config.get("targets"):
        with_section["targets"] = "${{ needs.compute-targets.outputs.targets }}"
    if config.get("inputs"):
        with_section.update(config["inputs"])
    if with_section:
        job["with"] = with_section

    # Build 'secrets' section
    secrets = config.get("secrets", [])
    if secrets:
        job["secrets"] = {s: f"${{{{ secrets.{s} }}}}" for s in secrets}

    return job


def generate_ci_yml(workflows: dict) -> str:
    """Generate the complete ci.yml content."""
    ci_config = {
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
        "jobs": {"compute-targets": build_compute_targets_job()},
    }

    # Add workflow jobs
    for name, config in workflows.items():
        ci_config["jobs"][name] = build_workflow_job(name, config)

    # Custom representer for multiline strings
    def str_representer(dumper: yaml.Dumper, data: str) -> yaml.Node:
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    yaml.add_representer(str, str_representer)

    # Generate YAML with proper formatting
    yaml_content = yaml.dump(ci_config, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)

    return HEADER + yaml_content


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate ci.yml from workflows.yaml")
    parser.add_argument("--check", action="store_true", help="Check if ci.yml is up to date (exit 1 if not)")
    args = parser.parse_args()

    with WORKFLOWS_YAML.open() as f:
        workflows = yaml.safe_load(f)

    generated = generate_ci_yml(workflows)

    if args.check:
        if not CI_YML.exists():
            print(f"Error: {CI_YML} does not exist", file=sys.stderr)
            return 1
        current = CI_YML.read_text()
        if current != generated:
            print(f"Error: {CI_YML} is out of date. Run 'python tools/ci/generate_ci.py' to update.", file=sys.stderr)
            return 1
        print(f"{CI_YML} is up to date")
        return 0

    CI_YML.write_text(generated)
    print(f"Generated {CI_YML}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
