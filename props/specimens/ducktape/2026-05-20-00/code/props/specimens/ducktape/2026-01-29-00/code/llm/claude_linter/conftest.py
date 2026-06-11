"""Shared pytest configuration and fixtures for ducktape_llm_common tests."""

from pathlib import Path

import pygit2
import pytest
import yaml


@pytest.fixture(autouse=True)
def isolate_test_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate test environment by redirecting cache and disabling user config.

    Sets XDG_CACHE_HOME to tmp_path so cache writes (e.g., claude-linter logs) go to the
    tmp directory instead of the user's home directory, which may be read-only in Bazel sandbox.

    Also disables loading user config in tests.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_LINTER_NO_USER_CONFIG", "1")
    return tmp_path


@pytest.fixture
def chdir_tmp_path(tmp_path, monkeypatch):
    """Change cwd to tmp_path for tests."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def chdir_tmp_path_git_repo(tmp_path, monkeypatch):
    """Change cwd to tmp_path and init a git repo via pygit2."""
    monkeypatch.chdir(tmp_path)
    repo = pygit2.init_repository(str(tmp_path), False)
    cfg = repo.config
    cfg["user.name"] = "test"
    cfg["user.email"] = "test@example.com"
    return tmp_path


@pytest.fixture
def pre_commit_config_non_fixing(chdir_tmp_path: Path):
    # Creates a non-fixing hook config
    linter = chdir_tmp_path / "non_fixing_linter.py"
    linter.write_text(
        """#!/usr/bin/env python
import sys
err=False
for f in sys.argv[1:]:
    if 'non-fixable-error' in open(f).read(): err=True
sys.exit(1 if err else 0)
"""
    )
    linter.chmod(0o755)
    config = {
        "repos": [
            {
                "repo": "local",
                "hooks": [
                    {"id": "non-fixing", "name": "nf", "entry": str(linter), "language": "script", "types": ["python"]}
                ],
            }
        ]
    }
    cfg = chdir_tmp_path / ".pre-commit-config.yaml"
    cfg.write_text(yaml.dump(config))
    return cfg


