"""Parse git diff output to extract file change statistics."""

import subprocess
from dataclasses import dataclass

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


def _strip_path_prefix(path: str) -> str:
    """Strip git diff path prefixes (a/ or b/)."""
    return path.removeprefix("b/").removeprefix("a/")


def parse_unified_diff(diff_output: str) -> list[FileChange]:
    """Parse unified diff format to extract file change statistics."""
    patch_set = PatchSet(diff_output)
    changes = []

    for patched_file in patch_set:
        if patched_file.target_file == "/dev/null":
            path = _strip_path_prefix(patched_file.source_file)
        else:
            path = _strip_path_prefix(patched_file.target_file)

        is_binary = patched_file.is_binary_file
        additions = patched_file.added
        deletions = patched_file.removed

        changes.append(FileChange(path=path, additions=additions, deletions=deletions, is_binary=is_binary))

    return changes


def parse_git_diff(diff_args: list[str] | None = None) -> list[FileChange]:
    """Run git diff and parse the output."""
    cmd = ["git", "diff"]
    if diff_args:
        cmd.extend(diff_args)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return parse_unified_diff(result.stdout)
