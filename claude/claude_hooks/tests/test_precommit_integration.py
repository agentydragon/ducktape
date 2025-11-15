"""Integration tests for pre-commit hook with real pre-commit execution."""

from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest

from claude_hooks.actions import PostToolContinue, PostToolFeedbackToClaude
from claude_hooks.inputs import HookContext, PostToolInput
from claude_hooks.precommit_autofix import PreCommitAutoFixerHook
from claude_hooks.tool_models import WriteInput


def _create_write_hook_input(file_path: Path, content: str, cwd: Path) -> PostToolInput:
    """Helper to create PostToolInput for Write operations with less boilerplate."""
    return PostToolInput(
        session_id=UUID("87491c5b-6b3d-46fc-b081-bfc0be6f1d33"),
        transcript_path=Path("/tmp/transcript.json"),
        cwd=cwd,
        hook_event_name="PostToolUse",
        tool_name="Write",
        tool_input=WriteInput(file_path=file_path, content=content),
        tool_response={"success": True},
    )


def _create_hook_context(cwd: Path) -> HookContext:
    """Helper to create HookContext with less boilerplate."""
    return HookContext(
        hook_name="precommit_autofix",
        hook_event="PostToolUse",
        session_id=UUID("87491c5b-6b3d-46fc-b081-bfc0be6f1d33"),
        cwd=cwd,
    )


def _create_configured_hook() -> PreCommitAutoFixerHook:
    """Helper to create configured PreCommitAutoFixerHook."""
    hook = PreCommitAutoFixerHook()
    hook.autofixer_config.enabled = True
    hook.autofixer_config.tools = ["Write"]
    return hook


def test_precommit_makes_changes_success_message(precommit_repo):
    """Test that pre-commit making changes returns success message."""
    # Create file with content that will trigger the foo->bar replacement
    test_file = precommit_repo / "test.py"
    test_file.write_text("print('foo world')")

    # Create hook input
    hook_input = PostToolInput(
        session_id=UUID("87491c5b-6b3d-46fc-b081-bfc0be6f1d33"),
        transcript_path=Path("/tmp/transcript.json"),
        cwd=precommit_repo,
        hook_event_name="PostToolUse",
        tool_name="Write",
        tool_input=WriteInput(file_path=str(test_file), content="print('foo world')"),
        tool_response={"success": True},
    )

    context = HookContext(
        hook_name="precommit_autofix",
        hook_event="PostToolUse",
        session_id=UUID("87491c5b-6b3d-46fc-b081-bfc0be6f1d33"),
        cwd=precommit_repo,
    )

    # Configure and run hook
    hook = PreCommitAutoFixerHook()
    hook.autofixer_config.enabled = True
    hook.autofixer_config.tools = ["Write"]

    result = hook.execute(hook_input, context)

    # Verify success message
    assert isinstance(result, PostToolFeedbackToClaude)
    assert result.feedback_to_claude == "🧹 pre-commit autofixes applied"

    # Verify file was actually changed by pre-commit
    assert test_file.read_text() == "print('bar world')"


def test_precommit_no_changes_continues(precommit_repo):
    """Test that pre-commit making no changes returns PostToolContinue."""
    # Create file with content that won't trigger any replacements
    test_file = precommit_repo / "test.py"
    test_file.write_text("print('hello world')")

    # Create hook input
    hook_input = PostToolInput(
        session_id=UUID("87491c5b-6b3d-46fc-b081-bfc0be6f1d33"),
        transcript_path=Path("/tmp/transcript.json"),
        cwd=precommit_repo,
        hook_event_name="PostToolUse",
        tool_name="Write",
        tool_input=WriteInput(file_path=str(test_file), content="print('hello world')"),
        tool_response={"success": True},
    )

    context = HookContext(
        hook_name="precommit_autofix",
        hook_event="PostToolUse",
        session_id=UUID("87491c5b-6b3d-46fc-b081-bfc0be6f1d33"),
        cwd=precommit_repo,
    )

    # Configure and run hook
    hook = PreCommitAutoFixerHook()
    hook.autofixer_config.enabled = True
    hook.autofixer_config.tools = ["Write"]

    result = hook.execute(hook_input, context)

    # Verify no action taken (PostToolContinue)
    assert isinstance(result, PostToolContinue)

    # Verify file was not changed
    assert test_file.read_text() == "print('hello world')"


@pytest.mark.skipif(
    not pytest.importorskip("pre_commit", reason="pre-commit not available"), reason="Requires pre-commit installation"
)
def test_precommit_crash_shows_formatted_output(precommit_repo, hook_context, configured_hook):
    """Test that unhandled exceptions show traceback."""
    test_file = precommit_repo / "test.py"
    content = "print('hello')"
    test_file.write_text(content)

    # Patch a method to raise ZeroDivisionError to simulate crash
    with patch.object(configured_hook, "_get_precommit_root") as mock_get_root:
        mock_get_root.side_effect = ZeroDivisionError("Simulated crash")

        hook_input = _create_write_hook_input(test_file, content, precommit_repo)
        result = configured_hook.execute(hook_input, hook_context)

    # Verify crash feedback with debugging guidance
    assert isinstance(result, PostToolFeedbackToClaude)
    assert "Unhandled ZeroDivisionError from PreCommitAutoFixerHook: Simulated crash" in result.feedback_to_claude
    assert "Logs:" in result.feedback_to_claude
    assert "Look for invocation ID:" in result.feedback_to_claude
    assert "Traceback:" in result.feedback_to_claude
