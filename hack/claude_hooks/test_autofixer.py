"""Unit tests for the pre-commit autofixer hook."""

from pathlib import Path

import pytest
import pytest_bazel
from hamcrest import assert_that, contains_string

from claude_hooks.actions import PostToolFeedbackToClaude
from claude_hooks.config import AutofixerConfig
from claude_hooks.precommit_autofix import NoChanges, PreCommitAutoFixerHook, extract_file_path
from claude_hooks.tool_models import EditInput, WriteInput


@pytest.mark.parametrize(
    ("tool_input", "expected"),
    [
        (EditInput(file_path=Path("/tmp/test.py"), old_string="old", new_string="new"), Path("/tmp/test.py")),
        (WriteInput(file_path=Path("/tmp/test.py"), content="content"), Path("/tmp/test.py")),
        ({"command": "ls"}, None),
    ],
)
def test_extract_file_path(tool_input, expected):
    assert extract_file_path(tool_input) == expected


def test_disabled_hook(autofixer_hook, integration_env):
    autofixer_hook.config["enabled"] = False

    hook_input = integration_env.build_post_tool_edit_input("test.py", "old", "new")

    result = autofixer_hook.execute(hook_input, integration_env.create_context())

    assert result.to_protocol() == {}


def test_unsupported_tool(autofixer_hook, integration_env):
    unsupported_tool = "Grep"
    hook_input = integration_env.create_hook_input(unsupported_tool, {"file_path": "test.py"})

    result = autofixer_hook.execute(hook_input, integration_env.create_context())

    assert result.to_protocol() == {}


def test_no_file_path(autofixer_hook, integration_env):
    hook_input = integration_env.create_hook_input(
        "Edit",
        {"command": "ls"},  # No file_path
    )

    result = autofixer_hook.execute(hook_input, integration_env.create_context())

    assert result.to_protocol() == {}


# File exclusion is now handled by pre-commit configuration,
# so no custom exclusion logic tests are needed


def test_get_precommit_root_success(autofixer_hook, unit_env):
    repo_root = autofixer_hook._get_precommit_root(unit_env.repo_path)
    assert repo_root == unit_env.repo_path


def test_run_precommit_success_with_changes(autofixer_hook, integration_env):
    # Create a test file with 'foo' that our test fixer will replace with 'bar'
    test_file = integration_env.write_file("test_file.py", "print('foo world')")

    changes_made = autofixer_hook._run_precommit_autofix(test_file, integration_env.create_context())
    final_content = integration_env.read_file("test_file.py")

    # The test fixer should have replaced 'foo' with 'bar'
    assert changes_made
    assert final_content == "print('bar world')"


def test_run_precommit_no_changes_real(autofixer_hook, unit_env):
    """Test _run_precommit_autofix with real pre-commit on well-formatted file."""
    # Create a well-formatted Python file that pre-commit won't change
    good_content = 'print("hello world")\n'
    test_file = unit_env.write_file("good_file.py", good_content)

    changes_made = autofixer_hook._run_precommit_autofix(test_file, unit_env.create_context())

    # Well-formatted file should not be changed
    assert isinstance(changes_made, NoChanges)
    assert unit_env.read_file("good_file.py") == good_content


def test_dry_run_mode(unit_env):
    # Create hook with dry run enabled
    hook = PreCommitAutoFixerHook()
    hook.autofixer_config = AutofixerConfig(
        enabled=True, timeout_seconds=30, tools=["Edit", "MultiEdit", "Write"], dry_run=True
    )

    test_file = unit_env.write_file("test.py", "print( 'hello' )")
    original_content = unit_env.read_file("test.py")

    changes_made = hook._run_precommit_autofix(test_file, unit_env.create_context())
    final_content = unit_env.read_file("test.py")

    # In dry run mode, no changes should be made
    assert isinstance(changes_made, NoChanges)
    assert original_content == final_content


def test_execute_success_with_changes(autofixer_hook, integration_env):
    # Create a file with 'foo' that our test fixer will change to 'bar'
    bad_code = "def hello():\n    print('foo world')"
    integration_env.write_file("test.py", bad_code)

    hook_input = integration_env.build_post_tool_edit_input("test.py", "old", "new")

    result = autofixer_hook.execute(hook_input, integration_env.create_context())

    # Pre-commit should have made changes, so we get feedback
    assert isinstance(result, PostToolFeedbackToClaude)
    protocol = result.to_protocol()
    assert protocol["decision"] == "block"
    assert_that(protocol["reason"], contains_string("🧹 pre-commit autofixes applied"))


def test_execute_success_no_changes(autofixer_hook, integration_env):
    # Create a well-formatted file that won't be changed
    good_code = "print('hello')\n"
    integration_env.write_file("test.py", good_code)

    hook_input = integration_env.build_post_tool_write_input("test.py", good_code)

    result = autofixer_hook.execute(hook_input, integration_env.create_context())

    assert result.to_protocol() == {}


def test_execute_precommit_failure(autofixer_hook, integration_env):
    # Test error handling by using a file that doesn't exist
    hook_input = integration_env.build_post_tool_edit_input("/nonexistent/path/test.py", "old", "new")

    # The hook should catch exceptions and continue gracefully
    result = autofixer_hook.execute(hook_input, integration_env.create_context())

    # Should not block Claude on internal errors
    assert result.to_protocol() == {}


if __name__ == "__main__":
    pytest_bazel.main()
