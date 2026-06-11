"""Shared test fixtures for claude_hooks tests."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pygit2
import pytest
import yaml

from claude_hooks.config import AutofixerConfig
from claude_hooks.inputs import HookContext, PostToolInput
from claude_hooks.precommit_autofix import PreCommitAutoFixerHook
from claude_hooks.tool_models import EditInput, WriteInput

# Core Infrastructure Fixtures
# ============================


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a basic git repository with user configuration."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    repo = pygit2.init_repository(str(repo_path))
    repo.config["user.name"] = "Test User"
    repo.config["user.email"] = "test@example.com"

    return repo_path


@pytest.fixture
def precommit_repo(git_repo: Path) -> Path:
    """Git repository with pre-commit configuration and initial commit."""
    # TODO: Consider using real ruff/isort hooks instead of mock script for more realistic integration testing
    # Create a simple test fixer script that replaces 'foo' with 'bar'
    fixer_script = git_repo / "test_fixer.py"
    fixer_script.write_text("""#!/usr/bin/env python3
import sys
from pathlib import Path

changed = False
for file_path in sys.argv[1:]:
    path = Path(file_path)
    if path.exists():
        content = path.read_text()
        new_content = content.replace('foo', 'bar')
        if content != new_content:
            path.write_text(new_content)
            changed = True

# Exit 1 if changes made (standard pre-commit behavior)
sys.exit(1 if changed else 0)
""")
    fixer_script.chmod(0o755)

    precommit_config = {
        "repos": [
            {
                "repo": "local",
                "hooks": [
                    {
                        "id": "test-fixer",
                        "name": "Test Fixer (foo->bar)",
                        "entry": f"python3 {fixer_script}",
                        "language": "system",
                        "pass_filenames": True,
                    }
                ],
            }
        ]
    }

    config_file = git_repo / ".pre-commit-config.yaml"
    config_file.write_text(yaml.dump(precommit_config))

    # Create initial commit using pygit2
    repo = pygit2.Repository(str(git_repo))
    # Add files to index (paths relative to repo root)
    repo.index.add(".pre-commit-config.yaml")
    repo.index.add("test_fixer.py")
    repo.index.write()
    # Create tree and commit
    tree = repo.index.write_tree()
    sig = pygit2.Signature("Test User", "test@example.com")
    repo.create_commit("HEAD", sig, sig, "Initial commit with pre-commit config", tree, [])

    return git_repo


@pytest.fixture
def claude_config_dir(tmp_path: Path) -> Path:
    """Create Claude configuration directory with hook settings."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()

    # Create adgn-claude-hooks config directory
    hooks_config_dir = claude_dir / "adgn-claude-hooks"
    hooks_config_dir.mkdir(parents=True)

    # Hook configuration in YAML format
    hooks_config_yaml = """
precommit_autofix:
  enabled: true
  timeout_seconds: 10  # Shorter timeout for tests
  tools:
    - Edit
    - MultiEdit
    - Write
  dry_run: false
"""

    (hooks_config_dir / "settings.yaml").write_text(hooks_config_yaml.strip())

    # Claude settings JSON
    claude_settings = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|MultiEdit|Write",
                    "hooks": [
                        {"type": "command", "command": "python -m claude_hooks.precommit_autofix", "timeout": 10}
                    ],
                }
            ]
        }
    }
    (claude_dir / "settings.json").write_text(json.dumps(claude_settings, indent=2))

    return claude_dir


@pytest.fixture(autouse=True)
def xdg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Set up isolated XDG environment variables for all tests automatically."""
    xdg_dirs = {
        "XDG_CONFIG_HOME": tmp_path / ".config",
        "XDG_DATA_HOME": tmp_path / ".local" / "share",
        "XDG_CACHE_HOME": tmp_path / ".cache",
        "XDG_STATE_HOME": tmp_path / ".local" / "state",
    }

    for key, path in xdg_dirs.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(key, str(path))

    return xdg_dirs


@pytest.fixture
def autofixer_hook():
    """Create configured PreCommitAutoFixerHook instance."""
    hook = PreCommitAutoFixerHook()
    hook.autofixer_config = AutofixerConfig(
        enabled=True, timeout_seconds=30, tools=["Edit", "MultiEdit", "Write"], dry_run=False
    )
    return hook


@pytest.fixture
def hook_context(precommit_repo: Path) -> HookContext:
    """Create HookContext for precommit integration tests."""
    return HookContext(
        hook_name="precommit_autofix",
        hook_event="PostToolUse",
        session_id=UUID("87491c5b-6b3d-46fc-b081-bfc0be6f1d33"),
        cwd=precommit_repo,
    )


@pytest.fixture
def configured_hook():
    """Create configured PreCommitAutoFixerHook for testing."""
    hook = PreCommitAutoFixerHook()
    hook.autofixer_config.enabled = True
    hook.autofixer_config.tools = ["Write"]
    return hook


def create_write_hook_input(file_path: Path, content: str, cwd: Path) -> PostToolInput:
    """Helper to create PostToolInput for Write operations."""
    return PostToolInput(
        session_id=UUID("87491c5b-6b3d-46fc-b081-bfc0be6f1d33"),
        transcript_path=Path("/tmp/transcript.json"),
        cwd=cwd,
        hook_event_name="PostToolUse",
        tool_name="Write",
        tool_input=WriteInput(file_path=file_path, content=content),
        tool_response={"success": True},
    )


# Composition Fixtures
# ===================


@pytest.fixture
def integration_env(
    precommit_repo: Path, claude_config_dir: Path, xdg_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
):
    """Full integration test environment with all components."""
    # Set up XDG config to point to our claude_config_dir
    monkeypatch.setenv("XDG_CONFIG_HOME", str(claude_config_dir.parent))

    class IntegrationEnv:
        def write_file(self, file_path: str, content: str) -> Path:
            full_path = precommit_repo / file_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
            return full_path

        def read_file(self, file_path: str) -> str:
            return (precommit_repo / file_path).read_text()

        def file_exists(self, file_path: str) -> bool:
            return (precommit_repo / file_path).exists()

        def create_context(self) -> HookContext:
            """Create a HookContext for this integration environment."""
            return HookContext(
                hook_name="test_hook",
                hook_event="PostToolUse",
                session_id=UUID("12345678-1234-5678-9abc-123456789abc"),
                cwd=precommit_repo,
            )

        @property
        def project_dir(self) -> Path:
            """Access to project directory for backward compatibility."""
            return precommit_repo

        @property
        def claude_config_dir(self) -> Path:
            """Access to claude config directory for backward compatibility."""
            return claude_config_dir

        @property
        def temp_dir(self) -> Path:
            """Access to temp directory for backward compatibility."""
            return precommit_repo.parent

        def create_hook_input(self, tool_name: str, tool_input: Any) -> PostToolInput:
            """Create a PostToolInput for any tool operation."""
            return PostToolInput(
                tool_name=tool_name,
                tool_input=tool_input,
                session_id=UUID("12345678-1234-5678-9abc-123456789abc"),
                transcript_path=precommit_repo.parent / "transcript.json",
                cwd=precommit_repo,
            )

        def build_post_tool_write_input(self, file_path: str, content: str) -> PostToolInput:
            """Create a PostToolInput for Write operations."""
            full_path = file_path if Path(file_path).is_absolute() else str(precommit_repo / file_path)
            return self.create_hook_input("Write", WriteInput(file_path=Path(full_path), content=content))

        def build_post_tool_edit_input(self, file_path: str, old_string: str, new_string: str) -> PostToolInput:
            """Create a PostToolInput for Edit operations."""
            full_path = file_path if Path(file_path).is_absolute() else str(precommit_repo / file_path)
            return self.create_hook_input(
                "Edit", EditInput(file_path=Path(full_path), old_string=old_string, new_string=new_string)
            )

    return IntegrationEnv()


@pytest.fixture
def unit_env(precommit_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Minimal unit test environment with repo and working directory."""
    monkeypatch.chdir(precommit_repo)

    class UnitEnv:
        def __init__(self) -> None:
            self.repo_path: Path = precommit_repo

        def write_file(self, file_path: str, content: str) -> Path:
            full_path = self.repo_path / file_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
            return full_path

        def read_file(self, file_path: str) -> str:
            return (self.repo_path / file_path).read_text()

        def create_context(self) -> HookContext:
            """Create a HookContext for this unit environment."""
            return HookContext(
                hook_name="test_hook",
                hook_event="PostToolUse",
                session_id=UUID("12345678-1234-5678-9abc-123456789abc"),
                cwd=self.repo_path,
            )

    return UnitEnv()
