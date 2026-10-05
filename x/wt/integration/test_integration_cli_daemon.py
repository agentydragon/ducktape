"""Integration tests for the CLI daemon with real git operations."""

import pygit2
import pytest
import pytest_bazel

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

    def test_create_worktree_reserved_name(self, real_temp_repo, wt_cli):
        """Test that creating worktrees with reserved names fails."""

        result = wt_cli.sh_c("main")
        assert result.returncode != 0
        assert "reserved" in result.stderr.lower() or "error" in result.stdout.lower()

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

    @pytest.mark.parametrize(("hydrate", "expected_entries"), [(True, {"README.md"}), (False, set())])
    def test_create_honors_hydrate_worktrees_config(
        self, real_temp_repo, daemon_config_factory, wt_cli, hydrate, expected_entries
    ):
        """`hydrate_worktrees` in config.yaml reaches the daemon: the worktree is checked out, or holds only `.git`."""
        # Rewrites config.yaml in the WT_DIR `wt_cli` is bound to, before the first CLI call starts the daemon.
        daemon_config_factory(real_temp_repo).integration(hydrate_worktrees=hydrate)

        result = wt_cli.sh_c("hydrate-test")
        assert result.returncode == 0, f"Create failed: {result.stderr}"

        wt_path = real_temp_repo / "worktrees" / "hydrate-test"
        assert {p.name for p in wt_path.iterdir() if p.name != ".git"} == expected_entries


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


if __name__ == "__main__":
    pytest_bazel.main()
