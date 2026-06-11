from __future__ import annotations

from functools import partial

import pytest_bazel

from git_commit_ai.editor import SCISSORS_MARK, render_editor_content

# Test helper - stats args are irrelevant for these tests
_render = partial(render_editor_content, cached=False, elapsed_s=0.0)


def test_editor_content_respects_commit_verbose_config(temp_repo, git_repo):
    # Seed initial commit
    temp_repo.write("README.md", "hello\n")
    temp_repo.stage("README.md")
    temp_repo.commit("init")

    # Stage a new file for commit
    temp_repo.write("staged.txt", "content\n")
    temp_repo.stage("staged.txt")

    # Enable commit.verbose via config (no -v flag)
    git_repo.config["commit.verbose"] = "true"
    content = _render(git_repo, "test msg")
    assert f"# {SCISSORS_MARK}" in content
    # Verbose diff appears below scissors without # prefix (git behavior)
    assert "diff --git" in content
    assert "# diff --git" not in content  # NOT commented

    # Disable commit.verbose; diff lines should not be included
    git_repo.config["commit.verbose"] = "false"
    content2 = _render(git_repo, "test msg")
    assert f"# {SCISSORS_MARK}" in content2
    assert "diff --git" not in content2


def test_editor_content_sections_for_staged_changes(temp_repo, git_repo):
    temp_repo.write("a.txt", "a\n")
    temp_repo.stage("a.txt")
    temp_repo.commit("init")

    temp_repo.write("b.txt", "b\n")
    temp_repo.stage("b.txt")

    content = _render(git_repo, "test msg")
    assert "# Changes to be committed:" in content
    assert "b.txt" in content
    assert f"# {SCISSORS_MARK}" in content


def test_editor_content_sections_for_unstaged_and_untracked(temp_repo, git_repo):
    # Init
    temp_repo.write("file.txt", "v1\n")
    temp_repo.stage("file.txt")
    temp_repo.commit("init")

    # Make unstaged change
    temp_repo.write("file.txt", "v2\n")
    # Create untracked file
    temp_repo.write("untracked.txt", "x\n")

    content = _render(git_repo, "test msg")
    assert "# Changes not staged for commit:" in content
    assert "file.txt" in content
    assert "# Untracked files:" in content
    assert "untracked.txt" in content
    assert f"# {SCISSORS_MARK}" in content


def test_editor_content_clean_repo(temp_repo, git_repo):
    temp_repo.write("r.txt", "x\n")
    temp_repo.stage("r.txt")
    temp_repo.commit("init")

    content = _render(git_repo, "test msg")
    # Should not show any change sections if clean
    assert "# Changes to be committed:" not in content
    assert "# Changes not staged for commit:" not in content
    assert "# Untracked files:" not in content
    assert f"# {SCISSORS_MARK}" in content


def test_editor_content_includes_user_context(temp_repo, git_repo):
    temp_repo.write("a.txt", "a\n")
    temp_repo.stage("a.txt")
    temp_repo.commit("init")

    temp_repo.write("b.txt", "b\n")
    temp_repo.stage("b.txt")

    content = _render(git_repo, "test msg", user_context="fixing the auth bug")
    assert "# User context (-m):" in content
    assert "# fixing the auth bug" in content


def test_editor_content_includes_previous_message_when_amending(temp_repo, git_repo):
    temp_repo.write("a.txt", "a\n")
    temp_repo.stage("a.txt")
    temp_repo.commit("init")

    temp_repo.write("b.txt", "b\n")
    temp_repo.stage("b.txt")

    content = _render(git_repo, "new msg", previous_message="old commit message")
    assert "# Previous commit message (being amended):" in content
    assert "# old commit message" in content


def test_editor_content_includes_both_user_context_and_previous_message(temp_repo, git_repo):
    temp_repo.write("a.txt", "a\n")
    temp_repo.stage("a.txt")
    temp_repo.commit("init")

    temp_repo.write("b.txt", "b\n")
    temp_repo.stage("b.txt")

    content = _render(git_repo, "new msg", previous_message="old commit", user_context="user hint")
    # Both should be present and commented
    assert "# User context (-m):" in content
    assert "# user hint" in content
    assert "# Previous commit message (being amended):" in content
    assert "# old commit" in content
    # User context should appear before previous message
    assert content.index("# User context") < content.index("# Previous commit message")


def test_staged_files_not_duplicated_in_unstaged_section(temp_repo, git_repo, snapshot):
    """Staged-only files must NOT appear in 'Changes not staged for commit'.

    Bug: When modifying an existing tracked file and staging it, the file
    incorrectly appeared in both "Changes to be committed" AND "Changes not
    staged for commit" because unstaged used HEAD→worktree instead of index→worktree.
    """
    # Init with a file
    temp_repo.write("tracked.txt", "v1\n")
    temp_repo.stage("tracked.txt")
    temp_repo.commit("init")

    # Modify and STAGE the file (bug case: should NOT appear in unstaged)
    temp_repo.write("tracked.txt", "v2\n")
    temp_repo.stage("tracked.txt")

    content = _render(git_repo, "test")

    # Snapshot the full content
    assert content == snapshot

    # Explicit assertion: tracked.txt must NOT appear in unstaged section
    assert "# Changes not staged for commit:" not in content, (
        "Unstaged section should not exist when all changes are staged"
    )


if __name__ == "__main__":
    pytest_bazel.main()
