"""Parse git diff output to extract file change statistics."""

from dataclasses import dataclass
import subprocess
import sys

from unidiff import PatchSet


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


def parse_unified_diff(diff_output: str) -> list[FileChange]:
    """
    Parse unified diff format to extract file change statistics.

    Uses the unidiff library to parse standard unified diff format.
    Handles git diff, svn diff, and standard diff output.

    Args:
        diff_output: Unified diff output (from git diff, svn diff, etc.)

    Returns:
        List of FileChange objects.

    Handles:
        - git diff format (diff --git a/... b/...)
        - Standard diff format (--- a/... +++ b/...)
        - Binary files
        - Added/deleted files
        - Renamed files
    """
    patch_set = PatchSet(diff_output)
    changes = []

    for patched_file in patch_set:
        # Get the target file path (use source_file if target doesn't exist - deleted files)
        path = patched_file.target_file
        if path.startswith("b/"):
            path = path[2:]
        elif path == "/dev/null":
            # File was deleted, use source file
            path = patched_file.source_file
            path = path.removeprefix("a/")

        # Check if binary
        is_binary = patched_file.is_binary_file

        # Count additions and deletions
        additions = patched_file.added
        deletions = patched_file.removed

        changes.append(
            FileChange(
                path=path,
                additions=additions,
                deletions=deletions,
                is_binary=is_binary,
            )
        )

    return changes


def parse_numstat_output(numstat_output: str) -> list[FileChange]:
    """
    Parse git diff --numstat output string.

    Args:
        numstat_output: Output from 'git diff --numstat' command.

    Returns:
        List of FileChange objects.

    Note: This is kept for backward compatibility with tests.
          For new code, prefer parse_unified_diff() which uses the
          standard unidiff library.
    """
    changes = []
    for line in numstat_output.strip().split("\n"):
        if not line:
            continue

        parts = line.split("\t")
        if len(parts) != 3:
            continue

        additions_str, deletions_str, path = parts

        # Handle binary files (shown as '-' for both additions and deletions)
        is_binary = additions_str == "-" and deletions_str == "-"

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

    Runs git diff via subprocess and parses the numstat output.

    Args:
        diff_args: Additional arguments to pass to git diff (e.g., ['HEAD~1', 'HEAD'])
                  If None, uses unstaged changes.

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


def parse_diff_from_stdin() -> list[FileChange]:
    """
    Parse diff from stdin using unidiff library.

    Reads unified diff format from stdin and extracts file change statistics.
    This allows the tool to work as a git pager or with piped input.

    Returns:
        List of FileChange objects.

    Example:
        git diff | git-diff-tree
        svn diff | git-diff-tree
    """
    diff_output = sys.stdin.read()
    return parse_unified_diff(diff_output)
