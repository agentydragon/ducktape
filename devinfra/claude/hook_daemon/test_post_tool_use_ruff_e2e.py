"""E2E test: PostToolUse hook with real ruff preserves unused imports.

Verifies that when ruff-check is a report-only hook (not in auto_apply_hooks),
it does NOT persist import removal to disk, and the hook output correctly
characterizes the situation.
"""

import os
import shutil
from pathlib import Path
from textwrap import dedent

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from devinfra.claude.claude_api.hooks.post_tool_use import PostToolUseInput
from devinfra.claude.hook_daemon.conftest import init_git_repo, write_precommit_config
from devinfra.claude.hook_daemon.post_tool_use import evaluate


def _find_ruff() -> str:
    """Resolve ruff binary via RUFF_BIN env var (rlocation) or PATH fallback."""
    if env_val := os.environ.get("RUFF_BIN"):
        r = Runfiles.Create()
        if r and (resolved := r.Rlocation(env_val)):
            return resolved
        if Path(env_val).exists():
            return env_val
    if which_path := shutil.which("ruff"):
        return which_path
    raise FileNotFoundError("ruff binary not found (set RUFF_BIN or add to PATH)")


_COMMON_INPUT = {
    "session_id": "test-session",
    "transcript_path": "/tmp/transcript.jsonl",
    "cwd": "/tmp",
    "permission_mode": "default",
    "hook_event_name": "PostToolUse",
    "tool_use_id": "toolu_test_ruff",
    "tool_response": "",
}

_TESTDATA = Path(__file__).resolve().parent / "testdata" / "ruff_repo"


@pytest.fixture
def ruff_repo(tmp_path: Path) -> Path:
    """Git repo with real ruff hooks and .claude_hooks config from testdata."""
    repo_path = tmp_path / "repo"
    shutil.copytree(_TESTDATA, repo_path)

    ruff_path = _find_ruff()

    write_precommit_config(
        repo_path,
        [
            {
                "id": "ruff-check",
                "name": "ruff-check",
                "entry": f"{ruff_path} check --fix --config ruff.toml",
                "language": "system",
                "files": r"\.py$",
            },
            {
                "id": "ruff-format",
                "name": "ruff-format",
                "entry": f"{ruff_path} format --config ruff.toml",
                "language": "system",
                "files": r"\.py$",
            },
        ],
    )

    init_git_repo(repo_path)
    return repo_path


def test_unused_import_preserved_after_hook(ruff_repo: Path) -> None:
    """Report-only ruff-check must not remove unused imports from disk.

    After PostToolUse hook runs, the file should still contain the unused
    import. The hook should warn about the violation but not report it as
    "modified file" (since the modification was reverted).
    """
    test_file = ruff_repo / "test.py"
    test_file.write_text(
        dedent("""\
        import os

        x = 1
    """)
    )

    inp = PostToolUseInput(**_COMMON_INPUT, tool_name="Edit", tool_input={"file_path": str(test_file)})
    result = evaluate(inp)

    # File content must be preserved — ruff-check is report-only, changes reverted
    assert "import os" in test_file.read_text(), (
        "ruff-check (report-only) removed the unused import from disk — Phase 2 revert failed"
    )

    # Hook should have produced output (ruff-check detected F401)
    assert result.hook_specific_output is not None, "Hook should report ruff-check findings"
    ctx = result.hook_specific_output.additional_context
    assert ctx is not None

    # The output must NOT say "modified file" for a report-only hook whose
    # changes were reverted — that misleads Claude into removing the import.
    assert "modified file" not in ctx, (
        f"Hook output says 'modified file' for a reverted report-only hook.\n"
        f"This misleads Claude into removing the import itself.\n"
        f"Output:\n{ctx}"
    )


if __name__ == "__main__":
    pytest_bazel.main()
