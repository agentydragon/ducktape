"""Parse git diff output to extract file change statistics."""

from dataclasses import dataclass
from pathlib import Path
import subprocess

import pygit2


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

    Uses pygit2 for repository discovery and validation.

    Args:
        diff_args: Additional arguments to pass to git diff (e.g., ['HEAD~1', 'HEAD'])
                  If None, uses unstaged changes.

    Returns:
        List of FileChange objects.

    Raises:
        RuntimeError: If not in a git repository.
        subprocess.CalledProcessError: If git command fails.
    """
    # Use pygit2 to discover and validate git repository
    repo_path = pygit2.discover_repository(str(Path.cwd()))
    if repo_path is None:
        raise RuntimeError(
            "Not in a git repository. Run this command from within a git repository."
        )

    # Open repository to validate it's accessible
    repo = pygit2.Repository(repo_path)

    # Run git diff --numstat using subprocess
    # We use subprocess here because:
    # 1. We already have a parser for numstat format
    # 2. pygit2's diff API doesn't provide numstat format directly
    # 3. The repo validation above ensures we're in a valid git repo
    cmd = ["git", "diff", "--numstat"]
    if diff_args:
        cmd.extend(diff_args)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        cwd=repo.workdir,
    )

    return parse_numstat_output(result.stdout)
