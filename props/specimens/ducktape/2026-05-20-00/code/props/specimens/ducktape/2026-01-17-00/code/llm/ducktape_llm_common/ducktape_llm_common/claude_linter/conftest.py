"""Shared pytest configuration and fixtures for ducktape_llm_common tests."""

import os
from pathlib import Path

import pygit2
import pytest
import yaml

# Disable loading user config in tests
os.environ["CLAUDE_LINTER_NO_USER_CONFIG"] = "1"


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
def local_git_repo_for_remote_hook(tmp_path):
    """Create a local git repo with a dummy remote hook manifest."""
    repo_path = tmp_path / "remote_repo"
    repo_path.mkdir()
    repo = pygit2.init_repository(str(repo_path), False)
    cfg = repo.config
    cfg["user.name"] = "test"
    cfg["user.email"] = "test@example.com"
    # dummy hook script
    dummy = repo_path / "dummy_hook.py"
    dummy.write_text(
        """#!/usr/bin/env python
import sys
for fn in sys.argv[1:]:
    if 'remote-error' in open(fn).read():
        print(f'Found remote error in {fn}', file=sys.stderr)
        sys.exit(1)
sys.exit(0)"""
    )
    dummy.chmod(0o755)
    # manifest
    manifest = [
        {
            "id": "dummy-remote-hook",
            "name": "Dummy Remote Hook",
            "entry": "dummy_hook.py",
            "language": "script",
            "types": ["python"],
        }
    ]
    (repo_path / ".pre-commit-hooks.yaml").write_text(yaml.dump(manifest))
    # commit all
    index = repo.index
    index.add_all()
    index.write()
    tree = index.write_tree()
    author = pygit2.Signature(cfg["user.name"], cfg["user.email"])
    repo.create_commit("HEAD", author, author, "initial", tree, [])
    return repo_path


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


@pytest.fixture
def pre_commit_config_remote(chdir_tmp_path: Path, local_git_repo_for_remote_hook):
    # Configure remote hook from local git repo
    repo_url = f"file://{local_git_repo_for_remote_hook}"
    config = {"repos": [{"repo": repo_url, "rev": "HEAD", "hooks": [{"id": "dummy-remote-hook"}]}]}
    cfg = chdir_tmp_path / ".pre-commit-config.yaml"
    cfg.write_text(yaml.dump(config))
    return cfg
