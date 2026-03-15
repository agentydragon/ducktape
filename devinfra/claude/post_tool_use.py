"""PostToolUse hook: lint-check files after Edit/Write tool calls.

Runs pre-commit on files that Claude Code just modified, reports any
issues back to the agent (with a short diff of what pre-commit would
change), then restores the original file content so the agent can
fix issues itself.

TODO: Import pre-commit as a Python dependency and use its API directly
instead of subprocess. This would give structured output (per-hook results,
exit codes) without messy stdout parsing. subprocess.run already accepts
Path objects for the command, so the transition path is: find pre-commit's
internal run API, call it with the file list, and get back typed results.
"""

from __future__ import annotations

import difflib
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devinfra.claude.claude_api.hooks.post_tool_use import (
    PostToolUseHookSpecificOutput,
    PostToolUseInput,
    PostToolUseOutput,
)

logger = logging.getLogger(__name__)

FILE_MODIFYING_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit"})

_TIMEOUT_SECONDS = 30
_MAX_ISSUES_SHOWN = 3
_MAX_DIFF_LINES = 20


@dataclass
class CheckResult:
    issue_count: int
    issues: list[str] = field(default_factory=list)
    fix_command: str = ""
    diff: str = ""


def _get_file_path(tool_input: dict[str, Any]) -> Path | None:
    file_path = tool_input.get("file_path")
    if file_path is None:
        return None
    return Path(file_path)


def _find_git_root(start: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent
    return None


def _make_short_diff(original: bytes, modified: bytes, filename: str) -> str:
    """Generate a truncated unified diff between original and modified content."""
    orig_lines = original.decode(errors="replace").splitlines(keepends=True)
    mod_lines = modified.decode(errors="replace").splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(orig_lines, mod_lines, fromfile=f"a/{filename}", tofile=f"b/{filename}"))
    if not diff_lines:
        return ""
    if len(diff_lines) > _MAX_DIFF_LINES:
        diff_lines = diff_lines[:_MAX_DIFF_LINES]
        diff_lines.append(f"... (diff truncated, {len(diff_lines)} more lines)\n")
    return "".join(diff_lines).rstrip()


def _run_precommit_check(file_path: Path, project_dir: Path) -> CheckResult | None:
    """Run pre-commit on a file, capture output and diff, then restore original content."""
    precommit = shutil.which("pre-commit")
    if precommit is None:
        return None

    original_content = file_path.read_bytes()
    try:
        result = subprocess.run(
            [precommit, "run", "--files", str(file_path)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            cwd=project_dir,
        )
        modified_content = file_path.read_bytes()
    finally:
        file_path.write_bytes(original_content)

    if result.returncode == 0:
        return None

    # Parse pre-commit output for failed hooks and their messages
    lines = (result.stdout or "").splitlines()
    failed_hooks = 0
    failure_lines: list[str] = []
    in_failure = False

    for line in lines:
        if "Failed" in line:
            failed_hooks += 1
            in_failure = True
            continue
        if "Passed" in line or "Skipped" in line:
            in_failure = False
            continue
        if in_failure and line.strip() and len(failure_lines) < _MAX_ISSUES_SHOWN:
            failure_lines.append(line.rstrip())

    diff = _make_short_diff(original_content, modified_content, file_path.name)

    return CheckResult(
        issue_count=max(failed_hooks, 1),
        issues=failure_lines,
        fix_command=f"pre-commit run --files {file_path}",
        diff=diff,
    )


def _format_check_result(result: CheckResult, file_path: Path) -> str:
    noun = "hook" if result.issue_count == 1 else "hooks"
    parts = [f"{result.issue_count} {noun} failed on {file_path.name}:"]
    for issue in result.issues:
        parts.append(f"  {issue}")
    if result.diff:
        parts.append("Changes pre-commit would make:")
        parts.append(result.diff)
    if result.fix_command:
        parts.append(f"Run `{result.fix_command}` to apply fixes.")
    return "\n".join(parts)


def evaluate(hook_input: PostToolUseInput) -> PostToolUseOutput:
    if hook_input.tool_name not in FILE_MODIFYING_TOOLS:
        return PostToolUseOutput()

    file_path = _get_file_path(hook_input.tool_input)
    if file_path is None or not file_path.exists():
        return PostToolUseOutput()

    project_dir = _find_git_root(file_path)
    if project_dir is None:
        return PostToolUseOutput()

    try:
        check_result = _run_precommit_check(file_path, project_dir)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Lint check failed on %s: %s", file_path, e)
        return PostToolUseOutput()

    if check_result is None:
        return PostToolUseOutput()

    return PostToolUseOutput(
        hook_specific_output=PostToolUseHookSpecificOutput(
            additional_context=_format_check_result(check_result, file_path)
        )
    )
