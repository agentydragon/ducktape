"""Tests for pre-commit hook syntax checking - no actual pre-commit needed."""

from pathlib import Path
from unittest.mock import patch

from claude_hooks.actions import PostToolFeedbackToClaude
from claude_hooks.inputs import HookContext, PostToolInput
from claude_hooks.precommit_autofix import PreCommitAutoFixerHook
from claude_hooks.tool_models import WriteInput


def test_python_syntax_error_skips_precommit(tmp_path):
    """Test that Python syntax errors are caught early and pre-commit is never called."""
    # Create file with syntax error
    broken_file = tmp_path / "broken.py"
    broken_file.write_text("def foo(")  # Missing closing paren and colon

    # Create hook input
    hook_input = PostToolInput(
        session_id="87491c5b-6b3d-46fc-b081-bfc0be6f1d33",
        transcript_path=Path("/tmp/transcript.json"),
        cwd=tmp_path,
        hook_event_name="PostToolUse",
        tool_name="Write",
        tool_input=WriteInput(file_path=str(broken_file), content="def foo("),
        tool_response={"success": True},
    )

    context = HookContext(
        hook_name="precommit_autofix",
        hook_event="PostToolUse",
        session_id="87491c5b-6b3d-46fc-b081-bfc0be6f1d33",
        cwd=tmp_path,
    )

    # Mock subprocess.run to ensure pre-commit is never called
    with patch("claude_hooks.precommit_autofix.subprocess.run") as mock_run:
        # Mock config to enable the hook
        hook = PreCommitAutoFixerHook()
        hook.autofixer_config.enabled = True
        hook.autofixer_config.tools = ["Write"]

        result = hook.execute(hook_input, context)

        # Ensure pre-commit subprocess was never called
        mock_run.assert_not_called()

    # Verify syntax error feedback with exact string
    assert isinstance(result, PostToolFeedbackToClaude)
    assert result.feedback_to_claude == "⚠️ Fix SyntaxError in broken.py:1:8: '(' was never closed."
