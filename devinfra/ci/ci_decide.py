# /// script
# requires-python = ">=3.12"
# dependencies = ["pydantic>=2.0", "pygit2>=1.14", "pyyaml>=6.0"]
# ///
"""CI decision engine - computes which workflows to run based on changed files.

Reads workflow definitions from workflows.yaml and uses git diff to determine
which path-based and always-run workflows should trigger.

Requires GITHUB_OUTPUT environment variable to be set.
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

from devinfra.ci.diff_utils import get_changed_files, get_ci_base_commit
from devinfra.ci.github_actions import CIEnvironment
from devinfra.ci.models import AlwaysTrigger, PathPatternTrigger, WorkflowConfig, WorkflowManifest
from util.fmt import format_limited_list

logger = logging.getLogger(__name__)


class CIDecision(BaseModel):
    """Result of CI decision computation."""

    workflows: set[str] = Field(default_factory=set)

    def to_outputs(self) -> dict[str, str]:
        return {"workflows": json.dumps(sorted(self.workflows))}


def should_trigger(name: str, config: WorkflowConfig, changed_files: set[str]) -> bool:
    """Check if a workflow should be triggered based on changed files."""
    workflow_file_changed = f".github/workflows/{name}.yml" in changed_files
    match config.trigger:
        case AlwaysTrigger():
            return True
        case PathPatternTrigger(pattern=pattern):
            regex = re.compile(pattern)
            if any(regex.match(f) for f in changed_files) or workflow_file_changed:
                if not workflow_file_changed:
                    logger.info("Path pattern '%s' matched -> triggers %s", pattern, name)
                return True
    return workflow_file_changed


def compute_decision(env: CIEnvironment, workflows: dict[str, WorkflowConfig]) -> CIDecision:
    """Compute which workflows to run based on changed files."""
    repo = pygit2.Repository(env.workspace)
    base_commit = get_ci_base_commit(repo, env)

    if not base_commit:
        logger.info("No base commit (new branch or initial commit), triggering all workflows")
        return CIDecision(workflows=set(workflows.keys()))

    changed_files = get_changed_files(repo, base_commit)
    logger.info("Changed files: %s", format_limited_list(sorted(changed_files), 20))

    triggered: set[str] = set()
    for name, config in workflows.items():
        if env.event_name not in config.events:
            logger.info("Skipping %s: event %s not in %s", name, env.event_name, config.events)
            continue
        if should_trigger(name, config, changed_files):
            triggered.add(name)
    return CIDecision(workflows=triggered)


def main() -> None:
    """Main entry point."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()])

    env = CIEnvironment.from_env()
    manifest_path = Path(__file__).parent / "workflows.yaml"

    manifest = WorkflowManifest.from_yaml(manifest_path)
    logger.info("Loaded %d workflow definitions", len(manifest.workflows))

    decision = compute_decision(env, manifest.workflows)

    env.write_outputs(decision.to_outputs())

    logger.info("\nDecision: %d workflows to run", len(decision.workflows))
    for w in sorted(decision.workflows):
        logger.info("  - %s", w)


if __name__ == "__main__":
    main()
