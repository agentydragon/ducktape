"""CI decision engine - computes affected targets and workflows to run.

Reads workflow definitions from workflows.yaml and uses bazel-diff to compute
exactly which Bazel targets are affected. Outputs a JSON list of workflows
to trigger instead of individual boolean flags.

This module provides the implementation logic. See ci_decide.py for the CLI entry point.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pygit2
from pydantic import BaseModel, Field

from fmt_util.fmt_util import format_limited_list
from tools.ci.bazel_query import filter_for_ci, query_intersection
from tools.ci.diff_utils import get_changed_files, get_ci_base_commit, has_infra_changes, run_bazel_diff
from tools.ci.github_actions import CIEnvironment, PushStrategy
from tools.ci.models import AlwaysTrigger, BazelPatternTrigger, PathPatternTrigger, WorkflowConfig, WorkflowManifest
from tools.env_utils import get_optional_env_path, get_required_existing_path

logger = logging.getLogger(__name__)


class CIDecision(BaseModel):
    """Result of CI decision computation."""

    targets: list[str] = Field(default_factory=list, description="Affected targets, or ['//...'] for all")
    workflows: set[str] = Field(default_factory=set)
    infra_changed: bool = False

    def to_outputs(self) -> dict[str, str | bool]:
        """Format decision as GitHub Actions output dict."""
        return {
            "targets": " ".join(self.targets),
            "workflows": json.dumps(sorted(self.workflows)),
            "infra_changed": self.infra_changed,
        }

    def write_targets_file(self, targets_path: Path) -> None:
        """Write targets to file for --target_pattern_file usage.

        Writes one target per line.
        This avoids shell argument length limits when passing many targets.
        """
        targets_path.write_text("\n".join(self.targets) + "\n" if self.targets else "")


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
            if targets and query_intersection(targets, pattern):
                return True

    return False


def compute_decision(env: CIEnvironment, workflows: dict[str, WorkflowConfig]) -> CIDecision:
    """Compute CI decision based on changes."""
    repo = pygit2.Repository(env.workspace)

    if not env.is_pull_request and env.push_strategy == PushStrategy.FULL:
        logger.info("Push with full strategy: building all targets")
        return CIDecision(targets=["//..."], workflows=set(workflows.keys()), infra_changed=True)

    base_commit = get_ci_base_commit(repo, env)

    if not base_commit:
        logger.info("No base commit (new branch or initial commit), triggering all workflows")
        return CIDecision(targets=["//..."], workflows=set(workflows.keys()), infra_changed=True)

    changed_files = get_changed_files(repo, base_commit)
    logger.info("Changed files: %s", format_limited_list(sorted(changed_files), 20))

    infra_changed = has_infra_changes(changed_files)
    if infra_changed:
        logger.info("Infrastructure change detected")

    jar_path = get_required_existing_path("BAZEL_DIFF_JAR")
    cache_dir = get_optional_env_path("BAZEL_DIFF_CACHE_DIR") or (env.workspace / ".bazel-diff-cache")
    targets = run_bazel_diff(repo, jar_path, env.workspace, base_commit, cache_dir)

    if not targets:
        logger.info("No Bazel targets affected")
    else:
        raw_count = len(targets)
        targets = filter_for_ci(targets)
        filtered = raw_count - len(targets)
        if filtered:
            logger.info("Filtered %d targets (source files, platform-incompatible, manual)", filtered)

        logger.info("Found %d affected targets: %s", len(targets), format_limited_list(targets, 20))
        if infra_changed:
            # //... already excludes manual-tagged targets by default in Bazel.
            # No first-party targets currently have non-Linux platform constraints,
            # so the platform filter from filter_for_ci is not needed here either.
            targets = ["//..."]

    triggered = {name for name, config in workflows.items() if should_trigger(name, config, targets, changed_files)}
    return CIDecision(targets=targets, workflows=triggered, infra_changed=infra_changed)


def main() -> None:
    """Main entry point."""
    # Configure logging to stderr
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()])

    env = CIEnvironment.from_env()
    manifest_path = get_required_existing_path("CI_WORKFLOWS_MANIFEST")

    manifest = WorkflowManifest.from_yaml(manifest_path)
    logger.info("Loaded %d workflow definitions", len(manifest.workflows))

    decision = compute_decision(env, manifest.workflows)

    env.write_outputs(decision.to_outputs())

    # Write targets file for artifact upload (avoids shell argument length limits)
    targets_file = env.workspace / "targets.txt"
    decision.write_targets_file(targets_file)
    logger.info("Wrote targets to %s", targets_file)

    logger.info("\nDecision: %d workflows to run", len(decision.workflows))
    for w in sorted(decision.workflows):
        logger.info("  - %s", w)
