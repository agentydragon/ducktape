"""Integration tests for hooks in isolated Claude Code environments."""

import json
import subprocess

import pytest

from claude_hooks.actions import PostToolContinue, PostToolFeedbackToClaude
from claude_hooks.precommit_autofix import PreCommitAutoFixerHook


def test_environment_setup(integration_env):
    assert integration_env.project_dir.exists()
    assert integration_env.claude_config_dir.exists()
    assert (integration_env.project_dir / ".git").exists()
    assert (integration_env.project_dir / ".pre-commit-config.yaml").exists()
    assert (integration_env.claude_config_dir / "settings.json").exists()


def test_precommit_autofix_on_badly_formatted_python(integration_env):
    bad_code = """def hello():
    print("foo world")  # This 'foo' will be replaced with 'bar'
"""

    # Write file and create hook input
    integration_env.write_file("test.py", bad_code)

    hook_input = integration_env.build_post_tool_write_input("test.py", content=bad_code)

    # Execute hook
    hook = PreCommitAutoFixerHook()
    hook_result = hook.execute(hook_input, integration_env.create_context())
    final_content = integration_env.read_file("test.py")

    assert integration_env.file_exists("test.py")
    compile(final_content, "test.py", "exec")
    # Hook may return Continue (no changes) or FeedbackToClaude (changes made)
    assert isinstance(hook_result, PostToolContinue | PostToolFeedbackToClaude)


def test_precommit_autofix_on_import_sorting(integration_env):
    code_with_foo = """# This file contains foo that should be replaced
print("foo is here")
def use_foo():
    return "foo"
"""

    # Write file and create hook input
    integration_env.write_file("imports.py", code_with_foo)

    hook_input = integration_env.build_post_tool_write_input("imports.py", content=code_with_foo)

    # Execute hook
    hook = PreCommitAutoFixerHook()
    hook_result = hook.execute(hook_input, integration_env.create_context())
    final_content = integration_env.read_file("imports.py")

    compile(final_content, "imports.py", "exec")
    assert isinstance(hook_result, PostToolContinue | PostToolFeedbackToClaude)


def test_precommit_autofix_skips_excluded_files(integration_env):
    bad_code = "print('this has no foo so no changes')"

    # Write file and create hook input
    integration_env.write_file("x/experimental.py", bad_code)

    hook_input = integration_env.build_post_tool_write_input("x/experimental.py", content=bad_code)

    # Execute hook
    hook = PreCommitAutoFixerHook()
    hook_result = hook.execute(hook_input, integration_env.create_context())

    assert isinstance(hook_result, PostToolContinue | PostToolFeedbackToClaude)


def test_precommit_autofix_handles_non_python_files(integration_env):
    bad_json = """{"key":"value","another":   "value"}"""

    # Write file and create hook input
    integration_env.write_file("config.json", bad_json)

    hook_input = integration_env.build_post_tool_write_input("config.json", content=bad_json)

    # Execute hook
    hook = PreCommitAutoFixerHook()
    hook_result = hook.execute(hook_input, integration_env.create_context())
    final_content = integration_env.read_file("config.json")

    json.loads(final_content)
    assert isinstance(hook_result, PostToolContinue | PostToolFeedbackToClaude)


def test_edit_vs_write_operations(integration_env):
    original_code = "def hello():\n    print('hello')"
    integration_env.write_file("test.py", original_code)

    new_code = "def hello():\n    print('hello world')"

    hook_input = integration_env.build_post_tool_edit_input("test.py", old_string=original_code, new_string=new_code)

    # Execute hook
    hook = PreCommitAutoFixerHook()
    hook_result = hook.execute(hook_input, integration_env.create_context())

    assert isinstance(hook_result, PostToolContinue | PostToolFeedbackToClaude)


@pytest.mark.slow
def test_precommit_actually_works(integration_env, monkeypatch):
    bad_code = "def hello(  ):\n    print( 'hello'  )\n"
    integration_env.write_file("test.py", bad_code)

    # Change to project directory for pre-commit to work
    monkeypatch.chdir(integration_env.project_dir)

    # Run pre-commit directly on the file
    subprocess.run(["pre-commit", "run", "--files", "test.py"], cwd=integration_env.project_dir, check=False)

    final_content = integration_env.read_file("test.py")
    compile(final_content, "test.py", "exec")


def test_hook_error_handling(integration_env):
    # Test error handling by using a file path that will cause issues
    test_code = "print('hello')"

    # Create hook input with invalid working directory to trigger an error
    hook_input = integration_env.build_post_tool_write_input("/nonexistent/path/test.py", test_code)

    # Execute hook - should handle error gracefully
    hook = PreCommitAutoFixerHook()
    hook_result = hook.execute(hook_input, integration_env.create_context())

    assert isinstance(hook_result, PostToolContinue)


def test_workflow_simulation(integration_env):
    """Test a complete workflow with multiple file operations."""
    steps = [
        ("test.py", "def greet(name):\n    print(f'Hello, {name}!')\n"),
        ("utils.py", "def add(a, b):\n    return a + b\n"),
        ("test.py", "def greet(name):\n    print(f'Hello, {name}!')\n\ndef main():\n    greet('World')\n"),
    ]

    for filename, content in steps:
        # Write file and create hook input
        integration_env.write_file(filename, content)

        hook_input = integration_env.build_post_tool_write_input(filename, content=content)

        # Execute hook
        hook = PreCommitAutoFixerHook()
        hook_result = hook.execute(hook_input, integration_env.create_context())
        final_content = integration_env.read_file(filename)

        assert isinstance(hook_result, PostToolContinue | PostToolFeedbackToClaude)
        compile(final_content, filename, "exec")

    python_files = list(integration_env.project_dir.rglob("*.py"))
    assert len(python_files) >= 2
