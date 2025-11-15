"""Parse git diff output to extract file change statistics."""

from dataclasses import dataclass
import subprocess
import sys


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

    Parses both git unified diff and standard unified diff formats.
    Counts additions (+) and deletions (-) from diff hunks.

    Args:
        diff_output: Unified diff output (from git diff, svn diff, etc.)

    Returns:
        List of FileChange objects.

    Handles:
        - git diff format (diff --git a/... b/...)
        - Standard diff format (--- a/... +++ b/...)
        - Binary files
        - Added/deleted files
    """
    changes: dict[str, FileChange] = {}
    current_file: str | None = None

    for line in diff_output.split("\n"):
        # Git diff format: diff --git a/path b/path
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                # Extract path from "b/path" (new file path)
                new_path = parts[3]
                current_file = new_path.removeprefix("b/")

                # Initialize if not seen before
                if current_file and current_file not in changes:
                    changes[current_file] = FileChange(
                        path=current_file,
                        additions=0,
                        deletions=0,
                        is_binary=False,
                    )

        # Standard diff format: +++ b/path
        elif line.startswith("+++ "):
            path = line[4:]
            if path.startswith("b/"):
                path = path[2:]
            elif path == "/dev/null":
                # File was deleted, keep current_file from --- line
                continue
            current_file = path

            if current_file and current_file not in changes:
                changes[current_file] = FileChange(
                    path=current_file,
                    additions=0,
                    deletions=0,
                    is_binary=False,
                )

        # Binary file marker
        elif line.startswith("Binary files ") and current_file:
            changes[current_file].is_binary = True

        # Count additions (lines starting with +, but not +++)
        elif line.startswith("+") and not line.startswith("+++") and current_file:
            changes[current_file].additions += 1

        # Count deletions (lines starting with -, but not ---)
        elif line.startswith("-") and not line.startswith("---") and current_file:
            changes[current_file].deletions += 1

    return list(changes.values())


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
    Parse diff from stdin.

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
