"""CI decision engine - computes affected targets and workflows to run.

Reads workflow definitions from workflows.yaml and uses bazel-diff to compute
exactly which Bazel targets are affected. Outputs a JSON list of workflows
to trigger instead of individual boolean flags.

This module provides the implementation logic. See ci_decide.py for the CLI entry point.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import pygit2
from pydantic import BaseModel, Field

from fmt_util import format_limited_list
from tools.ci.bazel_query import check_bazel_intersection, filter_compatible_targets, filter_to_rules
from tools.ci.diff_utils import (
    get_changed_files,
    get_ci_base_commit as get_base_commit,
    has_infra_changes,
    run_bazel_diff,
)
from tools.ci.github_actions import bool_output, format_output, get_output_path, get_workspace
from tools.ci.models import AlwaysTrigger, BazelPatternTrigger, PathPatternTrigger, WorkflowConfig, WorkflowManifest

logger = logging.getLogger(__name__)


class CIDecision(BaseModel):
    """Result of CI decision computation."""

    targets: list[str] = Field(default_factory=list)  # Affected targets, or ["//..."] for all
    workflows: list[str] = Field(default_factory=list)
    infra_changed: bool = False

    def to_github_output(self) -> str:
        """Format decision as GitHub Actions output content."""
        return format_output(
            {
                "targets": " ".join(self.targets),
                "workflows": json.dumps(self.workflows),
                "infra_changed": bool_output(self.infra_changed),
            }
        )

    def write_targets_file(self, targets_path: Path) -> None:
        """Write targets to file for --target_pattern_file usage.

        Writes one target per line.
        This avoids shell argument length limits when passing many targets.
        """
        targets_path.write_text("\n".join(self.targets) + "\n" if self.targets else "")


def filter_platform_incompatible(targets: list[str]) -> list[str]:
    """Filter out targets that are incompatible with the CI platform.

    Uses bazel cquery to check target_compatible_with constraints.
    Targets incompatible with the current platform are excluded.
    """
    if not targets:
        return targets

    compatible = filter_compatible_targets(targets)
    excluded = len(targets) - len(compatible)
    if excluded:
        logger.info("Filtered out %d platform-incompatible targets", excluded)
    return compatible


def should_trigger(name: str, config: WorkflowConfig, targets: list[str], changed_files: set[str]) -> bool:
    """Check if a workflow should be triggered."""
    if f".github/workflows/{name}.yml" in changed_files:
        logger.info("Workflow file changed -> triggers %s", name)
        return True

    match config.trigger:
        case AlwaysTrigger():
            return True
        case PathPatternTrigger(pattern=pattern):
            regex = re.compile(pattern)
            if any(regex.match(f) for f in changed_files):
                logger.info("Path pattern '%s' matched -> triggers %s", pattern, name)
                return True
        case BazelPatternTrigger(pattern=pattern):
            if targets and check_bazel_intersection(targets, pattern):
                return True

    return False


def get_triggered_workflows(
    workflows: dict[str, WorkflowConfig], targets: list[str], changed_files: set[str]
) -> list[str]:
    """Determine which workflows should run based on changes."""
    return sorted(name for name, config in workflows.items() if should_trigger(name, config, targets, changed_files))


def get_bazel_diff_jar() -> Path:
    """Get path to bazel-diff JAR from environment."""
    jar_path_str = os.environ.get("BAZEL_DIFF_JAR")
    if not jar_path_str:
        raise RuntimeError("BAZEL_DIFF_JAR environment variable not set")
    jar_path = Path(jar_path_str)
    if not jar_path.exists():
        raise FileNotFoundError(f"bazel-diff JAR not found at {jar_path}")
    return jar_path


def compute_decision(workflows: dict[str, WorkflowConfig], workspace: Path) -> CIDecision:
    """Compute CI decision based on changes."""
    repo = pygit2.Repository(workspace)
    base_commit = get_base_commit(repo)

    if not base_commit:
        logger.info("No base commit (new branch or initial commit), triggering all workflows")
        return CIDecision(targets=["//..."], workflows=sorted(workflows.keys()), infra_changed=True)

    changed_files = get_changed_files(repo, base_commit)
    logger.info("Changed files: %s", format_limited_list(sorted(changed_files), 20))

    infra_changed = has_infra_changes(changed_files)
    if infra_changed:
        logger.info("Infrastructure change detected")

    jar_path = get_bazel_diff_jar()
    targets = run_bazel_diff(repo, jar_path, workspace, base_commit)

    if not targets:
        logger.info("No Bazel targets affected")
    else:
        # Filter source files - bazel-diff returns labels like //:foo.py that aren't buildable
        raw_count = len(targets)
        targets = filter_to_rules(targets)
        if len(targets) < raw_count:
            logger.info("Filtered %d source files from %d bazel-diff targets", raw_count - len(targets), raw_count)

        logger.info("Found %d affected targets: %s", len(targets), format_limited_list(targets, 20))
        # Filter out platform-incompatible targets for Linux CI
        targets = filter_platform_incompatible(targets)
        if infra_changed:
            targets = ["//..."]

    triggered = get_triggered_workflows(workflows, targets, changed_files)
    return CIDecision(targets=targets, workflows=triggered, infra_changed=infra_changed)


def main() -> None:
    """Main entry point."""
    # Configure logging to stderr
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()])

    output_path = get_output_path()

    manifest_path_str = os.environ.get("CI_WORKFLOWS_MANIFEST")
    if not manifest_path_str:
        raise RuntimeError("CI_WORKFLOWS_MANIFEST environment variable not set")
    manifest_path = Path(manifest_path_str)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    manifest = WorkflowManifest.from_yaml(manifest_path)
    logger.info("Loaded %d workflow definitions", len(manifest.workflows))

    workspace = get_workspace()
    decision = compute_decision(manifest.workflows, workspace)

    output_path.write_text(decision.to_github_output())

    # Write targets file for artifact upload (avoids shell argument length limits)
    targets_file = workspace / "targets.txt"
    decision.write_targets_file(targets_file)
    logger.info("Wrote targets to %s", targets_file)

    logger.info("\nDecision: %d workflows to run", len(decision.workflows))
    for w in decision.workflows:
        logger.info("  - %s", w)
