#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.0", "pygit2>=1.14", "pyyaml>=6.0"]
# ///
"""CI decision engine - computes affected targets and workflows to run.

Reads workflow definitions from workflows.yaml and uses bazel-diff to compute
exactly which Bazel targets are affected. Outputs a JSON list of workflows
to trigger instead of individual boolean flags.

Requires GITHUB_OUTPUT environment variable to be set.
Requires BAZEL_DIFF_JAR environment variable pointing to bazel-diff JAR.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

# Add repo root to path for tools.ci imports when running via uv
_REPO_ROOT = Path(__file__).parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pygit2
from pydantic import BaseModel, Field

from devinfra.ci.bazel_query import filter_for_ci, query_with_targets
from devinfra.ci.diff_utils import get_changed_files, get_ci_base_commit, has_infra_changes, run_bazel_diff
from devinfra.ci.github_actions import CIEnvironment, PushStrategy
from devinfra.ci.models import AlwaysTrigger, BazelQueryTrigger, PathPatternTrigger, WorkflowConfig, WorkflowManifest
from util.bazel.workspace import BazelWorkspace
from util.env import get_optional_env_path, get_required_existing_path
from util.fmt import format_limited_list

logger = logging.getLogger(__name__)


class CIDecision(BaseModel):
    """Result of CI decision computation."""

    targets: list[str] = Field(default_factory=list, description="Affected targets, or ['//...'] for all")
    workflow_targets: dict[str, list[str]] = Field(
        default_factory=dict, description="Per-workflow targets computed from workflow queries"
    )
    workflows: set[str] = Field(default_factory=set)
    infra_changed: bool = False

    def to_outputs(self) -> dict[str, str | bool]:
        """Format decision as GitHub Actions output dict.

        Per-workflow targets are written to artifact files (targets-<workflow>.txt),
        not GHA outputs, because target lists can exceed output size limits.
        """
        return {
            "targets": " ".join(self.targets),
            "workflows": json.dumps(sorted(self.workflows)),
            "infra_changed": self.infra_changed,
        }

    def write_targets_files(self, workspace: Path) -> None:
        """Write target files for --target_pattern_file usage.

        Writes a shared targets.txt (all targets) and per-workflow files
        (targets-<workflow>.txt) for workflows with computed target sets.
        """
        workspace.joinpath("targets.txt").write_text("\n".join(self.targets) + "\n" if self.targets else "")
        for workflow_name, wf_targets in self.workflow_targets.items():
            path = workspace / f"targets-{workflow_name}.txt"
            path.write_text("\n".join(wf_targets) + "\n" if wf_targets else "")


def should_trigger(
    workspace: BazelWorkspace, name: str, config: WorkflowConfig, targets: list[str], changed_files: set[str]
) -> tuple[bool, list[str]]:
    """Check if a workflow should be triggered.

    Returns (triggered, workflow_targets). For BazelQueryTrigger workflows with
    ``targets: true``, workflow_targets is the query result — the subset of
    affected targets that match this workflow's query. This is used to pass
    per-workflow target lists to downstream jobs.
    """
    workflow_file_changed = f".github/workflows/{name}.yml" in changed_files
    if workflow_file_changed:
        logger.info("Workflow file changed -> triggers %s", name)

    match config.trigger:
        case AlwaysTrigger():
            return True, targets
        case PathPatternTrigger(pattern=pattern):
            regex = re.compile(pattern)
            if any(regex.match(f) for f in changed_files) or workflow_file_changed:
                if not workflow_file_changed:
                    logger.info("Path pattern '%s' matched -> triggers %s", pattern, name)
                return True, targets
        case BazelQueryTrigger(query=query):
            if targets:
                matched = query_with_targets(workspace, query, targets)
                if matched or workflow_file_changed:
                    return True, matched
            elif workflow_file_changed:
                return True, []

    return workflow_file_changed, []


def _compute_affected_targets(
    workspace: BazelWorkspace,
    repo: pygit2.Repository,
    env: CIEnvironment,
    base_commit: pygit2.Commit,
    changed_files: set[str],
) -> tuple[list[str], bool]:
    """Compute affected Bazel targets and whether infrastructure changed.

    Returns (targets, infra_changed). When infrastructure files changed,
    skips bazel-diff (which would fail if the parent BUILD graph is
    incompatible) and queries all CI-compatible targets directly.
    All returned targets are filtered for CI (no macOS-only, no manual).
    """
    infra_changed = has_infra_changes(changed_files)
    if infra_changed:
        logger.info("Infrastructure change detected, building all targets")
        targets = filter_for_ci(workspace, ["//..."])
        logger.info("Filtered to %d CI-compatible targets", len(targets))
        return targets, True

    jar_path = get_required_existing_path("BAZEL_DIFF_JAR")
    cache_dir = get_optional_env_path("BAZEL_DIFF_CACHE_DIR") or (env.workspace / ".bazel-diff-cache")
    targets = run_bazel_diff(repo, jar_path, env.workspace, base_commit, cache_dir)

    if not targets:
        logger.info("No Bazel targets affected")
        return targets, False

    raw_count = len(targets)
    targets = filter_for_ci(workspace, targets)
    filtered = raw_count - len(targets)
    if filtered:
        logger.info("Filtered %d targets (source files, platform-incompatible, manual)", filtered)

    logger.info("Found %d affected targets: %s", len(targets), format_limited_list(targets, 20))
    return targets, False


def compute_decision(env: CIEnvironment, workflows: dict[str, WorkflowConfig]) -> CIDecision:
    """Compute CI decision based on changes."""
    repo = pygit2.Repository(env.workspace)
    workspace = BazelWorkspace(root=env.workspace)

    if not env.is_pull_request and env.push_strategy == PushStrategy.FULL:
        logger.info("Push with full strategy: building all targets")
        return CIDecision(targets=["//..."], workflows=set(workflows.keys()), infra_changed=True)

    base_commit = get_ci_base_commit(repo, env)

    if not base_commit:
        logger.info("No base commit (new branch or initial commit), triggering all workflows")
        return CIDecision(targets=["//..."], workflows=set(workflows.keys()), infra_changed=True)

    changed_files = get_changed_files(repo, base_commit)
    logger.info("Changed files: %s", format_limited_list(sorted(changed_files), 20))

    targets, infra_changed = _compute_affected_targets(workspace, repo, env, base_commit, changed_files)

    triggered: set[str] = set()
    workflow_targets: dict[str, list[str]] = {}
    for name, config in workflows.items():
        if env.event_name not in config.events:
            logger.info("Skipping %s: event %s not in %s", name, env.event_name, config.events)
            continue
        should_run, wf_targets = should_trigger(workspace, name, config, targets, changed_files)
        if should_run:
            triggered.add(name)
            if config.targets:
                workflow_targets[name] = wf_targets
    return CIDecision(
        targets=targets, workflow_targets=workflow_targets, workflows=triggered, infra_changed=infra_changed
    )


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

    # Write target files for artifact upload. Per-workflow target files are used
    # instead of GHA outputs because target lists can exceed output size limits.
    decision.write_targets_files(env.workspace)
    logger.info("Wrote targets.txt and %d per-workflow target files", len(decision.workflow_targets))

    logger.info("\nDecision: %d workflows to run", len(decision.workflows))
    for w in sorted(decision.workflows):
        logger.info("  - %s", w)


if __name__ == "__main__":
    main()
