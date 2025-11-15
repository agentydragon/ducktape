"""Parse git diff output to extract file change statistics."""

from dataclasses import dataclass
from pathlib import Path
import subprocess

try:
    from git import InvalidGitRepositoryError, Repo

    HAS_GITPYTHON = True
except ImportError:
    HAS_GITPYTHON = False


@dataclass
class FileChange:
    """Represents changes to a single file."""

    path: str
    additions: int
    deletions: int
    is_binary: bool = False

    @property
    def total_changes(self) -> int:
        """Total number of line changes (additions + deletions)."""
        return self.additions + self.deletions


def parse_numstat_output(numstat_output: str) -> list[FileChange]:
    """
    Parse git diff --numstat output string.

    Args:
        numstat_output: Output from 'git diff --numstat' command.

    Returns:
        List of FileChange objects.

    TODO: Consider using GitPython library for more robust git operations
          (https://gitpython.readthedocs.io/) instead of parsing raw output.
    TODO: Handle edge cases:
          - File paths with tabs or special characters
          - File paths with spaces (currently handled by tab split)
          - Unicode file names
          - Very long file paths
          - Renamed files (shown as "old => new" format)
          - Malformed numstat output (invalid format)
    """
    changes = []
    for line in numstat_output.strip().split("\n"):
        if not line:
            continue

        parts = line.split("\t")
        if len(parts) != 3:
            # TODO: Log warning for malformed lines instead of silently skipping
            continue

        additions_str, deletions_str, path = parts

        # Handle binary files (shown as '-' for both additions and deletions)
        is_binary = additions_str == "-" and deletions_str == "-"

        # TODO: Handle renamed files (format: "old_name => new_name")
        # TODO: Validate path doesn't contain malicious characters

        try:
            additions = int(additions_str)
        except ValueError:
            additions = 0

        try:
            deletions = int(deletions_str)
        except ValueError:
            deletions = 0

        changes.append(
            FileChange(
                path=path,
                additions=additions,
                deletions=deletions,
                is_binary=is_binary,
            )
        )

    return changes


def parse_git_diff(diff_args: list[str] | None = None) -> list[FileChange]:
    """
    Parse git diff output using --numstat format.

    Uses GitPython library if available (recommended), falls back to subprocess.

    Args:
        diff_args: Additional arguments to pass to git diff (e.g., ['HEAD~1', 'HEAD'])
                  If None, uses unstaged changes.

    Returns:
        List of FileChange objects.

    Raises:
        subprocess.CalledProcessError: If git command fails (subprocess mode).
        InvalidGitRepositoryError: If not in a git repository (GitPython mode).
    """
    if HAS_GITPYTHON:
        return _parse_git_diff_gitpython(diff_args)
    return _parse_git_diff_subprocess(diff_args)


def _parse_git_diff_gitpython(diff_args: list[str] | None = None) -> list[FileChange]:
    """
    Parse git diff using GitPython library.

    Args:
        diff_args: Additional arguments to pass to git diff.

    Returns:
        List of FileChange objects.

    Raises:
        InvalidGitRepositoryError: If not in a git repository.
    """
    try:
        repo = Repo(Path.cwd(), search_parent_directories=True)
    except InvalidGitRepositoryError as e:
        raise InvalidGitRepositoryError(
            "Not in a git repository. Run this command from within a git repository."
        ) from e

    # Build git diff command arguments
    if diff_args:
        # GitPython handles argument validation and escaping
        numstat_output = repo.git.diff("--numstat", *diff_args)
    else:
        # Unstaged changes
        numstat_output = repo.git.diff("--numstat")

    return parse_numstat_output(numstat_output)


def _parse_git_diff_subprocess(diff_args: list[str] | None = None) -> list[FileChange]:
    """
    Parse git diff using subprocess (fallback when GitPython unavailable).

    Args:
        diff_args: Additional arguments to pass to git diff.

    Returns:
        List of FileChange objects.

    Raises:
        subprocess.CalledProcessError: If git command fails.
    """
    cmd = ["git", "diff", "--numstat"]
    if diff_args:
        cmd.extend(diff_args)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
    )

    return parse_numstat_output(result.stdout)
