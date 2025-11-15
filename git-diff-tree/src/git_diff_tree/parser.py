"""Parse git diff output to extract file change statistics."""

import subprocess
from dataclasses import dataclass
from typing import List


@dataclass
class FileChange:
    """Represents changes to a single file."""

    path: str
    additions: int
    deletions: int

    @property
    def total_changes(self) -> int:
        """Total number of line changes (additions + deletions)."""
        return self.additions + self.deletions


def parse_git_diff(diff_args: List[str] | None = None) -> List[FileChange]:
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

    changes = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue

        parts = line.split("\t")
        if len(parts) != 3:
            continue

        additions_str, deletions_str, path = parts

        # Handle binary files (shown as '-')
        try:
            additions = int(additions_str)
        except ValueError:
            additions = 0

        try:
            deletions = int(deletions_str)
        except ValueError:
            deletions = 0

        changes.append(FileChange(
            path=path,
            additions=additions,
            deletions=deletions,
        ))

    return changes
