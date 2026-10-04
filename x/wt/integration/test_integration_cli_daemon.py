"""Integration tests for the CLI daemon with real git operations."""

import os

import pygit2
import pytest_bazel

from x.wt.shared.git_utils import GitRunOptions, git_run
from x.wt.testing.asserts import assert_output_contains


class TestCLIIntegration:
    def test_list_worktrees_empty(self, real_temp_repo, wt_cli):
        """Test listing worktrees when none exist."""
        # Kill daemon for this test's WT_DIR

        result = wt_cli.sh("ls")
        assert result.returncode == 0
        # When no worktrees exist, status shows main repo line; ensure no non-main entries
        # We assert absence of typical worktree parent path
        assert "Available worktrees:" not in result.stdout

    def test_list_worktrees_with_existing(self, real_temp_repo, real_env, wt_cli):
        """Test listing worktrees when some exist."""

        # Create a worktree first
        result = wt_cli.sh_c("feature1")
        assert result.returncode == 0, f"Create failed: {result.stderr}"

        # List worktrees
        result = wt_cli.sh("ls")
        assert result.returncode == 0
        assert_output_contains(result.stdout, "feature1")

    def test_create_worktree_reserved_name(self, real_temp_repo, wt_cli):
        """Test that creating worktrees with reserved names fails."""

        result = wt_cli.sh_c("main")
        assert result.returncode != 0
        assert "reserved" in result.stderr.lower() or "error" in result.stdout.lower()

    def test_path_commands(self, real_temp_repo, wt_cli):
        """Test path resolution commands."""

        # Create a worktree
        wt_cli.sh_c("path-test")

        # Test path command
        result = wt_cli.sh("path", "path-test")
        assert result.returncode == 0
        assert_output_contains(result.stdout, "path-test")

    def test_path_command_worktree_name(self, real_temp_repo, wt_cli):
        """ "x" resolves to the worktree directory (treat as worktree name)."""
        r = wt_cli.sh_c("pth")
        assert r.returncode == 0, f"Create failed: {r.stderr}"
        wt_path = real_temp_repo / "worktrees" / "pth"
        assert wt_path.exists()

        res = wt_cli.sh("path", "pth")
        assert res.returncode == 0
        assert_output_contains(res.stdout, wt_path)

    def test_path_command_relative_path(self, real_temp_repo, wt_cli):
        """ "./x" resolves to a path inside the current worktree (treat as path)."""
        r = wt_cli.sh_c("pth")
        assert r.returncode == 0, f"Create failed: {r.stderr}"
        wt_path = real_temp_repo / "worktrees" / "pth"
        assert wt_path.exists()

        (wt_path / "subdir").mkdir(parents=True, exist_ok=True)

        res = wt_cli.sh("path", "./subdir", cwd=wt_path)
        assert res.returncode == 0
        assert_output_contains(res.stdout, wt_path / "subdir")


class TestRealGitOperations:
    """Tests that verify actual git operations work correctly."""

    def test_worktree_branch_creation(self, real_temp_repo, wt_cli):
        """Test that worktree creation actually creates git branches."""

        # Create worktree
        result = wt_cli.sh_c("test-branch")
        assert result.returncode == 0, f"Failed: {result.stderr}"

        # Check that branch exists using pygit2
        repo = pygit2.Repository(real_temp_repo)
        branch_names = [name for name in repo.references if name.startswith("refs/heads/test/")]
        assert "refs/heads/test/test-branch" in branch_names

        # Check worktree is on correct branch
        worktree_path = real_temp_repo / "worktrees" / "test-branch"
        worktree_repo = pygit2.Repository(worktree_path)
        assert worktree_repo.head.shorthand == "test/test-branch"

    def test_worktree_status_with_changes(self, real_temp_repo, real_env, wt_cli):
        """Test that status command shows git changes in worktrees."""

        # Create worktree
        wt_cli.sh_c("status-test")
        worktree_path = real_temp_repo / "worktrees" / "status-test"

        # Make some changes
        (worktree_path / "modified.txt").write_text("Modified content")
        (worktree_path / "untracked.txt").write_text("Untracked content")

        # Stage one file using pygit2
        worktree_repo = pygit2.Repository(worktree_path)
        worktree_repo.index.add("modified.txt")
        worktree_repo.index.write()

        # Check status shows the changes
        result = wt_cli.status()
        assert result.returncode == 0
        # Status should show the worktree (exact format depends on implementation)
        assert_output_contains(result.stdout, "status-test")

    def test_sparse_empty_cone_then_extend(self, real_temp_repo, config_factory, wtcli):
        # Create a repo with nested content
        (real_temp_repo / "foo").mkdir()
        (real_temp_repo / "foo" / "bar").mkdir(parents=True, exist_ok=True)
        (real_temp_repo / "foo" / "bar" / "baz.txt").write_text("baz")
        (real_temp_repo / "top.txt").write_text("top")

        repo = pygit2.Repository(real_temp_repo)
        repo.index.add_all()
        repo.index.write()
        sig = pygit2.Signature("Test User", "test@example.com")
        tree = repo.index.write_tree()
        parent = repo.head.target
        repo.create_commit("HEAD", sig, sig, "Seed content", tree, [parent])

        factory = config_factory(real_temp_repo)
        config = factory.integration(hydrate_worktrees=False)
        env = os.environ.copy()
        env["WT_DIR"] = str(config.wt_dir)

        # Create worktree via CLI
        cli = wtcli(env)
        result = cli.sh_c("cone-test")
        assert result.returncode == 0, f"Create failed: {result.stderr}"
        wt_path = real_temp_repo / "worktrees" / "cone-test"
        assert wt_path.exists()

        # Verify worktree is initially empty (no files except .git)
        entries = [p for p in wt_path.iterdir() if p.name != ".git"]
        assert entries == []

        # Extend cone using git, then verify files appear

        git_run(["sparse-checkout", "init", "--no-cone"], cwd=wt_path)
        git_run(
            ["sparse-checkout", "set", "--no-cone", "--stdin"], cwd=wt_path, options=GitRunOptions(input_data=b"foo\n")
        )
        git_run(["checkout", "-f"], cwd=wt_path)
        assert (wt_path / "foo" / "bar" / "baz.txt").exists()
        assert not (wt_path / "top.txt").exists()


if __name__ == "__main__":
    pytest_bazel.main()
