"""WorktreeCreate hook handler — creates a git worktree for Claude Code."""

import logging
import subprocess
from pathlib import Path

from devinfra.claude.claude_api.hooks.output import HookOutput
from devinfra.claude.claude_api.hooks.worktree_create import WorktreeCreateHookSpecificOutput, WorktreeCreateInput

logger = logging.getLogger(__name__)


def handle_worktree_create(hook: WorktreeCreateInput) -> HookOutput:
    """Create a git worktree and return the path.

    Creates a new worktree under ``<cwd>/.claude/worktrees/<name>`` with a fresh
    branch ``claude/worktree/<name>`` based on HEAD.
    """
    cwd = Path(hook.cwd)
    worktree_dir = cwd / ".claude" / "worktrees" / hook.name
    branch_name = f"claude/worktree/{hook.name}"

    if worktree_dir.exists():
        logger.info("worktree already exists at %s, returning path", worktree_dir)
        return HookOutput(hook_specific_output=WorktreeCreateHookSpecificOutput(worktree_path=str(worktree_dir)))

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    logger.info("creating worktree %s branch=%s", worktree_dir, branch_name)
    subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_dir), "HEAD"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )

    return HookOutput(hook_specific_output=WorktreeCreateHookSpecificOutput(worktree_path=str(worktree_dir)))
