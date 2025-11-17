#!/usr/bin/env python3
"""Tests for git-commit-ai --amend functionality with mocked AI (pygit2-only)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pygit2
import pytest

from adgn.git_commit_ai import cli
from adgn.git_commit_ai.cli import build_cache_key, get_commit_diff
from adgn.git_commit_ai.core import build_prompt

from .git_repo_utils import _commit, _init_repo, _stage

# Fixtures moved to tests/llm/conftest.py


# ----------------------------- tests --------------------------------


def test_get_commit_diff_normal_commit(temp_repo: pygit2.Repository):
    """Test get_commit_diff for a normal (non-amend) commit."""
    # Create initial file and commit
    test_file = Path(temp_repo.workdir) / "test.txt"
    test_file.write_text("initial content\n")
    _stage(temp_repo, "test.txt")
    _commit(temp_repo, "Initial commit")

    # Stage changes
    test_file.write_text("initial content\nmore content\n")
    _stage(temp_repo, "test.txt")

    # Get diff without amend
    diff = get_commit_diff(temp_repo, [], previous_message=None)

    assert "more content" in diff
    assert "@@" in diff  # Should have diff headers
    assert "=== Original commit" not in diff  # Should NOT have amend sections


def test_get_commit_diff_amend_with_staged_changes(temp_repo: pygit2.Repository):
    """Test get_commit_diff for --amend with staged changes."""
    # Create initial commit
    test_file = Path(temp_repo.workdir) / "test.txt"
    test_file.write_text("initial content\n")
    _stage(temp_repo, "test.txt")
    _commit(temp_repo, "Initial commit")

    # Make and stage new changes
    test_file.write_text("initial content\nmore content\n")
    _stage(temp_repo, "test.txt")

    # Get diff with amend (previous_message indicates amend)
    diff = get_commit_diff(temp_repo, [], previous_message="Initial commit")

    # Should have both sections
    assert "=== Original commit" in diff
    assert "=== New changes being added ===" in diff
    assert "initial content" in diff
    assert "more content" in diff


def test_get_commit_diff_amend_first_commit(temp_repo: pygit2.Repository):
    """Test get_commit_diff when amending the very first commit (no HEAD^)."""
    # Create first commit
    test_file = Path(temp_repo.workdir) / "test.txt"
    test_file.write_text("first file\n")
    _stage(temp_repo, "test.txt")
    _commit(temp_repo, "First commit ever")

    # Stage changes for amend
    test_file.write_text("first file\nupdated\n")
    _stage(temp_repo, "test.txt")

    # Get diff for amending first commit
    diff = get_commit_diff(temp_repo, [], previous_message="First commit ever")

    # Should handle missing HEAD^ gracefully
    assert "=== Original commit content ===" in diff  # Uses empty tree
    assert "=== New changes being added ===" in diff
    assert "updated" in diff


def test_get_commit_diff_amend_with_all_flag(temp_repo: pygit2.Repository):
    """Test get_commit_diff for --amend -a (all tracked changes)."""
    # Create initial commit
    test_file = Path(temp_repo.workdir) / "test.txt"
    test_file.write_text("initial\n")
    _stage(temp_repo, "test.txt")
    _commit(temp_repo, "Initial")

    # Make changes but don't stage
    test_file.write_text("initial\nmodified\n")

    # Get diff with amend and -a flag
    diff = get_commit_diff(temp_repo, ["-a"], previous_message="Initial")

    assert "=== Original commit" in diff
    assert "=== New changes being added ===" in diff
    assert "modified" in diff


def test_build_prompt_without_amend(temp_repo: pygit2.Repository):
    """Test build_prompt for regular commits."""
    # Create initial commit first (so HEAD exists)
    initial_file = Path(temp_repo.workdir) / "initial.txt"
    initial_file.write_text("initial\n")
    _stage(temp_repo, "initial.txt")
    _commit(temp_repo, "Initial commit")

    # Now create a file and stage it for a new commit
    test_file = Path(temp_repo.workdir) / "test.txt"
    test_file.write_text("content\n")
    _stage(temp_repo, "test.txt")

    # Use our helper to compute staged diff for prompt builder
    diff = get_commit_diff(temp_repo, [], previous_message=None)
    prompt = build_prompt(temp_repo, diff, [], previous_message=None)

    assert "Write a concise, imperative-mood Git commit message" in prompt
    assert "Previous commit message:" not in prompt
    assert "being amended" not in prompt


def test_build_prompt_with_amend(temp_repo: pygit2.Repository):
    """Test build_prompt for amend commits."""
    # Create initial commit
    test_file = Path(temp_repo.workdir) / "test.txt"
    test_file.write_text("initial\n")
    _stage(temp_repo, "test.txt")
    _commit(temp_repo, "My original message")

    # Stage changes
    test_file.write_text("initial\nmore\n")
    _stage(temp_repo, "test.txt")

    diff = get_commit_diff(temp_repo, [], previous_message="My original message")
    prompt = build_prompt(temp_repo, diff, [], previous_message="My original message")

    assert "Update and refine this existing commit message" in prompt
    assert "Previous commit message:" in prompt
    assert "My original message" in prompt
    assert "The commit is being amended" in prompt


async def test_full_amend_flow_integration(monkeypatch, tmp_path: Path, patch_fake_editor):
    """Integration test of the full amend flow with mocked AI."""
    tmpdir = tmp_path / "repo"
    tmpdir.mkdir(parents=True, exist_ok=True)
    repo = _init_repo(str(tmpdir), name="Test", email="test@test.com")
    # Ensure the CLI runs inside this temporary repository
    monkeypatch.chdir(str(tmpdir))
    # Create initial commit
    test_file = Path(repo.workdir) / "file.txt"
    test_file.write_text("version 1\n")
    _stage(repo, "file.txt")
    _commit(repo, "Initial implementation")

    # Make changes for amend
    test_file.write_text("version 1\nversion 2\n")
    _stage(repo, "file.txt")

    # Get previous message
    previous_message = repo.head.peel(pygit2.Commit).message.strip()
    assert previous_message == "Initial implementation"

    # Get the diff that would be shown to AI
    diff = get_commit_diff(repo, [], previous_message=previous_message)

    # Verify diff contains both original and new changes
    assert "=== Original commit" in diff
    assert "=== New changes being added ===" in diff
    assert "version 1" in diff
    assert "version 2" in diff

    # Build prompt as the tool would
    prompt = build_prompt(repo, diff, [], previous_message=previous_message)

    # Verify prompt is for amending
    assert "Update and refine" in prompt
    assert "Initial implementation" in prompt

    # Mock AI would generate updated message
    new_message = "Updated: Initial implementation\n\n- Added more changes"

    # Patch provider generate and cache so it uses cached message
    async def _fake_generate(*args, **kwargs) -> str:  # match production signature leniently
        return new_message

    monkeypatch.setattr("adgn.git_commit_ai.minicodex_backend.generate_commit_message_minicodex", _fake_generate)
    monkeypatch.setattr("adgn.git_commit_ai.cli.Cache.get", lambda self, key: new_message)

    # Run the tool; patch argv to avoid pytest args leaking
    with patch("sys.exit") as mock_exit:
        await cli.async_main([])
        mock_exit.assert_called_with(0)

    # Verify the committed message contains only the AI message
    fresh = pygit2.Repository(str(tmpdir))
    committed = fresh.head.peel(pygit2.Commit).message.strip()
    assert committed.startswith(("Subject line", "Updated:"))
    assert "editor-added comment" not in committed
    assert ">8" not in committed
    assert "diff line (commented)" not in committed


def test_cache_key_includes_amend_status():
    """Test that cache key differentiates between new and amend commits."""

    # Simulate cache key generation
    provider = "claude"
    model_name = "sonnet"
    scope = "staged"
    commitish = "abc123"
    diff = "test diff content"
    _ = hashlib.sha256(diff.encode()).hexdigest()

    # Key for new commit
    key_new = build_cache_key(
        model_name,
        include_all=(scope == "all"),
        previous_message=None,
        commitish=commitish,
        diff=diff,
        provider=provider,
    )

    # Key for amend
    key_amend = build_cache_key(
        model_name,
        include_all=(scope == "all"),
        previous_message="some msg",
        commitish=commitish,
        diff=diff,
        provider=provider,
    )

    # Should be different
    assert key_new != key_amend
    assert ":new:" in key_new
    assert ":amend:" in key_amend


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
