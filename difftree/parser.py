"""Parse git diff output to extract file change statistics."""

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
    return path.removeprefix("a/") if path.startswith("a/") else path.removeprefix("b/")


def parse_unified_diff(diff_output: str) -> list[FileChange]:
    """Parse unified diff format to extract file change statistics."""
    return [
        FileChange(
            path=_strip_path_prefix(
                patched_file.source_file if patched_file.target_file == "/dev/null" else patched_file.target_file
            ),
            additions=patched_file.added,
            deletions=patched_file.removed,
            is_binary=patched_file.is_binary_file,
        )
        for patched_file in PatchSet(diff_output)
    ]
