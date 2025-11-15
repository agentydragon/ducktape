"""Parse git diff output to extract file change statistics."""

from dataclasses import dataclass
import subprocess


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

    Args:
        diff_args: Additional arguments to pass to git diff (e.g., ['HEAD~1', 'HEAD'])
                  If None, uses unstaged changes.

    Returns:
        List of FileChange objects.
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
