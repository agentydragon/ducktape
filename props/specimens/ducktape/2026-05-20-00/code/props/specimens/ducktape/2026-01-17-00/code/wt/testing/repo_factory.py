"""Git repository factory to eliminate fixture duplication."""

from pathlib import Path

import pygit2

from wt.testing.data import TestData


class GitRepoFactory:
    """Factory for creating git repositories with different configurations."""

    def __init__(self, base_path: Path):
        """Initialize factory with base path."""
        self.base_path = base_path

    def create_repo(
        self,
        *,
        name: str = "repo",
        branches: list[str] | None = None,
        commits_per_branch: int = 1,
        with_worktrees: bool | list[str] = False,
        initial_files: dict[str, str] | None = None,
    ) -> Path:
        """Create a git repository with specified configuration.

        Args:
            name: Repository directory name
            branches: List of branch names to create (default: just main)
            commits_per_branch: Number of commits to make on each branch
            with_worktrees: If True, create worktrees for non-main branches.
                          If list, create worktrees with those names.
            initial_files: Dict of filename -> content for initial commit

        Returns:
            Path to the created repository
        """
        repo_path = self.base_path / name
        repo_path.mkdir(exist_ok=True)

        # Initialize repository
        repo = pygit2.init_repository(str(repo_path), initial_head=TestData.Branches.MAIN)

        # Configure git user
        repo.config["user.name"] = TestData.Git.USER_NAME
        repo.config["user.email"] = TestData.Git.USER_EMAIL

        # Create initial files
        files = initial_files or {"README.md": TestData.Files.README_CONTENT}
        for filename, content in files.items():
            (repo_path / filename).write_text(content)
            repo.index.add(filename)

        # Make initial commit
        repo.index.write()
        signature = TestData.Git.signature()
        tree = repo.index.write_tree()
        _initial_commit = repo.create_commit("HEAD", signature, signature, TestData.Commits.INITIAL, tree, [])

        # Create additional branches if requested
        if branches:
            self._create_branches(repo, repo_path, branches, commits_per_branch, signature)

        # Create worktrees if requested
        if with_worktrees:
            self._create_worktrees(repo, repo_path, with_worktrees, branches)

        return repo_path

    def _create_branches(
        self,
        repo: pygit2.Repository,
        repo_path: Path,
        branches: list[str],
        commits_per_branch: int,
        signature: pygit2.Signature,
    ) -> None:
        """Create additional branches with commits."""
        main_commit = repo.head.target

        for branch_name in branches:
            if branch_name == TestData.Branches.MAIN:
                continue  # Skip main branch as it already exists

            # Create branch from main
            branch_ref = repo.references.create(f"refs/heads/{branch_name}", main_commit)
            repo.checkout(branch_ref)

            # Make commits on this branch
            for i in range(commits_per_branch):
                filename = f"{branch_name}-{i}.txt"
                content = f"Content for {branch_name} commit {i + 1}"

                (repo_path / filename).write_text(content)
                repo.index.add(filename)
                repo.index.write()

                tree = repo.index.write_tree()
                commit_message = TestData.Commits.feature(f"{branch_name}-{i + 1}")

                # Get parent commit
                parent_commit = repo.head.target
                repo.create_commit("HEAD", signature, signature, commit_message, tree, [parent_commit])

        # Switch back to main
        repo.checkout("refs/heads/main")

    def _create_worktrees(
        self, repo: pygit2.Repository, repo_path: Path, with_worktrees: bool | list[str], branches: list[str] | None
    ) -> None:
        """Create worktrees for branches using pygit2."""
        if isinstance(with_worktrees, bool) and with_worktrees:
            # Create worktrees for all non-main branches
            worktree_names = [b for b in (branches or []) if b != TestData.Branches.MAIN]
        elif isinstance(with_worktrees, list):
            # Create worktrees with specified names (copy list to avoid aliasing param)
            worktree_names = list(with_worktrees)
        else:
            return

        # Create worktrees directory
        worktrees_dir = repo_path / TestData.Paths.WORKTREES_DIR_NAME
        worktrees_dir.mkdir(exist_ok=True)

        for worktree_name in worktree_names:
            worktree_path = worktrees_dir / worktree_name
            branch_name = worktree_name  # Assume worktree name matches branch name

            # Get or create branch reference
            branch_ref = repo.lookup_branch(branch_name)
            if branch_ref is None:
                # Create branch from HEAD
                commit = repo.head.peel(pygit2.Commit)
                branch_ref = repo.branches.local.create(branch_name, commit)

            # Add worktree using pygit2
            repo.add_worktree(worktree_name, str(worktree_path), branch_ref)


class RepoPresets:
    """Preset configurations for common repository types."""

    @staticmethod
    def minimal():
        """Minimal repo with just main branch and README."""
        return {}

    @staticmethod
    def with_branches():
        """Repo with multiple branches."""
        return {"branches": ["feature-1", "feature-2", "bugfix-1"], "commits_per_branch": 2}

    @staticmethod
    def with_worktrees():
        """Repo with branches and corresponding worktrees."""
        return {"branches": ["feature-1", "feature-2"], "commits_per_branch": 1, "with_worktrees": True}

    @staticmethod
    def integration_test():
        """Repo configured for integration tests."""
        return {
            "branches": [TestData.Branches.INTEGRATION_TEST],
            "initial_files": {
                "README.md": TestData.Files.README_CONTENT,
                ".gitignore": TestData.Files.GITIGNORE_CONTENT,
            },
        }

    @staticmethod
    def populated():
        """Heavily populated repo for complex tests."""
        return {
            "branches": ["feature-a", "feature-b", "experimental", "hotfix"],
            "commits_per_branch": 3,
            "with_worktrees": ["feature-a", "experimental"],
            "initial_files": {
                "README.md": TestData.Files.README_CONTENT,
                ".gitignore": TestData.Files.GITIGNORE_CONTENT,
                "src/main.py": "# Main application file\nprint('Hello, World!')",
                "tests/test_main.py": "# Test file\ndef test_main():\n    assert True",
            },
        }
