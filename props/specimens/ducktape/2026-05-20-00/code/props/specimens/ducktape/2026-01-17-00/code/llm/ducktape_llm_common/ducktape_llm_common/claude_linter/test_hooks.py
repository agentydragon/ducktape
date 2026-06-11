import json
from pathlib import Path

import pygit2
import pytest
import yaml
from click.testing import CliRunner

from ducktape_llm_common.claude_linter.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def pre_commit_config_non_fixing(tmp_path: Path):
    """Create a pre-commit config with a non-fixing linter."""
    non_fixing_linter_path = tmp_path / "non_fixing_linter.py"
    non_fixing_linter_path.write_text("""#!/usr/bin/env python
import sys

found_error = False
for filename in sys.argv[1:]:
    with open(filename, "r") as f:
        content = f.read()
    if "non-fixable-error" in content:
        print(f"Found a non-fixable error in {filename}", file=sys.stderr)
        found_error = True

if found_error:
    sys.exit(1)
sys.exit(0)
""")
    non_fixing_linter_path.chmod(0o755)

    config = {
        "repos": [
            {
                "repo": "local",
                "hooks": [
                    {
                        "id": "non-fixing-linter",
                        "name": "non-fixing-linter",
                        "entry": str(non_fixing_linter_path),
                        "language": "script",
                        "types": ["python"],
                    }
                ],
            }
        ]
    }
    config_path = tmp_path / ".pre-commit-config.yaml"
    with config_path.open("w") as f:
        yaml.dump(config, f)
    return config_path


@pytest.fixture
def pre_commit_config_fixing(tmp_path: Path):
    """Create a pre-commit config with a fixing linter."""
    fixing_linter_path = tmp_path / "fixing_linter.py"
    fixing_linter_path.write_text("""#!/usr/bin/env python
import sys

made_change = False
for filename in sys.argv[1:]:
    with open(filename, "r") as f:
        content = f.read()
    if "fix-me" in content:
        new_content = content.replace("fix-me", "fixed")
        with open(filename, "w") as f:
            f.write(new_content)
        print(f"Fixed {filename}")
        made_change = True

if made_change:
    sys.exit(1)
sys.exit(0)
""")
    fixing_linter_path.chmod(0o755)

    config = {
        "repos": [
            {
                "repo": "local",
                "hooks": [
                    {
                        "id": "fixing-linter",
                        "name": "fixing-linter",
                        "entry": str(fixing_linter_path),
                        "language": "script",
                        "types": ["python"],
                    }
                ],
            }
        ]
    }
    config_path = tmp_path / ".pre-commit-config.yaml"
    with config_path.open("w") as f:
        yaml.dump(config, f)
    return config_path


def create_pre_hook_payload(file_path: str, content: str) -> str:
    """Create a PreToolUse hook payload."""
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "12345678-1234-5678-1234-567812345678",
            "tool_name": "Write",
            "tool_input": {"file_path": file_path, "content": content},
        }
    )


def create_post_hook_payload(file_path: str, content: str | None = None) -> str:
    """Create a PostToolUse hook payload."""
    tool_input: dict[str, str] = {"file_path": file_path}
    if content:
        tool_input["content"] = content
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "12345678-1234-5678-1234-567812345678",
        "tool_name": "Write",
        "tool_input": tool_input,
    }
    return json.dumps(payload)


class TestHooks:
    """Consolidated hook tests using parametrization."""

    @pytest.mark.parametrize("use_git", [False, True], ids=["no_git", "git"])
    def test_pre_hook_approve(
        self, runner, chdir_tmp_path, chdir_tmp_path_git_repo, pre_commit_config_non_fixing, use_git
    ):
        """Test that pre-hook approves clean files."""
        # Use the appropriate fixture based on git/no-git
        tmp_path = chdir_tmp_path_git_repo if use_git else chdir_tmp_path

        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')")

        payload = create_pre_hook_payload(str(test_file), test_file.read_text())

        result = runner.invoke(cli, ["hook"], input=payload, catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.stdout)
        # New API format - continue:true means allowed
        assert output.get("continue") is True

    @pytest.mark.parametrize("use_git", [False, True], ids=["no_git", "git"])
    def test_pre_hook_block(
        self, runner, chdir_tmp_path, chdir_tmp_path_git_repo, pre_commit_config_non_fixing, use_git
    ):
        """Test that pre-hook blocks files with non-fixable errors."""
        # Use the appropriate fixture based on git/no-git
        tmp_path = chdir_tmp_path_git_repo if use_git else chdir_tmp_path

        test_file = tmp_path / "test.py"
        test_file.write_text("non-fixable-error")

        payload = create_pre_hook_payload(str(test_file), test_file.read_text())

        result = runner.invoke(cli, ["hook"], input=payload, catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.stdout)
        assert output.get("decision") == "block"
        assert output.get("continue") is True

    @pytest.mark.parametrize("use_git", [False, True], ids=["no_git", "git"])
    def test_post_hook_with_change(
        self, runner, chdir_tmp_path, chdir_tmp_path_git_repo, pre_commit_config_fixing, use_git
    ):
        """Test that post-hook reports when fixes are applied."""
        # Use the appropriate fixture based on git/no-git
        tmp_path = chdir_tmp_path_git_repo if use_git else chdir_tmp_path

        test_file = tmp_path / "test.py"
        content = "fix-me"
        test_file.write_text(content)

        payload = create_post_hook_payload(str(test_file), content)

        result = runner.invoke(cli, ["hook"], input=payload, catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.stdout)
        # After fix, the hook should allow continuing
        assert output.get("continue") is True
        # Check that the file was actually fixed
        assert test_file.read_text() == "fixed"


class TestRemoteHooks:
    """Test remote hook functionality."""

    @pytest.fixture
    def remote_hook_repo(self, tmp_path):
        """Create a git repository with a hook."""
        remote_path = tmp_path / "remote_repo"
        remote_path.mkdir()

        # Create a dummy hook
        hooks_dir = remote_path / ".pre-commit-hooks.yaml"
        hooks_config = [
            {
                "id": "dummy-remote-hook",
                "name": "Dummy Remote Hook",
                "entry": "dummy_hook.py",
                "language": "script",
                "types": ["python"],
            }
        ]
        with hooks_dir.open("w") as f:
            yaml.dump(hooks_config, f)

        # Create the hook script
        hook_script = remote_path / "dummy_hook.py"
        hook_script.write_text("""#!/usr/bin/env python
import sys
sys.exit(0)
""")
        hook_script.chmod(0o755)

        # Initialize as git repo using pygit2
        repo = pygit2.init_repository(str(remote_path), False)
        cfg = repo.config
        cfg["user.name"] = "test"
        cfg["user.email"] = "test@example.com"

        # Add all files
        index = repo.index
        index.add_all()
        index.write()

        # Create initial commit
        tree = index.write_tree()
        author = pygit2.Signature("test", "test@example.com")
        committer = author
        repo.create_commit("HEAD", author, committer, "Initial commit", tree, [])

        return remote_path

    @pytest.mark.parametrize("use_git", [False, True], ids=["no_git", "git"])
    def test_remote_hook(self, runner, chdir_tmp_path, chdir_tmp_path_git_repo, remote_hook_repo, use_git):
        """Test using a remote pre-commit hook."""
        # Use the appropriate fixture based on git/no-git
        tmp_path = chdir_tmp_path_git_repo if use_git else chdir_tmp_path

        # Create config pointing to remote repo
        config = {
            "repos": [{"repo": f"file://{remote_hook_repo}", "rev": "HEAD", "hooks": [{"id": "dummy-remote-hook"}]}]
        }
        config_path = tmp_path / ".pre-commit-config.yaml"
        with config_path.open("w") as f:
            yaml.dump(config, f)

        test_file = tmp_path / "test.py"
        test_file.write_text("import os\n\ndef func():\n    pass")

        payload = create_pre_hook_payload(str(test_file), test_file.read_text())

        result = runner.invoke(cli, ["hook"], input=payload, catch_exceptions=False)

        assert result.exit_code == 0
        output = json.loads(result.stdout)
        # New API format - continue:true means allowed
        assert output.get("continue") is True
