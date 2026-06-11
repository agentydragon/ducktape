#!/usr/bin/env python3
"""Tests for git-commit-ai cache key generation."""

from __future__ import annotations

import pygit2
import pytest
import pytest_bazel

from git_commit_ai.cli import build_cache_key


def test_cache_key_includes_amend_status():
    """Test that cache key differentiates between new and amend commits."""
    model_name = "sonnet"
    scope = "staged"
    head_oid = pygit2.Oid(hex="abc123def456abc123def456abc123def456abc1")
    diff = "test diff content"

    # Key for new commit
    key_new = build_cache_key(
        model_name, include_all=(scope == "all"), previous_message=None, head_oid=head_oid, diff=diff, user_context=None
    )

    # Key for amend
    key_amend = build_cache_key(
        model_name,
        include_all=(scope == "all"),
        previous_message="some msg",
        head_oid=head_oid,
        diff=diff,
        user_context=None,
    )

    # Should be different
    assert key_new != key_amend
    assert ":new:" in key_new
    assert ":amend:" in key_amend


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

if __name__ == "__main__":
    pytest_bazel.main()
