"""Tests for post_tool_use hook."""

from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_bazel

from devinfra.claude.claude_api.hooks.post_tool_use import (
    PostToolUseHookSpecificOutput,
    PostToolUseInput,
    PostToolUseOutput,
)
from devinfra.claude.post_tool_use import CheckResult, _find_git_root, _format_check_result, _make_short_diff, evaluate

_COMMON = {
    "session_id": "test-session",
    "transcript_path": "/tmp/transcript.jsonl",
    "cwd": "/tmp",
    "permission_mode": "default",
    "hook_event_name": "PostToolUse",
    "tool_use_id": "toolu_test123",
    "tool_response": "",
}


# === Guard tests ===


def test_non_file_tool_returns_default() -> None:
    inp = PostToolUseInput(**_COMMON, tool_name="Bash", tool_input={"command": "echo hi"})
    result = evaluate(inp)
    assert result.hook_specific_output is None


def test_missing_file_path_returns_default() -> None:
    inp = PostToolUseInput(**_COMMON, tool_name="Write", tool_input={})
    result = evaluate(inp)
    assert result.hook_specific_output is None


def test_nonexistent_file_returns_default() -> None:
    inp = PostToolUseInput(**_COMMON, tool_name="Edit", tool_input={"file_path": "/nonexistent/file.py"})
    result = evaluate(inp)
    assert result.hook_specific_output is None


# === Git root tests ===


def test_find_git_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    subdir = tmp_path / "a" / "b"
    subdir.mkdir(parents=True)
    assert _find_git_root(subdir / "file.py") == tmp_path


def test_find_git_root_no_git(tmp_path: Path) -> None:
    assert _find_git_root(tmp_path / "file.py") is None


# === Serialization tests ===


def test_output_serializes_camel_case() -> None:
    out = PostToolUseOutput(hook_specific_output=PostToolUseHookSpecificOutput(additional_context="formatted"))
    j = out.model_dump_json(by_alias=True)
    assert '"hookSpecificOutput"' in j
    assert '"additionalContext"' in j
    assert "formatted" in j


def test_stop_reason_requires_continue_false() -> None:
    with pytest.raises(ValueError, match="stop_reason requires continue=false"):
        PostToolUseOutput(stop_reason="done", continue_=True)


def test_stop_reason_with_continue_false() -> None:
    out = PostToolUseOutput(stop_reason="done", continue_=False)
    assert out.stop_reason == "done"
    assert out.continue_ is False


# === Diff generation tests ===


def test_make_short_diff_no_change() -> None:
    content = b"hello\nworld\n"
    assert _make_short_diff(content, content, "test.py") == ""


def test_make_short_diff_with_change() -> None:
    original = b"hello\nworld\n"
    modified = b"hello\nearth\n"
    diff = _make_short_diff(original, modified, "test.py")
    assert "a/test.py" in diff
    assert "-world" in diff
    assert "+earth" in diff


def test_make_short_diff_truncates() -> None:
    """Long diffs are truncated to _MAX_DIFF_LINES."""
    original = "".join(f"line{i}\n" for i in range(50)).encode()
    modified = "".join(f"changed{i}\n" for i in range(50)).encode()
    diff = _make_short_diff(original, modified, "big.py")
    assert "truncated" in diff


# === Format output tests ===


def test_format_check_result_basic() -> None:
    result = CheckResult(issue_count=1, issues=["bad indent"], fix_command="pre-commit run --files test.py")
    output = _format_check_result(result, Path("test.py"))
    assert "1 hook failed on test.py:" in output
    assert "bad indent" in output
    assert "pre-commit run" in output


def test_format_check_result_with_diff() -> None:
    result = CheckResult(issue_count=2, issues=["err1"], diff="--- a/f\n+++ b/f\n-old\n+new")
    output = _format_check_result(result, Path("f.py"))
    assert "2 hooks failed" in output
    assert "Changes pre-commit would make:" in output
    assert "+new" in output


# === Pre-commit integration tests ===


def test_precommit_with_diff(tmp_path: Path) -> None:
    """When pre-commit modifies a file, the diff is included and file is restored."""
    (tmp_path / ".git").mkdir()
    test_file = tmp_path / "test.py"
    original = b"x=1\n"
    test_file.write_bytes(original)

    # Simulate pre-commit that reformats the file
    def fake_run(cmd, **kwargs):
        test_file.write_bytes(b"x = 1\n")

        class FakeResult:
            returncode = 1
            stdout = (
                "ruff-format..................................................Failed\n"
                "- hook id: ruff-format\n"
                "- files were modified by this hook\n"
            )
            stderr = ""

        return FakeResult()

    with patch("devinfra.claude.post_tool_use.shutil.which", return_value="/usr/bin/pre-commit"):
        with patch("devinfra.claude.post_tool_use.subprocess.run", side_effect=fake_run):
            inp = PostToolUseInput(**_COMMON, tool_name="Write", tool_input={"file_path": str(test_file)})
            result = evaluate(inp)

    # File should be restored
    assert test_file.read_bytes() == original
    # Output should contain diff
    assert result.hook_specific_output is not None
    ctx = result.hook_specific_output.additional_context
    assert ctx is not None
    assert "-x=1" in ctx
    assert "+x = 1" in ctx


def test_precommit_no_file_change(tmp_path: Path) -> None:
    """When pre-commit fails but doesn't modify the file, no diff is shown."""
    (tmp_path / ".git").mkdir()
    test_file = tmp_path / "test.yaml"
    original = b"key: value\n"
    test_file.write_bytes(original)

    def fake_run(cmd, **kwargs):
        # Don't modify the file (e.g. check-yaml just validates)
        class FakeResult:
            returncode = 1
            stdout = (
                "check-yaml...................................................Failed\n"
                "- hook id: check-yaml\n"
                "- exit code: 1\n"
                "invalid yaml\n"
            )
            stderr = ""

        return FakeResult()

    with patch("devinfra.claude.post_tool_use.shutil.which", return_value="/usr/bin/pre-commit"):
        with patch("devinfra.claude.post_tool_use.subprocess.run", side_effect=fake_run):
            inp = PostToolUseInput(**_COMMON, tool_name="Write", tool_input={"file_path": str(test_file)})
            result = evaluate(inp)

    assert test_file.read_bytes() == original
    assert result.hook_specific_output is not None
    ctx = result.hook_specific_output.additional_context
    assert ctx is not None
    assert "Changes pre-commit would make:" not in ctx


def test_precommit_passes(tmp_path: Path) -> None:
    """When pre-commit passes, no output is returned."""
    (tmp_path / ".git").mkdir()
    test_file = tmp_path / "clean.py"
    test_file.write_bytes(b"x = 1\n")

    def fake_run(cmd, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = "All passed"
            stderr = ""

        return FakeResult()

    with patch("devinfra.claude.post_tool_use.shutil.which", return_value="/usr/bin/pre-commit"):
        with patch("devinfra.claude.post_tool_use.subprocess.run", side_effect=fake_run):
            inp = PostToolUseInput(**_COMMON, tool_name="Write", tool_input={"file_path": str(test_file)})
            result = evaluate(inp)

    assert result.hook_specific_output is None


def test_precommit_restores_on_timeout(tmp_path: Path) -> None:
    """File is restored even when pre-commit times out."""
    import subprocess

    (tmp_path / ".git").mkdir()
    test_file = tmp_path / "slow.py"
    original = b"x = 1\n"
    test_file.write_bytes(original)

    def fake_run(cmd, **kwargs):
        test_file.write_bytes(b"modified\n")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    with patch("devinfra.claude.post_tool_use.shutil.which", return_value="/usr/bin/pre-commit"):
        with patch("devinfra.claude.post_tool_use.subprocess.run", side_effect=fake_run):
            inp = PostToolUseInput(**_COMMON, tool_name="Write", tool_input={"file_path": str(test_file)})
            result = evaluate(inp)

    assert test_file.read_bytes() == original
    assert result.hook_specific_output is None


def test_precommit_not_found(tmp_path: Path) -> None:
    """When pre-commit is not installed, no output is returned."""
    (tmp_path / ".git").mkdir()
    test_file = tmp_path / "file.py"
    test_file.write_bytes(b"x = 1\n")

    with patch("devinfra.claude.post_tool_use.shutil.which", return_value=None):
        inp = PostToolUseInput(**_COMMON, tool_name="Write", tool_input={"file_path": str(test_file)})
        result = evaluate(inp)

    assert result.hook_specific_output is None


def test_precommit_multiple_hooks_fail(tmp_path: Path) -> None:
    """Multiple hooks failing reports correct count."""
    (tmp_path / ".git").mkdir()
    test_file = tmp_path / "test.py"
    test_file.write_bytes(b"x=1\n")

    def fake_run(cmd, **kwargs):
        class FakeResult:
            returncode = 1
            stdout = (
                "ruff-format..................................................Failed\n"
                "- files were modified\n"
                "ruff-check...................................................Failed\n"
                "- exit code: 1\n"
                "E001 bad style\n"
            )
            stderr = ""

        return FakeResult()

    with patch("devinfra.claude.post_tool_use.shutil.which", return_value="/usr/bin/pre-commit"):
        with patch("devinfra.claude.post_tool_use.subprocess.run", side_effect=fake_run):
            inp = PostToolUseInput(**_COMMON, tool_name="Write", tool_input={"file_path": str(test_file)})
            result = evaluate(inp)

    assert result.hook_specific_output is not None
    ctx = result.hook_specific_output.additional_context
    assert ctx is not None
    assert "2 hooks failed" in ctx


if __name__ == "__main__":
    pytest_bazel.main()
