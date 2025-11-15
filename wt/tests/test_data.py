"""Centralized test data to reduce duplication across test fixtures."""

from typing import ClassVar

import pygit2

# Shared timing constants for tests
WATCHER_DEBOUNCE_SECS: float = 0.05


class TestData:
    """Centralized test data constants."""

    class Git:
        """Git-related test data."""

        USER_NAME = "Test User"
        USER_EMAIL = "test@example.com"

        @classmethod
        def signature(cls):
            """Standard git signature for tests."""
            return pygit2.Signature(cls.USER_NAME, cls.USER_EMAIL)

    class Branches:
        """Branch naming constants."""

        MAIN = "main"
        MASTER = "master"
        TEST_PREFIX = "test/"
        FEATURE_PREFIX = "feature/"
        INTEGRATION_TEST = "integration-test-branch"

        @classmethod
        def prefixed_name(cls, name: str, prefix: str | None = None) -> str:
            """Create a prefixed branch name."""
            prefix = prefix or cls.TEST_PREFIX
            return f"{prefix}{name}"

    class Commits:
        """Commit message templates."""

        INITIAL = "Initial commit"
        FEATURE_TEMPLATE = "Add feature: {feature_name}"
        BUGFIX_TEMPLATE = "Fix bug: {bug_description}"
        REFACTOR = "Refactor code"

        @classmethod
        def feature(cls, feature_name: str) -> str:
            """Generate feature commit message."""
            return cls.FEATURE_TEMPLATE.format(feature_name=feature_name)

        @classmethod
        def bugfix(cls, bug_description: str) -> str:
            """Generate bugfix commit message."""
            return cls.BUGFIX_TEMPLATE.format(bug_description=bug_description)

    class Paths:
        """Path-related constants."""

        WT_DIR_NAME = ".wt"
        WORKTREES_DIR_NAME = "worktrees"
        CONFIG_FILE_NAME = "config.yaml"

        # Use this instead of hard-coded "WTDIR" scattered throughout
        TEST_WT_DIR_PARENT = "test-wt-config"

    class Files:
        """File content and names."""

        README_CONTENT = "# Test Repository\n\nThis is a test repository for worktree management testing."
        GITIGNORE_CONTENT = "*.pyc\n__pycache__/\n.pytest_cache/\n"

        # Standard test files
        FEATURE_FILE = "feature.txt"
        TEST_FILE = "test.py"
        CONFIG_FILE = "config.yaml"


class ConfigPresets:
    """Configuration presets for different test scenarios."""

    MINIMAL: ClassVar[dict] = {"github_enabled": False, "log_operations": False, "cow_method": "copy"}

    INTEGRATION: ClassVar[dict] = {"github_enabled": True, "log_operations": True, "cow_method": "copy"}

    E2E: ClassVar[dict] = {
        "github_enabled": False,
        "log_operations": True,
        "cache_expiration": 3600,
        "cache_refresh_age": 300,
    }

    GITHUB_ENABLED: ClassVar[dict] = {"github_enabled": True, "github_repo": "test-user/test-repo"}


class MockBehaviors:
    """Standard mock behaviors for test fixtures."""

    class GitHub:
        """GitHub API mock behaviors."""

        @staticmethod
        def empty_pr_list():
            """No pull requests found."""
            return []

        @staticmethod
        def single_pr(branch_name: str, pr_number: int = 123):
            """Single PR for a branch."""
            return [
                {
                    "number": pr_number,
                    "headRefName": branch_name,
                    "state": "open",
                    "title": f"PR for {branch_name}",
                    "mergeable": True,
                }
            ]

        @staticmethod
        def multiple_prs(branches: list[str]):
            """Multiple PRs for different branches."""
            return [
                {
                    "number": 100 + i,
                    "headRefName": branch,
                    "state": "open",
                    "title": f"PR for {branch}",
                    "mergeable": True,
                }
                for i, branch in enumerate(branches)
            ]

    class Git:
        """Git operation mock behaviors."""

        @staticmethod
        def clean_status():
            """Clean working directory status."""
            return [], []  # no dirty files, no untracked files

        @staticmethod
        def dirty_status():
            """Dirty working directory status."""
            return ["modified.txt"], ["untracked.txt"]

        @staticmethod
        def standard_branches():
            """Standard branch list for tests."""
            return [TestData.Branches.MAIN, "feature-1", "feature-2"]
